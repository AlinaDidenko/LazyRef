"""Matching ratings require matching task, rules, model and image evidence."""

from copy import deepcopy
from dataclasses import asdict, replace
import pytest

from lazyref.models import Candidate, SearchRequest
from lazyref import review_identity as identity


CRITERIA = [
    {"id": "c1", "kind": "subject", "label": "Person", "keywords": ["person"], "priority": "required"},
    {"id": "c2", "kind": "pose", "label": "Seated", "keywords": ["seated"], "priority": "preferred"},
]
PLAN = {"criteria": CRITERIA, "summary": "A seated person", "queries": ["seated"]}


@pytest.fixture
def request_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-config"))
    return SearchRequest("A seated person", 2, tmp_path / "results", model="fixture-model")


@pytest.mark.parametrize("changes", [
    {"description": "A standing person"},
    {"search_profile": "pose"},
    {"verify_sources": False},
    {"artistic_nudity": True},
    {"reference_mode": "visual"},
    {"model": "another-model"},
    {"reasoning_effort": "high"},
    {"enhanced_dedup": True},
    {"high_quality_only": True},
])
def test_changed_semantic_request_field_invalidates_assessment(request_data, changes):
    original = identity.context_key(request_data, PLAN, "a" * 64)
    assert identity.context_key(replace(request_data, **changes), PLAN, "a" * 64) != original


def test_effective_description_is_independently_bound(request_data):
    class RequestProxy:
        def __init__(self, description):
            self.search_description = description

        def __getattr__(self, key):
            return getattr(request_data, key)

    first = identity.context_key(RequestProxy("Effective first description"), PLAN, None)
    assert identity.context_key(RequestProxy("Effective second description"), PLAN, None) != first


@pytest.mark.parametrize("change", ["priority", "label", "order", "raw_description"])
def test_custom_conditions_do_not_alias_even_with_same_effective_description(request_data, change):
    request_data = replace(request_data, search_profile="custom", custom_criteria=deepcopy(CRITERIA))
    original = identity.context_key(request_data, PLAN, None)
    changed = deepcopy(request_data)
    if change == "priority":
        changed.custom_criteria[1]["priority"] = "required"
    elif change == "label":
        changed.custom_criteria[1]["label"] = "Standing"
    elif change == "order":
        changed.custom_criteria.reverse()
        for index, criterion in enumerate(changed.custom_criteria, 1):
            criterion["id"] = f"c{index}"
    else:
        changed.description = "A different raw description"
        assert changed.search_description == request_data.search_description
    assert identity.context_key(changed, PLAN, None) != original


@pytest.mark.parametrize("change", ["priority", "label", "order", "keywords", "guide", "no_guide"])
def test_changed_plan_or_reference_invalidates_assessment(request_data, change):
    plan, guide = deepcopy(PLAN), "a" * 64
    original = identity.context_key(request_data, plan, guide)
    if change == "priority":
        plan["criteria"][1]["priority"] = "required"
    elif change == "label":
        plan["criteria"][1]["label"] = "Standing"
    elif change == "order":
        plan["criteria"].reverse()
    elif change == "keywords":
        plan["criteria"][1]["keywords"].append("from behind")
    elif change == "guide":
        guide = "b" * 64
    else:
        guide = None
    assert identity.context_key(request_data, plan, guide) != original


def test_storage_and_search_limits_do_not_change_image_assessment(request_data, tmp_path):
    original = identity.context_key(request_data, PLAN, "a" * 64)
    moved = replace(request_data, count=10, output_dir=tmp_path / "moved", codex_path="another executable",
                    candidate_limit=120, search_rounds=8, discovery_limit=300, detail_limit=100,
                    search_strategy="wide", reuse_cache=False, screenshots=False,
                    reference_image=tmp_path / "moved-guide.png")
    assert identity.context_key(moved, {**PLAN, "summary": "Different summary", "queries": ["new search"]},
                                "a" * 64) == original


def test_rule_change_invalidates_assessment(request_data, tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.mkdir()
    for name in identity._RULE_FILES:
        (rules / name).write_text("original rule", encoding="utf-8")
    monkeypatch.setattr(identity, "_ROOT", rules)
    original = identity.context_key(request_data, PLAN, None)
    (rules / "validation.py").write_text("changed rule", encoding="utf-8")
    assert identity.context_key(request_data, PLAN, None) != original


def test_automatic_model_configuration_changes_invalidate_assessment(request_data, tmp_path):
    request_data = replace(request_data, model="")
    missing = identity.context_key(request_data, PLAN, None)
    config = tmp_path / "codex-config" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "first-fixture"', encoding="utf-8")
    first = identity.context_key(request_data, PLAN, None)
    config.write_text('model = "other-fixture"', encoding="utf-8")
    second = identity.context_key(request_data, PLAN, None)
    assert len({missing, first, second}) == 3
    assert identity.context_key(request_data, PLAN, None) == second


def test_unreadable_automatic_model_configuration_fails_closed(request_data, monkeypatch):
    read = identity._file_digest

    def unreadable(path):
        if path.name == "config.toml":
            raise PermissionError("synthetic read denied")
        return read(path)

    monkeypatch.setattr(identity, "_file_digest", unreadable)
    with pytest.raises(OSError, match="автоматичного вибору моделі"):
        identity.context_key(replace(request_data, model=""), PLAN, None)
    # Explicit model selection is independent of this unavailable config.
    assert len(identity.context_key(request_data, PLAN, None)) == 64


@pytest.fixture
def row():
    return {"candidate": asdict(Candidate("photo", "Photo", page_url="https://photos.invalid/view")),
            "sha256": "a" * 64, "preview_sha256": "b" * 64, "width": 1200, "height": 900,
            "method": "download", "file": "images/full.jpg", "preview": "images/preview.jpg",
            "assessment": None, "round": 1}


@pytest.mark.parametrize("field,value", [
    ("sha256", "c" * 64), ("preview_sha256", "d" * 64),
    ("width", 2400), ("height", 1800), ("method", "screenshot"),
])
def test_changed_image_evidence_invalidates_rating(row, field, value):
    original = identity.row_key("context", row)
    row[field] = value
    assert identity.row_key("context", row) != original


@pytest.mark.parametrize("field,value", [
    ("id", "other"), ("title", "Other photo"), ("page_url", "https://other.invalid/view"),
    ("source_kind", "photo"), ("source_notes", "Different evidence"), ("source_checked", True),
    ("image_url", "https://photos.invalid/original.jpg"), ("description", "Different description"),
])
def test_changed_candidate_or_source_evidence_invalidates_rating(row, field, value):
    original = identity.row_key("context", row)
    row["candidate"][field] = value
    assert identity.row_key("context", row) != original


def test_image_key_ignores_storage_location_but_binds_task(row):
    original = identity.row_key("context", row)
    row.update(file="moved/full.jpg", preview="moved/preview.jpg", assessment={"score": 90}, round=5)
    assert identity.row_key("context", row) == original
    assert identity.row_key("another context", row) != original


def test_serialization_uses_semantic_mapping_order_not_dict_insertion_order(request_data, row):
    assert identity.context_key(request_data, PLAN, None) == identity.context_key(
        request_data, dict(reversed(list(PLAN.items()))), None)
    assert identity.row_key("context", row) == identity.row_key("context", dict(reversed(list(row.items()))))
