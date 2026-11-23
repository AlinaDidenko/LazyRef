"""Обмежений локальний кеш. Ключі API та облікові дані сюди не потрапляють."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from . import cache_maintenance
from .models import Asset

TTL = 7 * 24 * 3600
SOURCE_TTL = 24 * 3600

# Previous releases included the entire storage module in rating keys. Keep
# those rule identifiers (LF and CRLF checkouts) across storage-only changes.
# Change the identifiers if the rules for reusing an assessment change.
CACHE_RULES_LF = "27929043efbc62a22652e80eebab23aab748a4b3c8c7e6644a538b43f52df02d"
CACHE_RULES_CRLF = "ab5f35268faea1bdd81024e9af514395a43485da2f435b4f9443a2b4675bfcf7"


def key_for(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def task_key(request):
    # Зміна промптів/правил автоматично робить старі оцінки непридатними.
    root = Path(__file__).parent
    rules = {name: file_hash(root / name) for name in (
        "codex.py", "priorities.py", "models.py", "wide_search.py", "wide_agent.py", "images.py", "visual_reference.py", "custom_priorities.py", "photo_filters.py")}
    with Path(__file__).open("rb") as module:
        rules["search_cache.py"] = CACHE_RULES_CRLF if module.readline().endswith(b"\r\n") else CACHE_RULES_LF
    # Автоматичний вибір моделі залежить також від конфігурації Codex.
    # Зберігаємо лише відбиток файлу, а не його вміст або секрети.
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        config_hash = file_hash(config) if not request.model and config.is_file() else None
    except OSError:
        config_hash = None
    return key_for({"description": request.search_description, "profile": request.search_profile,
        "verify_sources": request.verify_sources,
        "enhanced_dedup": request.enhanced_dedup, "high_quality_only": request.high_quality_only,
        "nudity": request.allows_artistic_nudity, "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "reference_mode": request.reference_mode,
        "custom_criteria": request.custom_criteria,
        "config": config_hash, "rules": rules,
        "guide": file_hash(request.reference_image) if request.reference_image else None})


def assessment_key(task, plan, asset):
    c = asset.candidate
    return key_for({"task": task, "criteria": plan.get("criteria", []), "image": file_hash(asset.preview),
        "source": [c.page_url, c.source_kind, c.source_checked, c.source_notes]})


class SearchCache:
    def __init__(self, root, enabled=True):
        self.root, self.enabled, self.db = Path(root), enabled, None
        self._activity = None
        self.warning = ""
        if enabled:
            try:
                cache_maintenance.check_storage_paths(self.root)
                (self.root / "images").mkdir(parents=True, exist_ok=True)
                self._activity = cache_maintenance.acquire_activity(self.root)
                self.db = sqlite3.connect(self.root / "cache.sqlite3", timeout=5)
                self.db.execute("CREATE TABLE IF NOT EXISTS entries (kind TEXT, key TEXT, value TEXT, updated REAL, PRIMARY KEY(kind,key))")
                self.db.commit()
            except (OSError, sqlite3.Error):
                if self.db is not None:
                    self.db.close()
                    self.db = None
                if self._activity is not None:
                    self._activity.close()
                    self._activity = None
                self._failed()

    def _failed(self):
        self.warning = "Локальний кеш недоступний; пошук продовжується без повторного використання."

    def get(self, kind, key, ttl=None):
        if not self.db:
            return None
        if ttl is None:
            ttl = SOURCE_TTL if kind == "source" else TTL
        try:
            row = self.db.execute("SELECT value,updated FROM entries WHERE kind=? AND key=?", (kind, key)).fetchone()
            if row and time.time() - row[1] < ttl:
                return json.loads(row[0])
        except (sqlite3.Error, ValueError, TypeError):
            self._failed()
        return None

    def put(self, kind, key, value):
        if not self.db:
            return
        try:
            self.db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?)",
                            (kind, key, json.dumps(value, ensure_ascii=False), time.time()))
            self.db.commit()
        except (sqlite3.Error, TypeError, ValueError):
            self._failed()

    def _store_file(self, path):
        checksum = file_hash(path)
        target = self.root / "images" / (checksum + ".jpg")
        if not target.exists() or file_hash(target) != checksum:
            temporary = target.with_suffix("." + uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(Path(path).read_bytes())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return checksum

    def _file(self, checksum):
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("Invalid cache checksum")
        path = self.root / "images" / (checksum + ".jpg")
        if (not path.resolve().is_relative_to(self.root.resolve()) or path.stat().st_size > 20_000_000
                or file_hash(path) != checksum):
            raise ValueError("Changed cache image")
        return path

    def store_asset(self, kind, asset):
        if not self.db:
            return
        try:
            payload = {"file": self._store_file(asset.path), "preview": self._store_file(asset.preview),
                       "width": asset.width, "height": asset.height, "method": asset.method}
            self.put(kind, key_for([asset.candidate.image_url, asset.candidate.thumbnail_url]), payload)
        except (OSError, ValueError):
            self._failed()

    def asset(self, kind, candidate):
        data = self.get(kind, key_for([candidate.image_url, candidate.thumbnail_url]))
        if not isinstance(data, dict):
            return None
        try:
            return Asset(candidate, self._file(data["file"]), self._file(data["preview"]),
                         data["width"], data["height"], data["method"])
        except (KeyError, OSError, ValueError, TypeError):
            return None

    def close(self):
        if self.db is None:
            return
        try:
            cache_maintenance.clean_if_idle(self.root, self.db, self._activity, TTL, SOURCE_TTL)
        except (OSError, sqlite3.Error):
            # Maintenance is optional. A later idle close will retry it.
            self.warning = "Не вдалося завершити очищення кешу."
        finally:
            self._activity.close()
            self.db.close()
            self._activity = self.db = None


def cached_rating(data, criteria, profile, verify_sources=True):
    """Пошкоджений або неповний запис не може стати прийнятим фото."""
    from .validation import validated_assessment
    try:
        return validated_assessment(data, criteria, profile, verify_sources)
    except ValueError:
        return None
