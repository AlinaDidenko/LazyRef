"""Large 16-bit previews resize safely while crop samples remain untouched."""

import hashlib
import json

import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication, QDialog

from lazyref.crop_ui import CropCanvas, CropDialog
from lazyref.cropping import load_crop_source
from lazyref.library import image_record
from lazyref.library_ui import ThumbnailCache


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def gray16(path, size, *, orientation=1, transparent=False):
    with Image.new("I;16", size, 1000) as pixels:
        with Image.new("I;16", (size[0] - size[0] // 2, size[1]), 40000) as bright:
            pixels.paste(bright, (size[0] // 2, 0))
        exif = Image.Exif()
        exif[274] = orientation
        kwargs = {"transparency": 1000} if transparent else {}
        pixels.save(path, exif=exif, **kwargs)
    return image_record(path, path.parent, "reference")


def assert_half_tones(pixmap, *, rotated=False, transparent=False):
    display = pixmap.toImage()
    assert not display.isNull()
    if rotated:
        first = display.pixelColor(display.width() // 2, display.height() // 4)
        second = display.pixelColor(display.width() // 2, 3 * display.height() // 4)
    else:
        first = display.pixelColor(display.width() // 4, display.height() // 2)
        second = display.pixelColor(3 * display.width() // 4, display.height() // 2)
    if transparent:
        assert first.alpha() == 0
    else:
        assert first.getRgb() == (3, 3, 3, 255)
    assert second.getRgb() == (155, 155, 155, 255)


@pytest.mark.parametrize("orientation", [1, 6])
@pytest.mark.parametrize("transparent", [False, True])
def test_large_gallery_preview_preserves_tones_orientation_and_transparency(
        qt_app, tmp_path, orientation, transparent):
    item = gray16(tmp_path / "gallery.png", (2048, 1536),
                  orientation=orientation, transparent=transparent)
    original = item.path.read_bytes()
    cache = ThumbnailCache()

    preview = cache.pixmap(item)

    assert 0 < preview.width() <= 360 and 0 < preview.height() <= 230
    assert (preview.width() > preview.height()) is (orientation == 1)
    assert_half_tones(preview, rotated=orientation == 6, transparent=transparent)
    assert cache.pixmap(item).cacheKey() == preview.cacheKey()
    assert item.path.read_bytes() == original
    with Image.open(item.path) as source:
        assert source.mode == "I;16" and source.size == (2048, 1536)
        assert source.getpixel((0, 0)) == 1000 and source.getpixel((2047, 0)) == 40000


def test_36_megapixel_crop_dialog_displays_and_saves_full_precision_samples(qt_app, tmp_path):
    item = gray16(tmp_path / "large.png", (6000, 6000))
    original = item.path.read_bytes()
    dialog = CropDialog(item, tmp_path / "workspace")
    try:
        assert dialog.canvas.image_size == (6000, 6000)
        preview = dialog.canvas.pixmap
        assert 0 < preview.width() <= 1600 and 0 < preview.height() <= 1200
        assert_half_tones(preview)
        dialog.canvas.selection = QRectF(2900, 1000, 200, 200)
        dialog.selection_changed()
        assert dialog.save_button.isEnabled()

        dialog.save_copy(False)

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.saved_item is not None and not dialog.error.text()
        with Image.open(dialog.saved_item.path) as crop:
            assert crop.mode == "I;16" and crop.size == (200, 200)
            assert crop.getpixel((0, 0)) == crop.getpixel((99, 199)) == 1000
            assert crop.getpixel((100, 0)) == crop.getpixel((199, 199)) == 40000
        manifest = json.loads((dialog.saved_item.path.parent / "crop.json").read_text(encoding="utf-8"))
        assert manifest["source_size"] == [6000, 6000]
        assert manifest["crop_box"] == [2900, 1000, 3100, 1200]
        assert manifest["source"]["sha256"] == hashlib.sha256(original).hexdigest()
        assert item.path.read_bytes() == original
    finally:
        dialog.close()
        shiboken6.delete(dialog)


def test_resized_crop_preview_preserves_rotated_16bit_transparency_and_source(qt_app, tmp_path):
    item = gray16(tmp_path / "transparent.png", (4096, 3072), orientation=6, transparent=True)
    original = item.path.read_bytes()
    with load_crop_source(item) as pixels:
        assert pixels.mode == "I;16" and pixels.size == (3072, 4096)
        canvas = CropCanvas(pixels)
        try:
            assert canvas.image_size == (3072, 4096)
            assert 0 < canvas.pixmap.width() <= 1600 and 0 < canvas.pixmap.height() <= 1200
            assert_half_tones(canvas.pixmap, rotated=True, transparent=True)
            # Preview construction must not mutate the caller's save source.
            assert pixels.mode == "I;16" and pixels.size == (3072, 4096)
            assert pixels.getpixel((1500, 1000)) == 1000
            assert pixels.getpixel((1500, 3000)) == 40000
            assert pixels.info["transparency"] == 1000
        finally:
            canvas.close()
            shiboken6.delete(canvas)
    assert item.path.read_bytes() == original


@pytest.mark.parametrize("mode,suffix", [("RGB", ".jpg"), ("RGBA", ".png")])
def test_large_ordinary_preview_keeps_colors_alpha_and_exif(qt_app, tmp_path, mode, suffix):
    source = tmp_path / ("ordinary" + suffix)
    dark = (240, 20, 10, 64) if mode == "RGBA" else (240, 20, 10)
    light = (10, 30, 230, 192) if mode == "RGBA" else (10, 30, 230)
    with Image.new(mode, (2048, 1536), dark) as pixels:
        pixels.paste(light, (1024, 0, 2048, 1536))
        exif = Image.Exif()
        exif[274] = 6
        pixels.save(source, exif=exif)
    original = source.read_bytes()
    preview = ThumbnailCache().pixmap(image_record(source, tmp_path, "reference"))
    assert 0 < preview.width() < preview.height() <= 230
    display = preview.toImage()
    top = display.pixelColor(display.width() // 2, display.height() // 4)
    bottom = display.pixelColor(display.width() // 2, 3 * display.height() // 4)
    assert top.red() > 230 and top.blue() < 20
    assert bottom.red() < 20 and bottom.blue() > 220
    assert top.alpha() == (64 if mode == "RGBA" else 255)
    assert bottom.alpha() == (192 if mode == "RGBA" else 255)
    assert source.read_bytes() == original
