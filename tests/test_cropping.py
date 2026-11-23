"""Обрізання лише локальних копій; жодних викликів Codex або мережі."""

import hashlib
import json
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image, ImageChops, ImageOps
import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from lazyref.cropping import crop_box, load_crop_source, save_crop
from lazyref.crop_ui import CropCanvas, CropDialog
from lazyref.library import LibraryScan, image_record, scan_library
from lazyref.library_ui import LibraryDialog, ThumbnailCache


def patterned_image(path, *, mode="RGB"):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new(mode, (320, 240))
    for x in range(320):
        for y in range(240):
            pixel = (x % 256, y, (x + y) % 256)
            image.putpixel((x, y), pixel + (x % 256,) if mode == "RGBA" else pixel)
    image.save(path)
    image.close()
    return path


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.mark.parametrize("kind", ["generated", "reference"])
@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_crop_is_exact_separate_copy_and_is_indexed_in_same_gallery(tmp_path, kind, mode):
    source = patterned_image(tmp_path / "archive/old/source.png", mode=mode)
    original = source.read_bytes()
    item = image_record(source, tmp_path / "archive", kind, description="Whole person", archived=True)
    result = save_crop(item, (40, 60, 200, 180), tmp_path, "Boots from the side")
    second = save_crop(item, (40, 60, 200, 180), tmp_path)
    assert source.read_bytes() == original
    assert result.path != second.path and not result.archived
    with Image.open(source) as image, Image.open(result.path) as cropped:
        assert cropped.size == (160, 120) and cropped.mode == mode
        assert not ImageChops.difference(cropped, image.crop((40, 60, 200, 180))).getbbox()
    manifest = json.loads((result.path.parent / "crop.json").read_text(encoding="utf-8"))
    assert manifest["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["crop_box"] == [40, 60, 200, 180]
    scanned = {i.path: i for i in scan_library(tmp_path).images}
    assert scanned[result.path].kind == kind and scanned[result.path].cropped
    assert scanned[result.path].description == "Boots from the side"
    assert scanned[second.path].description == ""  # Не успадковуємо вимоги до всього тіла.


def test_crop_coordinates_follow_visible_exif_orientation(tmp_path):
    source = patterned_image(tmp_path / "source.png")
    with Image.open(source) as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
    item = image_record(source, tmp_path, "generated")
    with load_crop_source(item) as oriented:
        assert oriented.size == (240, 320)
    result = save_crop(item, (30, 70, 190, 250), tmp_path)
    with Image.open(source) as image, Image.open(result.path) as copied:
        expected = ImageOps.exif_transpose(image).crop((30, 70, 190, 250))
        assert copied.size == (160, 180)
        assert not ImageChops.difference(copied, expected).getbbox()
        assert not copied.getexif().get(274)


@pytest.mark.parametrize("box", [None, (0, 0, 20, 20), (-1, 0, 100, 100), (0, 0, 321, 100),
                                (100, 10, 50, 80), (0.5, 0, 60, 60), (False, 0, 60, 60)])
def test_invalid_crop_creates_no_output(tmp_path, box):
    source = patterned_image(tmp_path / "source.png")
    item = image_record(source, tmp_path, "generated")
    with pytest.raises(ValueError):
        save_crop(item, box, tmp_path)
    assert not (tmp_path / "generated_references").exists()
    assert source.is_file()


def test_changed_original_is_rejected_without_writing(tmp_path):
    source = patterned_image(tmp_path / "source.png")
    item = image_record(source, tmp_path, "generated")
    source.write_bytes(b"different file")
    with pytest.raises(ValueError):
        save_crop(item, (0, 0, 100, 100), tmp_path)
    assert source.read_bytes() == b"different file"
    assert not (tmp_path / "generated_references").exists()


def test_failed_manifest_write_removes_only_new_copy(tmp_path, monkeypatch):
    source = patterned_image(tmp_path / "source.png")
    original = source.read_bytes()
    item = image_record(source, tmp_path, "generated")
    def fail(*args, **kwargs):
        raise OSError("Disk full")
    monkeypatch.setattr("lazyref.cropping.json.dump", fail)
    with pytest.raises(OSError, match="Disk full"):
        save_crop(item, (0, 0, 100, 100), tmp_path)
    assert source.read_bytes() == original
    assert list((tmp_path / "generated_references").iterdir()) == []


def screen_point(canvas, x, y):
    return canvas.screen_rect(QRectF(x, y, 0, 0)).topLeft().toPoint()


def drag(canvas, start, end):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)


