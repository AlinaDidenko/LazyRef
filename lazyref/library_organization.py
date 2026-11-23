"""Обране й добірки зберігають зв’язки з файлами, не копії зображень."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
from uuid import uuid4

from .library import is_link


def image_key(item):
    # NTFS file ID зберігається при перенесенні в архів на тому самому диску.
    # Розмір і mtime не дають передати позначки іншому файлу після заміни.
    identity = item.identity
    if len(identity) == 4 and identity[1]:
        value = ["file", *identity]
    else:
        value = ["path", str(item.path.absolute()).casefold(), *identity]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def collection_name(value):
    if not isinstance(value, str):
        raise ValueError("Введи назву добірки.")
    name = " ".join(value.split())
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Назва добірки має містити від 1 до 80 видимих символів.")
    return name, unicodedata.normalize("NFKC", name).casefold()


@dataclass
class OrganizationSnapshot:
    collections: dict[str, str] = field(default_factory=dict)
    favorites: set[str] = field(default_factory=set)
    memberships: dict[str, set[str]] = field(default_factory=dict)

    def favorite(self, item):
        return image_key(item) in self.favorites

    def assigned(self, item):
        return self.memberships.get(image_key(item), set()).copy()

    def names(self, item):
        assigned = self.assigned(item)
        return [name for key, name in self.collections.items() if key in assigned]


class OrganizationStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).absolute()
        self.path = self.workspace / ".lazyref" / "library.sqlite3"

    def checked_path(self, create=False):
        if str(self.path).startswith(("\\\\", "//")):
            raise ValueError("Обране та добірки зберігаються лише локально.")
        for entry in reversed((self.path, *self.path.parents)):
            try:
                if is_link(entry):
                    raise ValueError("Папка обраного не може бути посиланням на інший каталог.")
            except FileNotFoundError:
                pass
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            try:
                if is_link(sidecar):
                    raise ValueError("Непридатний службовий файл добірок.")
            except FileNotFoundError:
                pass
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size > 32 * 1024 * 1024):
            raise ValueError("Файл добірок недоступний або завеликий.")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path

    @contextmanager
    def connection(self, write=False):
        path = self.checked_path(create=write)
        if not write and not path.exists():
            yield None
            return
        conn = None
        try:
            conn = sqlite3.connect(path.as_uri() + ("?mode=rwc" if write else "?mode=ro"), uri=True,
                                   timeout=3, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and write:
                if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("Невідомий формат файлу добірок; його залишено без змін.")
                conn.execute("CREATE TABLE images (id TEXT PRIMARY KEY, path TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)))")
                conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE)")
                conn.execute("CREATE TABLE members (collection_id TEXT REFERENCES collections(id) ON DELETE CASCADE, image_id TEXT REFERENCES images(id), PRIMARY KEY(collection_id,image_id))")
                conn.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ValueError("Непідтримуваний формат файлу добірок; його залишено без змін.")
            yield conn
            if write:
                conn.commit()
        except sqlite3.Error as exc:
            raise ValueError("Не вдалося прочитати або зберегти обране й добірки. Попередні дані не перезаписано.") from exc
        finally:
            if conn is not None:
                conn.close()  # Незавершена транзакція відкочується.

    def snapshot(self):
        result = OrganizationSnapshot()
        with self.connection() as conn:
            if conn is None:
                return result
            rows = conn.execute("SELECT id,name FROM collections").fetchall()
            for key, name in sorted(rows, key=lambda row: str(row[1]).casefold()):
                collection_name(name)
                if not isinstance(key, str):
                    raise ValueError("Некоректний запис добірки.")
                result.collections[key] = name
            result.favorites = {row[0] for row in conn.execute("SELECT id FROM images WHERE favorite=1")}
            for collection, image in conn.execute("SELECT collection_id,image_id FROM members"):
                if collection not in result.collections:
                    raise ValueError("Неповний запис добірки.")
                result.memberships.setdefault(image, set()).add(collection)
        return result

    def create_collection(self, name):
        name, normalized = collection_name(name)
        key = uuid4().hex
        with self.connection(write=True) as conn:
            self.unique_name(conn, normalized)
            conn.execute("INSERT INTO collections VALUES (?,?,?)", (key, name, normalized))
        return key

    @staticmethod
    def unique_name(conn, name, except_id=""):
        if conn.execute("SELECT id FROM collections WHERE name_key=? AND id!=?", (name, except_id)).fetchone():
            raise ValueError("Добірка з такою назвою вже існує.")

    @staticmethod
    def require_collection(conn, key):
        if not isinstance(key, str) or not conn.execute("SELECT id FROM collections WHERE id=?", (key,)).fetchone():
            raise ValueError("Добірку вже видалено. Онови список.")

    def rename_collection(self, key, name):
        name, normalized = collection_name(name)
        with self.connection(write=True) as conn:
            self.require_collection(conn, key)
            self.unique_name(conn, normalized, key)
            conn.execute("UPDATE collections SET name=?,name_key=? WHERE id=?", (name, normalized, key))

    def delete_collection(self, key):
        with self.connection(write=True) as conn:
            self.require_collection(conn, key)
            conn.execute("DELETE FROM collections WHERE id=?", (key,))

    @staticmethod
    def remember(conn, item):
        item.checked()
        key = image_key(item)
        # Шлях — тільки довідкова інформація. Файли ніколи не відкриваються за адресами з БД.
        conn.execute("INSERT INTO images(id,path) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path",
                     (key, str(item.path)))
        return key

    def set_favorite(self, item, favorite):
        if type(favorite) is not bool:
            raise ValueError("Некоректна позначка обраного.")
        with self.connection(write=True) as conn:
            key = self.remember(conn, item)
            conn.execute("UPDATE images SET favorite=? WHERE id=?", (int(favorite), key))

    def set_collections(self, item, collections):
        if not isinstance(collections, (set, list, tuple)) or any(not isinstance(key, str) for key in collections):
            raise ValueError("Некоректний список добірок.")
        with self.connection(write=True) as conn:
            for collection in collections:
                self.require_collection(conn, collection)
            key = self.remember(conn, item)
            conn.execute("DELETE FROM members WHERE image_id=?", (key,))
            conn.executemany("INSERT INTO members VALUES (?,?)", [(c, key) for c in set(collections)])
