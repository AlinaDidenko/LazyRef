import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import pytest
import shiboken6

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from lazyref.models import SearchResult
from lazyref.ui import MainWindow, SettingsDialog, AccessLinksDialog, configure_app


def test_optional_photo_filters_persist_and_apply_to_repeated_search(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    import json
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    window = MainWindow()
    window.description.setPlainText("Чоловік сидить зі спини")
    window.output.setText(str(tmp_path))
    original = window.current_search_request()
    dialog = SettingsDialog(window.settings, window)
    assert dialog.enhanced_dedup.text().endswith("OFF") and dialog.high_quality_only.text().endswith("OFF")
    assert not original.enhanced_dedup and not original.high_quality_only
    dialog.enhanced_dedup.click()
    dialog.high_quality_only.click()
    assert dialog.enhanced_dedup.text().endswith("ON") and dialog.high_quality_only.text().endswith("ON")
    window.apply_settings(dialog.values())
    current = window.current_search_request()
    assert current.enhanced_dedup and current.high_quality_only
    stored = json.loads(ui.SETTINGS.read_text(encoding="utf-8"))
    reopened = SettingsDialog(stored, window)
    assert reopened.enhanced_dedup.isChecked() and reopened.high_quality_only.isChecked()
    window.last_search_request = original
    calls = []
    window.launch_search = calls.append
    window.repeat_last_search()
    assert calls[-1].enhanced_dedup and calls[-1].high_quality_only
    assert not original.enhanced_dedup and not original.high_quality_only
    assert not calls[-1].reuse_cache
    window.close()
    shiboken6.delete(reopened)
    shiboken6.delete(dialog)
    shiboken6.delete(window)


def test_wide_and_legacy_switch_apply_to_text_and_same_generated_guide(qt_app, tmp_path, monkeypatch):
    from lazyref.models import SearchRequest
    from lazyref.generation import GenerationResult
    from test_core import picture
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    window = MainWindow()
    window.output.setText(str(tmp_path))
    window.description.setPlainText("Чоловік сидить зі спини")
    calls = []
    window.launch_search = calls.append
    dialog = SettingsDialog(window.settings, window)
    assert dialog.values()["search_strategy"] == "wide"
    assert dialog.values()["discovery_limit"] == 200 and dialog.values()["detail_limit"] == 40
    window.start_search()
    assert calls[-1].search_strategy == "wide"
    dialog.strategy.setCurrentIndex(dialog.strategy.findData("legacy"))
    assert not dialog.discovery_limit.isEnabled() and not dialog.reuse_cache.isEnabled()
    window.settings.update(dialog.values())
    window.start_search()
    assert calls[-1].search_strategy == "legacy" and calls[-1].candidate_limit == 72
    guide = tmp_path / "guide.png"
    picture().save(guide)
    window.generation_search_request = SearchRequest("Чоловік сидить зі спини", 2, tmp_path)
    window.generation_result = GenerationResult(tmp_path, [guide], 1)
    window.settings.update(search_strategy="wide", reuse_cache=False)
    window.select_generated(guide)
    assert calls[-1].search_strategy == "wide" and not calls[-1].reuse_cache
    assert calls[-1].reference_image == guide
    window.settings["search_strategy"] = "legacy"
    window.select_generated(guide)
    assert calls[-1].search_strategy == "legacy" and calls[-1].reference_image == guide
    window.close()
    shiboken6.delete(dialog)
    shiboken6.delete(window)


def test_open_folder_works_before_first_run_and_tracks_displayed_results(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    from lazyref.generation import GenerationResult
    from lazyref.library import image_record
    from test_core import picture
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(Path(url.toLocalFile())) or True)
    window = MainWindow()
    output = tmp_path / "Фото з пробілами"
    window.output.setText(str(output))
    window.folder_button.click()
    assert output.is_dir() and opened == [output]  # Клік працює без нового пошуку.
    window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
    window.folder_button.click()
    assert opened[-1] == tmp_path / "generated_references"
    generated = tmp_path / "generated_references/AI_pose_test"
    generated.mkdir()
    window.show_generated(GenerationResult(generated, [], 1))
    window.folder_button.click()
    assert opened[-1] == generated
    result_folder = output / "LazyRef_test"
    result_folder.mkdir()
    window.show_results(SearchResult(result_folder, [], 5))
    window.folder_button.click()
    assert opened[-1] == result_folder
    archive = tmp_path / "archive"
    archive.mkdir()
    image = archive / "image.png"
    picture().save(image)
    window.choose_library_image(image_record(image, archive, "generated", archived=True))
    window.folder_button.click()
    assert opened[-1] == archive
    window.provider.setCurrentIndex(window.provider.findData("agent"))
    window.show_results(SearchResult(None, [], 5, error="Пошук перервано"))
    window.folder_button.click()
    assert opened[-1] == output  # Помилка без добірки теж не вимикає кнопку.
    window.close()
    shiboken6.delete(window)


@pytest.mark.parametrize("failure", ["windows", "path_is_file"])
def test_open_folder_reports_errors_instead_of_silent_noop(qt_app, tmp_path, monkeypatch, failure):
    from lazyref import ui
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    errors, opened = [], []
    monkeypatch.setattr(ui, "show_error", lambda parent, message: errors.append(str(message)))
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url) or False)
    window = MainWindow()
    path = tmp_path / "output"
    if failure == "path_is_file":
        path.write_text("existing file", encoding="utf-8")
    window.output.setText(str(path))
    window.folder_button.click()
    assert len(errors) == 1
    if failure == "path_is_file":
        assert not opened and path.read_text(encoding="utf-8") == "existing file"
    else:
        assert len(opened) == 1 and "Windows" in errors[0]
    window.close()
    shiboken6.delete(window)


