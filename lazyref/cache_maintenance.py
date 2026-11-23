"""Expire search cache entries and their private image copies when the cache is idle."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .library import is_link


ACTIVITY_DB = "activity.sqlite3"
SWEEP_INTERVAL = 24 * 3600
BLOB_NAME = re.compile(r"[0-9a-f]{64}\.jpg")


def check_storage_paths(root):
    """Do not open internal cache files through links into unrelated folders."""
    paths = [root, root / "images"]
    for name in ("cache.sqlite3", ACTIVITY_DB):
        paths.extend(root / (name + suffix) for suffix in ("", "-journal", "-wal", "-shm"))
    for path in paths:
        try:
            if is_link(path):
                raise OSError("Redirected search cache path")
        except FileNotFoundError:
            continue


def acquire_activity(root):
    # A separate rollback-journal database lets searches keep a shared lock
    # while writing cache entries normally. The OS releases it on process exit.
    activity = sqlite3.connect(root / ACTIVITY_DB, timeout=5, isolation_level=None)
    try:
        if activity.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise sqlite3.OperationalError("Unsupported cache activity journal mode")
        activity.execute("BEGIN")
        activity.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return activity
    except BaseException:
        activity.close()
        raise


def expire_entries(db, image_cutoff, source_cutoff):
    live, expired = set(), set()
    with db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("""
            SELECT value, updated <= ? FROM entries
            WHERE kind NOT IN ('source', 'task', 'assessment', 'screen')
        """, (image_cutoff,))
        for value, stale in rows:
            try:
                asset = json.loads(value)
            except (TypeError, ValueError, RecursionError):
                continue
            if isinstance(asset, dict):
                bucket = expired if stale else live
                for field in ("file", "preview"):
                    checksum = asset.get(field)
                    if isinstance(checksum, str):
                        bucket.add(checksum)
        db.execute("""
            DELETE FROM entries
            WHERE updated <= CASE WHEN kind = 'source' THEN ? ELSE ? END
        """, (source_cutoff, image_cutoff))
    return live, expired


def sweep_images(directory, live, expired, cutoff):
    with os.scandir(directory) as files:
        for entry in files:
            if not BLOB_NAME.fullmatch(entry.name):
                continue
            checksum = entry.name[:-4]
            if checksum in live:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    continue
                # A replaced entry or interrupted write can leave an unindexed
                # copy. Give those files the full image TTL before reclaiming.
                path = Path(entry.path)
                if checksum not in expired:
                    if info.st_mtime > cutoff:
                        continue
                    with path.open("rb") as blob:
                        if hashlib.file_digest(blob, "sha256").hexdigest() != checksum:
                            continue
                path.unlink()
            except OSError:
                # Windows may temporarily deny removal. Other copies can
                # still be reclaimed, and the next sweep retries this one.
                continue


def clean_if_idle(root, db, activity, ttl, source_ttl):
    activity.rollback()
    root = root.absolute()
    if str(root).startswith(("\\\\", "//")) or any(is_link(p) for p in reversed(root.parents)):
        return
    activity.execute("PRAGMA busy_timeout=0")
    try:
        activity.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return
        raise

    with activity:
        check_storage_paths(root)
        activity.execute("CREATE TABLE IF NOT EXISTS maintenance (id INTEGER PRIMARY KEY, last_sweep REAL NOT NULL)")
        previous = activity.execute("SELECT last_sweep FROM maintenance WHERE id = 1").fetchone()
        now = time.time()
        if previous and isinstance(previous[0], (int, float)) and 0 <= now - previous[0] < SWEEP_INTERVAL:
            return
        cutoff = now - ttl
        live, expired = expire_entries(db, cutoff, now - source_ttl)
        sweep_images(root / "images", live, expired, cutoff)
        activity.execute("INSERT OR REPLACE INTO maintenance VALUES (1, ?)", (now,))
