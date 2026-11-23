"""Candidate deadlines and cancellation without sockets, browsers or models."""

from threading import Event

import pytest

from lazyref.download_budget import DownloadBudget
from lazyref.images import download, prepare, screening_preview
from lazyref.models import Candidate, Cancelled, SearchRequest
from lazyref.network import Network
from lazyref.pipeline import SearchPipeline
from lazyref.wide_search import WideSearchPipeline
from test_wide_search import Agent, SCREEN_SCHEMA, photo_bytes


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("lazyref.download_budget.DownloadBudget", lambda: DownloadBudget(clock))
    return clock


def candidate(number):
    return Candidate(str(number), f"Photo {number}", image_url=f"https://img.example.org/{number}.jpg",
                     thumbnail_url=f"https://img.example.org/thumb/{number}.jpg",
                     page_url=f"https://example.org/photo/{number}")


@pytest.mark.parametrize("loader", [download, screening_preview])
def test_alternative_urls_share_budget_and_next_candidate_starts_fresh(tmp_path, clock, loader):
    network = Network(Event())
    observations = []

    def get(url, **kwargs):
        with network.use_budget() as budget:
            observations.append((url, budget, budget.check_direct()))
            if len(observations) == 1:
                clock.advance(44)
                raise OSError("Original URL unavailable")
            return photo_bytes(1)

    network.get = get
    try:
        loader(candidate(0), tmp_path, network)
        loader(candidate(1), tmp_path, network)
    finally:
        network.close()
    assert len(observations) == 3
    assert observations[0][1] is observations[1][1]
    assert observations[2][1] is not observations[1][1]
    assert [row[2] for row in observations] == [45, 1, 45]


@pytest.mark.parametrize("loader", [download, screening_preview])
def test_image_loader_reuses_outer_candidate_budget(tmp_path, clock, loader):
    network = Network(Event())
    outer = DownloadBudget(clock)
    clock.advance(40)

    def get(url, **kwargs):
        with network.use_budget() as budget:
            assert budget is outer
            assert budget.check_direct() == 5
        return photo_bytes(0)

    network.get = get
    try:
        with network.use_budget(outer):
            loader(candidate(0), tmp_path, network)
    finally:
        network.close()


@pytest.mark.parametrize("loader", [download, screening_preview])
def test_download_timeout_does_not_start_an_alternative_url(tmp_path, clock, loader):
    network = Network(Event())
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        clock.advance(45)
        with network.use_budget() as budget:
            budget.check_direct()

    network.get = get
    try:
        with pytest.raises(TimeoutError):
            loader(candidate(0), tmp_path, network)
    finally:
        network.close()
    assert len(calls) == 1


@pytest.mark.parametrize("loader", [download, screening_preview])
def test_cancel_after_response_does_not_return_an_image(tmp_path, clock, loader):
    network = Network(Event())

    def get(url, **kwargs):
        network.cancel.set()
        return photo_bytes(0)

    network.get = get
    try:
        with pytest.raises(Cancelled):
            loader(candidate(0), tmp_path, network)
    finally:
        network.close()


