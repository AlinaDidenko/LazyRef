"""Finished assessments can be exported again without buying another analysis."""

from dataclasses import asdict
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.models import SearchResult
from lazyref.review_session import ReviewSession, list_pending
from lazyref.retry_analysis import retry_review
from test_session_security import LocalClient, saved_session, valid_rating


def forbid_client(*args, **kwargs):
    pytest.fail("Export-only recovery must not construct CodexClient")


def test_export_failure_keeps_completed_session_visible_and_retries_without_codex(tmp_path, monkeypatch):
    session = saved_session(tmp_path)
    output = session.request().output_dir
    client = LocalClient()
    import lazyref.retry_analysis as retry_module
    real_export = retry_module.export_results
    exports = []

    def fail_once(*args, **kwargs):
        exports.append(True)
        raise OSError("synthetic disk full during export")

    monkeypatch.setattr(retry_module, "export_results", fail_once)
    first = retry_review(session.path, Event(), client=client)

    assert "synthetic disk full" in first.error
    assert first.pending_review == 0 and first.export_pending is True
    assert first.retry_path == session.path
    assert client.calls == [["pending"]]
    saved = ReviewSession.load(session.path)
    assert saved.pending_count == 0 and saved.export_pending is True
    assert all(asset.assessment is not None for asset in saved.assets())
    assert [item.path for item in list_pending(output)] == [session.path]

    monkeypatch.setattr(retry_module, "export_results", real_export)
    monkeypatch.setattr(retry_module, "CodexClient", forbid_client)
    second = retry_review(session.path, Event())

    assert not second.error and not second.cancelled
    assert second.folder and (second.folder / "manifest.json").is_file()
    assert {asset.candidate.id for asset in second.assets} == {"completed", "pending"}
    assert second.pending_review == 0 and second.export_pending is False
    assert second.retry_path is None
    assert client.calls == [["pending"]] and len(exports) == 1
    assert ReviewSession.load(session.path).export_pending is False
    assert list_pending(output) == []


def test_repeated_export_error_retains_marker_without_reanalysing(tmp_path, monkeypatch):
    session = saved_session(tmp_path)
    client = LocalClient()
    import lazyref.retry_analysis as retry_module

    def fail_export(*args, **kwargs):
        raise OSError("synthetic unavailable destination")

    monkeypatch.setattr(retry_module, "export_results", fail_export)
    first = retry_review(session.path, Event(), client=client)
    assert first.export_pending and client.calls == [["pending"]]
    monkeypatch.setattr(retry_module, "CodexClient", forbid_client)

    second = retry_review(session.path, Event())

    assert "synthetic unavailable destination" in second.error
    assert second.retry_path == session.path and second.export_pending
    assert second.pending_review == 0 and client.calls == [["pending"]]
    assert ReviewSession.load(session.path).export_pending


def test_successfully_exported_complete_session_still_refuses_reanalysis(tmp_path, monkeypatch):
    session = saved_session(tmp_path)
    client = LocalClient()
    result = retry_review(session.path, Event(), client=client)
    assert not result.error and result.export_pending is False
    assert ReviewSession.load(session.path).export_pending is False
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)
    with pytest.raises(ValueError, match="вже оцінені"):
        retry_review(session.path, Event())
    assert client.calls == [["pending"]]


def test_legacy_complete_session_missing_export_marker_defaults_to_finished(tmp_path, monkeypatch):
    session = saved_session(tmp_path)
    session.data["assets"][1]["assessment"] = asdict(valid_rating())
    session.data.pop("export_pending", None)
    session.save()
    loaded = ReviewSession.load(session.path)
    assert loaded.pending_count == 0 and loaded.export_pending is False
    assert list_pending(session.request().output_dir) == []
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)
    with pytest.raises(ValueError, match="вже оцінені"):
        retry_review(session.path, Event())


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_initial_search_export_error_preserves_all_assessments_for_local_retry(tmp_path, monkeypatch, strategy):
    from lazyref.models import Candidate, SearchRequest
    from lazyref.pipeline import SearchPipeline
    from test_core import make_asset
    from test_wide_search import Agent, request as wide_request, run as run_wide

    client = Agent()
    output = tmp_path / "results"

    def fail_export(*args, **kwargs):
        raise OSError("synthetic initial export failure")

    if strategy == "legacy":
        monkeypatch.setattr("lazyref.pipeline.export_results", fail_export)
        candidate = Candidate("first", "Photo 1", page_url="https://example.org/photo/1")
        pipeline = SearchPipeline(client, finder=lambda *args: ([candidate], []),
                                  loader=lambda c, directory, network: make_asset(directory, c.id, 1))
        first = pipeline.run(SearchRequest("Man seated at a bar from behind", 1, output,
                                           search_strategy="legacy", reuse_cache=False), Event())
    else:
        monkeypatch.setattr("lazyref.wide_search.export_results", fail_export)
        first = run_wide(wide_request(output, reuse_cache=False), client)

    assert "synthetic initial export failure" in first.error
    assert first.export_pending and first.pending_review == 0 and first.retry_path
    saved = ReviewSession.load(first.retry_path)
    assert saved.export_pending and saved.pending_count == 0
    assert len(saved.assets()) == first.examined > 0
    assert all(asset.assessment is not None for asset in saved.assets())
    assert [item.path for item in list_pending(output)] == [saved.path]
    previous_calls = list(client.calls)
    previous_plans = client.plan_calls
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)

    second = retry_review(saved.path, Event())

    assert not second.error and second.folder
    assert len(second.assets) == first.requested
    assert not second.export_pending and second.retry_path is None
    assert not ReviewSession.load(saved.path).export_pending
    assert list_pending(output) == []
    assert client.calls == previous_calls and client.plan_calls == previous_plans


