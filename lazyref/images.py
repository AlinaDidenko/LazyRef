"""Завантаження, нормалізація фото та пошук візуальних повторів."""

import hashlib
from io import BytesIO
from pathlib import Path
import warnings

import imagehash
from PIL import Image, ImageOps, ImageStat

from .models import Asset, Candidate, check_cancel
from .access import SourceBlocked
from .authenticity import GeneratedImage, check_generation_metadata
from .photo_filters import LowResolution, check_resolution
from .download_budget import request_budget

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_BYTES = 20_000_000


def prepare(data: bytes, candidate: Candidate, directory: Path, method: str, *, verify_sources=True) -> Asset:
    if len(data) > MAX_BYTES:
        raise ValueError("Фото завелике.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as original:
            original.load()
            if verify_sources:
                check_generation_metadata(original)
            frame = ImageOps.exif_transpose(original).convert("RGB")
    if min(frame.size) < 180:
        raise ValueError("Фото замале для референсу.")
    frame.thumbnail((4096, 4096))
    path = directory / f"{candidate.id}.jpg"
    preview = directory / f"{candidate.id}-preview.jpg"
    frame.save(path, "JPEG", quality=94)
    width, height = frame.size
    frame.thumbnail((1100, 1100))
    frame.save(preview, "JPEG", quality=88)
    return Asset(candidate, path, preview, width, height, method)


def download(candidate: Candidate, directory: Path, network) -> Asset:
    check_cancel(network.cancel)
    high_quality_only = getattr(network, "high_quality_only", False)
    if candidate.local_path:
        path = Path(candidate.local_path)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Локальний файл завеликий.")
        return check_resolution(prepare(path.read_bytes(), candidate, directory, "local",
            verify_sources=getattr(network, "verify_sources", True)), high_quality_only)
    last_error = None
    resolution_error = None
    with request_budget(network) as budget:
        for url in dict.fromkeys([candidate.image_url, candidate.thumbnail_url]):
            if not url:
                continue
            check_cancel(network.cancel)
            budget.check_direct()
            try:
                asset = prepare(network.get(url, max_bytes=MAX_BYTES), candidate, directory,
                    "download" if url == candidate.image_url else "preview", verify_sources=getattr(network, "verify_sources", True))
                check_cancel(network.cancel)
                return check_resolution(asset, high_quality_only)
            except Exception as exc:
                from .models import Cancelled
                if isinstance(exc, (Cancelled, SourceBlocked, GeneratedImage, TimeoutError)):
                    raise
                if isinstance(exc, LowResolution):
                    resolution_error = exc
                last_error = exc
    if resolution_error:
        raise resolution_error
    raise ValueError("Не вдалося завантажити фото.") from last_error


def screening_preview(candidate: Candidate, directory: Path, network):
    """Мініатюра до 448 px. Повертає також повне фото, якщо довелося завантажити його."""
    from .models import Cancelled
    last_error = None
    with request_budget(network) as budget:
        for url in dict.fromkeys([candidate.thumbnail_url, candidate.image_url]):
            if not url:
                continue
            check_cancel(network.cancel)
            budget.check_direct()
            try:
                data = network.get(url, max_bytes=MAX_BYTES)
                check_cancel(network.cancel)
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as original:
                        original.load()
                        if getattr(network, "verify_sources", True):
                            check_generation_metadata(original)
                        frame = ImageOps.exif_transpose(original).convert("RGB")
                if min(frame.size) < 64:
                    raise ValueError("Мініатюра замала навіть для попереднього відбору.")
                full = None
                if url == candidate.image_url and min(frame.size) >= 180:
                    full = prepare(data, candidate, directory, "download", verify_sources=getattr(network, "verify_sources", True))
                frame.thumbnail((448, 448))
                path = directory / f"{candidate.id}-screen.jpg"
                frame.save(path, "JPEG", quality=80)
                check_cancel(network.cancel)
                return Asset(candidate, path, path, *frame.size, "screening_thumbnail"), full
            except (Cancelled, SourceBlocked, GeneratedImage, TimeoutError):
                raise
            except Exception as exc:
                last_error = exc
    raise ValueError("Не вдалося завантажити мініатюру.") from last_error


class DuplicateIndex:
    """Порівнює пікселі й перцептивний хеш; колір зменшує хибні збіги."""
    def __init__(self, *, allow_larger=False):
        self.records = []
        self.allow_larger = allow_larger

    def add(self, path: Path) -> bool:
        """True: нове фото. False: уже бачили такий або дуже схожий файл."""
        return self.add_with_index(path)[1]

    @staticmethod
    def _describe(path: Path):
        with Image.open(path) as source, source.convert("RGB") as image:
            exact = hashlib.sha256(image.tobytes()).hexdigest()
            perceptual = imagehash.phash(image)
            difference = imagehash.dhash(image)
            with image.resize((16, 16)) as thumbnail:
                color = ImageStat.Stat(thumbnail).mean
            ratio = image.width / image.height
            quality = (min(image.size), image.width * image.height)
        return exact, perceptual, difference, color, ratio, quality

    def replace(self, index: int, path: Path):
        """Оновлює групу за фактично обраним фото, зберігаючи її індекс."""
        self.records[index] = self._describe(path)

    def add_with_index(self, path: Path, *, preferred=()) -> tuple[int, bool]:
        """Повертає сталий індекс групи й ознаку нового фото.

        За кількох збігів надає перевагу вказаним групам, не змінюючи
        правила допуску більшої копії до незалежного оцінювання.
        """
        exact, perceptual, difference, color, ratio, quality = self._describe(path)
        preferred_indices = set(preferred)
        first_match = None
        for index, (old_exact, old_hash, old_difference, old_color, old_ratio, old_quality) in enumerate(self.records):
            if exact == old_exact:
                duplicate = True
            else:
                color_delta = sum(abs(a - b) for a, b in zip(color, old_color)) / 3
                # Enhanced selection must be allowed to assess a larger copy.
                # Its own rating (including source evidence) is still required;
                # a perceptual match never inherits an earlier assessment.
                duplicate = (abs(ratio - old_ratio) < 0.12 and perceptual - old_hash <= 4
                    and difference - old_difference <= 6 and color_delta < 22
                    and (not self.allow_larger or quality <= old_quality))
            if duplicate:
                if not preferred_indices or index in preferred_indices:
                    return index, False
                if first_match is None:
                    first_match = index
        if first_match is not None:
            return first_match, False
        self.records.append((exact, perceptual, difference, color, ratio, quality))
        return len(self.records) - 1, True
