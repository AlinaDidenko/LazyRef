"""Перевірка передавання рівня до всіх викликів без мережі та витрат Codex."""

import json
from threading import Event

import pytest

from lazyref.generation import GenerationRequest, ReferenceGenerator
from lazyref.models import SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.review_session import ReviewSession
from lazyref.retry_analysis import retry_review
from test_core import make_asset
from test_search_quality import PLAN


@pytest.mark.parametrize("kind", ["legacy", "wide", "generation", "review"])
@pytest.mark.parametrize("effort", [None, "xhigh"])
def test_every_workflow_constructs_client_with_explicit_effort(tmp_path, monkeypatch, kind, effort):
    chosen = effort or "medium"
    calls = []
    class Client:
        def stop(self, *args, **kwargs):
            raise RuntimeError("Configuration checked; no cloud call")
        plan = request = assess = stop
    def factory(executable, model, **kwargs):
        calls.append((executable, model, kwargs))
        return Client()
    module = {"legacy": "pipeline", "wide": "wide_search", "generation": "generation", "review": "retry_analysis"}[kind]
    monkeypatch.setattr(f"lazyref.{module}.CodexClient", factory)
    options = {"reasoning_effort": effort} if effort else {}
    if kind == "generation":
        request = GenerationRequest("A seated person", 1, tmp_path / "results", model="test-model", **options)
        result = ReferenceGenerator(generated_root=tmp_path / "native").run(request, Event())
        report = result.folder / "generation.json"
    else:
        request = SearchRequest("A seated person", 1, tmp_path / "results", model="test-model",
            search_strategy="wide" if kind == "wide" else "legacy", reuse_cache=False, **options)
        if kind == "review":
            session = ReviewSession.create(request, PLAN)
            session.add(make_asset(tmp_path), 1)
            result = retry_review(session.path, Event())
            report = result.folder / "manifest.json"
        else:
            result = SearchPipeline().run(request, Event())
            # Навмисно зупинений план не створює добірки результатів.
            assert result.folder is None
            report = None
    assert result.error == "Configuration checked; no cloud call"
    assert calls == [("", "test-model", {"reasoning_effort": chosen})]
    if report is not None:
        assert json.loads(report.read_text(encoding="utf-8"))["requested_reasoning_effort"] == chosen


def test_saved_review_keeps_effort_and_can_override_without_rewriting_original(tmp_path, monkeypatch):
    request = SearchRequest("A seated person", 1, tmp_path / "results", reasoning_effort="high")
    session = ReviewSession.create(request, PLAN)
    session.add(make_asset(tmp_path), 1)
    assert ReviewSession.load(session.path).request().reasoning_effort == "high"
    assert session.request(reasoning_effort="low").reasoning_effort == "low"
    calls = []
    class Client:
        def assess(self, *args, **kwargs):
            raise RuntimeError("Stopped before cloud call")
    def factory(executable, model, **kwargs):
        calls.append(kwargs)
        return Client()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", factory)
    result = retry_review(session.path, Event(), reasoning_effort="low")
    assert calls == [{"reasoning_effort": "low"}]
    assert result.pending_review == 1
    assert ReviewSession.load(session.path).request().reasoning_effort == "high"
    assert json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))["requested_reasoning_effort"] == "low"
    # Старий файл без цього поля отримує явний Medium, а не налаштування Codex.
    session.data["request"].pop("reasoning_effort")
    session.save()
    assert ReviewSession.load(session.path).request().reasoning_effort == "medium"


@pytest.mark.parametrize("effort", ["", "extra high", "unknown", None, ["medium"]])
@pytest.mark.parametrize("request_type", [SearchRequest, GenerationRequest])
def test_invalid_effort_is_rejected_before_work(tmp_path, effort, request_type):
    with pytest.raises(ValueError, match="рівень міркування"):
        request_type("A seated person", 1, tmp_path, reasoning_effort=effort).validate()
