"""Зв’язки добірок, транзакції й збереження позначок без копіювання фото."""

import sqlite3

import pytest

from lazyref.library import image_record
from lazyref.library_organization import OrganizationStore
from test_library import picture


def photo(tmp_path, name="references/photo.png"):
    return image_record(picture(tmp_path / name), tmp_path, "reference")


def test_reading_empty_library_does_not_create_metadata_or_touch_images(tmp_path):
    item = photo(tmp_path)
    original = item.path.read_bytes()
    state = OrganizationStore(tmp_path).snapshot()
    assert not state.collections and not state.favorite(item)
    assert not (tmp_path / ".lazyref").exists() and item.path.read_bytes() == original


def test_one_file_in_many_collections_roundtrip_rename_and_remove_only_links(tmp_path):
    item = photo(tmp_path)
    original = item.path.read_bytes()
    store = OrganizationStore(tmp_path)
    hands = store.create_collection("Руки")
    project = store.create_collection("Персонаж для коміксу")
    store.set_favorite(item, True)
    store.set_collections(item, {hands, project})
    restarted = OrganizationStore(tmp_path)
    state = restarted.snapshot()
    assert state.favorite(item) and state.assigned(item) == {hands, project}
    store.rename_collection(hands, "Руки й плечі")
    assert restarted.snapshot().collections[hands] == "Руки й плечі"
    store.delete_collection(project)
    assert restarted.snapshot().assigned(item) == {hands}
    store.set_collections(item, set())
    assert not restarted.snapshot().assigned(item) and restarted.snapshot().favorite(item)
    store.set_favorite(item, False)
    assert not restarted.snapshot().favorite(item)
    assert list(tmp_path.rglob("*.png")) == [item.path] and item.path.read_bytes() == original


def test_identical_physical_copies_have_independent_marks(tmp_path):
    a, b = photo(tmp_path, "references/a.png"), photo(tmp_path, "references/b.png")
    assert a.path.read_bytes() == b.path.read_bytes()
    store = OrganizationStore(tmp_path)
    store.set_favorite(a, True)
    assert store.snapshot().favorite(a) and not store.snapshot().favorite(b)


def test_marks_follow_same_file_moved_to_archive_and_returned_from_trash(tmp_path):
    item = photo(tmp_path)
    store = OrganizationStore(tmp_path)
    key = store.create_collection("Персонаж")
    store.set_favorite(item, True)
    store.set_collections(item, {key})
    destination = tmp_path / "archive/old/photo.png"
    destination.parent.mkdir(parents=True)
    item.path.rename(destination)
    archived = image_record(destination, tmp_path, "reference", archived=True)
    assert store.snapshot().favorite(archived) and store.snapshot().assigned(archived) == {key}
    trash = tmp_path / "trash.png"
    destination.rename(trash)
    trash.rename(destination)
    restored = image_record(destination, tmp_path, "reference", archived=True)
    assert store.snapshot().favorite(restored)


def test_stale_photo_is_not_modified_and_new_contents_do_not_inherit_star(tmp_path):
    item = photo(tmp_path)
    store = OrganizationStore(tmp_path)
    store.set_favorite(item, True)
    item.path.write_bytes(item.path.read_bytes() + b"new contents")
    with pytest.raises(ValueError, match="змінився"):
        store.set_favorite(item, False)
    updated = image_record(item.path, tmp_path, "reference")
    assert not store.snapshot().favorite(updated)


def test_multiple_store_instances_do_not_overwrite_unrelated_updates(tmp_path):
    first, second = OrganizationStore(tmp_path), OrganizationStore(tmp_path)
    a, b = photo(tmp_path, "references/a.png"), photo(tmp_path, "references/b.png")
    first.snapshot()
    second.snapshot()
    c1 = first.create_collection("Проєкт 1")
    c2 = second.create_collection("Проєкт 2")
    first.set_collections(a, {c1, c2})
    second.set_favorite(b, True)
    assert first.snapshot().favorite(b) and second.snapshot().assigned(a) == {c1, c2}


def test_missing_collection_or_duplicate_name_never_removes_existing_membership(tmp_path):
    item = photo(tmp_path)
    store = OrganizationStore(tmp_path)
    c1 = store.create_collection("Руки")
    c2 = store.create_collection("Ноги")
    store.set_collections(item, {c1})
    with pytest.raises(ValueError, match="видалено"):
        store.set_collections(item, {c2, "missing"})
    with pytest.raises(ValueError, match="вже існує"):
        store.rename_collection(c1, "  ноги ")
    assert store.snapshot().assigned(item) == {c1}
    assert store.snapshot().collections[c1] == "Руки"


def test_mid_transaction_failure_rolls_back_all_membership_changes(tmp_path):
    item = photo(tmp_path)
    store = OrganizationStore(tmp_path)
    c1, c2 = store.create_collection("Руки"), store.create_collection("Ноги")
    store.set_collections(item, {c1})
    with store.connection(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON members BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(ValueError, match="не перезаписано"):
        store.set_collections(item, {c2})
    assert store.snapshot().assigned(item) == {c1}


@pytest.mark.parametrize("name", ["", "  ", "a" * 81, "bad\x00name", "hidden\u200bname", None])
def test_invalid_names_are_rejected_before_any_database_write(tmp_path, name):
    store = OrganizationStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_collection(name)
    assert not store.path.exists()


def test_names_are_text_not_paths_and_unicode_duplicates_are_rejected(tmp_path):
    store = OrganizationStore(tmp_path)
    key = store.create_collection("  <Руки> / проєкт  ")
    assert store.snapshot().collections[key] == "<Руки> / проєкт"
    with pytest.raises(ValueError, match="вже існує"):
        store.create_collection("<РУКИ> / ПРОЄКТ")
    assert not (tmp_path / "<Руки>").exists()


def test_corrupt_database_is_reported_and_never_replaced_with_empty_data(tmp_path):
    store = OrganizationStore(tmp_path)
    store.path.parent.mkdir()
    original = b"corrupt user data"
    store.path.write_bytes(original)
    with pytest.raises(ValueError):
        store.snapshot()
    with pytest.raises(ValueError):
        store.create_collection("Руки")
    assert store.path.read_bytes() == original


def test_unknown_database_format_is_left_untouched(tmp_path):
    store = OrganizationStore(tmp_path)
    store.path.parent.mkdir()
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE important (value TEXT)")
        conn.execute("INSERT INTO important VALUES ('keep')")
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="Невідомий формат"):
        store.create_collection("Руки")
    assert store.path.read_bytes() == original


def test_metadata_links_are_rejected_before_sqlite_open(tmp_path, monkeypatch):
    from lazyref import library_organization as module
    store = OrganizationStore(tmp_path)
    monkeypatch.setattr(module, "is_link", lambda p: p == store.path.parent)
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: pytest.fail("Do not follow link"))
    with pytest.raises(ValueError, match="посиланням"):
        store.create_collection("Руки")
