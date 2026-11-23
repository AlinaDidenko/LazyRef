from io import BytesIO
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient, PLAN_SCHEMA
from lazyref.usage import UsageLedger
from tools.run_model_matrix import MODELS, digest, initialize, make_jobs, request_for, report, run, write_json


@pytest.mark.parametrize("archived", [False, True])
def test_initialize_copies_selected_guides_from_default_or_archived_folder(tmp_path, monkeypatch, archived):
    workspace = tmp_path / "workspace"
    (workspace / "lazyref").mkdir(parents=True)
    (workspace / "lazyref/__init__.py").write_text("# source snapshot\n", encoding="utf-8")
    write_json(workspace / ".lazyref/settings.json", {})
    selection = workspace / ("archive/old-generated" if archived else "generated_references/Test_2026-11-06")
    selection.mkdir(parents=True)
    cases = []
    from PIL import Image
    for identifier in ("1D", "2B", "3B"):
        image = selection / f"{identifier}.png"
        Image.new("RGB", (2, 2), "green").save(image)
        cases.append({"id": identifier, "image": image.name, "description": identifier, "sha256": digest(image)})
    write_json(selection / "selection.json", {"cases": cases})
    monkeypatch.setattr("tools.run_model_matrix.find_codex", lambda configured: "codex.exe")
    root = tmp_path / "new-series"
    config = initialize(root, workspace, selection_dir=selection if archived else None)
    assert len(config["jobs"]) == 30
    for original, frozen in zip(cases, config["cases"]):
        assert digest(root / frozen["image"]) == original["sha256"]
        assert (selection / original["image"]).is_file()
    assert (root / "report.html").is_file()


def test_initialize_missing_selection_explains_archive_option_without_creating_series(tmp_path):
    root = tmp_path / "new-series"
    with pytest.raises(ValueError, match="--selection-dir"):
        initialize(root, tmp_path)
    assert not root.exists()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", None])
def test_cli_uses_explicit_reasoning_and_standard_and_records_them(tmp_path, monkeypatch, effort):
    from test_process_security import FakeProcessTree
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    monkeypatch.setattr("lazyref.codex.find_codex", lambda configured: "codex.exe")
    calls = []
    class Process:
        def __init__(self, args, stdout, **kwargs):
            calls.append(args)
            self.stdin = BytesIO()
            self.returncode = 0
            Path(args[args.index("-o") + 1]).write_text('{"ok":true}', encoding="utf-8")
            stdout.write(b'{"type":"thread.started","thread_id":"11111111-1111-1111-1111-111111111111"}\n')
            stdout.write(b'{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}\n')
            stdout.flush()
        def poll(self):
            return self.returncode
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    explicit = effort is not None
    options = {"reasoning_effort": effort, "service_tier": "default"} if explicit else {}
    client = CodexClient(model="gpt-6.1-terra", **options)
    client.usage_ledger = UsageLedger(tmp_path / "usage.json", "search")
    assert client.request("test", PLAN_SCHEMA, Event()) == {"ok": True}
    command = calls[0]
    assert command[command.index("--model") + 1] == "gpt-6.1-terra"
    configured = [arg for arg in command if arg.startswith('model_reasoning_effort=')]
    assert configured == ([f'model_reasoning_effort="{effort}"'] if explicit else [])
    assert ('service_tier="default"' in command) is explicit
    record = client.usage_ledger.snapshot()["calls"][0]
    assert record["requested_reasoning_effort"] == effort
    assert record["requested_service_tier"] == ("default" if explicit else None)
    assert client.usage_ledger.snapshot()["summary"]["reported_tokens"]["total_tokens"] == 120


def test_matrix_covers_each_requested_combination_exactly_once():
    cases = [{"description": "prompt " + str(i), "image": str(i) + ".png", "sha256": str(i)} for i in range(3)]
    jobs = make_jobs(cases)
    assert len(jobs) == 30 and len({j["id"] for j in jobs}) == 30
    assert {(j["case_number"], j["model"], j["profile"]) for j in jobs} == {
        (c, m, p) for c in (1, 2, 3) for m in MODELS for p in ("strict", "pose")}
    for i, case in enumerate(cases, 1):
        subset = [j for j in jobs if j["case_number"] == i]
        assert {j["description"] for j in subset} == {case["description"]}
        assert {j["guide_sha256"] for j in subset} == {case["sha256"]}


