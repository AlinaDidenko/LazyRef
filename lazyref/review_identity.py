"""Bind a retained assessment to the exact task and image it assessed.

The caller snapshots the context before the model call and verifies session
image files before trusting a matching key. These hashes are identity checks,
not signatures that authenticate an imported session.
"""

import hashlib
import json
import os
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_RULE_FILES = ("codex.py", "priorities.py", "validation.py",
               "visual_reference.py", "custom_priorities.py")


def _hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _automatic_model_config():
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        return {"state": "present", "sha256": _file_digest(config)}
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError as exc:
        # Unknown configuration must never compare equal to an absent one or
        # to an earlier unreadable configuration with different model choices.
        raise OSError("Не вдалося перевірити налаштування автоматичного вибору моделі. "
                      "Повторне використання оцінки зупинено.") from exc


def context_key(request, plan, guide_checksum):
    """Return an exact task identity using the request actually sent to assess.

    ``guide_checksum`` is the verified copied reference's SHA-256, or None.
    Count, storage paths and discovery limits do not alter an image assessment.
    Raw description is deliberately included even when custom priorities build
    a separate effective description; distinct user conditions cannot alias.
    """
    conditions = {
        "description": request.description,
        "search_description": request.search_description,
        "search_profile": request.search_profile,
        "custom_criteria": request.custom_criteria,
        "verify_sources": request.verify_sources,
        "artistic_nudity": request.allows_artistic_nudity,
        "reference_mode": request.reference_mode,
        "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "enhanced_dedup": request.enhanced_dedup,
        "high_quality_only": request.high_quality_only,
        "criteria": plan.get("criteria", []),
        "guide_sha256": guide_checksum,
    }
    return _hash({
        "version": 1,
        "conditions": conditions,
        "rules": {name: _file_digest(_ROOT / name) for name in _RULE_FILES},
        "automatic_model_config": _automatic_model_config() if not request.model else None,
    })


def row_key(context, row):
    """Bind a task identity to verified image bytes and complete source data."""
    return _hash({
        "context": context,
        "candidate": row["candidate"],
        "sha256": row["sha256"],
        "preview_sha256": row["preview_sha256"],
        "width": row["width"],
        "height": row["height"],
        "method": row["method"],
    })
