from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from PIL import Image

from lazyref.codex import CodexClient
from lazyref.exporter import export_results
from lazyref.generation import GenerationRequest, ReferenceGenerator, load_generation, validate_reference
from lazyref.models import Cancelled, SearchRequest
from test_core import make_asset, picture
from test_process_security import FakeProcessTree


class GeneratorClient:
    def __init__(self, root, failure=None):
        self.root, self.failure, self.calls = root, failure, 0

    def request(self, prompt, schema, cancel, **kwargs):
        self.calls += 1
        self.last_thread_id = str(uuid4())
        assert kwargs["timeout"] == 600
        assert kwargs["image_generation"] is True
        if self.calls == 2 and self.failure == "cancel":
            cancel.set()
            raise Cancelled()
        if self.calls == 2 and self.failure == "unavailable":
            return {"generation_available": False, "image_paths": [], "limitation": "Test limit"}
        directory = self.root / self.last_thread_id
        directory.mkdir(parents=True)
        path = directory / f"{self.calls}.png"
        picture(self.calls).save(path)
        return {"generation_available": True, "image_paths": [str(path)], "limitation": ""}


@pytest.mark.parametrize("failure,expected", [(None, 3), ("cancel", 1), ("unavailable", 1)])
def test_generation_count_and_partial_preservation(tmp_path, failure, expected):
    root = tmp_path / "native"
    client = GeneratorClient(root, failure)
    result = ReferenceGenerator(client, root).run(GenerationRequest("A seated man", 3, tmp_path / "guides", model="gpt-6.1-sol"), Event())
    assert len(result.images) == expected
    assert result.cancelled == (failure == "cancel")
    assert bool(result.error) == (failure == "unavailable")
    assert all(p.is_file() and p.is_relative_to(result.folder) for p in result.images)
    manifest = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert manifest["saved"] == expected and manifest["kind"] == "ai_generated_pose_guides"
    assert manifest["requested_model"] == "gpt-6.1-sol"
    assert manifest["requested_reasoning_effort"] == "medium"
    assert len(manifest["images"]) == expected
    assert all("Variant:" in row["prompt"] and "ai_generated" == row["kind"] for row in manifest["images"])


@pytest.mark.parametrize("mode", ["outside", "missing", "stale", "invalid", "multiple"])
def test_model_cannot_supply_arbitrary_or_stale_files(tmp_path, mode):
    root = tmp_path / "native"
    identifier = str(uuid4())
    # Вкладений файл змушує перевірити шлях відповіді, а не native scan.
    directory = root / identifier / "nested"
    directory.mkdir(parents=True)
    path = (tmp_path if mode == "outside" else directory) / "image.png"
    if mode == "invalid":
        path.write_text("not an image")
    elif mode != "missing":
        picture().save(path)
    if mode == "stale":
        os.utime(path, (1, 1))
    class Client:
        last_thread_id = identifier
        def request(self, *args, **kwargs):
            return {"generation_available": True, "image_paths": [str(path)] * (2 if mode == "multiple" else 1)}
    result = ReferenceGenerator(Client(), root).run(GenerationRequest("A seated man", 1, tmp_path / "guides"), Event())
    assert result.error and not result.images


def test_cancelled_generation_spends_no_calls(tmp_path):
    client = GeneratorClient(tmp_path / "native")
    cancel = Event()
    cancel.set()
    result = ReferenceGenerator(client).run(GenerationRequest("A seated man", 2, tmp_path / "guides"), cancel)
    assert result.cancelled and not result.images and client.calls == 0


def test_reference_validation_and_export_does_not_include_generated_guide(tmp_path):
    guide = tmp_path / "AI.png"
    picture().save(guide)
    request = SearchRequest("A seated man", 1, tmp_path / "photos", reference_image=guide)
    request.validate()
    with pytest.raises(ValueError, match="агента"):
        replace(request, provider="local").validate()
    folder, assets = export_results(request, [make_asset(tmp_path)], {}, {})
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert len(assets) == manifest["saved"] == 1
    assert manifest["reference_guide"]["kind"] == "ai_generated_pose_guide"
    assert manifest["reference_guide"]["included_in_results"] is False
    assert len(list(folder.glob("*.jpg"))) == 1 and not list(folder.glob("*.png"))
    guide.write_text("bad file")
    with pytest.raises(ValueError):
        request.validate()


