"""Вибір старих зразків і фрагментів без генерації, від UI до повторного аналізу."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event

from PIL import Image
import pytest
import shiboken6
from PySide6.QtWidgets import QApplication

from lazyref.codex import CodexClient, PLAN_SCHEMA
from lazyref.library import image_record
from lazyref.models import SearchRequest
from lazyref.review_session import ReviewSession
from lazyref.retry_analysis import retry_review
from lazyref.search_cache import task_key
from test_core import make_asset
from test_wide_search import Agent, request, run, manifest


PLAN = {"summary": "Черевик на носку, вид збоку", "focus": "pose",
    "queries": ["shoe tiptoe side view", "boot raised heel side", "shoe on toes profile", "foot in boot tiptoe"],
    "discovery_queries": ["shoe tiptoe", "boot raised heel"],
    "criteria": [
        {"id": "c1", "label": "Черевик", "kind": "subject", "keywords": ["shoe", "boot"]},
        {"id": "c2", "label": "Опора на носок", "kind": "pose", "keywords": ["tiptoe", "raised heel"]},
        {"id": "c3", "label": "Вид збоку", "kind": "view", "keywords": ["profile", "side view"]}]}


def guide_file(tmp_path, size=(80, 64)):
    path = tmp_path / "guide.png"
    Image.new("RGB", size, "green").save(path)
    return path


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.mark.parametrize("profile", ["strict", "pose"])
@pytest.mark.parametrize("verify", [False, True])
def test_visual_planning_and_assessment_use_crop_features_and_correct_attachments(tmp_path, profile, verify):
    guide = guide_file(tmp_path)
    asset = make_asset(tmp_path, "candidate")
    asset.candidate.source_notes = "PRIVATE_SOURCE_NOTE"
    calls = []
    def fake(prompt, schema, cancel, images=None, **kwargs):
        calls.append((prompt, schema, images))
        if schema is PLAN_SCHEMA:
            return deepcopy(PLAN)
        task = json.loads(prompt.split("Task data: ", 1)[1])
        assert task["attachments"][0]["position"] == 2
        row = {"id": asset.candidate.id, "score": 90, "reason": "Збігаються ракурс і опора",
            "matches": ["Опора на носок"], "misses": [], "reference_type": "ordinary",
            "criterion_checks": [{"id": c["id"], "status": "met", "evidence": "Видно на фото"} for c in PLAN["criteria"]]}
        if verify:
            row.update(media="photo", confidence="high")
        return {"images": [row]}
    client = CodexClient.__new__(CodexClient)
    client.request = fake
    plan = client.plan("Черевики в такому ракурсі", Event(), guide, profile, reference_mode="visual")
    result = client.assess("Черевики в такому ракурсі", [asset], Event(), plan["criteria"], guide,
        search_profile=profile, verify_sources=verify, reference_mode="visual")[asset.candidate.id]
    assert result.is_close and result.verify_sources is verify
    assert calls[0][2] == [guide] and calls[1][2] == [guide, asset.preview]
    assert "do not require a whole person" in calls[0][0]
    assert "Do not penalize missing unrelated anatomy" in calls[1][0]
    assert "AI-GENERATED POSE GUIDE" not in calls[0][0] + calls[1][0]
    if not verify:
        assert "PRIVATE_SOURCE_NOTE" not in calls[1][0]


def test_small_fragment_session_roundtrip_and_cache_identity(tmp_path):
    guide = guide_file(tmp_path, size=(32, 40))
    req = SearchRequest("Черевики в такому ракурсі", 5, tmp_path, reference_image=guide, reference_mode="visual")
    req.validate()
    with pytest.raises(ValueError):
        replace(req, reference_mode="pose").validate()
    assert task_key(req) != task_key(replace(req, reference_mode="pose"))
    session = ReviewSession.create(req, PLAN)
    restored = ReviewSession.load(session.path).request()
    assert restored.reference_mode == "visual" and restored.reference_image.read_bytes() == guide.read_bytes()
    assert restored.reference_args == {"reference_image": restored.reference_image, "reference_mode": "visual"}


def test_visual_mode_requires_a_selected_image(tmp_path):
    with pytest.raises(ValueError, match="Обери зображення"):
        SearchRequest("Черевики збоку", 5, tmp_path, reference_mode="visual").validate()


def test_old_sessions_keep_original_pose_mode(tmp_path):
    req = SearchRequest("Original pose", 5, tmp_path, reference_image=guide_file(tmp_path, (200, 200)))
    session = ReviewSession.create(req, PLAN)
    session.data["request"].pop("reference_mode")
    session.save()
    restored = ReviewSession.load(session.path).request()
    assert restored.reference_mode == "pose" and "reference_mode" not in restored.reference_args


def test_wide_pipeline_and_retry_preserve_visual_mode_without_new_discovery(tmp_path):
    class VisualAgent(Agent):
        def plan(self, description, cancel, **kwargs):
            assert kwargs["reference_mode"] == "visual"
            return super().plan(description, cancel, **kwargs)
        def assess(self, description, batch, cancel, **kwargs):
            assert kwargs["reference_mode"] == "visual"
            ratings = super().assess(description, batch, cancel, **kwargs)
            return {key: replace(value, verify_sources=False, media="unverified", confidence="not_checked")
                    for key, value in ratings.items()}
        def request(self, prompt, schema, cancel, **kwargs):
            assert "VISUAL REFERENCE MODE" in prompt
            return super().request(prompt, schema, cancel, **kwargs)
    req = request(tmp_path, reference_image=guide_file(tmp_path), reference_mode="visual",
                  verify_sources=False, reuse_cache=False)
    first = run(req, VisualAgent(fail_assess=2))
    assert first.error == "Test timeout" and first.pending_review > 0
    report = manifest(first)
    assert report["reference_mode"] == "visual"
    client = VisualAgent()
    retried = retry_review(first.retry_path, Event(), client=client)
    assert not retried.error and not retried.pending_review
    assert client.searches == client.plan_calls == 0 and client.assess_calls > 0
    assert all(p is not None for p in client.guide_args)


def test_legacy_pipeline_passes_the_same_fragment_to_plan_search_and_review(tmp_path):
    from lazyref.pipeline import SearchPipeline
    from lazyref.search_agent import SEARCH_SCHEMA
    from test_wide_search import candidate_row
    selected = guide_file(tmp_path)
    class LegacyVisualAgent(Agent):
        def plan(self, description, cancel, **kwargs):
            assert kwargs["reference_mode"] == "visual" and kwargs["reference_image"] == selected
            return super().plan(description, cancel, **kwargs)
        def assess(self, description, batch, cancel, **kwargs):
            assert kwargs["reference_mode"] == "visual" and kwargs["reference_image"] == selected
            return {key: replace(value, verify_sources=False, media="unverified", confidence="not_checked")
                    for key, value in super().assess(description, batch, cancel, **kwargs).items()}
        def request(self, prompt, schema, cancel, **kwargs):
            assert schema is SEARCH_SCHEMA and kwargs["images"] == [selected]
            assert "VISUAL REFERENCE MODE" in prompt and "AI-GENERATED POSE GUIDE" not in prompt
            kwargs["trace"].append({"type": "web_search", "action": "search"})
            return {"image_search_available": True, "queries": ["shoe side view"], "strategy": "test",
                    "limitation": "", "candidates": [candidate_row(0)]}
    def loader(candidate, directory, net):
        asset = make_asset(directory, candidate.id)
        asset.candidate = candidate
        return asset
    req = SearchRequest("Черевики в такому ракурсі", 1, tmp_path, reference_image=selected,
        reference_mode="visual", search_rounds=1, verify_sources=False)
    result = SearchPipeline(LegacyVisualAgent(), loader=loader).run(req, Event())
    assert not result.error and len(result.assets) == 1
    assert manifest(result)["reference_guide"]["kind"] == "visual_reference"


@pytest.mark.parametrize("kind", ["generated", "reference"])
def test_gallery_choice_prepares_then_searches_selected_file_without_generation(qt_app, tmp_path, monkeypatch, kind):
    from lazyref import ui
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    window = ui.MainWindow()
    window.output.setText(str(tmp_path / "results"))
    image = guide_file(tmp_path)
    item = image_record(image, tmp_path, kind, description="Черевики в такому ракурсі", archived=True)
    calls = []
    monkeypatch.setattr(window, "launch_search", calls.append)
    monkeypatch.setattr(window, "start_generation", lambda *args: pytest.fail("Must never regenerate chosen file"))
    window.description.setPlainText("Whole person old prompt")
    window.choose_library_image(item)
    assert not calls
    assert window.provider.currentData() == "image_reference" and window.selected_library_image == item
    assert window.description.toPlainText() == item.description
    window.settings.update(model="gpt-6-astra", reasoning_effort="medium")
    window.count.setValue(5)
    window.start_search()
    assert len(calls) == 1
    req = calls[0]
    assert req.reference_image == image and req.reference_mode == "visual"
    assert (req.model, req.reasoning_effort, req.count) == ("gpt-6-astra", "medium", 5)
    window.last_search_request = req
    window.settings["model"] = "gpt-6.1-sol"
    window.repeat_last_search()
    assert len(calls) == 2 and calls[1].model == "gpt-6.1-sol"
    assert calls[1].reference_image == image and calls[1].reference_mode == "visual"
    assert calls[1].reuse_cache is False
    window.close()
    shiboken6.delete(window)


def test_blank_crop_caption_does_not_reuse_whole_body_prompt(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    window = ui.MainWindow()
    window.description.setPlainText("Чоловік за барною стійкою")
    window.choose_library_image(image_record(guide_file(tmp_path), tmp_path, "generated", cropped=True))
    assert window.description.toPlainText() == ""
    assert window.selected_library_image is not None
    window.close()
    shiboken6.delete(window)
