"""Exercise automatic cache maintenance without opening a working search library."""

from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from lazyref.models import Asset, Candidate
from lazyref.search_cache import SOURCE_TTL, TTL, SearchCache


def blob(images, content, updated):
    path = images / (sha256(content).hexdigest() + ".jpg")
    path.write_bytes(content)
    os.utime(path, (updated, updated))
    return path


def seed_legacy_cache(root, rows):
    with closing(sqlite3.connect(root / "cache.sqlite3")) as db:
        db.execute("CREATE TABLE entries (kind TEXT, key TEXT, value TEXT, updated REAL, PRIMARY KEY(kind,key))")
        db.executemany("INSERT INTO entries VALUES (?,?,?,?)", rows)
        db.commit()


def test_first_maintenance_cleans_a_legacy_cache_and_preserves_history(tmp_path):
    root = tmp_path / "_search_cache"
    images = root / "images"
    images.mkdir(parents=True)
    now = time.time()
    old = now - 2 * TTL
    shared = blob(images, b"preview used by two entries", old)
    expired = blob(images, b"expired photo", now)
    current = blob(images, b"current photo", old)
    orphan = blob(images, b"old copy left by a replaced row", old)
    new_orphan = blob(images, b"recent unindexed copy", now)
    unrelated = images / "saved-by-hand.jpg"
    unrelated.write_bytes(b"keep")
    os.utime(unrelated, (old, old))
    lookalike = images / ("a" * 64 + ".jpg")
    lookalike.write_bytes(b"unindexed photo with a different digest")
    os.utime(lookalike, (old, old))
    history = tmp_path / "_sessions" / "old-search" / "images" / expired.name
    history.parent.mkdir(parents=True)
    history.write_bytes(expired.read_bytes())

    def payload(path):
        return {"file": path.stem, "preview": shared.stem, "width": 300,
                "height": 200, "method": "download"}

    # Persist the schema used before maintenance, without creating any new bookkeeping.
    seed_legacy_cache(root, [
        ("preview", "obsolete", json.dumps(payload(expired)), old),
        ("full_unverified", "usable", json.dumps(payload(current)), now),
        ("source", "day-old", "{}", now - SOURCE_TTL - 60),
        ("source", "usable", "{}", now),
        ("assessment", "day-old", "{}", now - SOURCE_TTL - 60),
        ("task", "obsolete", "{}", old),
    ])

    SearchCache(root, enabled=False).close()
    assert expired.exists() and orphan.exists()
    SearchCache(root).close()

    with closing(sqlite3.connect(root / "cache.sqlite3")) as db:
        assert set(db.execute("SELECT kind,key FROM entries")) == {
            ("full_unverified", "usable"), ("source", "usable"), ("assessment", "day-old"),
        }
    assert not expired.exists()
    assert not orphan.exists()
    assert new_orphan.read_bytes() == b"recent unindexed copy"
    assert current.read_bytes() == b"current photo"
    assert shared.read_bytes() == b"preview used by two entries"
    assert unrelated.read_bytes() == b"keep"
    assert lookalike.read_bytes() == b"unindexed photo with a different digest"
    assert history.read_bytes() == b"expired photo"


def test_one_locked_blob_does_not_prevent_other_removals_or_later_retry(tmp_path, monkeypatch):
    root = tmp_path / "_search_cache"
    images = root / "images"
    images.mkdir(parents=True)
    now = time.time()
    old = now - 2 * TTL
    copies = {blob(images, content, old) for content in (b"one", b"two", b"three")}
    seed_legacy_cache(root, [
        ("full", path.stem, json.dumps({"file": path.stem, "preview": path.stem}), old)
        for path in copies
    ])

    real_unlink = Path.unlink
    locked = set()

    def lock_first_copy(path, *args, **kwargs):
        if path in copies and not locked:
            locked.add(path)
        if path in locked:
            raise PermissionError("File in use")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", lock_first_copy)
        SearchCache(root).close()
    assert len(locked) == 1
    assert {path for path in copies if path.exists()} == locked

    monkeypatch.setattr(time, "time", lambda: now + 2 * TTL)
    SearchCache(root).close()
    assert all(not path.exists() for path in copies)


def test_cleanup_leaves_a_redirected_images_directory_alone(tmp_path):
    root = tmp_path / "_search_cache"
    root.mkdir()
    protected = tmp_path / "_sessions" / "old-search" / "images"
    protected.mkdir(parents=True)
    old = time.time() - 2 * TTL
    photo = blob(protected, b"saved session photo", old)
    orphan = blob(protected, b"saved session preview", old)
    seed_legacy_cache(root, [
        ("full", "obsolete", json.dumps({"file": photo.stem, "preview": photo.stem}), old),
    ])
    directory = root / "images"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(protected), str(directory))
    else:
        try:
            directory.symlink_to(protected, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Directory symlinks unavailable: {exc}")

    SearchCache(root).close()

    assert photo.read_bytes() == b"saved session photo"
    assert orphan.read_bytes() == b"saved session preview"
    assert directory.samefile(protected)


def test_an_active_search_keeps_its_cached_file_until_it_closes(tmp_path, monkeypatch):
    root = tmp_path / "_search_cache"
    original = tmp_path / "original.jpg"
    original.write_bytes(b"image bytes")
    candidate = Candidate("id", "sample", image_url="https://example.invalid/sample.jpg")
    first = SearchCache(root)
    try:
        first.store_asset("full", Asset(candidate, original, original, 300, 200))
        cached = first.asset("full", candidate)
        assert cached is not None
        later = time.time() + 2 * TTL
        monkeypatch.setattr(time, "time", lambda: later)
        SearchCache(root).close()
        assert cached.path.read_bytes() == b"image bytes"
    finally:
        first.close()

    SearchCache(root).close()
    assert not cached.path.exists()
    assert original.read_bytes() == b"image bytes"
