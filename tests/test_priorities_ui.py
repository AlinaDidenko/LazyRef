"""Редактор умов, очікування QThread, імпорт і компактні результати — локальні сценарії UI."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from copy import deepcopy

from PIL import Image
import pytest
import shiboken6
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from lazyref import ui
from lazyref.custom_priorities import make_plan
from lazyref.models import Cancelled, SearchRequest, SearchResult
from lazyref.priorities_ui import PrioritiesDialog
from test_custom_priorities import choices, CustomAgent
from test_priorities import PLAN
from test_core import make_asset
from test_search_quality import rating


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    win = ui.MainWindow()
    win.output.setText(str(tmp_path / "references"))
    yield win
    if win.worker and win.worker.isRunning():
        win.worker.cancel.set()
        win.worker.wait(3000)
    win.close()
    shiboken6.delete(win)


def test_editor_changes_words_clears_stale_keywords_and_validates_required(qt_app):
    dialog = PrioritiesDialog(deepcopy(PLAN))
    dialog.rows[0]["text"].setText("Жінка або чоловік")
    dialog.rows[0]["priority"].setCurrentIndex(1)
    dialog.remove_row(dialog.rows[-1])
    dialog.add_button.click()
    dialog.rows[-1]["text"].setText("Пальці руки видно повністю")
    rows = dialog.values()
    assert rows[0]["priority"] == "preferred" and rows[0]["keywords"] == []
    assert rows[-1]["label"] == "Пальці руки видно повністю"
    assert [r["id"] for r in rows] == [f"c{i+1}" for i in range(len(rows))]
    for row in dialog.rows:
        row["priority"].setCurrentIndex(2)
    dialog.confirm()
    assert dialog.result() != QDialog.DialogCode.Accepted and "хоча б одну" in dialog.error.text()
    dialog.rows[-1]["priority"].setCurrentIndex(0)
    dialog.confirm()
    assert dialog.result() == QDialog.DialogCode.Accepted
    shiboken6.delete(dialog)


@pytest.mark.parametrize("approve", [True, False])
def test_worker_waits_for_editor_and_repeat_keeps_approved_conditions(window, monkeypatch, tmp_path, approve):
    from lazyref import priorities_ui
    client = CustomAgent()
    results = []
    class LocalPipeline:
        def __init__(self, *, plan_editor, **kwargs):
            self.editor = plan_editor
        def run(self, request, cancel, *args):
            try:
                make_plan(client, request, cancel, self.editor)
                result = SearchResult(None, [], request.count, search_profile="custom")
            except Cancelled:
                result = SearchResult(None, [], request.count, search_profile="custom", cancelled=True)
            results.append(result)
            return result
    monkeypatch.setattr(ui, "SearchPipeline", LocalPipeline)
    class AutoDialog(PrioritiesDialog):
        def exec(self):
            assert client.plan_calls == 1 and not client.searches and not results
            assert window.worker.isRunning() and not window.search.isEnabled()
            for row, selected in zip(self.rows, choices()):
                row["priority"].setCurrentIndex(row["priority"].findData(selected["priority"]))
            QTimer.singleShot(0, self.confirm if approve else self.reject)
            return super().exec()
    monkeypatch.setattr(priorities_ui, "PrioritiesDialog", AutoDialog)
    window.search_profile.setCurrentIndex(window.search_profile.findData("custom"))
    window.description.setPlainText("Чоловік за барною стійкою зі спини")
    window.start_search()
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    window.worker.finished.connect(loop.quit)
    timer.start(4000)
    loop.exec()
    timer.stop()
    assert not window.worker.isRunning() and results
    assert results[0].cancelled is not approve
    assert window.priorities_dialog is None
    if approve:
        assert window.last_search_request.custom_criteria == choices()
        calls = []
        monkeypatch.setattr(window, "launch_search", calls.append)
        window.settings["model"] = "gpt-6.1-sol"
        window.repeat_last_search()
        assert calls[0].custom_criteria == choices() and calls[0].model == "gpt-6.1-sol"
        assert not calls[0].reuse_cache and client.plan_calls == 1
    else:
        assert window.last_search_request.custom_criteria is None


def test_import_button_copies_then_prepares_search_without_generation(window, monkeypatch, tmp_path):
    path = tmp_path / "Моє фото.png"
    Image.new("RGB", (220, 180)).save(path)
    calls = []
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(window, "launch_search", calls.append)
    monkeypatch.setattr(window, "start_generation", lambda *a: pytest.fail("Do not generate"))
    window.description.setPlainText("Взуття з таким самим ракурсом")
    window.provider.setCurrentIndex(window.provider.findData("image_reference"))
    assert not window.import_image_button.isHidden()
    window.import_image_button.click()
    selected = window.selected_library_image
    assert selected.path != path and selected.path.read_bytes() == path.read_bytes()
    assert not calls and window.description.toPlainText() == selected.description
    path.unlink()
    window.start_search()
    assert calls[0].reference_image == selected.path and calls[0].reference_mode == "visual"
    window.set_busy(True)
    assert not window.import_image_button.isEnabled()
    window.set_busy(False)


def test_cancelling_file_picker_preserves_selected_image(window, monkeypatch, tmp_path):
    from lazyref.library import image_record
    path = tmp_path / "selected.png"
    Image.new("RGB", (100, 100)).save(path)
    selected = image_record(path, tmp_path, "reference", description="Черевики збоку")
    window.choose_library_image(selected)
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    window.import_image()
    assert window.selected_library_image is selected
    assert not (tmp_path / "references").exists()


def test_result_info_auto_collapses_and_reveals_stats_and_guide_without_hiding_model(window, qt_app, tmp_path):
    asset = make_asset(tmp_path, "photo")
    asset.assessment = rating()
    window.last_search_request = SearchRequest("Чоловік за барною стійкою", 5, tmp_path)
    window.guide_banner.show()
    window.show_results(SearchResult(tmp_path, [asset], 5, examined=20))
    window.show()
    qt_app.processEvents()
    assert window.info_button.isVisible() and not window.info_button.isChecked()
    assert not window.info_content.isVisible() and not window.guide_banner.isVisible()
    assert window.scroll.isVisible() and window.search_model.isVisible() and window.repeat_search_button.isVisible()
    collapsed_height = window.scroll.height()
    window.info_button.click()
    qt_app.processEvents()
    assert window.info_content.isVisible() and window.guide_banner.isVisible()
    assert "оцінено: 20" in window.subtitle.text()
    assert window.scroll.height() < collapsed_height
    window.info_button.click()
    qt_app.processEvents()
    assert window.scroll.height() == collapsed_height
