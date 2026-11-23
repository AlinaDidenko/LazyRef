"""Обране і добірки в поточних картках та галереях — без моделей, мережі й копіювання."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from lazyref import library_ui
from lazyref.library import LibraryScan, image_record
from lazyref.library_ui import LibraryDialog
from lazyref.organization_ui import CollectionsDialog, OrganizationController
from test_library import picture


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def items(tmp_path):
    return [image_record(picture(tmp_path / path), tmp_path, kind, description=description, archived=archived)
        for path, kind, description, archived in [
            ("references/a.png", "reference", "Руки людини", False),
            ("archive/b.png", "reference", "Руки в перспективі", True),
            ("generated_references/c.png", "generated", "ШІ черевики", False)]]


def test_star_and_combined_collection_favorite_text_archive_filters(qt_app, tmp_path):
    rows = items(tmp_path)
    gallery = LibraryDialog(tmp_path, autoload=False)
    store = gallery.organizer.store
    hands, project = store.create_collection("Руки"), store.create_collection("Проєкт")
    store.set_collections(rows[0], {hands, project})
    store.set_collections(rows[1], {hands})
    store.set_collections(rows[2], {project})
    store.set_favorite(rows[1], True)
    store.set_favorite(rows[2], True)
    gallery.organizer.reload()
    gallery.organization_changed()
    gallery.set_scan(LibraryScan(rows))
    gallery.show()
    qt_app.processEvents()
    gallery.favorites.click()
    assert gallery.references.filtered == [rows[1]] and gallery.generated.filtered == [rows[2]]
    gallery.collection_filter.setCurrentIndex(gallery.collection_filter.findData(hands))
    assert gallery.references.filtered == [rows[1]] and not gallery.generated.filtered
    gallery.scope.setCurrentIndex(gallery.scope.findData("archive"))
    gallery.search.setText("перспективі")
    gallery.filter_changed()
    assert gallery.references.filtered == [rows[1]]
    card = gallery.references.grid.itemAt(0).widget()
    assert card.favorite_button.isChecked()
    assert "Руки" in card.collection_names.text()
    card.favorite_button.click()
    assert not gallery.references.filtered and not store.snapshot().favorite(rows[1])
    assert rows[1].path.exists()
    gallery.close()
    shiboken6.delete(gallery)


def test_membership_dialog_saves_multiple_links_and_cancel_preserves_old_links(qt_app, tmp_path):
    item = items(tmp_path)[0]
    organizer = OrganizationController(tmp_path)
    c1, c2 = organizer.store.create_collection("Руки"), organizer.store.create_collection("Проєкт")
    organizer.store.set_collections(item, {c1})
    organizer.reload()
    dialog = CollectionsDialog(organizer, item=item)
    for i in range(dialog.list.count()):
        dialog.list.item(i).setCheckState(Qt.CheckState.Checked)
    dialog.save_memberships()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert organizer.state.assigned(item) == {c1, c2}
    shiboken6.delete(dialog)
    dialog = CollectionsDialog(organizer, item=item)
    for i in range(dialog.list.count()):
        dialog.list.item(i).setCheckState(Qt.CheckState.Unchecked)
    dialog.reject()
    assert organizer.store.snapshot().assigned(item) == {c1, c2}
    shiboken6.delete(dialog)
    shiboken6.delete(organizer)


def test_manager_create_rename_delete_does_not_touch_star_or_photos(qt_app, tmp_path):
    item = items(tmp_path)[0]
    original = item.path.read_bytes()
    organizer = OrganizationController(tmp_path)
    organizer.store.set_favorite(item, True)
    organizer.reload()
    dialog = CollectionsDialog(organizer)
    dialog.name.setText("Проєкт")
    dialog.create.click()
    row = dialog.list.item(0)
    dialog.list.setCurrentItem(row)
    key = row.data(Qt.ItemDataRole.UserRole)
    organizer.store.set_collections(item, {key})
    dialog.name.setText("Персонаж для коміксу")
    dialog.rename.click()
    assert organizer.state.collections[key] == "Персонаж для коміксу"
    dialog.remove.click()
    assert not organizer.state.collections and organizer.state.favorite(item)
    assert not organizer.state.assigned(item) and item.path.read_bytes() == original
    dialog.close()
    shiboken6.delete(dialog)
    shiboken6.delete(organizer)


def test_real_modal_create_from_card_survives_gallery_refresh(qt_app, tmp_path):
    item = items(tmp_path)[0]
    gallery = LibraryDialog(tmp_path, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    gallery.show()
    qt_app.processEvents()
    card = gallery.references.grid.itemAt(0).widget()
    observations = []
    def edit():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, CollectionsDialog)
        dialog.name.setText("Черевики")
        dialog.create.click()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        observations.append(shiboken6.isValid(dialog))
        assert observations[-1]  # Оновлення картки не повинно закривати модальне вікно.
        dialog.save_memberships()
    QTimer.singleShot(10, edit)
    card.file_actions.edit_collections()
    assert observations == [True]
    assert gallery.organizer.state.names(item) == ["Черевики"]
    assert gallery.references.grid.itemAt(0).widget().collection_names.text() == "Добірки: Черевики"
    gallery.close()
    shiboken6.delete(gallery)


def test_star_syncs_between_current_generation_and_gallery(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    from lazyref.generation import GenerationResult
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    window = ui.MainWindow()
    item = items(tmp_path)[2]
    window.show_generated(GenerationResult(item.path.parent, [item.path], 1))
    card = window.gallery.findChild(ui.GeneratedCard)
    card.favorite_button.click()
    assert window.organizer.state.favorite(item)
    gallery = LibraryDialog(tmp_path, parent=window, organizer=window.organizer, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    gallery.generated.grid.itemAt(0).widget().favorite_button.click()
    assert not card.favorite_button.isChecked()
    window.set_busy(True)
    assert not card.favorite_button.isEnabled()
    window.set_busy(False)
    assert card.favorite_button.isEnabled()
    gallery.close()
    window.close()
    shiboken6.delete(gallery)
    shiboken6.delete(window)


def test_write_failure_keeps_star_in_actual_saved_state(qt_app, tmp_path, monkeypatch):
    item = items(tmp_path)[0]
    gallery = LibraryDialog(tmp_path, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    errors = []
    monkeypatch.setattr(library_ui, "show_error", lambda parent, error: errors.append(str(error)))
    def fail(*args):
        raise OSError("Disk full")
    monkeypatch.setattr(gallery.organizer.store, "set_favorite", fail)
    gallery.references.grid.itemAt(0).widget().favorite_button.click()
    assert errors == ["Disk full"] and not gallery.organizer.state.favorite(item)
    assert not gallery.references.grid.itemAt(0).widget().favorite_button.isChecked()
    gallery.close()
    shiboken6.delete(gallery)


def test_corrupt_metadata_does_not_hide_photos_or_erase_data(qt_app, tmp_path):
    item = items(tmp_path)[0]
    metadata = tmp_path / ".lazyref/library.sqlite3"
    metadata.parent.mkdir()
    metadata.write_bytes(b"corrupt user data")
    gallery = LibraryDialog(tmp_path, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    assert gallery.references.filtered == [item]
    assert not gallery.references.grid.itemAt(0).widget().favorite_button.isEnabled()
    assert not gallery.manage_collections.isEnabled() and "недоступні" in gallery.status.text()
    assert metadata.read_bytes() == b"corrupt user data"
    gallery.close()
    shiboken6.delete(gallery)


def test_invalid_local_import_reports_error_instead_of_crashing(qt_app, tmp_path, monkeypatch):
    from lazyref import ui
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "SETTINGS", tmp_path / "settings.json")
    path = tmp_path / "bad.png"
    path.write_bytes(b"broken")
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    errors = []
    monkeypatch.setattr(ui, "show_error", lambda parent, error: errors.append(str(error)))
    window = ui.MainWindow()
    window.import_image()
    assert errors and window.selected_library_image is None
    window.close()
    shiboken6.delete(window)
