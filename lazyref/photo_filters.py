"""Optional local filters. Never call a model, upscale images or change search budgets."""

from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps


MIN_LONG_SIDE = 1200
MIN_SHORT_SIDE = 800
QUALITY_DESCRIPTION = "Довша сторона ≥ 1200 px, коротша ≥ 800 px. Без збільшення маленьких фото."


class LowResolution(ValueError):
    message = "Фото {width} × {height} px замале: потрібно від 1200 × 800 px (або вертикально)."

    def __init__(self, width, height):
        self.width, self.height = width, height
        super().__init__(self.message.format(width=width, height=height))


def high_resolution(width, height):
    return max(width, height) >= MIN_LONG_SIDE and min(width, height) >= MIN_SHORT_SIDE


def check_resolution(asset, enabled):
    """Check the saved full file, never the screening thumbnail or claimed URL dimensions."""
    if enabled:
        with Image.open(asset.path) as image:
            width, height = image.size
        asset.width, asset.height = width, height
        if not high_resolution(width, height):
            raise LowResolution(width, height)
    return asset


@dataclass
class _View:
    ratio: float
    phash: int
    pixels: np.ndarray


def _view(image):
    small = image.resize((64, 64), Image.Resampling.LANCZOS)
    return _View(image.width / image.height, int(str(imagehash.phash(small)), 16),
                 np.asarray(small, dtype=np.int16))


def _views(path):
    # Bounded work: a 320px image and 46 small views per assessed photograph.
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    image.thumbnail((320, 320))
    views = [_view(image)]
    for fraction in (.9, .8, .7):
        width, height = round(image.width * fraction), round(image.height * fraction)
        for x in (0, .5, 1):
            for y in (0, .5, 1):
                left, top = round((image.width - width) * x), round((image.height - height) * y)
                views.append(_view(image.crop((left, top, left + width, top + height))))
        # Cropping just the top/bottom or sides changes the aspect ratio too.
        for axis in ("x", "y"):
            width = round(image.width * fraction) if axis == "x" else image.width
            height = round(image.height * fraction) if axis == "y" else image.height
            for offset in (0, .5, 1):
                left, top = round((image.width - width) * offset), round((image.height - height) * offset)
                views.append(_view(image.crop((left, top, left + width, top + height))))
    return views


def _matches(a, b, *, direct=False):
    if abs(a.ratio / b.ratio - 1) > .04 or not direct and (a.phash ^ b.phash).bit_count() > 10:
        return False
    # A hash alone can confuse two different poses on the same plain backdrop.
    # Verify actual pixels tile by tile; tolerate a small watermark, not a different subject.
    delta = np.abs(a.pixels - b.pixels).mean(axis=2)
    tiles = delta.reshape(8, 8, 8, 8).mean(axis=(1, 3)).ravel()
    if float(np.quantile(tiles, .85)) > (3 if direct else 6) or float(delta.mean()) > (3 if direct else 8):
        return False
    # Give textured edges (body/limbs) their own check so a large plain studio
    # background cannot outvote a changed pose. Small watermark regions may differ.
    active = np.zeros(64, dtype=bool)
    for pixels in (a.pixels, b.pixels):
        texture = pixels.reshape(8, 8, 8, 8, 3).std(axis=(1, 3)).max(axis=2).ravel()
        active |= texture > 8
    return active.sum() < 4 or float(np.quantile(tiles[active], .8)) <= 8


class ResultSelection:
    """Add eligible photos to a live result list; compare only when explicitly enabled.

    Full-to-crop comparisons are conservative: moderate crops and
    small watermarks, not arbitrary fragments or merely similar photoshoots.
    The best resolution among matching assessed photos wins; a close match
    takes precedence over a cropped partial match.
    """

    def __init__(self, enabled=False):
        self.enabled = enabled
        self._descriptors = {}
        self.removed_ids = set()

    def _get(self, asset):
        key = str(Path(asset.path).resolve())
        if key not in self._descriptors:
            self._descriptors[key] = _views(asset.path)
        return self._descriptors[key]

    def _same(self, a, b):
        left, right = self._get(a), self._get(b)
        # A small watermark on a plain background may change pHash drastically.
        # For the same framing accept only a much tighter direct pixel match.
        return (_matches(left[0], right[0], direct=True)
                or any(_matches(left[0], v) for v in right)
                or any(_matches(v, right[0]) for v in left[1:]))

    @staticmethod
    def _quality(asset):
        a = asset.assessment
        return (a.is_close, a.coverage, min(asset.width, asset.height),
                asset.width * asset.height, a.preference_coverage, a.score)

    def add(self, asset, selected):
        if not self.enabled:
            selected.append(asset)
            return
        matches = [old for old in selected if self._same(asset, old)]
        if not matches:
            selected.append(asset)
            return
        winner = max([*matches, asset], key=self._quality)
        for old in matches:
            if old is not winner:
                selected.remove(old)
                self.removed_ids.add(old.candidate.id)
        if asset is winner:
            selected.append(asset)
        else:
            self.removed_ids.add(asset.candidate.id)


def filter_export(request, assets, stats):
    """Also enforce the switches during saved-session export/recovery; no new model calls."""
    selected = []
    selector = ResultSelection(request.enhanced_dedup)
    old_ids = set(stats.get("enhanced_duplicate_ids", []))
    low_ids = set(stats.get("low_resolution_ids", []))
    old_low = len(low_ids)
    for asset in assets:
        try:
            check_resolution(asset, request.high_quality_only)
        except LowResolution:
            low_ids.add(asset.candidate.id)
            continue
        selector.add(asset, selected)
    stats["duplicates"] = stats.get("duplicates", 0) + len(selector.removed_ids - old_ids)
    stats["rejected"] = stats.get("rejected", 0) + len(low_ids) - old_low
    stats["enhanced_duplicate_ids"] = sorted(old_ids | selector.removed_ids)
    stats["low_resolution_ids"] = sorted(low_ids)
    return selected
