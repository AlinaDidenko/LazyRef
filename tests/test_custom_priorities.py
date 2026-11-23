"""Ручні умови: від підтвердження до пошуку, оцінок, кешу й повторного аналізу. Без хмари."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from threading import Event

import pytest

from lazyref.codex import CodexClient, PLAN_SCHEMA
from lazyref.custom_priorities import approved_plan, draft_choices, make_plan, validate_choices
from lazyref.exporter import export_results
from lazyref.pipeline import SearchPipeline
from lazyref.review_session import ReviewSession
from lazyref.retry_analysis import retry_review
from lazyref.search_cache import task_key, cached_rating
from lazyref.wide_search import WideSearchPipeline
from test_core import make_asset
from test_priorities import PLAN
from test_wide_search import Agent, request, manifest, preview_loader, full_loader, candidate_row


def choices():
    rows = draft_choices(PLAN)
    rows[0]["priority"] = "preferred"  # Навіть персонаж може бути побажанням.
    rows[-1]["priority"] = "ignored"  # Не повертати оголеність у запити.
    return rows


def evaluate(criteria, asset, *, missing=(), verify=False):
    client = CodexClient.__new__(CodexClient)
    def reply(prompt, schema, cancel, images=None, **kwargs):
        data = json.loads(prompt.split("Task data: ", 1)[1])
        assert data["criteria"] == criteria
        assert "USER-APPROVED CUSTOM PRIORITIES" in prompt
        row = {"id": asset.candidate.id, "score": 95, "reason": "Видимі ознаки",
            "matches": [], "misses": [], "reference_type": "ordinary",
            "criterion_checks": [{"id": c["id"], "status": "not_met" if c["id"] in missing else "met",
                                  "evidence": "Видно на фото"} for c in criteria]}
        if verify:
            row.update(media="photo", confidence="high")
        return {"images": [row]}
    client.request = reply
    return client.assess("Тестові умови", [asset], Event(), criteria, search_profile="custom",
                         verify_sources=verify)[asset.candidate.id]


@pytest.mark.parametrize("verify", [False, True])
def test_optional_subject_or_scene_never_blocks_close_but_required_mechanics_do(tmp_path, verify):
    active = approved_plan(choices())["criteria"]
    asset = make_asset(tmp_path, "photo")
    optional = evaluate(active, asset, missing=("c1", "c5"), verify=verify)
    assert optional.is_close and optional.coverage == 1 and optional.score == 95
    assert optional.unmet_criteria == [] and len(optional.unmet_preferences) == 2
    partial = evaluate(active, asset, missing=("c2",), verify=verify)
    assert not partial.is_close and partial.score == 74 and partial.eligible
    assert partial.unmet_criteria == [active[1]["label"]]
    assert all(c["priority"] == next(x["priority"] for x in active if x["id"] == c["id"])
               for c in partial.criterion_checks)
    assert "c6" not in {c["id"] for c in partial.criterion_checks}


def test_preferences_rank_equal_required_matches_but_never_replace_required_features(tmp_path):
    rows = choices()
    plan = approved_plan(rows)
    req = request(tmp_path, search_profile="custom", custom_criteria=rows, verify_sources=False)
    a, b = make_asset(tmp_path, "a"), make_asset(tmp_path, "b")
    a.assessment = evaluate(plan["criteria"], a, missing=("c5",))
    b.assessment = replace(evaluate(plan["criteria"], b), score=80)
    _, exported = export_results(req, [a, b], plan, {})
    assert [a.candidate.id for a in exported] == ["b", "a"]
    b.assessment = evaluate(plan["criteria"], b, missing=("c2",))
    _, exported = export_results(req, [b, a], plan, {})
    assert exported[0].candidate.id == "a"


def test_custom_priorities_override_hidden_kind_rules_even_for_partial_matches(tmp_path):
    rows = choices()
    rows[0]["priority"] = "required"
    rows[4]["priority"] = "required"  # Саме барна стійка тепер обов’язкова.
    asset = make_asset(tmp_path, "photo")
    for missed in ("c1", "c2", "c4", "c5"):
        rating = evaluate(approved_plan(rows)["criteria"], asset, missing=(missed,))
        assert rating.eligible and not rating.is_close and rating.score == 74
        assert rating.coverage == 4 / 5


def test_one_planning_call_then_local_approval_and_exact_repeat_without_replanning(tmp_path):
    req = request(tmp_path, search_profile="custom")
    client = Agent()
    rows = choices()
    def editor(draft, cancel):
        assert client.plan_calls == 1 and client.searches == 0
        return rows
    plan = make_plan(client, req, Event(), editor)
    assert req.custom_criteria == rows and plan == approved_plan(rows)
    assert PLAN["queries"][0] not in plan["queries"]
    assert rows[-1]["label"] not in req.search_description
    assert rows[-1]["label"] not in json.dumps(plan, ensure_ascii=False)
    repeated = replace(req, model="gpt-6.1-sol", reuse_cache=False)
    assert make_plan(client, repeated, Event(), lambda *a: pytest.fail("Already approved")) == plan
    assert client.plan_calls == 1
    changed = deepcopy(rows)
    changed[4]["priority"] = "required"
    assert task_key(req) != task_key(replace(req, custom_criteria=changed))


@pytest.mark.parametrize("bad", [None, [], [{"id": "c1"}], [{"id": "c1", "label": "x", "kind": [], "priority": []}]])
def test_invalid_saved_choices_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_choices(bad)


def test_choices_require_at_least_one_mandatory_condition():
    rows = choices()
    for c in rows:
        c["priority"] = "preferred"
    with pytest.raises(ValueError, match="хоча б одну"):
        validate_choices(rows)


def test_custom_planner_accepts_an_object_only_reference(tmp_path):
    client = CodexClient.__new__(CodexClient)
    draft = {**deepcopy(PLAN), "criteria": [{"id": "c1", "kind": "subject", "label": "Черевик", "keywords": ["shoe"]}]}
    def reply(prompt, schema, cancel, **kwargs):
        assert schema is PLAN_SCHEMA and "DRAFT" in prompt and kwargs["images"]
        return draft
    client.request = reply
    plan = client.plan("Черевик збоку", Event(), tmp_path / "guide.png", "custom", "visual")
    assert len(plan["criteria"]) == 1


class CustomAgent(Agent):
    def plan(self, description, cancel, **kwargs):
        assert kwargs["search_profile"] == "custom"
        self.plan_calls += 1
        return deepcopy(PLAN)

    def request(self, prompt, schema, cancel, **kwargs):
        from lazyref.search_agent import SEARCH_SCHEMA
        assert "USER-APPROVED CUSTOM PRIORITIES" in prompt
        data = json.loads(prompt.split("Task data: ", 1)[1])
        assert PLAN["criteria"][-1]["label"] not in data["description"] if "description" in data else True
        assert all(c.get("priority") in {"required", "preferred"} for c in data.get("criteria", []))
        if schema is SEARCH_SCHEMA:
            self.searches += 1
            kwargs["trace"].append({"type": "web_search", "action": "search"})
            return {"image_search_available": True, "queries": ["seated rear"], "strategy": "test",
                    "limitation": "", "candidates": [candidate_row(0)]}
        return super().request(prompt, schema, cancel, **kwargs)

    def assess(self, description, batch, cancel, **kwargs):
        self.assess_calls += 1
        assert kwargs["search_profile"] == "custom"
        if self.assess_calls == self.fail_assess:
            raise TimeoutError("Test timeout")
        return {a.candidate.id: evaluate(kwargs["criteria"], a) for a in batch}


@pytest.mark.parametrize("strategy", ["wide", "legacy"])
def test_approved_conditions_reach_every_stage_and_survive_review_session(tmp_path, strategy):
    req = replace(request(tmp_path, search_profile="custom", verify_sources=False, reuse_cache=False), search_strategy=strategy)
    client = CustomAgent(batch_size=8)
    def editor(draft, cancel):
        assert client.plan_calls == 1 and client.searches == 0
        return choices()
    if strategy == "wide":
        pipeline = WideSearchPipeline(client, full_loader, preview_loader=preview_loader, plan_editor=editor)
    else:
        def loader(candidate, directory, network):
            return preview_loader(candidate, directory, network)[1]
        pipeline = SearchPipeline(client, loader=loader, plan_editor=editor)
    result = pipeline.run(req, Event())
    assert not result.error and result.assets and client.plan_calls == 1
    report = manifest(result)
    assert report["custom_criteria"] == choices()
    assert report["plan"]["criteria"] == approved_plan(choices())["criteria"]
    path = next((tmp_path / "_sessions").glob("*/review.json"))
    saved = ReviewSession.load(path)
    assert saved.request().custom_criteria == choices()
    saved.data["plan"]["criteria"][0]["priority"] = "required"
    saved.save()
    with pytest.raises(ValueError, match="підтвердженим"):
        ReviewSession.load(path)


def test_retry_keeps_priorities_without_replanning_and_corrupt_cache_is_not_accepted(tmp_path):
    req = request(tmp_path, search_profile="custom", verify_sources=False, reuse_cache=False, custom_criteria=choices())
    result = WideSearchPipeline(CustomAgent(fail_assess=2, batch_size=8), full_loader,
                               preview_loader=preview_loader).run(req, Event())
    assert result.error == "Test timeout" and result.pending_review > 0
    client = CustomAgent()
    retried = retry_review(result.retry_path, Event(), client=client)
    assert not retried.error and not retried.pending_review
    assert client.plan_calls == client.searches == 0
    asset = retried.assets[0]
    data = asdict(asset.assessment)
    criteria = approved_plan(choices())["criteria"]
    assert cached_rating(data, criteria, "custom", False)
    data["criterion_checks"][0]["priority"] = "required"
    assert cached_rating(data, criteria, "custom", False) is None


@pytest.mark.parametrize("strategy", ["wide", "legacy"])
def test_cancelling_approval_never_starts_discovery_and_retains_usage_record(tmp_path, strategy):
    req = replace(request(tmp_path, search_profile="custom"), search_strategy=strategy)
    client = CustomAgent()
    pipeline = (WideSearchPipeline(client, plan_editor=lambda *a: None) if strategy == "wide"
                else SearchPipeline(client, plan_editor=lambda *a: None))
    result = pipeline.run(req, Event())
    assert result.cancelled and not result.error and not result.assets
    assert client.plan_calls == 1 and client.searches == client.assess_calls == 0
    assert result.usage_path.is_file()
    assert not list(tmp_path.glob("_sessions/*/review.json"))
    assert not req.custom_criteria
