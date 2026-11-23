"""Recovery receipts stay local, bounded and tied to their exact session."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from lazyref import review_recovery as recovery


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    monkeypatch.setattr(recovery, "_MEMORY", {})


def receipt(number=1):
    return {"key": f"{number:064x}", "assessment": {"score": 70, "reason": "Synthetic rating"}}


def test_disk_receipts_survive_memory_loss_and_clear_only_committed_ids(tmp_path):
    path = tmp_path / "review.json"
    assert recovery.remember(path, "session", {"a": receipt(), "b": receipt(2)}) is None
    recovery._MEMORY.clear()
    assert recovery.pending(path, "session") == {"a": receipt(), "b": receipt(2)}
    assert recovery.clear(path, "session", ["a"]) is None
    recovery._MEMORY.clear()
    assert recovery.pending(path, "session") == {"b": receipt(2)}
    assert recovery.clear(path, "session", ["b"]) is None
    assert not (tmp_path / "assessment-recovery.json").exists()
    assert recovery.pending(path, "session") == {}


def test_remember_and_pending_use_independent_snapshots(tmp_path):
    path = tmp_path / "review.json"
    entries = {"a": receipt()}
    recovery.remember(path, "session", entries)
    entries["a"]["assessment"]["score"] = 1
    loaded = recovery.pending(path, "session")
    loaded["a"]["assessment"]["score"] = 2
    assert recovery.pending(path, "session")["a"]["assessment"]["score"] == 70


@pytest.mark.parametrize("failure", ["partial_write", "fsync", "replace"])
def test_failed_atomic_write_preserves_previous_disk_and_new_memory(tmp_path, monkeypatch, failure):
    path = tmp_path / "review.json"
    sidecar = tmp_path / "assessment-recovery.json"
    assert recovery.remember(path, "session", {"a": receipt()}) is None
    previous = sidecar.read_bytes()
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

    def open_file(file, *args, **kwargs):
        stream = original_open(file, *args, **kwargs)
        return PartialWriter(stream) if file.suffix == ".tmp" else stream

    def replace(file, destination):
        if destination == sidecar:
            raise PermissionError("synthetic locked receipt")
        return original_replace(file, destination)

    def fail_fsync(*args):
        raise OSError("synthetic flush failure")

    if failure == "partial_write":
        monkeypatch.setattr(Path, "open", open_file)
    elif failure == "replace":
        monkeypatch.setattr(Path, "replace", replace)
    else:
        monkeypatch.setattr(recovery.os, "fsync", fail_fsync)
    assert recovery.remember(path, "session", {"b": receipt(2)})
    assert sidecar.read_bytes() == previous
    assert recovery.pending(path, "session") == {"a": receipt(), "b": receipt(2)}
    assert list(tmp_path.glob("*.tmp")) == []


def test_memory_survives_unavailable_directory(tmp_path):
    path = tmp_path / "unavailable" / "review.json"
    assert recovery.remember(path, "session", {"a": receipt()})
    assert recovery.pending(path, "session") == {"a": receipt()}


def test_unreadable_receipt_raises_without_losing_new_memory(tmp_path, monkeypatch):
    path = tmp_path / "review.json"
    recovery.remember(path, "session", {"a": receipt()})
    original_open = Path.open

    def locked(file, *args, **kwargs):
        if file.name == "assessment-recovery.json":
            raise PermissionError("synthetic read lock")
        return original_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", locked)
        assert recovery.remember(path, "session", {"b": receipt(2)})
        with pytest.raises(PermissionError):
            recovery.pending(path, "session")
    assert recovery.pending(path, "session") == {"a": receipt(), "b": receipt(2)}


def test_receipts_cannot_cross_session_identity(tmp_path):
    path = tmp_path / "review.json"
    recovery.remember(path, "first", {"a": receipt()})
    with pytest.raises(ValueError, match="сесії"):
        recovery.pending(path, "second")
    other = tmp_path / "other"
    other.mkdir()
    assert recovery.pending(other / "review.json", "first") == {}


@pytest.mark.parametrize("payload", [
    b"{", b"null", b"[]", b"\xff",
    json.dumps({"version": True, "kind": "lazyref_assessment_recovery", "session_id": "session", "entries": {}}).encode(),
    json.dumps({"version": 1, "kind": "other", "session_id": "session", "entries": {}}).encode(),
    json.dumps({"version": 1, "kind": "lazyref_assessment_recovery", "session_id": "session", "entries": [], "path": "outside"}).encode(),
])
def test_malformed_disk_receipt_fails_closed_but_new_memory_is_retained(tmp_path, payload):
    path = tmp_path / "review.json"
    sidecar = tmp_path / "assessment-recovery.json"
    sidecar.write_bytes(payload)
    assert recovery.remember(path, "session", {"a": receipt()})
    assert sidecar.read_bytes() == payload
    with pytest.raises(ValueError):
        recovery.pending(path, "session")
    sidecar.unlink()
    assert recovery.pending(path, "session") == {"a": receipt()}


@pytest.mark.parametrize("entries", [
    [], {"": receipt()}, {"a": {}}, {"a": {"key": "wrong", "assessment": {}}},
    {"a": {"key": "a" * 64, "assessment": []}},
    {"a": {"key": "a" * 64, "assessment": {}, "path": "outside"}},
    {"a": {"key": "a" * 64, "assessment": {"score": float("nan")}}},
    {str(i): receipt(i) for i in range(301)},
])
def test_invalid_new_receipts_are_not_remembered(tmp_path, entries):
    path = tmp_path / "review.json"
    with pytest.raises(ValueError):
        recovery.remember(path, "session", entries)
    assert recovery.pending(path, "session") == {}
    assert not (tmp_path / "assessment-recovery.json").exists()


def test_receipt_file_size_is_bounded_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "MAX_BYTES", 1024)
    path = tmp_path / "review.json"
    sidecar = tmp_path / "assessment-recovery.json"
    sidecar.write_bytes(b" " * 1025)
    with pytest.raises(ValueError, match="Завеликий"):
        recovery.pending(path, "session")
    sidecar.unlink()
    large = receipt()
    large["assessment"]["reason"] = "x" * 1024
    with pytest.raises(ValueError, match="Завеликий"):
        recovery.remember(path, "session", {"a": large})
    assert recovery.pending(path, "session") == {}


def test_symlink_receipt_is_rejected_before_read(tmp_path, monkeypatch):
    sidecar = tmp_path / "assessment-recovery.json"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == sidecar or original(path))
    with pytest.raises(ValueError, match="посиланням"):
        recovery.pending(tmp_path / "review.json", "session")


def test_failed_clear_is_only_a_warning_and_preserves_uncommitted_receipts(tmp_path, monkeypatch):
    path = tmp_path / "review.json"
    recovery.remember(path, "session", {"a": receipt(), "b": receipt(2)})
    monkeypatch.setattr(recovery, "_write", lambda *args: (_ for _ in ()).throw(OSError("synthetic disk error")))
    assert recovery.clear(path, "session", ["a"])
    # The committed receipt may remain on disk. The caller must never use it
    # to overwrite an already committed main-session rating.
    assert recovery.pending(path, "session")["b"] == receipt(2)
    assert all("a" not in entries for entries in recovery._MEMORY.values())


def test_concurrent_threads_do_not_drop_independent_receipts(tmp_path):
    path = tmp_path / "review.json"
    with ThreadPoolExecutor(max_workers=4) as executor:
        warnings = list(executor.map(lambda i: recovery.remember(path, "session", {str(i): receipt(i)}), range(12)))
    assert warnings == [None] * 12
    recovery._MEMORY.clear()
    assert recovery.pending(path, "session") == {str(i): receipt(i) for i in range(12)}