def run_pipeline(tmp_path, monkeypatch, strategy, loader, *, preview=None, client=None, count=2):
    candidates = [candidate(i) for i in range(count)]
    request = SearchRequest("Man seated at a bar seen from behind", 1, tmp_path / "results",
                            search_strategy=strategy, search_rounds=1, reuse_cache=False,
                            discovery_limit=20, detail_limit=8)
    client = client or Agent()
    if strategy == "legacy":
        pipeline = SearchPipeline(client, finder=lambda *args: (candidates, []), loader=loader)
    else:
        monkeypatch.setattr("lazyref.wide_search.discover_wide", lambda *args:
                            (candidates, {"queries": ["fixture"], "strategy": "fixture", "limitation": ""}))

        def thumbnail(c, directory, network):
            return prepare(photo_bytes(int(c.id)), c, directory, "screening_thumbnail"), None

        pipeline = WideSearchPipeline(client, loader, preview_loader=preview or thumbnail)
    return pipeline.run(request, Event())


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_fallback_shares_candidate_deadline_and_next_candidate_is_fresh(tmp_path, monkeypatch, clock, strategy):
    observations, captures = [], []

    def loader(c, directory, network):
        with network.use_budget() as budget:
            observations.append((c.id, budget, budget.check_direct()))
            if c.id == "0":
                clock.advance(45)
                budget.check_direct()
            return prepare(photo_bytes(int(c.id)), c, directory, "download")

    def capture(self, c, *, deadline):
        captures.append((c.id, deadline, clock()))
        clock.advance(20)
        raise TimeoutError("Synthetic browser timeout")

    monkeypatch.setattr("lazyref.capture.BrowserCapture.capture", capture)
    result = run_pipeline(tmp_path, monkeypatch, strategy, loader)
    assert not result.error and [a.candidate.id for a in result.assets] == ["1"]
    assert captures == [("0", 100, 45)]
    assert [row[2] for row in observations] == [45, 45]
    assert observations[0][1].total_deadline == 100
    assert observations[1][1].total_deadline == 165


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_exhausted_combined_deadline_skips_browser_and_continues(tmp_path, monkeypatch, clock, strategy):
    def loader(c, directory, network):
        if c.id == "0":
            clock.advance(100)
            raise TimeoutError("Synthetic late cleanup")
        with network.use_budget() as budget:
            assert budget.check_direct() == 45
        return prepare(photo_bytes(1), c, directory, "download")

    monkeypatch.setattr("lazyref.capture.BrowserCapture.capture",
                        lambda *args, **kwargs: pytest.fail("Exhausted candidate must not start a browser"))
    result = run_pipeline(tmp_path, monkeypatch, strategy, loader)
    assert not result.error and [a.candidate.id for a in result.assets] == ["1"]


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_cancellation_accompanying_transport_error_never_starts_browser(tmp_path, monkeypatch, clock, strategy):
    def loader(c, directory, network):
        network.cancel.set()
        raise OSError("Connection interrupted")

    monkeypatch.setattr("lazyref.capture.BrowserCapture.capture",
                        lambda *args, **kwargs: pytest.fail("Cancellation must not start a browser"))
    result = run_pipeline(tmp_path, monkeypatch, strategy, loader)
    assert result.cancelled and not result.assets


def test_preview_timeout_skips_candidate_without_stopping_wide_search(tmp_path, monkeypatch, clock):
    previews = []

    def preview(c, directory, network):
        with network.use_budget() as budget:
            previews.append((c.id, budget.check_direct()))
            if c.id == "0":
                clock.advance(45)
                budget.check_direct()
        return prepare(photo_bytes(1), c, directory, "screening_thumbnail"), None

    def loader(c, directory, network):
        return prepare(photo_bytes(int(c.id)), c, directory, "download")

    result = run_pipeline(tmp_path, monkeypatch, "wide", loader, preview=preview)
    assert not result.error and [a.candidate.id for a in result.assets] == ["1"]
    assert previews == [("0", 45), ("1", 45)]
    assert any("Photo 0" in warning and "budget exhausted" in warning for warning in result.warnings)


@pytest.mark.parametrize("candidate_count", [2, 12])
def test_model_screening_timeout_remains_visible(tmp_path, monkeypatch, clock, candidate_count):
    class TimeoutAgent(Agent):
        def request(self, prompt, schema, cancel, **kwargs):
            if schema is SCREEN_SCHEMA:
                raise TimeoutError("Synthetic model timeout")
            return super().request(prompt, schema, cancel, **kwargs)

    result = run_pipeline(tmp_path, monkeypatch, "wide",
                          lambda *args: pytest.fail("No full downloads after model timeout"),
                          client=TimeoutAgent(), count=candidate_count)
    assert result.error == "Synthetic model timeout"
    assert result.examined == 0 and not result.assets


def test_wide_model_wait_is_not_charged_to_later_full_download(tmp_path, monkeypatch, clock):
    starts = []

    def preview(c, directory, network):
        with network.use_budget() as budget:
            starts.append(("preview", c.id, budget.started_at))
        clock.advance(40)
        return prepare(photo_bytes(int(c.id)), c, directory, "screening_thumbnail"), None

    class SlowModel(Agent):
        def request(self, prompt, schema, cancel, **kwargs):
            if schema is SCREEN_SCHEMA:
                clock.advance(300)
            return super().request(prompt, schema, cancel, **kwargs)

    def loader(c, directory, network):
        with network.use_budget() as budget:
            starts.append(("full", c.id, budget.started_at))
            assert budget.check_direct() == 45
        return prepare(photo_bytes(int(c.id)), c, directory, "download")

    result = run_pipeline(tmp_path, monkeypatch, "wide", loader, preview=preview, client=SlowModel())
    assert result.assets and not result.error
    assert starts == [("preview", "0", 0), ("preview", "1", 40), ("full", "0", 380), ("full", "1", 380)]
