"""Регресії конкретної сцени: чоловік сидить за барною стійкою спиною до камери."""

from dataclasses import replace
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient
from lazyref.models import Assessment, Candidate, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.exporter import export_results
from test_core import make_asset


PLAN = {"summary": "Чоловік за барною стійкою зі спини", "focus": "scene",
    "queries": ["man seated at bar counter rear view", "man at pub counter seen from behind",
                "seated male bar patron back view", "male sitting at pub bar rear view"],
    "discovery_queries": ["man bar counter", "man seated pub"],
    "criteria": [
        {"id": "c1", "label": "Чоловік", "kind": "subject", "keywords": ["man", "male"]},
        {"id": "c2", "label": "Сидить", "kind": "pose", "keywords": ["sitting", "seated"]},
        {"id": "c3", "label": "Барна стійка", "kind": "scene", "keywords": ["bar", "pub", "counter"]},
        {"id": "c4", "label": "Вигляд зі спини", "kind": "view", "keywords": ["rear view", "from behind", "back view"]}]}


def rating(score=85, failed=(), uncertain=()):
    checks = [{"id": c["id"], "label": c["label"], "kind": c["kind"],
        "status": "not_met" if c["id"] in failed else "uncertain" if c["id"] in uncertain else "met",
        "evidence": "Visible evidence"} for c in PLAN["criteria"]]
    return Assessment("photo", "high", score, "Test explanation", [], [], "ordinary", checks)


def test_bar_regression_requires_scene_and_rear_view_even_with_inflated_score():
    assert rating().is_close
    assert not rating(99, failed=("c3",)).is_close  # Автобус — не бар.
    assert not rating(99, failed=("c4",)).is_close  # Бар, але обличчям до камери.
    assert not rating(99, uncertain=("c3",)).is_close
    assert not rating(99, failed=("c3", "c4")).eligible  # Просто чоловік на дивані.
    assert not rating(99, failed=("c1",)).eligible
    assert rating(70, failed=("c3",)).eligible  # Лише явно позначена часткова альтернатива.
    assert not rating(37, failed=("c3", "c4")).eligible




@pytest.mark.parametrize("bad", ["missing", "duplicate", "unknown", "uncertain"])
def test_visual_criteria_are_complete_and_uncertain_never_means_close(tmp_path, bad):
    asset = make_asset(tmp_path)
    checks = [{"id": c["id"], "status": "met", "evidence": "visible"} for c in PLAN["criteria"]]
    if bad == "missing": checks.pop()
    if bad == "duplicate": checks[-1] = checks[0]
    if bad == "unknown": checks[-1]["id"] = "invented"
    if bad == "uncertain": checks[-1]["status"] = "uncertain"
    row = {"id": "a", "media": "photo", "confidence": "high", "score": 99, "reference_type": "ordinary",
           "reason": "Test", "matches": [], "misses": [], "criterion_checks": checks}
    client = CodexClient.__new__(CodexClient)
    client.request = lambda *args: {"images": [row]}
    if bad == "uncertain":
        result = client.assess("bar scene", [asset], Event(), PLAN["criteria"])
        assert not result["a"].is_close and result["a"].unmet_criteria == ["Вигляд зі спини"]
        assert result["a"].score == 74
    else:
        with pytest.raises(RuntimeError, match="ознаки"):
            client.assess("bar scene", [asset], Event(), PLAN["criteria"])










def test_export_prioritizes_verified_conjunction_and_separates_alternatives(tmp_path):
    exact = make_asset(tmp_path, "exact")
    exact.assessment = rating(78)
    partial = make_asset(tmp_path, "bus", 1)
    partial.assessment = rating(99, failed=("c3",))
    weak = make_asset(tmp_path, "sofa", 2)
    weak.assessment = rating(99, failed=("c3", "c4"))
    req = SearchRequest("bar rear view", 10, tmp_path / "out")
    folder, saved = export_results(req, [partial, weak, exact], PLAN, {})
    assert [a.candidate.id for a in saved] == ["exact", "bus"]
    assert saved[0].path.parent == folder
    assert saved[1].path.parent == folder / "partial"
    report = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["close_matches"] == 1 and report["partial_matches"] == 1
    assert report["results"][1]["file"].startswith("partial/")
    assert all((folder / r["file"]).is_file() for r in report["results"])


