"""Порівняння правил відбору без хмарних запитів та захист попередніх промптів."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient, PLAN_SCHEMA, ASSESS_SCHEMA
from lazyref.models import Assessment, Candidate, SearchRequest
from lazyref.network import Network
from lazyref.pipeline import SearchPipeline
from lazyref.priorities import PROFILES
from lazyref.search_agent import SEARCH_SCHEMA, discover, feedback_from
from test_core import make_asset


DESCRIPTION = "A man seated at a bar, rear view"
PLAN = {"summary": "Чоловік на високому сидінні зі спини", "focus": "scene",
    "queries": ["man bar rear view", "seated man forearms on table rear view",
                "man high stool feet hanging back view", "male seated studio rear view"],
    "discovery_queries": ["seated rear view", "man high stool"],
    "criteria": [
        {"id": "c1", "label": "Чоловік", "kind": "subject", "keywords": ["man"]},
        {"id": "c2", "label": "Передпліччя на поверхні", "kind": "pose", "keywords": ["forearms supported"]},
        {"id": "c3", "label": "Сидить високо, ноги висять", "kind": "pose", "keywords": ["high stool"]},
        {"id": "c4", "label": "Зі спини", "kind": "view", "keywords": ["rear view"]},
        {"id": "c5", "label": "Барна стійка", "kind": "scene", "keywords": ["bar"]},
        {"id": "c6", "label": "Без сорочки", "kind": "detail", "keywords": ["shirtless"]},
    ]}


def response(asset_id, missing=("c5", "c6"), uncertain=()):
    return {"id": asset_id, "media": "photo", "confidence": "high", "score": 90,
        "reason": "Збігаються опора рук і згин ніг; одяг та оточення відрізняються.",
        "matches": ["Руки спираються на поверхню"], "misses": ["Одягнений, інша поверхня"],
        "reference_type": "ordinary", "criterion_checks": [
            {"id": c["id"], "status": "not_met" if c["id"] in missing else "uncertain" if c["id"] in uncertain else "met",
             "evidence": "Оцінка за видимими ознаками"} for c in PLAN["criteria"]]}


def assessment(profile, missing=("c5", "c6"), uncertain=()):
    row = response("a", missing, uncertain)
    checks = [{**check, "kind": c["kind"], "label": c["label"]}
              for check, c in zip(row["criterion_checks"], PLAN["criteria"])]
    return Assessment("photo", "high", 90, row["reason"], row["matches"], row["misses"],
                      "ordinary", checks, profile)


def test_previous_prompts_and_schemas_are_byte_for_byte_unchanged():
    expected = json.loads((Path(__file__).parent / "fixtures/strict_0_3_1_prompts.json").read_text())
    captured = {}
    class Captured(Exception): pass
    def stop(name):
        def request(prompt, schema, *args, **kwargs):
            captured[name] = {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()}
            raise Captured()
        return request
    client = CodexClient.__new__(CodexClient)
    guide = Path("guide.png")
    for name, fn in [("plan", lambda: client.plan(DESCRIPTION, Event())),
                     ("assess", lambda: client.assess(DESCRIPTION, [], Event(), [])),
                     ("plan_guide", lambda: client.plan(DESCRIPTION, Event(), guide)),
                     ("assess_guide", lambda: client.assess(DESCRIPTION, [], Event(), [], guide))]:
        client.request = stop(name)
        with pytest.raises(Captured): fn()
    net = Network(Event())
    try:
        for name, reference in (("discover", None), ("discover_guide", guide)):
            client.request = stop(name)
            with pytest.raises(Captured):
                discover(client, SearchRequest(DESCRIPTION, 2, Path("unused"), reference_image=reference),
                    {"queries": ["seated man bar rear view"], "criteria": []}, [], [], {}, net, Event(), lambda s: None)
    finally:
        net.close()
    assert captured == expected


def test_pose_accepts_readable_clothed_pose_with_other_surface_but_strict_is_partial():
    old, new = assessment("strict"), assessment("pose")
    assert old.coverage == 2 / 3 and old.eligible and not old.is_close
    assert new.coverage == 1 and new.eligible and new.is_close
    assert new.unmet_criteria == [] and new.unmet_preferences == ["Барна стійка", "Без сорочки"]
    assert old.unmet_criteria == ["Барна стійка", "Без сорочки"]
    assert old.unmet_preferences == []


@pytest.mark.parametrize("missing,uncertain,eligible", [
    (("c1",), (), False),  # Персонаж не відповідає.
    (("c4",), (), False),  # Неправильний ракурс.
    ((), ("c4",), False),  # Ракурс прихований.
    (("c2", "c3"), (), False),  # Тільки персонаж і ракурс — жодного збігу пози.
    ((), ("c2", "c3"), False),  # Весь рух прихований одягом/меблями.
    (("c3",), (), True),  # Частково корисна опора рук, але інші ноги.
])
def test_pose_never_calls_wrong_or_hidden_mechanics_close(missing, uncertain, eligible):
    rating = assessment("pose", missing, uncertain)
    assert rating.eligible is eligible
    assert not rating.is_close


@pytest.mark.parametrize("overrides", [{"media": "ai_generated"}, {"media": "illustration"},
    {"confidence": "low"}, {"reference_type": "sexual"}, {"reference_type": "unsafe_or_uncertain_nudity"}])
def test_pose_keeps_photo_and_content_filters(overrides):
    assert not replace(assessment("pose"), **overrides).eligible


def test_optional_feedback_does_not_pull_pose_search_back_to_nude_bar():
    rows = [{"title": "Clothed man at desk", "score": 70, "reason": "Feet touch floor", "media": "photo",
             "criterion_checks": assessment("pose", ("c3", "c5", "c6")).criterion_checks}]
    assert set(feedback_from(rows, [], {}, {}, "pose")["missing_conditions"]) == {"Сидить високо, ноги висять"}
    assert len(feedback_from(rows, [], {}, {}, "strict")["missing_conditions"]) == 3




@pytest.mark.parametrize("profile", ["strict", "pose"])
def test_plan_and_guide_assessment_use_selected_policy_and_same_call_count(tmp_path, profile):
    client = CodexClient.__new__(CodexClient)
    asset = make_asset(tmp_path)
    guide = tmp_path / "guide.png"
    calls = []
    def request(prompt, schema, cancel, images=None):
        calls.append((prompt, schema, images))
        return deepcopy(PLAN) if schema == PLAN_SCHEMA else {"images": [response(asset.candidate.id)]}
    client.request = request
    plan = client.plan(DESCRIPTION, Event(), guide, search_profile=profile)
    assert plan["search_profile"]["id"] == profile
    result = client.assess(DESCRIPTION, [asset], Event(), plan["criteria"], guide, profile)[asset.candidate.id]
    assert len(calls) == 2  # Один план та одна оцінка, додаткових викликів для пріоритетів немає.
    assert calls[0][2] == [guide] and calls[1][2] == [guide, asset.preview]
    if profile == "pose":
        assert plan["focus"] == "pose" and plan["criteria"][-1]["priority"] == "preferred"
        assert result.score == 90 and result.is_close
        assert "PREFERENCES" in calls[1][0] and "AI-GENERATED POSE GUIDE" in calls[1][0]
    else:
        assert "priority" not in plan["criteria"][-1]
        assert result.score == 74 and not result.is_close


def test_pose_plan_without_body_mechanics_fails_before_search():
    client = CodexClient.__new__(CodexClient)
    plan = deepcopy(PLAN)
    plan["criteria"] = [plan["criteria"][0]]
    client.request = lambda *args, **kwargs: plan
    with pytest.raises(RuntimeError, match="механіку пози"):
        client.plan(DESCRIPTION, Event(), search_profile="pose")


@pytest.mark.parametrize("profile", ["strict", "pose"])
def test_pipeline_records_and_applies_profile_through_search_and_export(tmp_path, profile):
    calls = []
    class Client(CodexClient):
        def __init__(self): pass
        def request(self, prompt, schema, cancel, images=None, **kwargs):
            calls.append(schema)
            if schema == PLAN_SCHEMA: return deepcopy(PLAN)
            if schema == SEARCH_SCHEMA:
                if profile == "pose":
                    assert "alternative settings" in prompt
                    assert '"id": "pose"' in prompt
                kwargs["trace"].append({"type": "web_search", "action": "search", "query": "man high stool rear view"})
                return {"image_search_available": True, "strategy": "Test", "queries": ["man high stool rear view"],
                    "limitation": "", "candidates": [{"title": "Clothed man at table", "image_url": "https://example.org/a.jpg",
                    "page_url": "https://example.org/photo", "source_kind": "unknown"}]}
            assert schema == ASSESS_SCHEMA
            # Справжній parser assess() звіряє ID за порядком вкладень.
            ordering = json.loads(prompt.split("Attachment IDs: ")[1])
            return {"images": [response(ordering[0]["id"])]}
    def loader(candidate, directory, net):
        asset = make_asset(directory, candidate.id)
        asset.candidate = candidate
        return asset
    request = SearchRequest(DESCRIPTION, 1, tmp_path, search_rounds=1, search_profile=profile)
    result = SearchPipeline(Client(), loader=loader).run(request, Event())
    assert not result.error and len(result.assets) == 1
    assert result.assets[0].assessment.is_close is (profile == "pose")
    assert len(calls) == 3
    report = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["search_profile"] == PROFILES[profile]
    assert report["plan"]["search_profile"] == PROFILES[profile]
    assert report["results"][0]["assessment"]["search_profile"] == profile
    assert report["results"][0]["match"] == ("close" if profile == "pose" else "partial")
    assert result.search_profile == profile


def test_unknown_profile_fails_without_model_calls(tmp_path):
    with pytest.raises(ValueError, match="пріоритетів"):
        SearchRequest(DESCRIPTION, 1, tmp_path, search_profile="typo").validate()
