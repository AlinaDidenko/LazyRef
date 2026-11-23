"""Пошук через інструмент Codex, зворотний зв'язок і відсів позначених генерацій."""

from io import BytesIO
import json
from threading import Event

import pytest
from PIL import PngImagePlugin

from lazyref.authenticity import GeneratedImage
from lazyref.images import download, prepare
from lazyref.models import Candidate, Cancelled, SearchRequest
from lazyref.network import Network
from lazyref.pipeline import SearchPipeline
from lazyref.search_agent import discover, remote_url
from test_core import make_asset, picture
from test_search_quality import PLAN, rating


def row(i=0, **overrides):
    return {"title": f"man at bar {i}", "image_url": f"https://images.example.org/{i}.jpg",
            "page_url": f"https://example.org/photos/{i}", "thumbnail_url": "", "description": "rear view",
            "creator": "", "source_notes": "Немає підтвердження походження", "source_kind": "unknown",
            "source_checked": False, **overrides}


class Agent:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.searches, self.assessments, self.feedback = 0, 0, []

    def plan(self, *args):
        return PLAN

    def request(self, prompt, schema, cancel, *, web_search, status, trace):
        assert web_search is True
        self.searches += 1
        data = json.loads(prompt.split("Task data: ")[1])
        self.feedback.append(data["feedback"])
        if self.mode != "no_tool":
            trace.append({"type": "web_search", "action": "search", "query": f"new query {self.searches}"})
        if self.mode == "unavailable":
            return {"image_search_available": False, "limitation": "Only text search", "candidates": []}
        start = (self.searches - 1) * 6
        rows = [row(i) for i in range(start, start + 6)]
        if self.mode == "empty": rows = []
        if self.mode == "generated":
            rows = [row(start, source_kind="generated_label", source_notes="AI-generated image")]
        return {"image_search_available": True, "queries": [f"new query {self.searches}"],
                "strategy": "Шукаю інший ракурс", "limitation": "", "candidates": rows}

    def assess(self, description, batch, cancel, criteria):
        self.assessments += 1
        if self.mode == "cancel" and self.assessments == 2:
            cancel.set()
            raise Cancelled()
        return {a.candidate.id: rating(85) if self.searches > 1 and self.mode == "normal"
                else rating(70, failed=("c3",)) for a in batch}


def loader(candidate, directory, network):
    seed = int(candidate.title.rsplit(" ", 1)[-1])
    asset = make_asset(directory, candidate.id, seed)
    asset.candidate = candidate
    return asset


def test_agent_uses_visual_feedback_and_stops_when_enough_close(tmp_path, monkeypatch):
    client = Agent()
    result = SearchPipeline(client, loader=loader).run(
        SearchRequest("man at bar rear view", 2, tmp_path, candidate_limit=24), Event())
    assert not result.error and client.searches == 2
    assert client.feedback[1]["missing_conditions"] == {"Барна стійка": 6}
    assert client.feedback[1]["previous_queries"] == ["new query 1"]
    assert len(client.feedback[1]["excluded_image_urls"]) == 6
    assert len(result.assets) == 2 and all(a.assessment.is_close for a in result.assets)
    report = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["statistics"]["search_mode"] == "codex_image_agent"
    assert report["statistics"]["stop_reason"] == "enough_close_matches"
    assert len(report["statistics"]["retrieval"]) == 2


@pytest.mark.parametrize("mode,expected", [("no_tool", "не виконав пошук"), ("unavailable", "недоступний")])
def test_agent_never_silently_falls_back_or_accepts_unsearched_urls(tmp_path, mode, expected):
    client = Agent(mode)
    result = SearchPipeline(client, loader=loader).run(SearchRequest("man at bar", 2, tmp_path), Event())
    assert expected in result.error and not result.assets
    assert client.assessments == 0
    report = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["statistics"]["retrieval"][0]["status"] == "interrupted"


@pytest.mark.parametrize("mode,limit,rounds,examined,stop", [
    ("partial", 8, 6, 8, "assessment_limit"),
    ("partial", 72, 1, 6, "agent_round_limit"),
    ("empty", 72, 6, 0, "no_new_usable_candidates"),
    ("generated", 72, 6, 0, "no_new_usable_candidates"),
    ("cancel", 24, 6, 6, "cancelled"),
])
def test_agent_limits_cancellation_and_generated_sources(tmp_path, mode, limit, rounds, examined, stop):
    client = Agent(mode)
    result = SearchPipeline(client, loader=loader).run(
        SearchRequest("man at bar", 2, tmp_path, candidate_limit=limit, search_rounds=rounds), Event())
    assert not result.error and result.examined == examined
    report = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["statistics"]["stop_reason"] == stop
    if mode == "generated":
        assert client.assessments == 0 and len(report["statistics"]["precheck_rejections"]) == 2
    if mode == "cancel":
        assert result.cancelled and len(result.assets) == 2


