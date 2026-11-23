"""Власні фото копіюються локально й залишаються придатними до пошуку після переміщення оригіналу."""

import hashlib
import json

from PIL import Image
import pytest

from lazyref.importing import import_reference
from lazyref.library import scan_library
from lazyref.models import SearchRequest


@pytest.mark.parametrize("fmt,suffix", [("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")])
def test_import_copies_exact_pixels_and_metadata_and_indexes_gallery(tmp_path, fmt, suffix):
    source = tmp_path / ("Власне фото" + suffix)
    Image.new("RGB", (160, 220), "green").save(source, format=fmt)
    original = source.read_bytes()
    workspace = tmp_path / "app"
    item = import_reference(source, workspace, "Черевики у цьому ракурсі")
    assert item.path.read_bytes() == source.read_bytes() == original
    assert item.kind == "reference" and item.path != source
    record = json.loads((item.path.parent / "import.json").read_text(encoding="utf-8"))
    assert record["images"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    source.unlink()
    scan = scan_library(workspace)
    assert not scan.warnings and [i.path for i in scan.images] == [item.path]
    req = SearchRequest(item.description, 5, workspace / "references", reference_image=item.checked(), reference_mode="visual")
    req.validate()


@pytest.mark.parametrize("bad", ["corrupt", "small", "wrong_format", "animated", "large"])
def test_invalid_import_leaves_no_gallery_files(tmp_path, bad):
    source = tmp_path / "photo.png"
    if bad == "corrupt":
        source.write_text("not an image")
    elif bad == "small":
        Image.new("RGB", (10, 200)).save(source)
    elif bad == "wrong_format":
        Image.new("RGB", (100, 100)).save(source, format="BMP")
    elif bad == "animated":
        Image.new("RGB", (100, 100), "red").save(source, save_all=True, append_images=[Image.new("RGB", (100, 100), "green")], duration=100)
    else:
        with source.open("wb") as stream:
            stream.truncate(20_000_001)
    original = source.read_bytes()
    with pytest.raises(ValueError):
        import_reference(source, tmp_path / "app")
    assert source.read_bytes() == original
    assert not (tmp_path / "app" / "references").exists()


def test_failed_manifest_write_does_not_leave_partial_import_or_touch_original(tmp_path, monkeypatch):
    from lazyref import importing
    source = tmp_path / "photo.png"
    Image.new("RGB", (100, 100)).save(source)
    def failed(*a, **k):
        raise OSError("Disk full")
    monkeypatch.setattr(importing.json, "dump", failed)
    with pytest.raises(OSError, match="Disk full"):
        import_reference(source, tmp_path / "app")
    assert source.is_file()
    assert not list((tmp_path / "app" / "references").iterdir())
