"""Manifest failures preserve prior metadata and completed images, without Codex."""

import json
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from PIL import Image

from lazyref import generation
from lazyref.models import Cancelled


@pytest.mark.parametrize("failure", ["partial_write", "replace"])
def test_failed_atomic_manifest_update_preserves_previous_file(tmp_path, monkeypatch, failure):
    target = tmp_path / "generation.json"
    generation._write_generation_manifest(target, {"images": ["completed.png"]})
    previous = target.read_bytes()
    original_open, original_replace = Path.open, Path.replace

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, payload):
            self.stream.write(payload[:12])
            raise OSError("synthetic full disk")

    def fail_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return PartialWriter(stream) if path.suffix == ".tmp" else stream

    def fail_replace(path, destination):
        if destination == target:
            raise PermissionError("synthetic locked manifest")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "open" if failure == "partial_write" else "replace",
                        fail_open if failure == "partial_write" else fail_replace)
    with pytest.raises(OSError):
        generation._write_generation_manifest(target, {"images": ["completed.png", "new.png"]})
    assert target.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [target]


class LocalGenerator:
    def __init__(self, root, *, cancelled=False):
        self.root, self.calls, self.cancelled = root, 0, cancelled

    def request(self, prompt, schema, cancel, **kwargs):
        self.calls += 1
        self.last_thread_id = str(uuid4())
        target = self.root / self.last_thread_id / "native.png"
        target.parent.mkdir(parents=True)
        Image.new("RGB", (200, 200), "blue").save(target)
        call = self.usage_ledger.begin("generation", "synthetic", 0)
        call.finish("cancelled" if self.cancelled else "completed")
        if self.cancelled:
            cancel.set()
            raise Cancelled()
        return {"generation_available": True, "image_paths": [str(target)], "limitation": ""}


def test_persistent_manifest_failure_returns_completed_images_and_stops_new_calls(tmp_path, monkeypatch):
    client = LocalGenerator(tmp_path / "native")
    original = Path.replace
    failing = False
    previous = []

    def progress(value):
        nonlocal failing
        previous.append(next((tmp_path / "guides").glob("*/generation.json")).read_bytes())
        failing = True

    def replace(path, destination):
        if failing and Path(destination).name == "generation.json":
            raise PermissionError("synthetic locked manifest")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    result = generation.ReferenceGenerator(client, client.root).run(
        generation.GenerationRequest("A seated person", 3, tmp_path / "guides"), Event(), progress=progress)
    assert client.calls == 1 and len(result.images) == 1
    assert "synthetic locked manifest" in result.error
    assert result.usage["calls"] and result.usage_path.is_file()
    assert result.images[0].is_file()
    manifest = result.folder / "generation.json"
    assert manifest.read_bytes() == previous[0]
    loaded, _ = generation.load_generation(manifest)
    assert loaded.images == result.images
    assert not list(result.folder.glob("*.tmp"))


@pytest.mark.parametrize("when", ["initial", "first_image", "cancelled_final"])
def test_manifest_failure_never_discards_partial_generation_result(tmp_path, monkeypatch, when):
    client = LocalGenerator(tmp_path / "native", cancelled=when == "cancelled_final")
    original = generation._write_generation_manifest

    def write(path, data):
        if (when == "initial" or when == "first_image" and data["saved"]
                or when == "cancelled_final" and data["cancelled"]):
            raise OSError("synthetic manifest failure")
        return original(path, data)

    monkeypatch.setattr(generation, "_write_generation_manifest", write)
    result = generation.ReferenceGenerator(client, client.root).run(
        generation.GenerationRequest("A seated person", 2, tmp_path / "guides"), Event())
    assert "synthetic manifest failure" in result.error and result.folder.is_dir()
    assert client.calls == (0 if when == "initial" else 1)
    assert len(result.images) == (0 if when == "initial" else 1)
    assert result.cancelled is (when == "cancelled_final")
    assert all(path.is_file() for path in result.images)


def test_generation_worker_receives_partial_result_on_manifest_failure(tmp_path, monkeypatch):
    from lazyref import ui
    client = LocalGenerator(tmp_path / "native")
    generator = generation.ReferenceGenerator(client, client.root)
    original = generation._write_generation_manifest

    def write(path, data):
        if data["saved"]:
            raise OSError("synthetic manifest failure")
        return original(path, data)

    monkeypatch.setattr(generation, "_write_generation_manifest", write)
    monkeypatch.setattr(ui, "ReferenceGenerator", lambda: generator)
    worker = ui.GenerationWorker(generation.GenerationRequest("A seated person", 1, tmp_path / "guides"))
    worker.run()  # Synchronous: no real thread, CLI or service.
    assert worker.result.folder and len(worker.result.images) == 1
    assert worker.result.error and worker.result.usage["calls"]