def test_discovery_validates_urls_deduplicates_and_checks_source_evidence(tmp_path):
    class Client:
        def request(self, *args, **kwargs):
            kwargs["trace"].extend([{"type": "web_search", "action": "search", "query": "bar"},
                {"type": "web_search", "action": "openPage", "url": "https://example.org/photos/1"}])
            return {"image_search_available": True, "queries": ["bar"], "strategy": "test", "limitation": "",
                "candidates": [row(1, source_checked=True), row(1), row(2, image_url="file:///secret"),
                    row(3, image_url="http://127.0.0.1/image"), row(4, source_checked=True),
                    row(5, page_url="https://blocked.example/photo")]}
    net = Network(Event())
    net.access.blocked["blocked.example"] = "CAPTCHA"
    try:
        candidates, report = discover(Client(), SearchRequest("man at bar", 2, tmp_path), PLAN,
                                      [], [], {}, net, Event(), lambda s: None)
        assert len(candidates) == 2 and report["discarded"] == 3
        assert candidates[0].source_checked and not candidates[1].source_checked
        assert len(candidates[0].id) == 20 and candidates[0].provider == "agent"
    finally:
        net.close()


@pytest.mark.parametrize("url", ["http://localhost/x", "https://u:p@example.org/x", "https://foo.local/x",
    "http://[::1]/x", "ftp://example.org/x", "https://example.org:8888/x", "http://10.2.3.4/x"])
def test_search_urls_cannot_point_at_local_files_or_services(url):
    with pytest.raises(ValueError): remote_url(url)


def test_agent_reported_captcha_persists_and_excludes_candidates(tmp_path):
    class Client:
        def request(self, *args, **kwargs):
            kwargs["trace"].append({"type": "web_search", "action": "search", "query": "bar"})
            return {"image_search_available": True, "queries": ["bar"], "candidates": [row()],
                    "blocked_sources": [{"url": "https://example.org/gate", "reason": "captcha"}]}
    net = Network(Event())
    try:
        candidates, report = discover(Client(), SearchRequest("man at bar", 2, tmp_path), PLAN,
                                      [], [], {}, net, Event(), lambda s: None)
        assert not candidates and net.access.blocked["example.org"] == "CAPTCHA"
        assert report["blocked_sources"] == [{"url": "https://example.org/gate", "reason": "CAPTCHA"}]
    finally:
        net.close()


def encoded_metadata(key, value):
    buffer = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    picture().save(buffer, "PNG", pnginfo=info)
    return buffer.getvalue()


@pytest.mark.parametrize("key,value", [
    ("parameters", "a man, Steps: 30, Sampler: Euler, CFG scale: 7"),
    ("prompt", '{"3":{"class_type":"KSampler","inputs":{}}}'),
    ("Software", "Stable Diffusion"),
])
def test_explicit_generation_metadata_is_rejected_before_jpeg_conversion(tmp_path, key, value):
    with pytest.raises(GeneratedImage):
        prepare(encoded_metadata(key, value), Candidate("ai", "AI"), tmp_path, "download")
    assert not list(tmp_path.glob("*.jpg"))


def test_photo_editor_or_freeform_prompt_is_not_proof_of_generation(tmp_path):
    for i, (key, value) in enumerate([("Software", "Adobe Photoshop"), ("prompt", "a photographer's notes")]):
        assert prepare(encoded_metadata(key, value), Candidate(str(i), "photo"), tmp_path, "download").path.exists()


def test_generated_file_does_not_fall_back_to_thumbnail_or_screenshot(tmp_path, monkeypatch):
    calls = []
    data = encoded_metadata("parameters", "Steps: 30, Sampler: Euler")
    candidate = Candidate("ai", "AI", "https://example.org/a.png", "https://example.org/page",
                          "https://example.org/thumb.jpg")
    net = Network(Event())
    net.get = lambda url, **kw: calls.append(url) or data
    with pytest.raises(GeneratedImage): download(candidate, tmp_path, net)
    assert calls == [candidate.image_url]
    monkeypatch.setattr("lazyref.pipeline.BrowserCapture.capture", lambda *a: pytest.fail("No screenshot fallback"))
    def ai_loader(*args): raise GeneratedImage("Generator metadata")
    result = SearchPipeline(Agent(), finder=lambda *a: ([candidate], []), loader=ai_loader).run(
        SearchRequest("man at bar", 2, tmp_path / "out"), Event())
    assert not result.assets and not result.error and result.rejected == 1
    net.close()
