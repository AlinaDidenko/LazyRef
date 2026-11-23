"""Local receipts for completed assessments awaiting the main session commit.

The caller owns condition/file validation and only applies receipts to matching,
uncommitted rows. A failed disk write also retains the receipt in this process;
that last fallback cannot survive closing the application.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4


MAX_ENTRIES = 300
MAX_BYTES = 20_000_000
_KIND = "lazyref_assessment_recovery"
_MEMORY = {}
_LOCK = RLock()


def _location(path, session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError("Некоректний ідентифікатор сесії відновлення оцінок.")
    session_path = Path(path).resolve()
    return (str(session_path), session_id), session_path.parent / "assessment-recovery.json"


def _entries(value):
    if not isinstance(value, dict) or len(value) > MAX_ENTRIES:
        raise ValueError("Некоректна кількість оцінок у журналі відновлення.")
    for ident, receipt in value.items():
        if (not isinstance(ident, str) or not ident or len(ident) > 300
                or not isinstance(receipt, dict) or set(receipt) != {"key", "assessment"}
                or not isinstance(receipt["key"], str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["key"]) is None
                or not isinstance(receipt["assessment"], dict)):
            raise ValueError("Некоректний запис оцінки в журналі відновлення.")
    return deepcopy(value)


def _encoded(session_id, entries):
    data = {"version": 1, "kind": _KIND, "session_id": session_id, "entries": entries}
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Оцінки неможливо записати в журнал відновлення.") from exc
    if len(encoded) > MAX_BYTES:
        raise ValueError("Завеликий журнал відновлення оцінок.")
    return encoded


def _read(sidecar, session_id):
    if sidecar.is_symlink() or getattr(sidecar, "is_junction", lambda: False)():
        raise ValueError("Журнал відновлення оцінок не може бути посиланням.")
    try:
        with sidecar.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > MAX_BYTES:
        raise ValueError("Завеликий журнал відновлення оцінок.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Пошкоджений журнал відновлення оцінок.") from exc
    if (not isinstance(data, dict) or set(data) != {"version", "kind", "session_id", "entries"}
            or type(data["version"]) is not int or data["version"] != 1
            or data["kind"] != _KIND or data["session_id"] != session_id):
        raise ValueError("Журнал відновлення не відповідає цій сесії.")
    entries = _entries(data["entries"])
    _encoded(session_id, entries)  # Reject non-finite JSON values as well.
    return entries


def _write(sidecar, encoded):
    temporary = sidecar.with_name(f".{sidecar.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(sidecar)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Do not hide the primary write/replace failure.


def pending(path, session_id):
    """Return an independent disk+RAM snapshot; malformed disk state fails closed."""
    key, sidecar = _location(path, session_id)
    with _LOCK:
        merged = _read(sidecar, session_id)
        merged.update(_MEMORY.get(key, {}))
        merged = _entries(merged)
        _encoded(session_id, merged)
        return merged


def remember(path, session_id, entries):
    """Retain validated receipts first, then persist; return a warning on failure."""
    key, sidecar = _location(path, session_id)
    entries = _entries(entries)
    _encoded(session_id, entries)
    with _LOCK:
        memory = {**_MEMORY.get(key, {}), **entries}
        memory = _entries(memory)
        _encoded(session_id, memory)
        _MEMORY[key] = memory
        try:
            merged = _read(sidecar, session_id)
            merged.update(memory)
            merged = _entries(merged)
            encoded = _encoded(session_id, merged)
            _MEMORY[key] = merged
            _write(sidecar, encoded)
        except (OSError, ValueError) as exc:
            return "Оцінки залишилися в пам’яті; журнал відновлення не збережено: " + str(exc)
    return None


def clear(path, session_id, ids):
    """After the main commit, discard only its IDs; cleanup never undoes it."""
    key, sidecar = _location(path, session_id)
    ids = set(ids)
    with _LOCK:
        memory = {ident: receipt for ident, receipt in _MEMORY.get(key, {}).items() if ident not in ids}
        if memory:
            _MEMORY[key] = memory
        else:
            _MEMORY.pop(key, None)
        try:
            merged = _read(sidecar, session_id)
            merged.update(memory)
            merged = {ident: receipt for ident, receipt in merged.items() if ident not in ids}
            encoded = _encoded(session_id, _entries(merged))
            if merged:
                _write(sidecar, encoded)
            else:
                sidecar.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            return "Основну сесію збережено, але журнал відновлення не очищено: " + str(exc)
    return None
