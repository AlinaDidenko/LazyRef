"""Local filters: no real network, model, or generated image calls."""

from dataclasses import replace
from io import BytesIO
import json
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from lazyref.exporter import export_results
from lazyref.images import download, screening_preview
from lazyref.models import Asset, Candidate, SearchRequest
from lazyref.photo_filters import LowResolution, ResultSelection, check_resolution, filter_export
from lazyref.pipeline import SearchPipeline
from lazyref.review_session import ReviewSession
from lazyref.search_cache import task_key
from lazyref.wide_search import WideSearchPipeline
from test_core import picture
from test_search_quality import PLAN, rating
from test_retry_analysis import Client
from test_wide_search import Agent, preview_loader, request as wide_request


def asset(tmp_path, name, image):
    path = tmp_path / f"{name}.jpg"
    image.save(path, quality=94)
    return Asset(Candidate(name, name, image_url=f"https://example.org/{name}.jpg"), path, path,
                 *image.size, assessment=rating())


def encoded(image):
    out = BytesIO()
    image.save(out, "JPEG", quality=94)
    return out.getvalue()


@pytest.mark.parametrize("size,accepted", [((1200, 800), True), ((800, 1200), True),
    ((1199, 800), False), ((2400, 799), False), ((1000, 1000), False)])
def test_quality_checks_actual_full_file_not_claimed_dimensions(tmp_path, size, accepted):
    photo = asset(tmp_path, "size", Image.new("RGB", size))
    photo.width, photo.height = 9999, 9999
    assert check_resolution(photo, False) is photo
    if accepted:
        assert check_resolution(photo, True) is photo
    else:
        with pytest.raises(LowResolution):
            check_resolution(photo, True)
    assert (photo.width, photo.height) == size


@pytest.mark.parametrize("variation", ["resize", "crop", "side_crop", "watermark"])
def test_optional_dedup_finds_variants_keeps_better_assessed_copy(tmp_path, variation):
    original = picture(size=(1200, 900))
    changed = original.resize((600, 450))
    if variation == "crop":
        changed = original.crop((120, 90, 1080, 810))
    elif variation == "side_crop":
        changed = original.crop((120, 0, 1080, 900))
    elif variation == "watermark":
        ImageDraw.Draw(changed).text((20, 420), "PHOTO COPYRIGHT", fill="white")
    photos = [asset(tmp_path, "variant", changed), asset(tmp_path, "original", original)]
    untouched, selected = [], []
    off, on = ResultSelection(False), ResultSelection(True)
    for photo in photos:
        off.add(photo, untouched)
        on.add(photo, selected)
    assert untouched == photos
    assert [p.candidate.id for p in selected] == ["original"]
    assert on.removed_ids == {"variant"}


def test_similar_backgrounds_and_different_poses_stay_distinct(tmp_path):
    photos = []
    for i, arm_end in enumerate(((420, 170), (420, 300), (370, 380))):
        image = Image.new("RGB", (600, 450), "#dddddd")
        draw = ImageDraw.Draw(image)
        draw.ellipse((270, 40, 330, 100), fill="#654321")
        draw.line([(300, 100), (300, 280), (230, 420)], fill="#543210", width=22)
        draw.line([(300, 280), (380, 420)], fill="#543210", width=22)
        draw.line([(230, 220), (300, 150), arm_end], fill="#543210", width=18)
        photos.append(asset(tmp_path, str(i), image))
    selector, selected = ResultSelection(True), []
    for photo in photos:
        selector.add(photo, selected)
    assert len(selected) == 3


@pytest.mark.parametrize("seeds", [(10, 11), (19, 20)])
def test_repeating_patterns_on_plain_background_are_not_duplicates(tmp_path, seeds):
    selector, selected = ResultSelection(True), []
    for seed in seeds:
        selector.add(asset(tmp_path, str(seed), picture(seed)), selected)
    assert len(selected) == 2


def test_quality_skips_small_file_but_keeps_small_screening_thumbnail(tmp_path):
    c = Candidate("one", "Photo", image_url="https://example.org/full.jpg", thumbnail_url="https://example.org/thumb.jpg")
    data = {c.image_url: encoded(picture(size=(1200, 900))), c.thumbnail_url: encoded(picture(size=(240, 180)))}
    net = SimpleNamespace(cancel=Event(), high_quality_only=True, verify_sources=False,
                          get=lambda url, **kw: data[url])
    thumbnail, full = screening_preview(c, tmp_path, net)
    assert thumbnail.width == 240 and full is None
    assert download(c, tmp_path, net).width == 1200
    data[c.image_url] = data[c.thumbnail_url]
    with pytest.raises(LowResolution):
        download(c, tmp_path, net)


