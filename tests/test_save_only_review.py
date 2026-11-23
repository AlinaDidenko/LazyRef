"""Local export keeps the original analysis conditions and never buys a new rating."""

import json
import os
from threading import Event

import pytest
from PIL import Image

from lazyref.review_session import ReviewSession, digest
from lazyref.retry_analysis import retry_review
from test_review_condition_reuse import RecordingClient, create_session, rating_for, retain_rating


def forbid_client(*args, **kwargs):
    pytest.fail("Saving an existing result must not construct CodexClient")


@pytest.mark.parametrize("storage", ["committed", "receipt"])
def test_save_only_ignores_new_model_and_reasoning_and_exports_original_conditions(tmp_path, monkeypatch, storage):
    session = create_session(tmp_path)
    retain_rating(session, storage)
    client = RecordingClient()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)

    result = retry_review(session.path, Event(), save_only=True, client=client,
                          model="different-model", reasoning_effort="high")

    assert not result.error and len(result.assets) == 1 and result.examined == 1
    assert not client.calls and result.pending_review == 0
    assert not result.export_pending and result.retry_path is None
    manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requested_model"] == "fixture-model"
    assert manifest["requested_reasoning_effort"] == "medium"
    saved = ReviewSession.load(session.path)
    assert saved.data["request"]["model"] == "fixture-model"
    assert saved.data["request"]["reasoning_effort"] == "medium"
    assert saved.pending_for(saved.request()) == 0


@pytest.mark.parametrize("problem", ["missing_rating", "changed_conditions", "changed_image",
                                     "corrupt_image", "invalid_rating", "rating_from_other_conditions"])
def test_save_only_invalid_or_incomplete_session_fails_without_model_or_session_rewrite(tmp_path, monkeypatch, problem):
    session = create_session(tmp_path)
    if problem != "missing_rating":
        retain_rating(session, "committed")
    row = session.data["assets"][0]
    if problem == "changed_conditions":
        session.data["request"]["description"] = "A standing person"
    elif problem in {"changed_image", "corrupt_image"}:
        image = session.path.parent / row["file"]
        Image.new("RGB", (200, 200), "red").save(image)
        if problem == "changed_image":
            row["sha256"] = digest(image)
    elif problem == "invalid_rating":
        row["assessment"]["score"] = "invalid score"
    elif problem == "rating_from_other_conditions":
        # This is a legitimate rating from an explicit reassessment. Its
        # conditions cannot be reconstructed from the original request alone.
        request = session.request(reasoning_effort="high")
        session.complete(session.assets(request=request),
                         {"photo": rating_for(session.data["plan"]["criteria"])},
                         request.search_profile, context=session.context(request))
    session.save()
    original = session.path.read_bytes()
    client = RecordingClient()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)

    with pytest.raises(ValueError):
        retry_review(session.path, Event(), save_only=True, client=client,
                     model="different-model", reasoning_effort="high")

    assert not client.calls
    assert session.path.read_bytes() == original
    assert not (session.path.parent / "attempts").exists()
    assert not list(session.request().output_dir.glob("*/manifest.json"))


def test_explicit_reanalysis_still_uses_new_reasoning_conditions(tmp_path):
    session = create_session(tmp_path)
    retain_rating(session, "committed")
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client, reasoning_effort="high")

    assert not result.error and len(client.calls) == 1
    assert client.calls[0][1] == ["photo"]
    manifest = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requested_reasoning_effort"] == "high"
    saved = ReviewSession.load(session.path)
    assert saved.data["request"]["reasoning_effort"] == "medium"
    assert saved.pending_for(saved.request(reasoning_effort="high")) == 0
    assert saved.pending_for(saved.request()) == 1


