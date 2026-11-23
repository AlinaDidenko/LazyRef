"""Cleanup failures must not replace photos and resumable search results."""

import json
from dataclasses import replace
from threading import Event

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication

from lazyref import pipeline as legacy_module, ui, wide_search as wide_module
from lazyref.models import Candidate, Cancelled, SearchRequest
from lazyref.retry_analysis import retry_review
from lazyref.review_session import ReviewSession
from test_core import make_asset
from test_wide_search import Agent, full_loader, preview_loader, request as wide_request


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def workflow(strategy, output, client=None, *, count=2):
    client = client or Agent()
    if strategy == "legacy":
        candidates = [Candidate(str(i), f"Photo {i}", page_url=f"https://photo.invalid/{i}")
                      for i in range(count)]
        pipeline = legacy_module.SearchPipeline(client, finder=lambda *args: (candidates, []),
            loader=lambda candidate, directory, network: make_asset(directory, candidate.id, int(candidate.id)))
        request = SearchRequest("Man seated at a bar seen from behind", count, output,
            candidate_limit=max(8, count), model="fixture-model", reuse_cache=False)
    else:
        pipeline = wide_module.WideSearchPipeline(client, full_loader, preview_loader=preview_loader)
        request = replace(wide_request(output, reuse_cache=False, model="fixture-model"), count=count)
    return pipeline, request, client


def failing_cleanup(monkeypatch, strategy, failures):
    """Only local objects are used; close their real handles before reporting failure."""
    module = legacy_module if strategy == "legacy" else wide_module
    events = []

    def finished(name):
        events.append(name)
        if name in failures:
            error = OSError(f"synthetic {name} cleanup failure")
            if name == "capture":
                error.add_note("synthetic retained browser recovery data")
            raise error

    class Network(module.Network):
        def close(self):
            super().close()
            finished("network")

    class Capture(module.BrowserCapture):
        def close(self):
            super().close()
            finished("capture")

    class Lock(module.ReviewLock):
        def __exit__(self, *args):
            super().__exit__(*args)
            finished("lock")

    monkeypatch.setattr(module, "Network", Network)
    monkeypatch.setattr(module, "BrowserCapture", Capture)
    monkeypatch.setattr(module, "ReviewLock", Lock)
    if strategy == "wide":
        class Cache(module.SearchCache):
            def close(self):
                super().close()
                finished("cache")
        monkeypatch.setattr(module, "SearchCache", Cache)
    return events


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("failures", [{"capture"}, {"network"}, {"capture", "network"}],
                         ids=["browser", "network", "both"])
def test_completed_search_reaches_worker_and_ui_despite_cleanup_failure(
        qt_app, tmp_path, monkeypatch, strategy, failures):
    events = failing_cleanup(monkeypatch, strategy, failures)
    pipeline, request, client = workflow(strategy, tmp_path / "results")
    monkeypatch.setattr(ui, "SearchPipeline", lambda *args, **kwargs: pipeline)
    window = ui.MainWindow()
    window.save_settings = lambda: None
    worker = ui.SearchWorker(request)
    received = []
    worker.completed.connect(received.append)
    worker.completed.connect(window.show_results)
    try:
        worker.run()  # Exercise the real exception handler and completed signal.
        assert len(received) == 1
        result = received[0]
        assert result.folder and len(result.assets) == request.count
        assert all(asset.path.is_file() for asset in result.assets)
        assert len(window.gallery.findChildren(ui.ImageCard)) == request.count
        assert window.last_folder == result.folder
        manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
        stats = manifest["statistics"]
        assert result.examined == stats["examined"] > 0
        assert result.duplicates == stats["duplicates"] and result.rejected == stats["rejected"]
        assert result.usage == stats["usage"]
        assert result.usage_path == result.folder / "usage.json"
        assert result.usage_path.is_file() and not result.cancelled
        assert not result.pending_review and not result.export_pending and result.retry_path is None
        for name in failures:
            assert f"synthetic {name} cleanup failure" in result.error
        assert events == (["lock", "cache"] if strategy == "wide" else ["lock"]) + ["capture", "network"]
    finally:
        window.close()
        shiboken6.delete(window)
        shiboken6.delete(worker)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_every_cleanup_is_attempted_when_all_of_them_fail(tmp_path, monkeypatch, strategy):
    failures = {"lock", "capture", "network"} | ({"cache"} if strategy == "wide" else set())
    events = failing_cleanup(monkeypatch, strategy, failures)
    pipeline, request, client = workflow(strategy, tmp_path / "results")

    result = pipeline.run(request, Event())

    assert result.folder and len(result.assets) == request.count
    assert set(events) == failures and len(events) == len(failures)
    for name in failures:
        assert f"synthetic {name} cleanup failure" in result.error
    # Secondary cleanup detail must not be lost when the exception is rendered.
    assert "synthetic retained browser recovery data" in result.error


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("stop", ["error", "cancel"])
def test_cleanup_retains_original_failure_and_unfinished_review(tmp_path, monkeypatch, strategy, stop):
    class StoppingAgent(Agent):
        def assess(self, description, batch, cancel, **kwargs):
            if stop == "cancel" and self.assess_calls == 1:
                cancel.set()
                raise Cancelled()
            return super().assess(description, batch, cancel, **kwargs)

    client = StoppingAgent(fail_assess=2 if stop == "error" else 0)
    failing_cleanup(monkeypatch, strategy, {"capture", "network"})
    pipeline, request, client = workflow(strategy, tmp_path / "results", client, count=8)
    result = pipeline.run(request, Event())

    assert result.folder and result.assets and result.examined > 0
    assert result.cancelled is (stop == "cancel")
    if stop == "error":
        assert "Test timeout" in result.error
    assert "synthetic capture cleanup failure" in result.error
    assert "synthetic network cleanup failure" in result.error
    assert result.retry_path and result.pending_review > 0
    saved = ReviewSession.load(result.retry_path)
    assert saved.pending_count == result.pending_review
    missing = {asset.candidate.id for asset in saved.assets() if asset.assessment is None}
    retained = {asset.candidate.id for asset in saved.assets() if asset.assessment is not None}
    assert missing and retained

    resumed_client = Agent()
    resumed = retry_review(result.retry_path, Event(), client=resumed_client)
    assert not resumed.error and not resumed.pending_review
    sent = {ident for kind, ids in resumed_client.calls if kind == "assess" for ident in ids}
    assert sent == missing and sent.isdisjoint(retained)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_cleanup_keeps_export_retry_and_service_copies(tmp_path, monkeypatch, strategy):
    failing_cleanup(monkeypatch, strategy, {"capture", "network"})
    module = legacy_module if strategy == "legacy" else wide_module
    def fail_export(*args, **kwargs):
        raise OSError("synthetic export failure")
    monkeypatch.setattr(module, "export_results", fail_export)
    pipeline, request, client = workflow(strategy, tmp_path / "results")

    result = pipeline.run(request, Event())

    assert "synthetic export failure" in result.error
    assert "synthetic capture cleanup failure" in result.error
    assert "synthetic network cleanup failure" in result.error
    assert result.assets and all(asset.path.is_file() and asset.review_owned for asset in result.assets)
    assert result.folder is None and result.retry_path and result.export_pending
    assert result.pending_review == 0 and result.examined > 0
    def no_model(*args, **kwargs):
        pytest.fail("Cleanup must not force another analysis")
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)

    resumed = retry_review(result.retry_path, Event(), save_only=True)

    assert not resumed.error and len(resumed.assets) == request.count
    assert not resumed.export_pending and resumed.retry_path is None