def test_canvas_draw_move_resize_and_widget_scale_preserve_pixel_coordinates(qt_app):
    with Image.new("RGB", (800, 600)) as pixels:
        canvas = CropCanvas(pixels)
    canvas.resize(824, 624)  # Масштаб 1:1 + відступи.
    canvas.show()
    qt_app.processEvents()
    drag(canvas, screen_point(canvas, 100, 100), screen_point(canvas, 400, 300))
    assert canvas.box() == (100, 100, 400, 300)
    drag(canvas, screen_point(canvas, 250, 200), screen_point(canvas, 270, 230))
    assert canvas.box() == (120, 130, 420, 330)
    drag(canvas, canvas.handles()["br"].toPoint(), screen_point(canvas, 450, 370))
    assert canvas.box() == (120, 130, 450, 370)
    canvas.resize(424, 324)
    qt_app.processEvents()
    assert canvas.box() == (120, 130, 450, 370)
    drag(canvas, screen_point(canvas, 285, 250), screen_point(canvas, 800, 600))
    box = crop_box(canvas.box(), canvas.image_size)
    assert (box[2] - box[0], box[3] - box[1]) == (330, 240)
    assert box[2:] == (800, 600)
    canvas.close()
    shiboken6.delete(canvas)


def test_dialog_cancel_writes_nothing_and_valid_selection_controls_save(qt_app, tmp_path):
    item = image_record(patterned_image(tmp_path / "source.png"), tmp_path, "generated")
    dialog = CropDialog(item, tmp_path)
    assert not dialog.save_button.isEnabled() and not dialog.search_button.isEnabled()
    dialog.canvas.selection = QRectF(10, 20, 31, 50)
    dialog.selection_changed()
    assert not dialog.save_button.isEnabled()
    dialog.canvas.selection = QRectF(10, 20, 32, 50)
    dialog.selection_changed()
    assert dialog.save_button.isEnabled()
    dialog.reject()
    assert dialog.saved_item is None and not (tmp_path / "generated_references").exists()
    shiboken6.delete(dialog)


@pytest.mark.parametrize("search", [False, True])
def test_real_modal_crop_refreshes_gallery_and_can_select_new_copy(qt_app, tmp_path, search):
    item = image_record(patterned_image(tmp_path / "archive/source.png"), tmp_path / "archive",
                        "generated", archived=True)
    gallery = LibraryDialog(tmp_path, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    gallery.tabs.setCurrentIndex(1)
    gallery.scope.setCurrentIndex(gallery.scope.findData("archive"))
    gallery.show()
    qt_app.processEvents()
    card = gallery.generated.grid.itemAt(0).widget()
    def save():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, CropDialog)
        dialog.canvas.selection = QRectF(60, 50, 140, 100)
        dialog.selection_changed()
        dialog.description.setText("Boots from the side")
        QTest.mouseClick(dialog.search_button if search else dialog.save_button, Qt.MouseButton.LeftButton)
    QTimer.singleShot(20, save)
    card.file_actions.crop()
    qt_app.processEvents()
    copies = [i for i in gallery.images if i.cropped]
    assert len(copies) == 1 and copies[0].path.is_file()
    assert item.path.is_file() and len(gallery.generated.images) == 2
    assert gallery.scope.currentData() == "all"
    assert gallery.selected_image == (copies[0] if search else None)
    if search:
        assert gallery.result() == QDialog.DialogCode.Accepted
    gallery.close()
    shiboken6.delete(gallery)


