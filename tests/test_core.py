from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from PIL import Image, ImageDraw

from lazyref.capture import same_image
from lazyref.codex import CodexClient
from lazyref.exporter import export_results
from lazyref.images import DuplicateIndex, prepare
from lazyref.models import Assessment, Asset, Candidate, Cancelled, SearchRequest
from lazyref.network import public_url
from lazyref.pipeline import SearchPipeline


def picture(seed=0, size=(600, 450)):
    image = Image.new("RGB", size, (40 + seed * 13, 80, 120))
    draw = ImageDraw.Draw(image)
    for i in range(12):
        x = (i * 71 + seed * 31) % size[0]
        y = (i * 47 + seed * 67) % size[1]
        draw.ellipse((x, y, x + 30 + seed, y + 60), fill=(170, 30 + seed * 4, 80 + i * 9))
    return image


def make_asset(tmp_path, name="a", seed=0, score=85, medium="photo"):
    buffer = BytesIO()
    picture(seed).save(buffer, "PNG")
    asset = prepare(buffer.getvalue(), Candidate(name, name, page_url="https://example.org/photo"), tmp_path, "download")
    asset.assessment = Assessment(medium, "high", score, "Тестове пояснення", [], [])
    return asset


def test_deduplicate_reencoded_and_resized_image(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    third = tmp_path / "third.jpg"
    picture().save(first, quality=95)
    picture().resize((360, 270)).save(second, quality=68)
    picture(6).save(third)
    index = DuplicateIndex()
    assert index.add(first)
    assert not index.add(second)
    assert index.add(third)


def test_bad_and_tiny_images_are_rejected(tmp_path):
    with pytest.raises(Exception):
        prepare(b"<html>access denied</html>", Candidate("a", "a"), tmp_path, "download")
    buffer = BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, "PNG")
    with pytest.raises(ValueError, match="замале"):
        prepare(buffer.getvalue(), Candidate("a", "a"), tmp_path, "download")


@pytest.mark.parametrize("media,confidence,score,expected", [
    ("photo", "high", 85, True), ("photo", "high", 30, False),
    ("ai_generated", "high", 99, False), ("illustration", "high", 99, False),
    ("photo", "low", 99, False), ("uncertain", "high", 99, False),
    ("photo", "high", 10, False), ("video_frame", "high", 90, True)])
def test_authenticity_filters_are_not_relaxed_for_count(media, confidence, score, expected):
    assert Assessment(media, confidence, score, "", [], []).eligible is expected


def test_export_sorts_filters_caps_and_never_overwrites(tmp_path):
    request = SearchRequest("Людина сидить", 2, tmp_path / "results")
    assets = [make_asset(tmp_path, "partial", 1, 65), make_asset(tmp_path, "generated", 2, 100, "ai_generated"),
              make_asset(tmp_path, "close", 3, 90), make_asset(tmp_path, "extra", 4, 30)]
    first, saved = export_results(request, assets, {}, {})
    second, again = export_results(request, assets, {}, {})
    assert first != second
    assert [a.candidate.id for a in saved] == ["close", "partial"]
    assert len(list(first.glob("*.jpg"))) == 1
    assert len(list((first / "partial").glob("*.jpg"))) == 1
    assert (first / "sources.csv").exists()
    assert (first / "manifest.json").exists()
    assert all(a.path.exists() for a in again)


def test_export_replenishes_failed_copy(tmp_path, monkeypatch):
    import lazyref.exporter as exporter
    assets = [make_asset(tmp_path, "a", 1, 90), make_asset(tmp_path, "b", 2, 80)]
    original = exporter.shutil.copyfile
    def fail_first(src, dst):
        if src == assets[0].path:
            raise OSError("read failed")
        return original(src, dst)
    monkeypatch.setattr(exporter.shutil, "copyfile", fail_first)
    folder, saved = export_results(SearchRequest("A seated person", 1, tmp_path / "out"), assets, {}, {})
    assert [a.candidate.id for a in saved] == ["b"]
    assert len(list(folder.glob("*.jpg"))) == 1


def test_private_and_non_http_urls_rejected():
    for url in ["file:///C:/secret", "http://127.0.0.1/a", "http://[::1]/a", "https://user:pass@example.com/a", "http://192.168.1.1/a"]:
        with pytest.raises(ValueError):
            public_url(url)


def test_screenshot_matches_target_not_unrelated_largest_image():
    assert same_image("https://a.org/photo.jpg?size=1200", "https://a.org/photo.jpg?size=600")
    assert not same_image("https://ads.org/photo.jpg", "https://a.org/photo.jpg")
    assert not same_image("https://a.org/banner.jpg", "https://a.org/photo.jpg")
    assert not same_image("https://a.org/image?id=1", "https://a.org/image?id=2")


