"""Session browsing stays metadata-only; recovery validates photos in the worker."""

import json
from threading import Event, get_ident

import pytest
import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from lazyref import generation, review_recovery, review_session, ui
from lazyref.models import SearchRequest
from lazyref.review_session import ReviewSession, list_pending
from lazyref.retry_analysis import retry_review
from test_review_condition_reuse import RecordingClient, create_session, retain_rating


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def no_model(*args, **kwargs):
    raise AssertionError("A recovered complete assessment must not invoke the model")


def worker_only_checks(monkeypatch):
    """Fail ordinary Python calls on the GUI thread, without killing a QThread."""
    main_thread = get_ident()
    calls = []

    def guard(owner, name):
        original = getattr(owner, name)

        def checked(*args, **kwargs):
            thread = get_ident()
            calls.append((name, thread))
            assert thread != main_thread, f"{name} ran on the GUI thread"
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, checked)

    for name in ("recover", "request", "saved_analysis_request", "context", "pending_for", "owned_file", "assets"):
        guard(ReviewSession, name)
    guard(review_session, "digest")
    guard(review_recovery, "pending")
    guard(generation, "validate_reference")
    return calls


@pytest.mark.parametrize("storage", ["committed", "receipt"])
@pytest.mark.parametrize("guide", [False, True])
def test_metadata_loading_and_session_list_do_not_recover_or_read_photos(
        qt_app, tmp_path, monkeypatch, storage, guide):
    session = create_session(tmp_path, guide=guide)
    retain_rating(session, storage)
    output = session.request().output_dir
    before = {p.relative_to(session.path.parent): p.read_bytes()
              for p in session.path.parent.rglob("*") if p.is_file()}
    calls = worker_only_checks(monkeypatch)

    metadata = ReviewSession.load_metadata(session.path)
    assert metadata.pending_count == (1 if storage == "receipt" else 0)
    assert not metadata.needs_save
    sessions = list_pending(output)
    assert [s.path for s in sessions] == [session.path]
    assert sessions[0].pending_count == metadata.pending_count
    dialog = ui.PendingReviewsDialog(output)
    try:
        assert dialog.items.count() == 1
        dialog.items.setCurrentRow(0)
        dialog.choose_current()
        assert dialog.selected_path == session.path
        assert dialog.selected_save_only is (None if storage == "receipt" else True)
        assert calls == []
        assert {p.relative_to(session.path.parent): p.read_bytes()
                for p in session.path.parent.rglob("*") if p.is_file()} == before
    finally:
        dialog.close()
        shiboken6.delete(dialog)


