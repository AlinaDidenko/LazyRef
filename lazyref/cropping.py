"""Точне локальне обрізання в новий PNG; оригінали й архіви не змінюються."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from .library import LibraryImage, image_record, is_link

MIN_CROP_SIDE = 32
GRAYSCALE_16_MODES = {"I", "I;16", "I;16L", "I;16B", "I;16N"}


def display_rgba(pixels):
    """Only a display copy is reduced to 8 bits; saved crop pixels stay intact."""
    if pixels.mode in GRAYSCALE_16_MODES:
        with pixels.convert("I") as integers:
            with integers.point(lambda value: value / 257) as scaled:
                with scaled.convert("L") as gray:
                    rgba = gray.convert("RGBA")
            transparent = pixels.info.get("transparency")
            if type(transparent) is int and 0 <= transparent <= 65535:
                with integers.point([0 if value == transparent else 255 for value in range(65536)], "L") as alpha:
                    rgba.putalpha(alpha)
            return rgba
    return pixels.convert("RGBA")


def display_copy_for_resize(pixels):
    """Return a display-only copy in a mode supported by Pillow reduction."""
    if pixels.mode in GRAYSCALE_16_MODES:
        transparent = pixels.info.get("transparency")
        if type(transparent) is int and 0 <= transparent <= 65535:
            # Convert the exact transparent sample to alpha before filtering.
            return display_rgba(pixels)
        # Keep the full grayscale range until after reduction. I;16 itself
        # cannot use Pillow's fast reduce(), while I supports the same values.
        return pixels.convert("I")
    return pixels.copy()


def load_crop_source(item: LibraryImage):
    path = item.checked()
    if not 0 < path.stat().st_size <= 50 * 1024 * 1024:
        raise ValueError("Для обрізання потрібен файл до 50 МБ.")
    try:
        with Image.open(path) as source:
            if (source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > 40_000_000
                    or min(source.size) < MIN_CROP_SIDE):
                raise ValueError("Непридатний формат або розмір зображення.")
            with ImageOps.exif_transpose(source) as oriented:
                if source.format == "PNG" and oriented.mode in GRAYSCALE_16_MODES:
                    pixels = oriented.convert("I;16")
                else:
                    pixels = oriented.convert("RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB")
            pixels.load()
    except Image.DecompressionBombError as exc:
        raise ValueError("Зображення завелике для обрізання.") from exc
    item.checked()
    return pixels


def crop_box(box, size):
    if (not isinstance(box, (tuple, list)) or len(box) != 4
            or any(type(n) is not int for n in box)):
        raise ValueError("Некоректна рамка обрізання.")
    left, top, right, bottom = box
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError("Рамка має бути всередині зображення.")
    if min(right - left, bottom - top) < MIN_CROP_SIDE:
        raise ValueError(f"Виділи фрагмент не менше {MIN_CROP_SIDE} × {MIN_CROP_SIDE} пікселів.")
    return left, top, right, bottom


def save_crop(item: LibraryImage, box, workspace: Path, description="") -> LibraryImage:
    if item.kind not in {"generated", "reference"}:
        raise ValueError("Невідомий тип зображення.")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("Підпис фрагмента має містити до 2000 символів.")
    with load_crop_source(item) as pixels:
        box = crop_box(box, pixels.size)
        source_size = pixels.size
        fragment = pixels.crop(box)
    # A 16-bit grayscale PNG may use one transparent sample (tRNS). Retain
    # that sample without reducing its pixels to 8-bit RGBA.
    transparency = fragment.info.get("transparency") if fragment.mode == "I;16" else None
    fragment.info.clear()
    if type(transparency) is int:
        fragment.info["transparency"] = transparency
    workspace = Path(workspace).absolute()
    storage = workspace / ("generated_references" if item.kind == "generated" else "references")
    if str(storage).startswith(("\\\\", "//")):
        raise ValueError("Копію можна зберегти лише в локальну папку програми.")
    for folder in reversed((storage, *storage.parents)):
        if folder.exists() and is_link(folder):
            raise ValueError("Папка копій не може бути посиланням на інший каталог.")
    storage.mkdir(parents=True, exist_ok=True)
    folder = storage / (datetime.now().strftime("Crop_%Y-%m-%d_%H-%M-%S_") + uuid4().hex[:12])
    folder.mkdir(exist_ok=False)
    target, manifest = folder / "crop.png", folder / "crop.json"
    try:
        source = item.checked()
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        with target.open("xb") as stream:
            fragment.save(stream, format="PNG")
        if target.stat().st_size > 20_000_000:
            raise ValueError("Копія завелика для повторного аналізу (до 20 МБ). Виділи меншу область.")
        data = {"kind": "lazyref_crop", "image_kind": item.kind, "description": description.strip(),
                "created": datetime.now().astimezone().isoformat(), "requested_model": item.model,
                "source": {"path": str(source), "sha256": source_hash, "description": item.description},
                "source_size": list(source_size), "crop_box": list(box),
                "images": [{"file": target.name, "width": fragment.width, "height": fragment.height,
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}]}
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
    except Exception:
        # Лише щойно створена нами пара файлів; оригінал ніколи не видаляється.
        target.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        folder.rmdir()
        raise
    finally:
        fragment.close()
    return image_record(target, storage, item.kind, description=description.strip(), model=item.model, cropped=True)