def test_assessment_guide_is_first_but_never_receives_a_score(tmp_path):
    guide = tmp_path / "AI.png"
    picture().save(guide)
    asset = make_asset(tmp_path, "candidate")
    client = CodexClient.__new__(CodexClient)
    def request(prompt, schema, cancel, images):
        assert images == [guide, asset.preview]
        assert '"position": 2' in prompt and "NOT a candidate" in prompt
        return {"images": [{"id": "candidate", "media": "photo", "confidence": "high", "score": 88,
            "reason": "Similar pose", "matches": [], "misses": [], "reference_type": "ordinary", "criterion_checks": []}]}
    client.request = request
    assert list(client.assess("A seated man", [asset], Event(), reference_image=guide)) == ["candidate"]


def test_plan_attaches_guide_and_requires_pose(tmp_path):
    guide = tmp_path / "AI.png"
    picture().save(guide)
    client = CodexClient.__new__(CodexClient)
    pose = {"id": "c1", "label": "Сидить, руки на стійці", "kind": "pose", "keywords": ["sitting"]}
    def request(prompt, schema, cancel, **kwargs):
        assert kwargs["images"] == [guide] and "AI-GENERATED POSE GUIDE" in prompt
        return {"summary": "Test", "queries": ["man seated bar"], "discovery_queries": ["man bar"],
            "focus": "scene", "criteria": [pose]}
    client.request = request
    assert client.plan("A seated man", Event(), guide)["criteria"][0]["kind"] == "pose"
    pose["kind"] = "subject"
    with pytest.raises(RuntimeError, match="позу"):
        client.plan("A seated man", Event(), guide)


def test_pipeline_passes_selected_guide_through_plan_and_assessment(tmp_path):
    from lazyref.models import Assessment, Candidate
    from lazyref.pipeline import SearchPipeline
    guide = tmp_path / "AI.png"
    picture().save(guide)
    called = []
    class Client:
        def plan(self, description, cancel, reference_image=None):
            assert reference_image == guide
            called.append("plan")
            return {"summary": description, "queries": ["man seated"], "criteria": []}
        def assess(self, description, batch, cancel, reference_image=None):
            assert reference_image == guide and all(a.path != guide for a in batch)
            called.append("assess")
            return {a.candidate.id: Assessment("photo", "high", 90, "Pose matches", [], []) for a in batch}
    result = SearchPipeline(Client(), finder=lambda *args: ([Candidate("a", "Photo")], []),
        loader=lambda c, directory, net: make_asset(directory, c.id)).run(
            SearchRequest("A seated man", 1, tmp_path / "photos", reference_image=guide), Event())
    assert not result.error and called == ["plan", "assess"] and len(result.assets) == 1


def test_discovery_sees_actual_guide_not_just_description(tmp_path):
    from lazyref.search_agent import discover
    from lazyref.network import Network
    from test_search_agent import row
    from test_search_quality import PLAN
    guide = tmp_path / "AI.png"
    picture().save(guide)
    class Client:
        def request(self, prompt, schema, cancel, **kwargs):
            assert kwargs["images"] == [guide] and kwargs["web_search"] is True
            assert "AI-GENERATED POSE GUIDE" in prompt
            kwargs["trace"].append({"type": "web_search", "action": "search", "query": "bar rear seated"})
            return {"image_search_available": True, "queries": ["bar rear seated"], "strategy": "test",
                "limitation": "", "candidates": [row()]}
    net = Network(Event())
    try:
        candidates, report = discover(Client(), SearchRequest("man at bar", 1, tmp_path, reference_image=guide),
            PLAN, [], [], {}, net, Event(), lambda s: None)
        assert len(candidates) == 1 and report["status"] == "ok"
    finally:
        net.close()


@pytest.mark.parametrize("count", [0, 9])
def test_invalid_generation_count_is_rejected_before_client_call(tmp_path, count):
    client = GeneratorClient(tmp_path)
    with pytest.raises(ValueError):
        ReferenceGenerator(client).run(GenerationRequest("A seated man", count, tmp_path / "guides"), Event())
    assert client.calls == 0


def test_duplicate_generation_does_not_fill_requested_count(tmp_path):
    root = tmp_path / "native"
    root.mkdir()
    class Client:
        def request(self, *args, **kwargs):
            self.last_thread_id = str(uuid4())
            directory = root / self.last_thread_id
            directory.mkdir()
            path = directory / "same.png"
            picture().save(path)
            return {"generation_available": True, "image_paths": [str(path)]}
    result = ReferenceGenerator(Client(), root).run(GenerationRequest("A seated man", 2, tmp_path / "guides"), Event())
    assert len(result.images) == 1 and "повторний" in result.error