def test_verification_toggle_persists_and_old_libraries_are_absent(qt_app, tmp_path, monkeypatch):
    import json
    from lazyref.models import SearchRequest
    from lazyref.generation import GenerationResult
    from test_core import picture, make_asset
    from dataclasses import replace
    from lazyref.ui import ImageCard
    from PySide6.QtWidgets import QLabel
    settings = tmp_path / "settings.json"
    # Старі ключі не читаються зі сховища й не записуються назад у JSON.
    settings.write_text('{"pexels_key":"obsolete-test-key","remember_api_keys":true}', encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", settings)
    w = MainWindow()
    assert [w.provider.itemData(i) for i in range(w.provider.count())] == ["agent", "agent_reference", "image_reference"]
    assert w.verify_sources.isChecked()
    w.verify_sources.click()
    assert "OFF" in w.verify_sources.text()
    w.output.setText(str(tmp_path))
    w.description.setPlainText("Чоловік у присіді, ракурс три чверті")
    calls = []
    w.launch_search = calls.append
    w.start_search()
    assert calls[-1].verify_sources is False
    guide = tmp_path / "guide.png"
    picture().save(guide)
    w.generation_search_request = SearchRequest(w.description.toPlainText(), 2, tmp_path)
    w.generation_result = GenerationResult(tmp_path, [guide], 1)
    w.select_generated(guide)
    assert calls[-1].verify_sources is False and calls[-1].reference_image == guide
    w.verify_sources.click()
    w.select_generated(guide)
    assert calls[-1].verify_sources is True and calls[-1].reference_image == guide
    asset = make_asset(tmp_path)
    asset.assessment = replace(asset.assessment, verify_sources=False, media="unverified", confidence="not_checked")
    card = ImageCard(asset, 1)
    assert any(label.text() == "Походження не перевірено" for label in card.findChildren(QLabel))
    w.show_results(SearchResult(tmp_path, [asset], 2, verify_sources=False))
    assert "OFF · Походження не перевірено" in w.subtitle.text()
    w.verify_sources.setChecked(False)
    w.set_busy(True)
    assert not w.verify_sources.isEnabled()
    w.set_busy(False)
    w.close()
    shiboken6.delete(card)
    shiboken6.delete(w)
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["verify_sources"] is False and "pexels_key" not in saved and "remember_api_keys" not in saved
    again = MainWindow()
    assert not again.verify_sources.isChecked()
    again.close()
    shiboken6.delete(again)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    app.processEvents()
    shiboken6.delete(app)




def test_language_changes_on_save_and_keeps_the_current_search(qt_app, tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QDialog, QLabel, QPushButton
    from lazyref import ui
    from test_core import make_asset

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(ui, "SETTINGS", settings)
    window = MainWindow()
    reopened = None
    try:
        window.description.setPlainText("Людина сидить навпочіпки")
        window.count.setValue(4)
        window.search_profile.setCurrentIndex(window.search_profile.findData("pose"))
        asset = make_asset(tmp_path)
        window.show_results(SearchResult(tmp_path, [asset], 4, search_profile="pose"))
        card = window.gallery.findChild(ui.ImageCard)

        def choose_english(dialog):
            assert dialog.language.currentText() == "Українська"
            dialog.language.setCurrentIndex(dialog.language.findData("en"))
            return outcome

        monkeypatch.setattr(SettingsDialog, "exec", choose_english)
        outcome = QDialog.DialogCode.Rejected
        window.settings_button.click()
        assert window.settings_button.text() == "Налаштування"
        assert not settings.exists()

        outcome = QDialog.DialogCode.Accepted
        window.settings_button.click()
        assert window.settings_button.text() == "Settings"
        assert window.search.text() == "Find references  →"
        assert window.description.toPlainText() == "Людина сидить навпочіпки"
        assert window.count.value() == 4 and window.search_profile.currentData() == "pose"
        assert "Pose and anatomy" in window.subtitle.text()
        assert card is window.gallery.findChild(ui.ImageCard)
        assert "Open photo" in {button.text() for button in card.findChildren(QPushButton)}
        assert asset.assessment.reason in {label.text() for label in card.findChildren(QLabel)}
        assert json.loads(settings.read_text(encoding="utf-8"))["language"] == "en"

        reopened = MainWindow()
        assert reopened.settings_button.text() == "Settings"
        reopened.apply_settings({"language": "uk"})
        assert reopened.settings_button.text() == "Налаштування"
    finally:
        window.close()
        shiboken6.delete(window)
        if reopened is not None:
            reopened.close()
            shiboken6.delete(reopened)


def test_startup_guide_opt_out_persists_and_settings_can_reopen_it(qt_app, tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QLabel, QPushButton, QRadioButton
    from lazyref import ui

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(ui, "SETTINGS", settings)
    opened = []

    def dismiss(guide):
        choices = {button.text(): button for button in guide.findChildren(QRadioButton)}
        if not opened:
            assert guide.windowTitle() == "Інструкція LazyRef"
            assert choices["Українська"].isChecked()
            assert not settings.exists()
            choices["Українська"].click()
            assert json.loads(settings.read_text(encoding="utf-8"))["language"] == "uk"
            guide.dont_show_again.setChecked(True)
            choices["English"].click()
            assert guide.parent().settings_button.text() == "Settings"
            assert json.loads(settings.read_text(encoding="utf-8"))["language"] == "en"
            assert guide.dont_show_again.isChecked()
        if guide.dont_show_again is not None:
            assert choices["English"].isChecked() and not choices["Українська"].isChecked()
            assert "Interface language" in {label.text() for label in guide.findChildren(QLabel)}
            assert "Got it" in {button.text() for button in guide.findChildren(QPushButton)}
        else:
            assert not choices
        opened.append((guide.windowTitle(), guide.dont_show_again is not None))
        assert "How to use LazyRef" in {label.text() for label in guide.findChildren(QLabel)}
        if guide.dont_show_again is not None:
            assert guide.dont_show_again.text() == "Don't show again"
            guide.dont_show_again.setChecked(len(opened) == 2)
        guide.reject()

    monkeypatch.setattr(ui.GuideDialog, "exec", dismiss)
    for index in range(3):
        window = MainWindow()
        try:
            window.show_startup_guide()
            stored = json.loads(settings.read_text(encoding="utf-8"))
            assert stored.get("hide_startup_guide", False) is (index > 0)
            if index == 2:
                dialog = SettingsDialog(window.settings, window)
                dialog.guide_button.click()
                assert dialog.guide_button.text() == "Open quick start guide"
        finally:
            window.close()
            shiboken6.delete(window)

    assert opened == [("LazyRef quick start", True), ("LazyRef quick start", True),
                      ("LazyRef quick start", False)]


def test_priorities_persist_and_same_generated_guide_can_use_both(qt_app, tmp_path, monkeypatch):
    import json
    from lazyref.models import SearchRequest
    from lazyref.generation import GenerationResult
    from test_core import picture
    settings = tmp_path / "settings.json"
    # Старі налаштування мігрують без непомітної зміни пошуку.
    settings.write_text('{"search_revision":2}', encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", settings)
    window = MainWindow()
    assert window.search_profile.currentData() == "strict"
    guide = tmp_path / "guide.png"
    picture().save(guide)
    window.output.setText(str(tmp_path))
    window.description.setPlainText("Чоловік сидить за стійкою зі спини")
    window.generation_search_request = SearchRequest(window.description.toPlainText(), 3, tmp_path)
    window.generation_result = GenerationResult(tmp_path, [guide], 1)
    calls = []
    window.launch_search = calls.append
    for profile in ("pose", "strict", "pose"):
        window.search_profile.setCurrentIndex(window.search_profile.findData(profile))
        window.select_generated(guide)
        assert calls[-1].search_profile == profile and calls[-1].reference_image == guide
    assert len(calls) == 3
    assert json.loads(settings.read_text(encoding="utf-8"))["search_profile"] == "pose"
    window.set_busy(True)
    assert not window.search_profile.isEnabled()
    window.set_busy(False)
    assert window.search_profile.isEnabled()
    window.show_results(SearchResult(tmp_path, [], 3, search_profile="pose"))
    window.search_profile.setCurrentIndex(window.search_profile.findData("strict"))
    # Підпис попереднього результату не змінюється від вибору для наступного пошуку.
    assert "Поза й анатомія" in window.subtitle.text()
    window.search_profile.setCurrentIndex(window.search_profile.findData("pose"))
    window.close()
    shiboken6.delete(window)
    reopened = MainWindow()
    assert reopened.search_profile.currentData() == "pose"
    reopened.close()
    shiboken6.delete(reopened)


def test_retry_button_picker_and_worker_use_saved_review_without_new_search(qt_app, tmp_path, monkeypatch):
    from lazyref.ui import PendingReviewsDialog
    from test_retry_analysis import interrupted
    first, _ = interrupted(tmp_path, guide=True)
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    window = MainWindow()
    window.save_settings = lambda: None
    window.output.setText(str(tmp_path / "out"))
    window.show()
    window.show_results(first)
    qt_app.processEvents()
    assert window.retry_row.isVisible() and "6" in window.retry_button.text()
    assert "аналіз не завершено" in window.empty_title.text()
    dialog = PendingReviewsDialog(tmp_path / "out", window)
    assert dialog.items.count() == 1 and not dialog.resume.isEnabled()
    dialog.items.setCurrentRow(0)
    assert dialog.resume.isEnabled()
    dialog.choose_current()
    assert dialog.selected_path == first.retry_path
    shiboken6.delete(dialog)
    calls = []
    def retry(path, cancel, status, progress, **kwargs):
        assert kwargs["reasoning_effort"] == "low"
        calls.append(path)
        return SearchResult(tmp_path, [], 6, search_profile="pose")
    monkeypatch.setattr("lazyref.ui.retry_review", retry)
    monkeypatch.setattr("lazyref.ui.SearchPipeline.run", lambda *a: pytest.fail("No new search"))
    monkeypatch.setattr("lazyref.ui.ReferenceGenerator.run", lambda *a: pytest.fail("No generation"))
    window.description.setPlainText("Зовсім інший опис для нового пошуку")
    window.reasoning.set_value("low")
    window.search_profile.setCurrentIndex(window.search_profile.findData("strict"))
    window.retry_button.click()
    assert not window.library_button.isEnabled() and not window.retry_button.isEnabled()
    assert window.worker.wait(3000)
    qt_app.processEvents()
    assert calls == [first.retry_path]
    assert window.description.toPlainText() == "Чоловік сидить за баром зі спини"
    assert window.search_profile.currentData() == "pose"
    assert not window.retry_row.isVisible() and window.library_button.isEnabled()
    window.close()
    shiboken6.delete(window)


@pytest.mark.parametrize("kind", ["search", "retry"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_long_wait_banner_is_nonblocking_and_can_cancel_both_workers(qt_app, tmp_path, monkeypatch, kind, interrupt):
    from threading import Event
    from lazyref.models import SearchRequest
    from test_retry_analysis import interrupted
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    first, _ = interrupted(tmp_path)
    shown, release = Event(), Event()
    observed = []
    def execute(cancel, wait_changed):
        wait_changed(True)
        shown.set()
        release.wait(3)
        observed.append(cancel.is_set())
        wait_changed(False)
        return SearchResult(tmp_path, [], 6, cancelled=cancel.is_set())
    def run(self, request, cancel, status, progress):
        return execute(cancel, self.wait_changed)
    def retry(path, cancel, status, progress, **kwargs):
        return execute(cancel, kwargs["wait_changed"])
    monkeypatch.setattr("lazyref.ui.SearchPipeline.run", run)
    monkeypatch.setattr("lazyref.ui.retry_review", retry)
    window = MainWindow()
    window.save_settings = lambda: None
    window.show()
    try:
        if kind == "search":
            window.launch_search(SearchRequest("A seated person", 6, tmp_path))
        else:
            window.launch_review(first.retry_path)
        assert shown.wait(2)
        qt_app.processEvents()
        assert window.long_wait_panel.isVisible() and window.long_wait_stop.isEnabled()
        assert window.worker.isRunning() and not window.worker.cancel.is_set()
        assert not window.search_model.isEnabled() and not window.generation_model.isEnabled()
        assert not window.reasoning.isEnabled()
        assert not window.repeat_search_button.isEnabled()
        if interrupt:
            window.long_wait_stop.click()
            assert window.worker.cancel.is_set() and not window.long_wait_panel.isVisible()
        release.set()
        assert window.worker.wait(3000)
        qt_app.processEvents()
        assert observed == [interrupt] and not window.long_wait_panel.isVisible()
        assert window.search.isEnabled()
    finally:
        release.set()
        if window.worker:
            window.worker.wait(3000)
        qt_app.processEvents()
        window.close()
        shiboken6.delete(window)


def test_partial_results_are_distinct_and_user_limit_is_preserved(qt_app, tmp_path, monkeypatch):
    import json
    from test_core import make_asset
    from test_search_quality import rating
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"candidate_limit": 24, "search_revision": 2}), encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", path)
    window = MainWindow()
    window.save_settings = lambda: None
    assert window.settings["candidate_limit"] == 24
    asset = make_asset(tmp_path, "bus")
    asset.assessment = rating(70, failed=("c3",))
    window.show_results(SearchResult(tmp_path, [asset], 10, examined=72))
    assert "Близьких: 0" in window.subtitle.text() and "часткових: 1" in window.subtitle.text()
    assert "Близьких збігів не знайдено" in window.status.text()
    window.close()
    shiboken6.delete(window)


def test_generated_variants_require_selection_and_preserve_separate_counts(qt_app, tmp_path, monkeypatch):
    from lazyref.generation import GenerationResult
    from lazyref.models import SearchRequest
    from lazyref.ui import GeneratedCard
    from test_core import picture
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    window = MainWindow()
    window.save_settings = lambda: None
    window.show()
    window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
    qt_app.processEvents()
    assert window.generation_row.isVisible() and "Створити" in window.search.text()
    window.count.setValue(7)
    window.generation_count.setValue(2)
    assert window.count.value() == 7 and window.generation_count.value() == 2
    guide = tmp_path / "AI.png"
    picture().save(guide)
    window.generation_search_request = SearchRequest("A seated man", 7, tmp_path)
    window.description.setPlainText("A seated man with hands on counter")
    calls = []
    window.launch_search = calls.append
    window.show_generated(GenerationResult(tmp_path, [guide], requested=2, cancelled=True))
    assert not calls and "1 із 2" in window.subtitle.text()
    card = window.gallery.findChild(GeneratedCard)
    assert card is not None
    card.choose.click()
    assert len(calls) == 1 and calls[0].reference_image == guide and calls[0].count == 7
    assert calls[0].provider == "agent"
    assert calls[0].description == "A seated man with hands on counter"
    window.set_busy(True)
    assert not window.generation_count.isEnabled() and not window.variants_button.isEnabled()
    window.set_busy(False)
    window.provider.setCurrentIndex(window.provider.findData("agent"))
    assert not window.generation_row.isVisible()
    window.close()
    shiboken6.delete(window)


def test_generation_worker_finishes_before_choice_is_enabled(qt_app, tmp_path, monkeypatch):
    from lazyref.generation import GenerationResult
    from lazyref.models import SearchRequest
    from test_core import picture
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    guide = tmp_path / "AI.png"
    picture().save(guide)
    calls = []
    def generate(self, request, *args):
        calls.append(request)
        return GenerationResult(tmp_path, [guide], request.count)
    monkeypatch.setattr("lazyref.ui.ReferenceGenerator.run", generate)
    window = MainWindow()
    window.save_settings = lambda: None
    window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
    window.generation_count.setValue(4)
    window.generation_model.setCurrentText("Astra 6.2")
    window.search_model.setCurrentText("Sol 6.2")
    window.reasoning.set_value("xhigh")
    window.description.setPlainText("A seated man")
    window.output.setText(str(tmp_path))
    window.count.setValue(9)
    window.start_search()
    assert window.worker.wait(3000)
    qt_app.processEvents()
    assert window.generation_result and len(window.generation_result.images) == 1
    assert calls[0].count == 4 and window.generation_search_request.count == 9
    assert calls[0].model == "gpt-6.2-astra"
    assert calls[0].reasoning_effort == "xhigh"
    assert window.current_search_request().model == "gpt-6.2-sol"
    assert window.search.isEnabled() and not window.worker.isRunning()
    window.close()
    shiboken6.delete(window)


def test_saved_generation_reopens_all_variants_without_cloud_calls(qt_app, tmp_path, monkeypatch):
    import json
    from lazyref.ui import GeneratedCard
    from test_core import picture
    folder = tmp_path / "saved-guides"
    folder.mkdir()
    images = []
    for number in range(3):
        path = folder / f"{number}.png"
        picture(number).save(path)
        images.append({"file": path.name})
    manifest = folder / "generation.json"
    manifest.write_text(json.dumps({"kind": "ai_generated_pose_guides", "description": "A man sneaking in profile",
        "requested": 2, "images": images, "warnings": ["Three ready images from two calls"]}), encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr("lazyref.ui.QFileDialog.getOpenFileName", lambda *args: (str(manifest), ""))
    monkeypatch.setattr("lazyref.ui.ReferenceGenerator.run", lambda *args: pytest.fail("No new generation expected"))
    window = MainWindow()
    window.save_settings = lambda: None
    window.output.setText(str(tmp_path / "photos"))
    window.settings["model"] = "test-model"
    window.verify_sources.setChecked(False)
    window.count.setValue(7)
    searches = []
    window.launch_search = searches.append
    window.open_saved_generation()
    qt_app.processEvents()
    assert not searches and window.worker is None
    assert window.provider.currentData() == "agent_reference"
    assert window.description.toPlainText() == "A man sneaking in profile"
    assert "доступно: 3" in window.subtitle.text() and "запитано: 2" in window.subtitle.text()
    cards = window.gallery.findChildren(GeneratedCard)
    assert len(cards) == 3
    cards[-1].choose.click()
    assert len(searches) == 1 and searches[0].reference_image == folder / "2.png"
    assert searches[0].count == 7 and searches[0].model == "test-model" and not searches[0].verify_sources
    window.set_busy(True)
    assert not window.open_generation_button.isEnabled()
    window.set_busy(False)
    assert window.open_generation_button.isEnabled()
    window.close()
    shiboken6.delete(window)


@pytest.mark.parametrize("new_model", ["", "another-model"])
def test_changing_model_after_opening_guide_applies_to_next_search(qt_app, tmp_path, monkeypatch, new_model):
    import json
    from lazyref.models import SearchRequest
    from lazyref.generation import GenerationResult
    from test_core import picture
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("lazyref.ui.SETTINGS", settings_path)
    guide = tmp_path / "guide.png"
    picture().save(guide)
    window = MainWindow()
    window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
    window.output.setText(str(tmp_path))
    window.description.setPlainText("A man leaning over the camera")
    window.settings["model"] = "gpt-6-sol"
    old_request = SearchRequest(window.description.toPlainText(), 5, tmp_path, model="gpt-6-sol")
    window.generation_search_request = old_request
    window.generation_result = GenerationResult(tmp_path, [guide], 1)
    searches = []
    window.launch_search = searches.append
    dialog = SettingsDialog(window.settings, window)
    try:
        dialog.model.set_value(new_model)
        values = dialog.values()
        values.update(codex_path="updated-codex.exe", screenshots=False, artistic_nudity=False)
        window.apply_settings(values)
        window.select_generated(guide)
        assert len(searches) == 1
        assert searches[0].model == new_model
        assert searches[0].codex_path == "updated-codex.exe"
        assert not searches[0].screenshots and not searches[0].artistic_nudity
        assert searches[0].reference_image == guide and searches[0].provider == "agent"
        assert old_request.model == "gpt-6-sol"  # Історичний запит не переписуємо.
        assert json.loads(settings_path.read_text(encoding="utf-8"))["model"] == new_model
    finally:
        window.close()
        shiboken6.delete(dialog)
        shiboken6.delete(window)


def test_usage_button_shows_local_counters_and_legacy_reports_have_no_fake_zero(qt_app, tmp_path, monkeypatch):
    from lazyref.usage import UsageLedger
    from lazyref.ui import UsageDialog
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    ledger = UsageLedger(tmp_path / "usage.json", "search")
    call = ledger.begin("assessment", "test", 2)
    call.observe({"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 150}})
    call.finish("completed")
    window = MainWindow()
    window.save_settings = lambda: None
    window.show_results(SearchResult(None, [], 1, usage=ledger.snapshot(), usage_path=ledger.path))
    assert window.usage_button.isEnabled() and "1 150" in window.usage_button.text()
    dialog = UsageDialog(window.usage_data, window.usage_path, window)
    assert "Аналіз фото" in dialog.report.toPlainText()
    assert "не відсоток ліміту" in dialog.report.toPlainText()
    shiboken6.delete(dialog)
    window.set_busy(True)
    assert not window.usage_button.isEnabled()
    window.set_busy(False)
    assert window.usage_button.isEnabled()
    window.show_results(SearchResult(None, [], 1))
    assert not window.usage_button.isEnabled() and window.usage_data == {}
    window.close()
    shiboken6.delete(window)


@pytest.mark.parametrize("old_model", ["gpt-6-sol", "custom-model", ""])
def test_independent_model_choices_migrate_sync_and_persist(qt_app, tmp_path, monkeypatch, old_model):
    import json
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": old_model}), encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", path)
    window = MainWindow()
    assert window.search_model.value() == window.generation_model.value() == old_model
    window.search_model.set_value("gpt-6-astra")
    assert window.generation_model.value() == old_model
    window.generation_model.set_value("gpt-6.1-sol")
    dialog = SettingsDialog(window.settings, window)
    assert dialog.model.value() == "gpt-6-astra"
    assert dialog.generation_model.value() == "gpt-6.1-sol"
    # Вибір стандартної моделі не повинен залишати старий --model.
    dialog.model.set_value("")
    dialog.generation_model.set_value("gpt-6.1-terra")
    window.apply_settings(dialog.values())
    assert window.search_model.value() == ""
    assert window.generation_model.value() == "gpt-6.1-terra"
    assert window.current_search_request().model == ""
    window.close()
    shiboken6.delete(dialog)
    shiboken6.delete(window)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["model"] == "" and saved["generation_model"] == "gpt-6.1-terra"
    reopened = MainWindow()
    assert reopened.search_model.value() == ""
    assert reopened.generation_model.value() == "gpt-6.1-terra"
    reopened.close()
    shiboken6.delete(reopened)


@pytest.mark.parametrize("with_guide,first_outcome", [(True, "error"), (False, "empty"), (True, "cancelled")])
def test_repeat_search_changes_model_keeps_task_and_preserves_old_export(qt_app, tmp_path, monkeypatch, with_guide, first_outcome):
    import hashlib
    import json
    from dataclasses import asdict, replace
    from lazyref.exporter import export_results
    from lazyref.generation import GenerationResult
    from test_core import picture
    monkeypatch.setattr("lazyref.ui.SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr("lazyref.ui.ReferenceGenerator.run", lambda *a: pytest.fail("Do not regenerate selected guide"))
    calls, exports = [], []
    def run(self, request, cancel, status, progress):
        calls.append(request)
        folder, assets = export_results(request, [], {}, {"examined": 0})
        exports.append(folder)
        return SearchResult(folder, assets, request.count, search_profile=request.search_profile,
            search_strategy=request.search_strategy, verify_sources=request.verify_sources,
            error="Temporary failure" if len(calls) == 1 and first_outcome == "error" else "",
            cancelled=len(calls) == 1 and first_outcome == "cancelled")
    monkeypatch.setattr("lazyref.ui.SearchPipeline.run", run)
    window = MainWindow()
    window.show()
    window.output.setText(str(tmp_path / "results"))
    window.description.setPlainText("An elegant man stepping on an object, side view")
    window.count.setValue(5)
    window.verify_sources.setChecked(False)
    window.search_profile.setCurrentIndex(window.search_profile.findData("pose"))
    window.search_model.set_value("gpt-6-sol")
    window.generation_model.set_value("gpt-6.1-terra")
    window.settings.update(reuse_cache=True, discovery_limit=200, detail_limit=40, search_rounds=6)
    guide = tmp_path / "guide.png"
    picture().save(guide)
    before = hashlib.sha256(guide.read_bytes()).hexdigest()
    request = replace(window.current_search_request(), reference_image=guide if with_guide else None)
    if with_guide:
        window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
        window.generation_result = GenerationResult(tmp_path, [guide], 1)
        window.generation_search_request = request
    try:
        window.launch_search(request)
        assert window.worker.wait(3000)
        qt_app.processEvents()
        assert window.repeat_search_button.isVisible() and window.repeat_search_button.isEnabled()
        assert "Модель цієї спроби: Sol 6" in window.subtitle.text()
        original_manifest = (exports[0] / "manifest.json").read_bytes()
        original_request = asdict(calls[0])
        # Повтор бере попередню задачу навіть коли поля форми вже відредаговані.
        window.description.setPlainText("An unrelated new description")
        window.count.setValue(9)
        window.verify_sources.setChecked(True)
        for new_model, effort in (("gpt-6-astra", "high"), ("", "xhigh"), ("", "low")):
            previous_subtitle = window.subtitle.text()
            window.search_model.set_value(new_model)
            window.reasoning.set_value(effort)
            assert window.subtitle.text() == previous_subtitle
            window.repeat_search_button.click()
            assert window.worker.wait(3000)
            qt_app.processEvents()
            latest = calls[-1]
            assert latest.model == new_model and not latest.reuse_cache
            expected = dict(original_request, model=new_model, reasoning_effort=effort, reuse_cache=False)
            assert asdict(latest) == expected
            assert window.description.toPlainText() == request.description and window.count.value() == 5
            assert not window.verify_sources.isChecked()
            assert window.generation_model.value() == "gpt-6.1-terra"
            manifest = json.loads((exports[-1] / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["requested_model"] == new_model
            assert manifest["requested_reasoning_effort"] == effort
            assert manifest["search_budgets"]["reuse_cache"] is False
        assert len(calls) == 4 and len(set(exports)) == 4
        assert asdict(calls[0]) == original_request
        assert (exports[0] / "manifest.json").read_bytes() == original_manifest
        assert hashlib.sha256(guide.read_bytes()).hexdigest() == before
        assert "Модель цієї спроби: З налаштувань Codex" in window.subtitle.text()
    finally:
        if window.worker:
            window.worker.wait(3000)
        qt_app.processEvents()
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("previous", [None, "", "invalid", ["high"], "high"])
def test_reasoning_picker_defaults_to_medium_syncs_and_persists(qt_app, tmp_path, monkeypatch, previous):
    import json
    path = tmp_path / "settings.json"
    data = {"model": "gpt-6-astra"}
    if previous is not None:
        data["reasoning_effort"] = previous
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr("lazyref.ui.SETTINGS", path)
    window = MainWindow()
    expected = "high" if previous == "high" else "medium"
    assert window.reasoning.value() == expected
    assert window.current_search_request().reasoning_effort == expected
    assert [window.reasoning.itemText(i) for i in range(window.reasoning.count())] == ["Low", "Medium", "High", "Extra high"]
    window.reasoning.set_value("xhigh")
    dialog = SettingsDialog(window.settings, window)
    assert dialog.reasoning.currentText() == "Extra high" and dialog.values()["reasoning_effort"] == "xhigh"
    dialog.reasoning.set_value("low")
    window.apply_settings(dialog.values())
    assert window.reasoning.value() == window.current_search_request().reasoning_effort == "low"
    window.close()
    shiboken6.delete(dialog)
    shiboken6.delete(window)
    reopened = MainWindow()
    assert reopened.reasoning.value() == "low"
    assert json.loads(path.read_text(encoding="utf-8"))["reasoning_effort"] == "low"
    reopened.close()
    shiboken6.delete(reopened)