def fail_temporary_cleanup(monkeypatch):
    from lazyref import result_cleanup
    real_temporary = result_cleanup.tempfile.TemporaryDirectory

    class FailingTemporaryDirectory(real_temporary):
        def cleanup(self):
            super().cleanup()
            raise OSError("synthetic temporary directory cleanup failure")

    monkeypatch.setattr(result_cleanup.tempfile, "TemporaryDirectory", FailingTemporaryDirectory)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_temporary_cleanup_failure_keeps_exports_and_still_closes_other_resources(tmp_path, monkeypatch, strategy):
    events = failing_cleanup(monkeypatch, strategy, {"capture", "network"})
    fail_temporary_cleanup(monkeypatch)
    pipeline, request, client = workflow(strategy, tmp_path / "results")

    result = pipeline.run(request, Event())

    assert result.folder and len(result.assets) == request.count
    assert all(asset.path.is_file() for asset in result.assets)
    assert "synthetic temporary directory cleanup failure" in result.error
    assert "synthetic capture cleanup failure" in result.error
    assert "synthetic network cleanup failure" in result.error
    assert events[-2:] == ["capture", "network"]
    assert result.examined > 0 and not result.export_pending and not result.pending_review


def test_temporary_cleanup_does_not_replace_a_primary_body_exception(tmp_path, monkeypatch):
    from lazyref.models import SearchResult
    from lazyref.result_cleanup import temporary_directory
    fail_temporary_cleanup(monkeypatch)
    result = SearchResult(None, [], 2, error="previous result error")
    primary = RuntimeError("synthetic primary body failure")

    with pytest.raises(RuntimeError) as caught:
        with temporary_directory(result, dir=tmp_path, prefix="fixture-"):
            raise primary

    assert caught.value is primary
    assert result.error.startswith("previous result error")
    assert "synthetic temporary directory cleanup failure" in result.error


def test_retry_cache_cleanup_failure_keeps_completed_ratings_and_exports(tmp_path, monkeypatch):
    from lazyref.search_cache import SearchCache
    from test_session_security import LocalClient, saved_session
    session = saved_session(tmp_path)
    session.data["request"].update(search_strategy="wide", reuse_cache=True, model="fixture-model")
    session.save()
    real_close = SearchCache.close
    closes = []

    def fail_close(cache):
        real_close(cache)
        closes.append(cache)
        raise OSError("synthetic retry cache cleanup failure")

    monkeypatch.setattr(SearchCache, "close", fail_close)
    client = LocalClient()
    result = retry_review(session.path, Event(), client=client)

    assert len(closes) == 1 and "synthetic retry cache cleanup failure" in result.error
    assert client.calls == [["pending"]] and result.examined == 2
    assert result.folder and len(result.assets) == 2
    assert all(asset.path.is_file() for asset in result.assets)
    assert not result.pending_review and not result.export_pending and result.retry_path is None
    assert ReviewSession.load(session.path).pending_count == 0