@pytest.mark.parametrize("failure", ["export", "main_session_write"])
def test_save_only_recovers_new_reanalysis_conditions_after_local_failure(tmp_path, monkeypatch, failure):
    from lazyref import review_recovery
    import lazyref.retry_analysis as retry_module

    session = create_session(tmp_path)
    retain_rating(session, "committed")
    client = RecordingClient()
    real_export, real_save = retry_module.export_results, ReviewSession.save

    def fail_export(*args, **kwargs):
        raise OSError("synthetic export failure")

    def fail_main_write_after_assessment(self):
        if client.calls:
            raise OSError("synthetic post-assessment write failure")
        return real_save(self)

    if failure == "export":
        monkeypatch.setattr(retry_module, "export_results", fail_export)
    else:
        monkeypatch.setattr(ReviewSession, "save", fail_main_write_after_assessment)

    first = retry_review(session.path, Event(), client=client,
                         model="different-model", reasoning_effort="high")

    assert first.error and "synthetic" in first.error
    assert len(client.calls) == 1 and first.export_pending
    stored = json.loads(session.path.read_text(encoding="utf-8"))
    assert stored["request"]["model"] == "fixture-model"
    assert stored["request"]["reasoning_effort"] == "medium"
    assert stored["analysis_settings"] == {"model": "different-model", "reasoning_effort": "high"}
    if failure == "main_session_write":
        assert stored["assets"][0]["assessment"] is None
        assert (session.path.parent / "assessment-recovery.json").is_file()
    monkeypatch.setattr(retry_module, "export_results", real_export)
    monkeypatch.setattr(ReviewSession, "save", real_save)
    # Emulate restarting the app: the disk receipt and persisted analysis
    # settings alone must be sufficient to recover the already-paid rating.
    monkeypatch.setattr(review_recovery, "_MEMORY", {})
    recovered = ReviewSession.load(session.path)
    assert recovered.pending_count == 0
    assert recovered.saved_analysis_request().model == "different-model"
    assert recovered.saved_analysis_request().reasoning_effort == "high"
    monkeypatch.setattr(retry_module, "CodexClient", forbid_client)

    second = retry_review(session.path, Event(), save_only=True,
                          model="third-model", reasoning_effort="low")

    assert not second.error and len(second.assets) == 1 and not second.export_pending
    assert len(client.calls) == 1
    manifest = json.loads((second.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requested_model"] == "different-model"
    assert manifest["requested_reasoning_effort"] == "high"


def test_reanalysis_cannot_call_model_if_new_analysis_settings_cannot_be_saved(tmp_path, monkeypatch):
    session = create_session(tmp_path)
    retain_rating(session, "committed")
    original = session.path.read_bytes()
    client = RecordingClient()

    def fail_save(self):
        raise OSError("synthetic analysis settings write failure")

    monkeypatch.setattr(ReviewSession, "save", fail_save)
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)
    try:
        result = retry_review(session.path, Event(), client=client,
                              model="different-model", reasoning_effort="high")
    except OSError as exc:
        error = str(exc)
    else:
        error = result.error

    assert error and "synthetic analysis settings write failure" in error
    assert not client.calls
    assert session.path.read_bytes() == original
    assert not (session.path.parent / "assessment-recovery.json").exists()


@pytest.mark.parametrize("settings", [[], {}, {"model": [] , "reasoning_effort": "high"},
                                      {"model": "fixture-model", "reasoning_effort": "invalid"}])
def test_malformed_saved_analysis_settings_fail_locally_without_modifying_session(tmp_path, monkeypatch, settings):
    session = create_session(tmp_path)
    retain_rating(session, "committed")
    session.data["analysis_settings"] = settings
    session.save()
    original = session.path.read_bytes()
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)

    with pytest.raises(ValueError):
        retry_review(session.path, Event(), save_only=True)

    assert session.path.read_bytes() == original