def test_request_cannot_inherit_user_cache_or_verification_settings(tmp_path):
    config = {"count": 5, "executable": "codex.exe", "candidate_limit": 72, "screenshots": True,
        "artistic_nudity": True, "search_rounds": 6, "search_strategy": "wide", "discovery_limit": 200,
        "detail_limit": 40, "reuse_cache": False, "verify_sources": False}
    job = {"id": "test", "description": "A woman shouting", "model": "gpt-6-luna", "profile": "pose", "guide": "woman.png"}
    request = request_for(tmp_path, config, job)
    assert request.count == 5 and request.reuse_cache is False and request.verify_sources is False
    assert request.search_profile == "pose" and request.model == "gpt-6-luna"
    assert request.reference_image == tmp_path / "woman.png"
    assert request.output_dir == tmp_path / "runs/test/results"


def test_report_does_not_turn_unknown_usage_into_zero_or_mark_pending_complete(tmp_path):
    cases = [{"description": "Test pose", "image": "guide.png", "sha256": "hash"}]
    jobs = make_jobs(cases)
    write_json(tmp_path / "matrix.json", {"jobs": jobs, "cases": cases})
    write_json(tmp_path / "state.json", {"status": "running", "jobs": {j["id"]: {"status": "pending"} for j in jobs}})
    write_json(tmp_path / "runs" / jobs[0]["id"] / "outcome.json", {
        "status": "failed", "error": "Timeout", "elapsed_seconds": 500,
        "usage": {"summary": {"status": "incomplete", "reported_tokens": {"total_tokens": None}}}})
    counts = report(tmp_path)
    assert counts == {"failed": 1, "pending": 9}
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["tests"][0]["tokens"] is None
    assert summary["tests"][1]["status"] == "pending"
    assert 'Timeout' in (tmp_path / "report.html").read_text(encoding="utf-8")


def test_queue_skips_finished_cell_and_continues_after_individual_failure(tmp_path, monkeypatch):
    from test_process_security import FakeProcessTree
    monkeypatch.setattr("tools.run_model_matrix.ProcessTree", FakeProcessTree)
    import ctypes
    if hasattr(ctypes, "windll"):
        monkeypatch.setattr(ctypes.windll.kernel32, "SetThreadExecutionState", lambda flags: 1)
    monkeypatch.setattr("tools.run_model_matrix.verify_frozen", lambda *args: None)
    cases = [{"description": f"Pose {i}", "image": f"guide{i}.png", "sha256": str(i)} for i in range(3)]
    jobs = make_jobs(cases)
    write_json(tmp_path / "matrix.json", {"jobs": jobs, "cases": cases, "workspace": str(tmp_path)})
    write_json(tmp_path / "state.json", {"status": "ready", "jobs": {j["id"]: {"status": "pending"} for j in jobs}})
    write_json(tmp_path / "runs" / jobs[0]["id"] / "outcome.json", {"status": "completed", "finished_at": "already finished"})
    started = []
    class Process:
        returncode = 0
        pid = 123
        def __init__(self, args, **kwargs):
            job_id = args[args.index("--job") + 1]
            started.append(job_id)
            write_json(tmp_path / "runs" / job_id / "outcome.json", {
                "status": "failed" if len(started) == 1 else "completed",
                "error": "A single test failed" if len(started) == 1 else "", "finished_at": "done"})
        def poll(self):
            return 0
    monkeypatch.setattr("tools.run_model_matrix.subprocess.Popen", Process)
    run(tmp_path)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(started) == 29 and jobs[0]["id"] not in started
    assert state["status"] == "completed_with_errors"
    assert state["jobs"][jobs[1]["id"]]["status"] == "failed"
    assert state["jobs"][jobs[-1]["id"]]["status"] == "completed"
    assert not (tmp_path / "runner.lock").exists()
