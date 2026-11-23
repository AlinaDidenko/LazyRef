import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import pytest
import shiboken6
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from lazyref import library_ui
from lazyref.library import LibraryScan, image_record, scan_library
from lazyref.library_ui import ImageFileActions, LibraryCard, LibraryDialog, trash_image
from test_library import picture, reference


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def test_tabs_filter_pagination_and_safe_context_menu(qt_app, tmp_path):
    images = [image_record(picture(tmp_path / f"ref{i}.png"), tmp_path, "reference",
                           description="A pointing person", archived=i == 0) for i in range(26)]
    images.append(image_record(picture(tmp_path / "guide.png"), tmp_path, "generated", description="AI pose"))
    dialog = LibraryDialog(tmp_path, autoload=False)
    dialog.set_scan(LibraryScan(images))
    dialog.show()
    qt_app.processEvents()
    page = dialog.references
    assert dialog.tabs.count() == 2 and len(dialog.generated.images) == 1
    assert page.grid.count() == 24 and page.next.isEnabled()
    page.navigate(1)
    assert page.grid.count() == 2 and not page.next.isEnabled()
    card = page.grid.itemAt(0).widget()
    menu = card.file_actions.menu(card)
    assert [a.text() for a in menu.actions() if not a.isSeparator()] == [
        "Шукати референси за цим зображенням", "Обрізати й зберегти копію", "Додати в обране", "Додати до добірок…", "Знайти зображення", "Видалити"]
    menu.deleteLater()
    dialog.scope.setCurrentIndex(dialog.scope.findData("archive"))
    assert page.grid.count() == 1 and page.page == 0
    dialog.search.setText("no match")
    dialog.filter_changed()
    assert page.grid.count() == 0 and page.empty.isVisible()
    dialog.close()
    shiboken6.delete(dialog)


@pytest.mark.parametrize("tuple_result", [True, False])
def test_delete_moves_only_selected_file_and_refresh_does_not_restore_it(qt_app, tmp_path, monkeypatch, tuple_result):
    first = reference(tmp_path / "references/one")
    second = reference(tmp_path / "archive/two")
    original = first.read_bytes()
    calls = []
    trash = tmp_path / "trash.png"
    def move_to_trash(filename):
        calls.append(filename)
        Path(filename).rename(trash)
        return (True, str(trash)) if tuple_result else True
    monkeypatch.setattr(library_ui.QFile, "moveToTrash", move_to_trash)
    dialog = LibraryDialog(tmp_path, autoload=False)
    dialog.set_scan(scan_library(tmp_path))
    card = next(dialog.references.grid.itemAt(i).widget() for i in range(dialog.references.grid.count())
                if dialog.references.grid.itemAt(i).widget().item.path == first)
    card.file_actions.remove()
    assert calls == [str(first)] and trash.read_bytes() == original
    assert second.exists() and len(dialog.references.images) == 1
    dialog.set_scan(scan_library(tmp_path))
    assert {i.path for i in dialog.images} == {second}
    dialog.close()
    shiboken6.delete(dialog)


@pytest.mark.parametrize("result", [(False, ""), False])
def test_trash_failure_never_falls_back_to_permanent_delete(qt_app, tmp_path, monkeypatch, result):
    path = picture(tmp_path / "image.png")
    record = image_record(path, tmp_path, "reference")
    monkeypatch.setattr(library_ui.QFile, "moveToTrash", lambda *a: result)
    with pytest.raises(OSError, match="Кошика"):
        trash_image(record)
    assert path.is_file()


def test_busy_or_stale_card_cannot_delete(qt_app, tmp_path, monkeypatch):
    path = picture(tmp_path / "image.png")
    record = image_record(path, tmp_path, "reference")
    dialog = LibraryDialog(tmp_path, autoload=False)
    actions = ImageFileActions(record, dialog, can_delete=lambda: False)
    monkeypatch.setattr(library_ui.QFile, "moveToTrash", lambda *a: pytest.fail("Must not remove"))
    actions.remove()
    assert not actions.menu(dialog).actions()[-1].isEnabled()
    path.write_bytes(b"replacement")
    actions.can_delete = lambda: True
    errors = []
    monkeypatch.setattr(library_ui, "show_error", lambda parent, error: errors.append(str(error)))
    actions.remove()
    assert errors and path.exists()
    dialog.close()
    shiboken6.delete(dialog)


@pytest.mark.skipif(os.name != "nt", reason="Windows Explorer")
def test_reveal_selects_exact_file_with_spaces_without_shell(qt_app, tmp_path, monkeypatch):
    path = picture(tmp_path / "my folder/image space.png")
    record = image_record(path, tmp_path, "reference")
    calls = []
    monkeypatch.setattr(library_ui.QProcess, "startDetached", lambda *args: calls.append(args) or (True, 123))
    library_ui.reveal_image(record)
    assert calls[0][1] == ["/select,", str(path)]
    assert Path(calls[0][0]).name == "explorer.exe"


def test_background_scan_can_close_without_destroying_running_thread(qt_app, tmp_path):
    reference(tmp_path / "references/one")
    dialog = LibraryDialog(tmp_path, autoload=False)
    dialog.refresh()
    dialog.reject()
    for _ in range(100):
        if dialog.worker is None:
            break
        QTest.qWait(10)
    assert dialog.worker is None and dialog.closing
    shiboken6.delete(dialog)


def test_main_window_has_library_and_current_image_actions(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    from lazyref.generation import GenerationResult
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    window = ui.MainWindow()
    window.output.setText(str(tmp_path / "results"))
    guide = picture(tmp_path / "guide.png")
    window.show_generated(GenerationResult(tmp_path, [guide], 1))
    card = window.gallery.findChild(ui.GeneratedCard)
    assert card.file_actions and window.library_button.isEnabled()
    window.set_busy(True)
    assert not window.library_button.isEnabled() and not card.file_actions.can_delete()
    window.set_busy(False)
    window.image_deleted(str(guide))
    assert not window.generation_result.images
    window.save_settings()
    assert str(tmp_path / "results") in window.settings["library_roots"]
    window.close()
    shiboken6.delete(window)