@pytest.fixture(scope="module")
def save_only_qt_app():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("entry", ["button", "file_picker"])
def test_ui_save_retry_uses_original_medium_after_user_selects_high(save_only_qt_app, tmp_path, monkeypatch, entry):
    import shiboken6
    from lazyref.ui import MainWindow

    session = create_session(tmp_path)
    retain_rating(session, "committed")
    calls, results = [], []

    def observe_retry(*args, **kwargs):
        calls.append(kwargs.copy())
        result = retry_review(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr("lazyref.ui.retry_review", observe_retry)
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)
    monkeypatch.setattr("lazyref.ui.SearchPipeline.run", lambda *args: pytest.fail("No new search"))
    window = MainWindow()
    window.save_settings = lambda: None
    try:
        window.show()
        window.set_retry(session.path, 0, export_pending=True)
        assert "Повторити збереження" in window.retry_button.text()
        window.reasoning.set_value("high")
        assert window.settings["reasoning_effort"] == "high"
        window.description.setPlainText("New description intended for a future search")
        if entry == "button":
            window.retry_button.click()
        else:
            # The file-picker dispatches this entry without a preselected mode.
            window.launch_review(session.path)
        worker = window.worker
        assert worker is not None and worker.wait(5000)
        save_only_qt_app.processEvents()

        assert len(calls) == len(results) == 1
        assert calls[0]["save_only"] is True
        assert not results[0].error and len(results[0].assets) == 1
        manifest = json.loads((results[0].folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["requested_model"] == "fixture-model"
        assert manifest["requested_reasoning_effort"] == "medium"
        assert manifest["description"] == "A seated person"
        assert window.settings["reasoning_effort"] == "high"
        assert not window.export_pending and window.pending_review == 0
    finally:
        if window.worker and window.worker.isRunning():
            window.worker.cancel.set()
            window.worker.wait(5000)
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("entry", ["button", "session_list"])
def test_ui_save_intent_survives_session_becoming_incomplete(save_only_qt_app, tmp_path, monkeypatch, entry):
    import shiboken6
    from PySide6.QtWidgets import QDialog
    from lazyref.ui import MainWindow, PendingReviewsDialog

    session = create_session(tmp_path)
    retain_rating(session, "committed")
    calls, errors = [], []

    def observe_retry(*args, **kwargs):
        calls.append(kwargs.copy())
        try:
            return retry_review(*args, **kwargs)
        except ValueError as exc:
            errors.append(str(exc))
            raise

    monkeypatch.setattr("lazyref.ui.retry_review", observe_retry)
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", forbid_client)
    window = MainWindow()
    window.save_settings = lambda: None
    try:
        window.show()
        window.set_retry(session.path, 0, export_pending=True)
        window.reasoning.set_value("high")
        if entry == "session_list":
            dialog = PendingReviewsDialog(session.request().output_dir, window)
            assert dialog.items.count() == 1
            dialog.items.setCurrentRow(0)
            # The displayed ready-to-save state remains the user's intent even
            # if another window changes the file before they click Continue.
            session.data["assets"][0]["assessment"] = None
            session.data["assets"][0].pop("assessment_key", None)
            session.save()
            dialog.choose_current()
            assert dialog.selected_save_only is True
            monkeypatch.setattr(dialog, "exec", lambda: QDialog.DialogCode.Accepted)
            monkeypatch.setattr("lazyref.ui.PendingReviewsDialog", lambda *args: dialog)
        else:
            session.data["assets"][0]["assessment"] = None
            session.data["assets"][0].pop("assessment_key", None)
            session.save()
        original = session.path.read_bytes()

        if entry == "session_list":
            window.open_pending_reviews()
        else:
            window.retry_button.click()
        worker = window.worker
        assert worker is not None and worker.wait(5000)
        save_only_qt_app.processEvents()

        assert len(calls) == len(errors) == 1
        assert calls[0]["save_only"] is True
        assert "Новий аналіз не запускався" in errors[0]
        assert session.path.read_bytes() == original
        assert not (session.path.parent / "attempts").exists()
        assert window.pending_review == 1 and window.export_pending
    finally:
        if window.worker and window.worker.isRunning():
            window.worker.cancel.set()
            window.worker.wait(5000)
        window.close()
        shiboken6.delete(window)