@pytest.mark.parametrize("generate", [False, True])
def test_cli_generation_tool_is_enabled_only_for_generation(monkeypatch, generate):
    from io import BytesIO
    client = CodexClient.__new__(CodexClient)
    client.executable, client.model, client.timeout = "codex.exe", "", 240
    class Process:
        def __init__(self, args, **kwargs):
            assert f"features.image_generation={str(generate).lower()}" in args
            assert 'web_search="disabled"' in args and 'features.shell_tool=false' in args
            Path(args[args.index("-o") + 1]).write_text('{"ok":true}', encoding="utf-8")
            self.stdin, self.returncode = BytesIO(), 0
        def poll(self):
            return 0
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    assert client.request("Test", {}, Event(), image_generation=generate) == {"ok": True}


@pytest.mark.parametrize("response", [
    {"generation_available": False, "image_paths": [], "limitation": "Не бачу шляху до файла"},
    {"generation_available": True, "image_paths": [], "limitation": ""},
    {"generation_available": True, "image_paths": ["invented.png"], "limitation": ""},
])
def test_native_files_complete_all_variants_when_model_cannot_report_paths(tmp_path, monkeypatch, response):
    """Регресія Sol: інструмент створив PNG, але фінальний JSON заперечує успіх."""
    from io import BytesIO
    root = tmp_path / "native"
    processes = []
    client = CodexClient.__new__(CodexClient)
    client.executable, client.model, client.timeout = "codex.exe", "test", 600
    class Process:
        def __init__(self, args, stdout, **kwargs):
            thread_id = str(uuid4())
            processes.append(thread_id)
            directory = root / thread_id
            directory.mkdir(parents=True)
            picture(len(processes)).save(directory / "exec-result.png")
            stdout.write((json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n").encode())
            # Останній рядок потоку може не закінчуватися переносом.
            stdout.write(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 20}}).encode())
            stdout.flush()
            Path(args[args.index("-o") + 1]).write_text(json.dumps(response), encoding="utf-8")
            self.stdin, self.returncode = BytesIO(), 0
        def poll(self):
            return 0
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    result = ReferenceGenerator(client, root).run(GenerationRequest("A seated man", 3, tmp_path / "guides"), Event())
    assert not result.error and len(result.images) == len(processes) == 3
    manifest = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert [r["codex_thread_id"] for r in manifest["images"]] == processes
    assert all(r["path_source"] == "codex_thread_artifact" for r in manifest["images"])
    assert result.usage["summary"]["reported_tokens"]["total_tokens"] == 360


@pytest.mark.parametrize("mode", ["missing", "other_thread", "stale", "future", "invalid", "bad_id"])
def test_native_recovery_never_uses_an_unrelated_or_unusable_file(tmp_path, mode):
    root = tmp_path / "native"
    identifier = str(uuid4())
    other = root / str(uuid4())
    other.mkdir(parents=True)
    picture().save(other / "newer-unrelated.png")
    directory = root / identifier
    directory.mkdir()
    path = directory / "image.png"
    if mode not in {"missing", "other_thread"}:
        if mode == "invalid":
            path.write_text("broken image")
        else:
            picture().save(path)
    if mode == "stale":
        os.utime(path, (1, 1))
    elif mode == "future":
        os.utime(path, (4102444800, 4102444800))
    class Client:
        last_thread_id = "../" + identifier if mode == "bad_id" else identifier
        def request(self, *args, **kwargs):
            return {"generation_available": mode in {"other_thread", "stale", "future"},
                "image_paths": ([str(other / "newer-unrelated.png")] if mode == "other_thread"
                                else [str(path)] if mode in {"stale", "future"} else []),
                "limitation": "No output path"}
    result = ReferenceGenerator(Client(), root).run(GenerationRequest("A seated man", 1, tmp_path / "guides"), Event())
    assert result.error and not result.images