@pytest.mark.parametrize("strategy", ["retry", "legacy", "wide"])
@pytest.mark.parametrize("saved_count", [0, 1], ids=["all_copies_fail", "partial_export"])
def test_exhausted_copy_failures_preserve_export_retry_and_partial_folder(tmp_path, monkeypatch, strategy, saved_count):
    import lazyref.exporter as exporter
    from lazyref.models import Candidate, SearchRequest
    from lazyref.pipeline import SearchPipeline
    from test_core import make_asset
    from test_wide_search import Agent, request as wide_request, run as run_wide

    output = tmp_path / "results"
    real_copy = exporter.shutil.copyfile
    copied = []

    def fail_export_copy(source, target, *args, **kwargs):
        destination = Path(target)
        exporting = (destination.parent.name.startswith("LazyRef_")
                     or destination.parent.parent.name.startswith("LazyRef_"))
        if exporting:
            if len(copied) >= saved_count:
                raise OSError("synthetic exhausted copy failure")
            copied.append(destination)
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(exporter.shutil, "copyfile", fail_export_copy)
    if strategy == "retry":
        session = saved_session(tmp_path)
        client = LocalClient()
        first = retry_review(session.path, Event(), client=client)
    elif strategy == "legacy":
        client = Agent()
        candidates = [Candidate(str(i), f"Photo {i}", page_url=f"https://example.org/photo/{i}") for i in (1, 2)]
        pipeline = SearchPipeline(client, finder=lambda *args: (candidates, []),
                                  loader=lambda c, directory, network: make_asset(directory, c.id, int(c.id)))
        first = pipeline.run(SearchRequest("Man seated at a bar from behind", 2, output,
                                           search_strategy="legacy", reuse_cache=False), Event())
    else:
        client = Agent()
        first = run_wide(wide_request(output, reuse_cache=False), client)

    assert first.error and first.export_pending and first.pending_review == 0
    assert first.retry_path and len(first.assets) == saved_count
    assert first.folder and (first.folder / "sources.csv").is_file()
    manifest = first.folder / "manifest.json"
    old_report = manifest.read_bytes()
    assert json.loads(old_report)["saved"] == saved_count
    assert all(asset.path.is_file() and asset.path.is_relative_to(first.folder) for asset in first.assets)
    session = ReviewSession.load(first.retry_path)
    assert session.export_pending and session.pending_count == 0
    assert len(session.assets()) >= first.requested
    assert [item.path for item in list_pending(output)] == [session.path]
    previous_calls = list(client.calls)
    monkeypatch.setattr(exporter.shutil, "copyfile", real_copy)
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)

    second = retry_review(session.path, Event())

    assert not second.error and len(second.assets) == first.requested
    assert second.folder != first.folder and manifest.read_bytes() == old_report
    assert not second.export_pending and second.retry_path is None
    assert not ReviewSession.load(session.path).export_pending
    assert list_pending(output) == [] and client.calls == previous_calls


@pytest.fixture(scope="module")
def export_qt_app():
    import os
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_ui_offers_export_retry_when_no_assessment_is_pending(export_qt_app, tmp_path, monkeypatch):
    import shiboken6
    from lazyref.ui import MainWindow
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    session = saved_session(tmp_path)
    result = SearchResult(None, [], 2, error="synthetic export error",
                          retry_path=session.path, pending_review=0, export_pending=True)
    window = MainWindow()
    calls = []
    window.launch_review = lambda path, **kwargs: calls.append((path, kwargs))
    try:
        window.show_results(result)
        assert not window.retry_row.isHidden()
        assert "Повторити збереження" in window.retry_button.text()
        assert window.retry_button.isEnabled()
        window.set_busy(True)
        assert not window.retry_button.isEnabled()
        window.set_busy(False)
        assert window.retry_button.isEnabled()
        window.retry_button.click()
        assert calls == [(session.path, {"save_only": True})]
    finally:
        window.close()
        shiboken6.delete(window)