def test_export_retry_filters_and_statistics_are_idempotent(tmp_path):
    base = picture(size=(1200, 900))
    photos = [asset(tmp_path, "original", base), asset(tmp_path, "cropped", base.crop((120, 90, 1080, 810))),
              asset(tmp_path, "tiny", picture(5))]
    req = SearchRequest("A seated person", 5, tmp_path / "out", enhanced_dedup=True)
    stats = {}
    selected = filter_export(req, photos, stats)
    assert len(selected) == 2 and stats["duplicates"] == 1
    filter_export(req, photos, stats)
    assert stats["duplicates"] == 1
    req.high_quality_only = True
    folder, exported = export_results(req, photos, PLAN, stats)
    assert [a.candidate.id for a in exported] == ["original"]
    assert stats["low_resolution_ids"] == ["cropped", "tiny"]
    report = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert report["photo_filters"]["high_quality_only"] is True


def test_session_defaults_flags_and_distinct_cache_keys(tmp_path):
    req = SearchRequest("A seated person", 5, tmp_path, enhanced_dedup=True, high_quality_only=True)
    session = ReviewSession.create(req, PLAN)
    loaded = ReviewSession.load(session.path).request()
    assert loaded.enhanced_dedup and loaded.high_quality_only
    assert len({task_key(req), task_key(replace(req, enhanced_dedup=False)), task_key(replace(req, high_quality_only=False))}) == 3
    session.data["request"].pop("enhanced_dedup")
    session.data["request"].pop("high_quality_only")
    session.save()
    old = ReviewSession.load(session.path).request()
    assert not old.enhanced_dedup and not old.high_quality_only
    with pytest.raises(ValueError):
        replace(req, enhanced_dedup="OFF").validate()


def test_legacy_quality_rejection_precedes_assessment_and_screenshot(tmp_path, monkeypatch):
    candidates = [Candidate("small", "Small"), Candidate("large", "Large")]
    def loader(c, directory, network):
        size = (600, 450) if c.id == "small" else (1200, 900)
        return asset(directory, c.id, picture(size=size))
    monkeypatch.setattr("lazyref.pipeline.BrowserCapture.capture", lambda *a: pytest.fail("No screenshot for small photo"))
    client = Client()
    req = SearchRequest("A seated person", 2, tmp_path / "out", high_quality_only=True)
    result = SearchPipeline(client, lambda *a: (candidates, []), loader).run(req, Event())
    assert not result.error
    assert client.batches == [["large"]]
    assert result.low_resolution_rejected == 1 and result.examined == 1
    assert len(result.assets) == 1 and result.assets[0].width == 1200


def test_wide_retries_low_cached_preview_original_and_filters_before_analysis(tmp_path):
    loaded = []
    def loader(c, directory, network):
        loaded.append(c.id)
        photo = asset(directory, c.id, picture(int(c.title.split()[-1]), size=(1200, 900)))
        photo.candidate = c
        return photo
    req = wide_request(tmp_path, high_quality_only=True, reuse_cache=False)
    client = Agent()
    result = WideSearchPipeline(client, loader, preview_loader=preview_loader).run(req, Event())
    assert not result.error and loaded
    assert result.assets and all(a.width >= 1200 and a.height >= 800 for a in result.assets)


def test_wide_all_small_never_runs_detailed_assessment(tmp_path):
    def loader(c, directory, network):
        return preview_loader(c, directory, network)[1]
    client = Agent()
    result = WideSearchPipeline(client, loader, preview_loader=preview_loader).run(
        wide_request(tmp_path, high_quality_only=True, reuse_cache=False), Event())
    assert not result.error and not result.assets
    assert client.assess_calls == 0 and result.low_resolution_rejected == 20


def test_enhanced_filter_in_live_pipeline_and_retry_export(tmp_path):
    from lazyref.retry_analysis import retry_review
    source = picture(size=(1200, 900))
    photos = {"original": source, "crop": source.crop((120, 90, 1080, 810))}
    candidates = [Candidate(name, name) for name in photos]
    def loader(c, directory, network):
        return asset(directory, c.id, photos[c.id])
    req = SearchRequest("A seated person", 5, tmp_path / "out", enhanced_dedup=True)
    result = SearchPipeline(Client(), lambda *a: (candidates, []), loader).run(req, Event())
    assert not result.error and result.examined == 2
    assert len(result.assets) == 1 and result.duplicates == 1
    path = next((req.output_dir / "_sessions").glob("*/review.json"))
    session = ReviewSession.load(path)
    session.data["export_pending"] = True  # Simulate a later export retry, no analysis required.
    session.save()
    client = Client()
    retried = retry_review(path, Event(), client=client)
    assert not retried.error and not client.batches
    assert retried.enhanced_dedup and len(retried.assets) == 1 and retried.duplicates == 1
