"""Перевірка обліку без запуску Codex або хмарних запитів."""
from io import BytesIO
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient, PLAN_SCHEMA, ASSESS_SCHEMA
from lazyref.generation import GenerationRequest, ReferenceGenerator
from lazyref.models import Cancelled, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.usage import UsageLedger, format_usage_report, related_generation_usage, token_values
from test_core import picture
from test_process_security import FakeProcessTree


USAGE = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 200,
         "reasoning_output_tokens": 100, "cache_write_input_tokens": 50}


def record(ledger, stage="planning", usage=USAGE, status="completed"):
    call = ledger.begin(stage, "test-model", 1)
    call.observe({"type": "thread.started", "thread_id": "test-thread"})
    call.observe({"type": "turn.started"})
    if usage is not None:
        call.observe({"type": "turn.completed", "usage": usage})
    call.finish(status)
    return call


def test_counts_cache_and_reasoning_once_and_ignores_duplicate_completion(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    call = record(ledger, usage={**USAGE, "secret": "do-not-store"})
    call.observe({"type": "turn.completed", "usage": USAGE})
    call.observe({"type": "turn.started"})
    call.observe({"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 100}})
    call.finish("completed")
    data = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert data["summary"]["reported_tokens"]["total_tokens"] == 1800
    assert data["summary"]["calls"] == 1 and data["summary"]["status"] == "complete"
    assert len(data["calls"][0]["turns"]) == 2
    assert "secret" not in ledger.path.read_text(encoding="utf-8")
    assert "800 (неповні дані)" in format_usage_report(data)


def test_missing_usage_is_unknown_and_preserves_other_calls(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    record(ledger, "planning")
    record(ledger, "assessment", usage=None, status="cancelled")
    data = ledger.snapshot()
    assert data["summary"]["reported_tokens"]["total_tokens"] == 1200
    assert data["summary"]["incomplete_calls"] == 1
    assert data["by_stage"]["assessment"]["reported_tokens"]["total_tokens"] is None
    assert "Облік неповний" in format_usage_report(data)
    assert "не дорівнює нулю" in format_usage_report(data)


def test_invalid_numbers_and_partial_core_fields(tmp_path):
    assert token_values({"input_tokens": True, "output_tokens": -2, "cached_input_tokens": "20"})["total_tokens"] is None
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    record(ledger)
    record(ledger, usage={"input_tokens": 500})
    summary = ledger.snapshot()["summary"]
    assert summary["reported_tokens"]["total_tokens"] == 1700
    assert summary["incomplete_calls"] == 1


@pytest.mark.parametrize("stage,schema,kwargs", [
    ("planning", PLAN_SCHEMA, {}), ("assessment", ASSESS_SCHEMA, {}),
    ("search", {}, {"web_search": True}), ("generation", {}, {"image_generation": True})])
def test_real_reader_captures_final_json_without_newline_and_adds_no_calls(tmp_path, monkeypatch, stage, schema, kwargs):
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    client = CodexClient.__new__(CodexClient)
    client.executable, client.model, client.timeout, client.usage_ledger = "codex.exe", "test", 10, ledger
    processes = []
    class Process:
        def __init__(self, args, stdout, **kwargs):
            processes.append(args)
            self.stdin, self.returncode = BytesIO(), 0
            # Пошкоджений рядок і сторонній тип події не заважають фінальному лічильнику.
            stdout.write(b'bad-json\n[]\n')
            stdout.write((json.dumps({"type": "turn.started"}) + "\n").encode())
            stdout.write(json.dumps({"type": "turn.completed", "usage": USAGE}).encode())
            stdout.flush()
            Path(args[args.index("-o") + 1]).write_text('{"ok":true}', encoding="utf-8")
        def poll(self):
            return 0
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    assert client.request("private description", schema, Event(), **kwargs) == {"ok": True}
    assert len(processes) == 1
    data = ledger.snapshot()
    assert data["by_stage"][stage]["reported_tokens"]["total_tokens"] == 1200
    assert "private description" not in ledger.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode,has_usage", [("cancel", False), ("cancel", True), ("error", True), ("timeout", False)])
def test_process_failure_and_stop_keep_available_usage(tmp_path, monkeypatch, mode, has_usage):
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    client = CodexClient.__new__(CodexClient)
    client.executable, client.model, client.timeout, client.usage_ledger = "codex.exe", "test", 1, ledger
    cancel = Event()
    class Process:
        def __init__(self, args, stdout, **kwargs):
            self.stdin = BytesIO()
            self.returncode = 1 if mode == "error" else None
            stdout.write(b'{"type":"turn.started"}\n')
            if has_usage:
                stdout.write(json.dumps({"type": "turn.completed", "usage": USAGE}).encode())
            stdout.flush()
            if mode == "cancel":
                cancel.set()
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -1
        def wait(self, **kwargs):
            return self.returncode
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    if mode == "timeout":
        times = iter((0, 2))
        monkeypatch.setattr("lazyref.codex.time.monotonic", lambda: next(times))
    error = Cancelled if mode == "cancel" else TimeoutError if mode == "timeout" else RuntimeError
    with pytest.raises(error):
        client.request("Test", PLAN_SCHEMA, cancel)
    data = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert data["summary"]["reported_tokens"]["total_tokens"] == (1200 if has_usage else None)
    assert data["calls"][0]["status"] == {"cancel": "cancelled", "timeout": "timed_out", "error": "error"}[mode]
    assert data["summary"]["incomplete_calls"] == (0 if has_usage else 1)


def test_already_cancelled_request_never_starts_process(tmp_path, monkeypatch):
    client = CodexClient.__new__(CodexClient)
    client.usage_ledger = UsageLedger(tmp_path / "usage.json", "search")
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", lambda *a, **kw: pytest.fail("No process expected"))
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        client.request("Test", PLAN_SCHEMA, cancel)
    assert client.usage_ledger.snapshot()["summary"]["status"] == "no_calls"


@pytest.mark.parametrize("fail_plan", [False, True])
def test_pipeline_exports_usage_even_when_no_photos_or_plan_fails(tmp_path, fail_plan):
    class Client:
        def plan(self, *args):
            record(self.usage_ledger)
            if fail_plan:
                raise RuntimeError("Invalid plan")
            return {"summary": "test", "queries": ["test"], "criteria": []}
    result = SearchPipeline(Client(), finder=lambda *a: ([], [])).run(
        SearchRequest("A seated person", 1, tmp_path), Event())
    assert not result.assets and result.usage_path.is_file()
    assert result.usage["summary"]["reported_tokens"]["total_tokens"] == 1200
    if fail_plan:
        assert result.error and result.folder is None and result.usage_path.parent.name == "_usage"
    else:
        manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["usage"]["run_id"] == result.usage["run_id"]


def test_generation_refusal_is_counted_and_shared_package_is_not_added_twice(tmp_path):
    from uuid import uuid4
    root = tmp_path / "native"
    root.mkdir()
    class Client:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            self.last_thread_id = str(uuid4())
            record(self.usage_ledger, "generation")
            if self.calls == 2:
                return {"generation_available": False, "image_paths": [], "limitation": "Test refusal"}
            directory = root / self.last_thread_id
            directory.mkdir()
            path = directory / "image.png"
            picture().save(path)
            return {"generation_available": True, "image_paths": [str(path)]}
    result = ReferenceGenerator(Client(), root).run(GenerationRequest("A seated person", 3, tmp_path / "guides"), Event())
    assert result.error and len(result.images) == 1
    assert result.usage["summary"]["reported_tokens"]["total_tokens"] == 2400
    related = related_generation_usage(result.images[0])
    assert related["run_id"] == result.usage["run_id"]
    for index in range(2):
        search = UsageLedger(tmp_path / f"search-{index}.json", "search", related)
        record(search, "search")
        assert search.snapshot()["summary"]["reported_tokens"]["total_tokens"] == 1200
        assert search.snapshot()["related_generation"]["summary"]["reported_tokens"]["total_tokens"] == 2400
    assert related_generation_usage(tmp_path / "legacy.png")["status"] == "unavailable"


def test_disk_failure_keeps_in_memory_measurements(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", fail)
    record(ledger)
    assert ledger.write_error and ledger.snapshot()["summary"]["reported_tokens"]["total_tokens"] == 1200
