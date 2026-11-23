"""Власне фото: перевірена локальна копія в галереї, без генерації та зміни оригіналу."""

from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image

from .library import image_record, is_link

MAX_BYTES = 20_000_000


def import_reference(source: Path, workspace: Path, description=""):
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("Опис має містити до 2000 символів.")
    source = Path(source).absolute()
    item = image_record(source, source.parent, "reference")
    if not 0 < source.stat().st_size <= MAX_BYTES:
        raise ValueError("Обери фото розміром до 20 МБ.")
    with item.checked().open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    item.checked()
    if len(data) > MAX_BYTES:
        raise ValueError("Обери фото розміром до 20 МБ.")
    try:
        with Image.open(BytesIO(data)) as image:
            if (image.format not in {"JPEG", "PNG", "WEBP"} or min(image.size) < 32
                    or image.width * image.height > 40_000_000):
                raise ValueError("Потрібне PNG, JPG або WEBP: від 32 пікселів на сторону, до 40 мегапікселів.")
            if getattr(image, "is_animated", False):
                raise ValueError("Обери звичайне фото або окремий кадр замість анімації.")
            image.load()
            size, fmt = image.size, image.format
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Не вдалося прочитати фото. Обери інший файл.") from exc
    storage = Path(workspace).absolute() / "references"
    if str(storage).startswith(("\\\\", "//")):
        raise ValueError("Копію можна зберегти лише в локальній папці програми.")
    for folder in reversed((storage, *storage.parents)):
        if folder.exists() and is_link(folder):
            raise ValueError("Папка копій не може бути посиланням на інший каталог.")
    storage.mkdir(parents=True, exist_ok=True)
    folder = storage / (datetime.now().strftime("Imported_%Y-%m-%d_%H-%M-%S_") + uuid4().hex[:12])
    folder.mkdir(exist_ok=False)
    target = folder / ("image" + {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[fmt])
    manifest = folder / "import.json"
    try:
        with target.open("xb") as stream:
            stream.write(data)
        metadata = {"kind": "lazyref_import", "description": description.strip(),
            "created": datetime.now().astimezone().isoformat(), "source_filename": source.name,
            "images": [{"file": target.name, "width": size[0], "height": size[1],
                        "sha256": hashlib.sha256(data).hexdigest()}]}
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
        return image_record(target, storage, "reference", description=description.strip())
    except Exception:
        # Тільки нові файли цього імпорту. Оригінал не змінюємо.
        target.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        folder.rmdir()
        raise
