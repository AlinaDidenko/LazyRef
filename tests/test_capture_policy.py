"""Fallback screenshots stay optional and never replace a successful download."""

from threading import Event

import pytest

from lazyref.authenticity import GeneratedImage
from lazyref.images import prepare
from lazyref.models import Candidate, Cancelled, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.wide_search import WideSearchPipeline
from test_wide_search import Agent, photo_bytes


def run_policy(tmp_path, monkeypatch, strategy, mode, *, screenshots=True, verify_sources=True, page_url=True):
    candidates = [Candidate(str(i), f"Photo {i}", image_url=f"https://images.example.org/{i}.jpg",
                            thumbnail_url=f"https://images.example.org/thumb/{i}.jpg",
                            page_url=f"https://example.org/photo/{i}" if page_url else "") for i in range(8)]
    events = []
    cancel = Event()

    def loader(candidate, directory, network):
        events.append(("download", candidate.id))
        if mode == "captcha":
            network.access.block(candidate.page_url, "CAPTCHA")
        if mode == "generated":
            raise GeneratedImage("Synthetic generator metadata")
        if mode == "cancel":
            cancel.set()
            raise Cancelled()
        if mode != "success" and not (mode == "timeout_then_download" and candidate.id == "3"):
            raise OSError("Synthetic direct download failure")
        return prepare(photo_bytes(int(candidate.id)), candidate, directory, "download")

    def capture(browser, candidate, deadline=None):
        events.append(("capture", candidate.id))
        if mode in {"timeout", "timeout_then_download"}:
            raise TimeoutError("Synthetic complete-page deadline")
        return photo_bytes(int(candidate.id))

    monkeypatch.setattr("lazyref.capture.BrowserCapture.capture", capture)
    request = SearchRequest("Man seated at a bar seen from behind", 1, tmp_path / "results",
                            screenshots=screenshots, verify_sources=verify_sources,
                            search_strategy=strategy, search_rounds=1, reuse_cache=False,
                            discovery_limit=20, detail_limit=8)
    client = Agent()
    if strategy == "legacy":
        pipeline = SearchPipeline(client, finder=lambda *args: (candidates, []), loader=loader)
    else:
        def discover(*args):
            return candidates, {"queries": ["fixture"], "strategy": "fixture", "limitation": ""}

        def thumbnail(candidate, directory, network):
            asset = prepare(photo_bytes(int(candidate.id)), candidate, directory, "screening_thumbnail")
            return asset, None  # The preview is not a cached full-resolution download.

        monkeypatch.setattr("lazyref.wide_search.discover_wide", discover)
        pipeline = WideSearchPipeline(client, loader, preview_loader=thumbnail)
    return pipeline.run(request, cancel), events


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_successful_direct_download_does_not_start_screenshot(tmp_path, monkeypatch, strategy):
    result, events = run_policy(tmp_path, monkeypatch, strategy, "success")
    assert result.assets and not result.error
    assert any(kind == "download" for kind, _ in events)
    assert not any(kind == "capture" for kind, _ in events)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_screenshot_only_runs_after_that_candidates_download_failed(tmp_path, monkeypatch, strategy):
    result, events = run_policy(tmp_path, monkeypatch, strategy, "failure")
    assert result.assets and not result.error
    captures = [index for index, event in enumerate(events) if event[0] == "capture"]
    assert captures
    for index in captures:
        assert events[index - 1] == ("download", events[index][1])


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("mode", ["captcha", "generated", "cancel"])
def test_access_generation_and_cancellation_never_trigger_fallback(tmp_path, monkeypatch, strategy, mode):
    result, events = run_policy(tmp_path, monkeypatch, strategy, mode)
    assert not result.assets
    assert not any(kind == "capture" for kind, _ in events)
    assert result.cancelled is (mode == "cancel")


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("options", [{"screenshots": False}, {"verify_sources": False}, {"page_url": False}])
def test_fallback_requires_enabled_option_source_checks_and_known_page(tmp_path, monkeypatch, strategy, options):
    result, events = run_policy(tmp_path, monkeypatch, strategy, "failure", **options)
    assert not result.assets
    assert not any(kind == "capture" for kind, _ in events)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_deadline_exhaustion_moves_on_and_screenshot_attempts_are_capped(tmp_path, monkeypatch, strategy):
    result, events = run_policy(tmp_path, monkeypatch, strategy, "timeout_then_download")
    assert result.assets and not result.error
    assert result.assets[0].candidate.id == "3"
    assert [ident for kind, ident in events if kind == "capture"] == ["0", "1", "2"]
    assert ("download", "3") in events