@pytest.mark.parametrize("entry", ["file_picker", "session_list"])
@pytest.mark.parametrize("guide", [False, True])
def test_receipt_only_session_recovers_off_gui_thread_without_reanalysis(
        qt_app, tmp_path, monkeypatch, entry, guide):
    session = create_session(tmp_path, guide=guide)
    retain_rating(session, "receipt")
    output = session.request().output_dir
    expected_guide = session.path.parent / session.data["guide"]["file"] if guide else None
    # Only the journal contains the complete rating, as after a failed main write.
    assert json.loads(session.path.read_text(encoding="utf-8"))["assets"][0]["assessment"] is None
    monkeypatch.setattr(review_recovery, "_MEMORY", {})
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)
    real_retry = retry_review
    results = []

    def observe_retry(*args, **kwargs):
        result = real_retry(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(ui, "retry_review", observe_retry)
    window = ui.MainWindow()
    window.save_settings = lambda: None
    window.output.setText(str(output))
    window.reasoning.set_value("high")
    window.last_search_request = SearchRequest("A previous unrelated search", 2, tmp_path / "old")
    calls = worker_only_checks(monkeypatch)
    guarded_digest = review_session.digest
    entered, release, ticked = Event(), Event(), Event()

    def slow_digest(path):
        # A blocked physical read must not block QTimer or the cancel button.
        assert get_ident() != main_thread
        entered.set()
        assert release.wait(5), "Test must release the background photo check"
        return guarded_digest(path)

    main_thread = get_ident()
    monkeypatch.setattr(review_session, "digest", slow_digest)
    dialog = ui.PendingReviewsDialog(output, window)
    if entry == "file_picker":
        monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *a, **k: (str(session.path), ""))
        dialog.choose_file(output)
    else:
        dialog.items.setCurrentRow(0)
        dialog.choose_current()
    assert dialog.selected_save_only is None
    monkeypatch.setattr(dialog, "exec", lambda: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ui, "PendingReviewsDialog", lambda *a, **k: dialog)
    try:
        window.show()
        QTimer.singleShot(0, ticked.set)
        window.open_pending_reviews()
        assert window.worker is not None and entered.wait(2)
        QTest.qWait(30)
        assert ticked.is_set() and window.worker.isRunning()
        assert not results and window.last_search_request is None
        assert not window.repeat_search_button.isEnabled()
        release.set()
        assert window.worker.wait(5000)
        qt_app.processEvents()

        assert len(results) == 1 and not results[0].error
        assert len(results[0].assets) == 1 and not results[0].export_pending
        manifest = json.loads((results[0].folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["requested_model"] == "fixture-model"
        assert manifest["requested_reasoning_effort"] == "medium"
        assert window.settings["reasoning_effort"] == "high"
        assert window.last_search_request.reasoning_effort == "medium"
        assert window.last_search_request.reference_image == expected_guide
        assert window.last_search_request.description == "A seated person"
        assert calls and all(thread != main_thread for _, thread in calls)
        assert {name for name, _ in calls} >= {"recover", "pending", "request", "owned_file", "digest", "assets"}
        if guide:
            assert "validate_reference" in {name for name, _ in calls}
    finally:
        release.set()
        if window.worker and window.worker.isRunning():
            window.worker.cancel.set()
            assert window.worker.wait(5000)
        qt_app.processEvents()
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("target", ["guide", "full", "preview"])
def test_invalid_photo_or_guide_is_rejected_in_worker_without_stale_repeat_request(
        qt_app, tmp_path, monkeypatch, target):
    session = create_session(tmp_path, guide=True)
    retain_rating(session, "receipt")
    row = session.data["assets"][0]
    filename = session.data["guide"]["file"] if target == "guide" else row["file" if target == "full" else "preview"]
    (session.path.parent / filename).write_bytes(b"synthetic damaged image")
    before = {p.relative_to(session.path.parent): p.read_bytes()
              for p in session.path.parent.rglob("*") if p.is_file()}
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)
    window = ui.MainWindow()
    window.save_settings = lambda: None
    window.last_search_request = SearchRequest("An unrelated older task", 2, tmp_path / "previous")
    calls = worker_only_checks(monkeypatch)
    try:
        window.show()
        window.launch_review(session.path, save_only=True)
        assert window.worker is not None
        assert window.worker.wait(5000)
        qt_app.processEvents()

        assert "відсутнє або змінене" in window.details.toPlainText()
        assert not window.gallery.findChildren(ui.ImageCard)
        assert window.retry_path == session.path
        assert window.last_search_request is None
        assert not window.repeat_search_button.isEnabled()
        assert calls and all(thread != get_ident() for _, thread in calls)
        assert {p.relative_to(session.path.parent): p.read_bytes()
                for p in session.path.parent.rglob("*") if p.is_file() and p.name != "review.lock"} == before
    finally:
        if window.worker and window.worker.isRunning():
            window.worker.cancel.set()
            assert window.worker.wait(5000)
        qt_app.processEvents()
        window.close()
        shiboken6.delete(window)


def test_auto_mode_does_not_reuse_a_receipt_for_changed_conditions(tmp_path, monkeypatch):
    session = create_session(tmp_path)
    retain_rating(session, "receipt")
    session.data["request"]["description"] = "A standing person facing forward"
    session.save()
    client = RecordingClient()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)

    result = retry_review(session.path, Event(), save_only=None, client=client, reasoning_effort="high")

    assert not result.error and len(client.calls) == 1
    assert client.calls[0][0] == "A standing person facing forward"
    manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requested_reasoning_effort"] == "high"
    assert manifest["description"] == "A standing person facing forward"


def test_auto_mode_does_not_turn_advertised_completed_ratings_into_analysis(tmp_path, monkeypatch):
    session = create_session(tmp_path)
    retain_rating(session, "committed")
    session.data["request"]["description"] = "A standing person facing forward"
    session.save()
    before = session.path.read_bytes()
    client = RecordingClient()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)

    with pytest.raises(ValueError, match="Новий аналіз не запускався"):
        retry_review(session.path, Event(), save_only=None, client=client, reasoning_effort="high")

    assert not client.calls and session.path.read_bytes() == before