def test_thread_id_is_native_and_resets_even_without_usage_ledger(monkeypatch):
    from io import BytesIO
    identifier = str(uuid4())
    event_batches = iter([
        [{"type": "thread.started", "thread_id": identifier}],
        [{"type": "item.completed", "item": {"type": "agent_message", "text": identifier}}],
        [{"type": "thread.started", "thread_id": "../not-a-thread"}],
    ])
    client = CodexClient.__new__(CodexClient)
    client.executable, client.model, client.timeout = "codex.exe", "test", 600
    class Process:
        def __init__(self, args, stdout, **kwargs):
            for event in next(event_batches):
                stdout.write(json.dumps(event).encode())
            stdout.flush()
            Path(args[args.index("-o") + 1]).write_text('{"ok":true}', encoding="utf-8")
            self.stdin, self.returncode = BytesIO(), 0
        def poll(self):
            return 0
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    for expected in (identifier, "", ""):
        client.request("Test", {}, Event(), image_generation=True)
        assert client.last_thread_id == expected
    client.last_thread_id = identifier
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        client.request("Test", {}, cancel, image_generation=True)
    assert client.last_thread_id == ""


class MultiFileGenerator:
    def __init__(self, root, groups):
        self.root, self.groups, self.calls = root, groups, 0

    def request(self, prompt, schema, cancel, **kwargs):
        from PIL.PngImagePlugin import PngInfo
        assert self.calls < len(self.groups), "Unexpected extra generation call"
        group = self.groups[self.calls]
        self.calls += 1
        self.last_thread_id = str(uuid4())
        directory = self.root / self.last_thread_id
        directory.mkdir(parents=True)
        for i, seed in enumerate(group):
            path = directory / f"exec-{i}.png"
            if seed is None:
                path.write_text("incomplete PNG")
            else:
                metadata = PngInfo()
                metadata.add_text("copy", f"{self.calls}-{i}")
                picture(seed).save(path, pnginfo=metadata)
        return {"generation_available": False, "image_paths": [], "limitation": "No output paths"}


@pytest.mark.parametrize("groups,requested,expected", [
    ([[1, 2]], 2, 2),
    ([[1], [2, 3]], 2, 3),  # Справжній збій: два файли другого виклику.
    ([[1, 1, 2]], 2, 2),   # Різні PNG-метадані, однакові пікселі.
    ([[1], [1, 2]], 2, 2), # Повтор попереднього варіанта не ховає нове зображення.
    ([[None, 1]], 1, 1),   # Непридатний сусідній файл не скасовує готовий зразок.
])
def test_multiple_native_files_are_saved_without_extra_generation(tmp_path, groups, requested, expected):
    root = tmp_path / "native"
    client = MultiFileGenerator(root, groups)
    progress = []
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated man", requested, tmp_path / "guides"), Event(), progress=progress.append)
    assert not result.error and not result.cancelled
    assert len(result.images) == expected and client.calls == len(groups)
    assert progress[-1] == 100 and max(progress) == 100
    data = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert len(data["images"]) == data["saved"] == expected
    assert len({r["pixel_sha256"] for r in data["images"]}) == expected
    assert len(data["attempts"]) == client.calls
    assert sum(len(a["saved_files"]) for a in data["attempts"]) == expected
    assert sum(len(a["skipped_files"]) for a in data["attempts"]) == sum(map(len, groups)) - expected
    loaded, description = load_generation(result.folder / "generation.json")
    assert loaded.images == result.images and loaded.requested == requested and description == "A seated man"


def test_cancellation_between_native_files_preserves_saved_variant(tmp_path):
    root = tmp_path / "native"
    client = MultiFileGenerator(root, [[1, 2]])
    cancel = Event()
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated man", 2, tmp_path / "guides"), cancel, progress=lambda _: cancel.set())
    assert result.cancelled and not result.error and len(result.images) == 1 and client.calls == 1
    data = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert data["saved"] == 1 and data["cancelled"]


@pytest.mark.parametrize("mode", ["outside", "missing", "invalid", "empty", "bad_json"])
def test_loading_saved_generation_rejects_bad_files(tmp_path, mode):
    folder = tmp_path / "guides"
    folder.mkdir()
    source = (tmp_path if mode == "outside" else folder) / "image.png"
    if mode == "invalid":
        source.write_text("not a PNG")
    elif mode != "missing":
        picture().save(source)
    data = {"kind": "ai_generated_pose_guides", "description": "A seated man", "requested": 1,
        "images": [] if mode == "empty" else [{"file": "../image.png" if mode == "outside" else source.name}]}
    manifest = folder / "generation.json"
    manifest.write_text("bad JSON" if mode == "bad_json" else json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_generation(manifest)
