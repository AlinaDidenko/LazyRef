import json
import os
from pathlib import Path
from threading import Event

from PIL import Image
import pytest

from lazyref.generation import load_generation
from lazyref.library import image_record, relative_image, scan_library


def picture(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (220, 240), "green").save(path)
    return path


def metadata(path, **data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def reference(root, description="A seated person"):
    file = picture(root / "partial/01_partial.jpg")
    metadata(root / "manifest.json", description=description, requested_model="gpt-6-sol",
             reference_guide={"path": "C:/old/unavailable/guide.png"}, results=[{"file": "partial/01_partial.jpg"}])
    return file


def test_current_archived_external_and_generated_are_separate_without_cache_noise(tmp_path):
    workspace = tmp_path / "app"
    current = reference(workspace / "references/LazyRef_2026-11-10_12-00-00_1")
    archived = reference(workspace / "archive/older/references/LazyRef_2026-11-05_12-00-00_1")
    external = reference(tmp_path / "other/LazyRef_external")
    guide = picture(workspace / "archive/older/generated_references/selected/pose.png")
    metadata(guide.parent / "generation.json", kind="ai_generated_pose_guides", description="A pointing woman",
             images=[{"file": guide.name}])
    metadata(guide.parent.parent / "selection.json", cases=[{"image": "selected/pose.png"}])
    for folder in ["_sessions/session", "_search_cache", "_frozen", "woman_analysis"]:
        picture(workspace / "references" / folder / "thumbnail.jpg")
    # Навіть валідний manifest у службовому каталозі не є результатом пошуку.
    reference(workspace / "references/_sessions/fake")
    result = scan_library(workspace, [workspace / "references", tmp_path / "other"])
    assert not result.warnings
    assert {i.path for i in result.images} == {current, archived, external, guide}
    assert {i.path for i in result.images if i.archived} == {archived, guide}
    assert [i.path for i in result.images if i.kind == "generated"] == [guide]
    assert next(i for i in result.images if i.path == current).model == "gpt-6-sol"


def test_partial_export_and_generation_without_manifest_survive(tmp_path):
    ref = picture(tmp_path / "references/LazyRef_interrupted/01_close.jpg")
    guide = picture(tmp_path / "generated_references/AI_pose_interrupted/01_AI_pose.png")
    picture(ref.parent / "contact_sheet.png")
    (ref.parent / "manifest.json").write_text("{broken", encoding="utf-8")
    scan = scan_library(tmp_path)
    assert {i.path for i in scan.images} == {ref, guide}
    assert len(scan.warnings) == 1


@pytest.mark.parametrize("name", ["../other.png", "/other.png", "C:/other.png", "C:other.png",
                                  "\\\\server\\share\\a.png", "a.png:stream", "partial/../a.png", "./a.png"])
def test_metadata_cannot_point_outside_its_collection(tmp_path, name):
    with pytest.raises((ValueError, OSError)):
        relative_image(tmp_path, name)


def test_invalid_or_missing_rows_do_not_hide_other_collections(tmp_path):
    path = reference(tmp_path / "references/saved")
    metadata(tmp_path / "references/bad/manifest.json", results=[None, {"file": "C:/outside.jpg"},
                                                                {"file": "deleted.jpg"}])
    scan = scan_library(tmp_path)
    assert [i.path for i in scan.images] == [path]
    assert len(scan.warnings) == 2


def test_checked_record_rejects_a_replaced_file_and_missing_files_do_not_reappear(tmp_path):
    path = reference(tmp_path / "references/one")
    old = scan_library(tmp_path).images[0]
    path.write_bytes(b"changed file")
    with pytest.raises(ValueError, match="змінився"):
        old.checked()
    path.unlink()
    assert scan_library(tmp_path).images == []


def test_scan_does_not_follow_symlinks(tmp_path):
    outside = picture(tmp_path / "outside/image.png")
    folder = tmp_path / "app/references/example"
    folder.mkdir(parents=True)
    try:
        (folder / "linked.png").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable for this Windows account")
    metadata(folder / "manifest.json", results=[{"file": "linked.png"}])
    assert not scan_library(tmp_path / "app").images


def test_cancelled_scan_stops_before_loading_files(tmp_path):
    reference(tmp_path / "references/example")
    cancel = Event()
    cancel.set()
    result = scan_library(tmp_path, cancel=cancel)
    assert result.cancelled and not result.images


def test_generated_collection_still_opens_after_one_file_was_deleted(tmp_path):
    remaining = picture(tmp_path / "two.png")
    manifest = metadata(tmp_path / "generation.json", kind="ai_generated_pose_guides",
        description="A seated person", requested=2, images=[{"file": "one.png"}, {"file": remaining.name}])
    result, description = load_generation(manifest)
    assert result.images == [remaining] and description == "A seated person"
    assert any("видалено або переміщено: 1" in w for w in result.warnings)


def test_scan_keeps_physical_copies_separate_for_unambiguous_deletion(tmp_path):
    one = reference(tmp_path / "references/one")
    two = reference(tmp_path / "archive/two")
    assert one.read_bytes() == two.read_bytes()
    assert {i.path for i in scan_library(tmp_path).images} == {one, two}


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC paths")
def test_unc_roots_are_rejected_before_any_filesystem_access(tmp_path, monkeypatch):
    original = Path.lstat
    def no_network(path, *args, **kwargs):
        if str(path).startswith("\\\\"):
            pytest.fail("No metadata access to a remote share")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", no_network)
    result = scan_library(tmp_path, [r"\\example.invalid\share"])
    assert not result.images and len(result.warnings) == 1