def test_missing_or_duplicate_model_ids_fail_closed(tmp_path):
    assets = [make_asset(tmp_path, "a"), make_asset(tmp_path, "b", 3)]
    client = CodexClient.__new__(CodexClient)
    row = {"id": "a", "media": "photo", "confidence": "high", "score": 80,
           "reason": "Фото", "matches": [], "misses": [], "reference_type": "ordinary"}
    client.request = lambda *args: {"images": [row, row]}
    with pytest.raises(RuntimeError, match="зіставити"):
        client.assess("A person", assets, Event())
    client.request = lambda *args: {"images": [row]}
    with pytest.raises(RuntimeError, match="не всі"):
        client.assess("A person", assets, Event())


class FakeClient:
    def __init__(self, fail_second=False, cancel_second=False):
        self.calls = 0
        self.fail_second = fail_second
        self.cancel_second = cancel_second

    def plan(self, description, cancel):
        if cancel.is_set():
            raise Cancelled()
        return {"summary": description, "queries": ["sitting"], "criteria": []}

    def assess(self, description, batch, cancel):
        self.calls += 1
        if self.calls == 2 and self.fail_second:
            raise RuntimeError("test cloud failure")
        if self.calls == 2 and self.cancel_second:
            cancel.set()
            raise Cancelled()
        return {a.candidate.id: Assessment("photo", "high", 70, "Test", [], []) for a in batch}


@pytest.mark.parametrize("mode", ["normal", "cancel", "failure"])
def test_pipeline_saves_only_completed_assessments(tmp_path, mode):
    client = FakeClient(fail_second=mode == "failure", cancel_second=mode == "cancel")
    candidates = [Candidate(str(i), str(i)) for i in range(8)]
    def loader(candidate, directory, network):
        return make_asset(directory, candidate.id, int(candidate.id))
    pipeline = SearchPipeline(client, lambda *args: (candidates, []), loader)
    request = SearchRequest("Людина сидить", 8, tmp_path / "out", candidate_limit=8)
    result = pipeline.run(request, Event())
    assert len(result.assets) == (8 if mode == "normal" else 6)
    assert result.examined == (8 if mode == "normal" else 6)
    assert result.cancelled == (mode == "cancel")
    assert bool(result.error) == (mode == "failure")
    assert all(a.path.exists() for a in result.assets)


def test_cancel_before_any_inference(tmp_path):
    event = Event()
    event.set()
    result = SearchPipeline(FakeClient()).run(SearchRequest("A person", 1, tmp_path), event)
    assert result.cancelled and not result.assets and result.folder is None


def test_unavailable_links_are_exported_without_fake_photo_count(tmp_path):
    import json
    client = FakeClient()
    def finder(request, queries, network, status):
        network.access_links.append({"title": "=test", "url": "https://www.deviantart.com/artist/art/study",
            "source": "DeviantArt", "reason": "Unavailable image"})
        return [], []
    result = SearchPipeline(client, finder).run(SearchRequest("Standing figure", 2, tmp_path,
        artistic_nudity=True), Event())
    assert not result.error and not result.assets and result.examined == 0 and client.calls == 0
    assert len(result.access_links) == 1 and result.folder
    assert not list(result.folder.glob("*.jpg"))
    assert "'=test" in (result.folder / "access_links.csv").read_text(encoding="utf-8-sig")
    manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["saved"] == 0 and manifest["artistic_nudity"] is True
    assert len(manifest["statistics"]["access_links"]) == 1


@pytest.mark.parametrize("gate_at", ["download", "screenshot"])
def test_pipeline_skips_captcha_and_saves_next_source(tmp_path, monkeypatch, gate_at):
    candidates = [Candidate("blocked1", "Blocked", page_url="https://blocked.example/1"),
                  Candidate("blocked2", "Also blocked", page_url="https://blocked.example/2"),
                  Candidate("available", "Available", page_url="https://open.example/photo")]
    downloads = []
    screenshots = []

    def loader(candidate, directory, network):
        downloads.append(candidate.id)
        if candidate.id == "blocked1":
            if gate_at == "download":
                network.access.block(candidate.page_url, "CAPTCHA")
            raise ValueError("Direct download unavailable")
        asset = make_asset(directory, candidate.id)
        asset.candidate = candidate
        return asset

    def capture(browser, candidate, deadline=None):
        screenshots.append(candidate.id)
        browser.access.block(candidate.page_url, "CAPTCHA")

    monkeypatch.setattr("lazyref.pipeline.BrowserCapture.capture", capture)
    result = SearchPipeline(FakeClient(), lambda *args: (candidates, []), loader).run(
        SearchRequest("A person", 2, tmp_path / "out", screenshots=True), Event())
    assert downloads == ["blocked1", "available"]
    assert screenshots == ([] if gate_at == "download" else ["blocked1"])
    assert not result.error
    assert [a.candidate.id for a in result.assets] == ["available"]
    assert len(list(result.folder.rglob("*.jpg"))) == 1
    assert sum("CAPTCHA" in item for item in result.warnings) == 1

