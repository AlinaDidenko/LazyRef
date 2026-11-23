"""Generation artifacts are bound to a call and survive interrupted final output."""

import hashlib
import json
from threading import Event
from uuid import uuid4

import pytest
from PIL import Image

from lazyref.generation import GenerationRequest, ReferenceGenerator, load_generation
from lazyref.models import Cancelled


def png(path, color="blue"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 200), color).save(path)
    return path


@pytest.mark.parametrize("identifier", [None, "", "not-a-uuid"])
def test_missing_native_identity_cannot_claim_another_fresh_threads_file(tmp_path, identifier):
    root = tmp_path / "native"
    other = png(root / str(uuid4()) / "unrelated.png")

    class Client:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            if identifier is not None:
                self.last_thread_id = identifier
            return {"generation_available": True, "image_paths": [str(other)], "limitation": ""}

    client = Client()
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated person", 1, tmp_path / "guides"), Event())

    assert result.error and result.images == []
    assert client.calls == 1 and other.is_file()
    manifest = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert manifest["saved"] == 0 and manifest["images"] == []


def test_known_native_identity_never_accepts_other_thread_from_model_response(tmp_path):
    root = tmp_path / "native"
    other = png(root / str(uuid4()) / "unrelated.png")

    class Client:
        calls = 0
        last_thread_id = str(uuid4())

        def request(self, *args, **kwargs):
            self.calls += 1
            return {"generation_available": True, "image_paths": [str(other)], "limitation": ""}

    client = Client()
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated person", 1, tmp_path / "guides"), Event())

    assert result.error and not result.images and client.calls == 1


@pytest.mark.parametrize("failure", [Cancelled, TimeoutError, RuntimeError])
@pytest.mark.parametrize("requested", [1, 3])
def test_completed_current_thread_image_is_recovered_after_cli_failure(tmp_path, failure, requested):
    root = tmp_path / "native"
    unrelated = png(root / str(uuid4()) / "unrelated.png", "red")
    native_id = str(uuid4())
    current = root / native_id / "completed.png"
    cancel = Event()

    class Client:
        calls = 0
        last_thread_id = ""

        def request(self, *args, **kwargs):
            self.calls += 1
            assert self.calls == 1, "Recovery must not request another generation"
            self.last_thread_id = native_id
            png(current)
            if failure is Cancelled:
                cancel.set()
                raise Cancelled()
            raise failure("synthetic CLI completion failure")

    client = Client()
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated person", requested, tmp_path / "guides"), cancel)

    assert client.calls == 1
    assert len(result.images) == 1 and result.images[0].read_bytes() == current.read_bytes()
    assert unrelated.read_bytes() != result.images[0].read_bytes()
    assert current.is_file() and unrelated.is_file()
    assert result.cancelled is (failure is Cancelled)
    expected_error = "" if failure is Cancelled else "synthetic CLI completion failure"
    assert result.error == expected_error
    manifest = json.loads((result.folder / "generation.json").read_text(encoding="utf-8"))
    assert manifest["cancelled"] is result.cancelled and manifest["error"] == expected_error
    assert manifest["saved"] == 1 and len(manifest["attempts"]) == 1
    attempt = manifest["attempts"][0]
    assert attempt["call"] == 1 and attempt["codex_thread_id"] == native_id
    assert attempt["saved_files"] == [result.images[0].name]
    assert manifest["images"][0]["codex_thread_id"] == native_id


def test_failed_call_without_thread_identity_does_not_recover_other_files(tmp_path):
    root = tmp_path / "native"
    unrelated = png(root / str(uuid4()) / "unrelated.png")

    class Client:
        calls = 0
        last_thread_id = ""

        def request(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("synthetic timeout without native identity")

    client = Client()
    result = ReferenceGenerator(client, root).run(
        GenerationRequest("A seated person", 2, tmp_path / "guides"), Event())

    assert result.error == "synthetic timeout without native identity"
    assert not result.images and client.calls == 1 and unrelated.is_file()


def generation_manifest(tmp_path):
    image = png(tmp_path / "image.png")
    data = {"kind": "ai_generated_pose_guides", "description": "A seated person", "requested": 1,
            "images": [{"file": image.name, "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]}
    manifest = tmp_path / "generation.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest, image, data


def test_saved_generation_matching_hash_loads_without_integrity_warning(tmp_path):
    manifest, image, _ = generation_manifest(tmp_path)
    result, description = load_generation(manifest)
    assert result.images == [image] and description == "A seated person"
    assert result.warnings == []


def test_replaced_valid_generation_image_is_rejected_by_stored_hash(tmp_path):
    manifest, image, _ = generation_manifest(tmp_path)
    png(image, "green")
    with pytest.raises(ValueError):
        load_generation(manifest)


@pytest.mark.parametrize("checksum", [None, 1, "", "g" * 64, "a" * 63])
def test_malformed_stored_generation_hash_is_not_treated_as_legacy(tmp_path, checksum):
    manifest, _, data = generation_manifest(tmp_path)
    data["images"][0]["sha256"] = checksum
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_generation(manifest)


def test_legacy_generation_without_hash_loads_with_integrity_warning(tmp_path):
    manifest, image, data = generation_manifest(tmp_path)
    data["images"][0].pop("sha256")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    result, description = load_generation(manifest)
    assert result.images == [image] and description == "A seated person"
    assert result.warnings, "Legacy content cannot be represented as hash-verified"