@pytest.mark.parametrize("search", [False, True, None])
def test_pending_gallery_filter_cannot_destroy_modal_crop_or_result_handler(qt_app, tmp_path, search):
    item = image_record(patterned_image(tmp_path / "source.png"), tmp_path,
                        "reference", description="photo")
    gallery = LibraryDialog(tmp_path, autoload=False)
    gallery.set_scan(LibraryScan([item]))
    gallery.show()
    qt_app.processEvents()
    card = gallery.references.grid.itemAt(0).widget()
    actions = card.file_actions
    observed = []
    errors = []
    gallery.search.setText("photo")  # Starts the real 200 ms debounce timer.

    def finish():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, CropDialog) and shiboken6.isValid(dialog)
            assert not shiboken6.isValid(card), "The filter must actually delete the original card"
            assert shiboken6.isValid(actions), "The result emitter must survive its original card"
            observed.append(True)
            if search is None:
                dialog.reject()
            else:
                dialog.canvas.selection = QRectF(60, 50, 140, 100)
                dialog.selection_changed()
                dialog.description.setText("Saved after filter")
                QTest.mouseClick(dialog.search_button if search else dialog.save_button, Qt.MouseButton.LeftButton)
        except BaseException as exc:
            errors.append(exc)
            if isinstance(dialog, QDialog) and shiboken6.isValid(dialog):
                dialog.reject()

    timer = QTimer(gallery)
    timer.setSingleShot(True)
    timer.timeout.connect(finish)
    timer.start(350)
    actions.crop()
    timer.stop()
    qt_app.processEvents()
    qt_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not errors and observed == [True]
    assert not shiboken6.isValid(actions)
    copies = [i for i in gallery.images if i.cropped]
    if search is None:
        assert not copies and not (tmp_path / "references").exists()
    else:
        assert len(copies) == 1 and copies[0].path.is_file()
        assert gallery.selected_image == (copies[0] if search else None)
        if search:
            assert gallery.result() == QDialog.DialogCode.Accepted
    assert item.path.is_file()
    gallery.close()
    shiboken6.delete(gallery)


@pytest.mark.parametrize("orientation", [1, 6])
def test_imported_16bit_png_crop_keeps_all_samples_and_exif_coordinates(tmp_path, orientation):
    from lazyref.importing import import_reference
    source = tmp_path / "source.png"
    with Image.new("I;16", (64, 96)) as image:
        image.putdata([(x * 1021 + y * 443) % 65536 for y in range(96) for x in range(64)])
        exif = Image.Exif()
        exif[274] = orientation
        image.save(source, exif=exif)
    original = source.read_bytes()
    item = import_reference(source, tmp_path / "workspace")
    result = save_crop(item, (8, 16, 56, 64), tmp_path / "workspace")
    assert source.read_bytes() == item.path.read_bytes() == original
    with Image.open(source) as image, Image.open(result.path) as copied:
        with ImageOps.exif_transpose(image) as oriented:
            expected = oriented.crop((8, 16, 56, 64))
            assert copied.mode == "I;16" and copied.size == (48, 48)
            assert copied.tobytes() == expected.tobytes()
            assert copied.getextrema()[1] > 255
            assert not copied.getexif().get(274)


def test_16bit_crop_canvas_and_gallery_preview_scale_brightness_without_clipping(qt_app, tmp_path):
    source = tmp_path / "source.png"
    with Image.new("I;16", (64, 64)) as image:
        image.putdata([1000 if x < 32 else 40000 for y in range(64) for x in range(64)])
        image.save(source)
    item = image_record(source, tmp_path, "reference")
    result = save_crop(item, (16, 0, 48, 32), tmp_path)
    with load_crop_source(result) as pixels:
        canvas = CropCanvas(pixels)
        assert pixels.getpixel((0, 0)) == 1000 and pixels.getpixel((31, 0)) == 40000
    for preview in (canvas.pixmap, ThumbnailCache().pixmap(result)):
        display = preview.toImage()
        assert display.pixelColor(0, 0).getRgb() == (3, 3, 3, 255)
        assert display.pixelColor(31, 0).getRgb() == (155, 155, 155, 255)
    shiboken6.delete(canvas)


def test_16bit_png_transparent_sample_survives_crop_and_preview(qt_app, tmp_path):
    source = tmp_path / "source.png"
    with Image.new("I;16", (64, 64)) as image:
        image.putdata([1000 if x < 32 else 40000 for y in range(64) for x in range(64)])
        image.save(source, transparency=1000)
    item = image_record(source, tmp_path, "reference")
    result = save_crop(item, (16, 0, 48, 32), tmp_path)
    with Image.open(result.path) as copied:
        assert copied.mode == "I;16" and copied.info["transparency"] == 1000
        assert copied.getpixel((0, 0)) == 1000 and copied.getpixel((31, 0)) == 40000
        canvas = CropCanvas(copied)
        display = canvas.pixmap.toImage()
        assert display.pixelColor(0, 0).alpha() == 0
        assert display.pixelColor(31, 0).getRgb() == (155, 155, 155, 255)
    shiboken6.delete(canvas)
