"""Validate local preferences before widgets use them; retain damaged originals."""

from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from .i18n import DEFAULT_LANGUAGE, LANGUAGES, translate
from .priorities import PROFILES
from .reasoning import DEFAULT_REASONING_EFFORT, REASONING_LABELS


def _text(value, *, empty=True):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return empty or bool(value.strip())


def normalize_settings(value, workspace):
    """Keep valid/unknown preferences; default only malformed known fields."""
    if not isinstance(value, dict):
        return {}, ["формат файла"]
    data, invalid = dict(value), []
    integers = {
        "search_revision": (1, None, 2),
        "candidate_limit": (8, 120, 72),
        "search_rounds": (1, 8, 6),
        "discovery_limit": (20, 300, 200),
        "detail_limit": (8, 120, 40),
        "generation_count": (1, 8, 3),
    }
    for key, (low, high, default) in integers.items():
        if key in data and (type(data[key]) is not int or data[key] < low
                            or high is not None and data[key] > high):
            data[key] = default
            invalid.append(key)
    for key, default in {"screenshots": True, "artistic_nudity": True,
                         "reuse_cache": True, "verify_sources": True,
                         "enhanced_dedup": False, "high_quality_only": False,
                         "hide_startup_guide": False}.items():
        if key in data and type(data[key]) is not bool:
            data[key] = default
            invalid.append(key)
    for key, choices, default in (("search_profile", PROFILES, "strict"),
                                  ("search_strategy", ("wide", "legacy"), "wide"),
                                  ("language", LANGUAGES, DEFAULT_LANGUAGE),
                                  ("reasoning_effort", REASONING_LABELS, DEFAULT_REASONING_EFFORT)):
        if key in data and (not isinstance(data[key], str) or data[key] not in choices):
            data[key] = default
            invalid.append(key)
    for key in ("model", "generation_model", "codex_path", "output_dir"):
        if key in data and not _text(data[key], empty=key != "output_dir"):
            data[key] = (str(Path(workspace) / "references") if key == "output_dir" else
                         data.get("model", "") if key == "generation_model" else "")
            invalid.append(key)
    if "library_roots" in data:
        roots = data["library_roots"]
        cleaned = [p for p in roots if _text(p, empty=False)] if isinstance(roots, list) else []
        if not isinstance(roots, list) or cleaned != roots:
            data["library_roots"] = cleaned
            invalid.append("library_roots")
    return data, invalid


@dataclass
class LoadedSettings:
    data: dict
    warning: str = ""
    original: bytes | None = None
    unreadable: bool = False
    backup_path: Path | None = None

    def preserve_original(self, path):
        """A failed backup must never permit replacing the damaged source."""
        if self.unreadable:
            raise OSError("Початковий файл налаштувань недоступний; його не перезаписано.")
        if self.original is None or self.backup_path is not None:
            return
        path = Path(path)
        backup = path.with_name(f"{path.stem}.invalid-{uuid4().hex}{path.suffix}")
        created = False
        try:
            with backup.open("xb") as stream:
                created = True
                stream.write(self.original)
        except OSError:
            if created:
                backup.unlink(missing_ok=True)
            raise
        self.backup_path = backup


def load_settings(path, workspace):
    try:
        original = Path(path).read_bytes()
    except FileNotFoundError:
        return LoadedSettings({})
    except OSError:
        return LoadedSettings({}, "Не вдалося прочитати налаштування. Застосовано типові значення; "
                              "початковий файл не буде перезаписано.", unreadable=True)
    try:
        value = json.loads(original)
        data, invalid = normalize_settings(value, workspace)
    except (ValueError, UnicodeError, RecursionError):
        data, invalid = {}, ["формат файла"]
    if not invalid:
        return LoadedSettings(data)
    language = data.get("language", DEFAULT_LANGUAGE)
    warning = translate(
        "Некоректні налаштування: {fields}. Застосовано типові значення для цих полів. Перед збереженням "
        "початковий файл буде збережено окремою резервною копією.",
        language, fields=", ".join(invalid))
    return LoadedSettings(data, warning, original=original)
