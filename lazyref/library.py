"""Локальний каталог результатів: читає поточні й архівні папки без перенесення фото."""

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from threading import Event


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SKIP_DIRECTORIES = {".git", ".venv", ".lazyref", "__pycache__", "node_modules",
                    "_search_cache", "_sessions", "_usage", "_frozen", "reports",
                    "review", "thumbnails", "contact_sheets", "alignment_evidence"}
MANIFEST_NAMES = {"manifest.json", "generation.json", "selection.json", "matrix.json", "crop.json", "import.json"}


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0)
                                             & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def checked_file(path: Path, root: Path) -> Path:
    """Лише звичайний локальний файл усередині довіреного кореня, без junction/symlink."""
    root = root.absolute()
    path = path.absolute()
    if str(path).startswith(("\\\\", "//")) or not path.is_relative_to(root):
        raise ValueError("Файл поза папкою галереї.")
    for entry in reversed((path, *path.parents)):
        if is_link(entry):
            raise ValueError("Посилання на інші файли або папки не підтримуються.")
    if path.resolve() != path or not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("Зображення недоступне або має непідтримуваний формат.")
    return path


def relative_image(folder: Path, value) -> Path:
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise ValueError("Некоректний шлях зображення.")
    parts = value.replace("\\", "/").split("/")
    # Windows can trim trailing spaces and dots when it opens a path component.
    if (PureWindowsPath(value).drive or any(not p or p.endswith((".", " ")) for p in parts)
            or any(":" in p or "\x00" in p for p in parts)):
        raise ValueError("Шлях зображення має бути відносним до добірки.")
    return checked_file(folder.joinpath(*parts), folder)


def text(value) -> str:
    return value[:2000] if isinstance(value, str) else ""


@dataclass(frozen=True)
class LibraryImage:
    path: Path
    root: Path
    kind: str  # reference / generated
    description: str = ""
    model: str = ""
    archived: bool = False
    timestamp: float = 0
    identity: tuple = ()
    cropped: bool = False
    review_owned: bool = False

    @property
    def deletion_error(self):
        if self.review_owned:
            return ("Це службова копія збереженого аналізу. Видалення недоступне, "
                    "щоб не пошкодити відновлення добірки.")
        return ""

    @property
    def search_text(self):
        return " ".join((self.description, self.model, str(self.path.relative_to(self.root)))).casefold()

    def checked(self):
        path = checked_file(self.path, self.root)
        if self.identity and file_identity(path) != self.identity:
            raise ValueError("Файл змінився після відкриття галереї. Натисни «Оновити».")
        return path


@dataclass
class LibraryScan:
    images: list[LibraryImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


def file_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def image_record(path, root, kind, *, description="", model="", archived=False, cropped=False, review_owned=False):
    path = checked_file(Path(path), Path(root))
    # Архівування змінює час деяких файлів; спочатку беремо дату з імені добірки.
    timestamp = path.stat().st_mtime
    for parent in path.parents:
        match = re.fullmatch(r"(?:LazyRef|AI_pose)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_\d+)?", parent.name)
        if match:
            try:
                timestamp = datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S").timestamp()
            except ValueError:
                pass
            break
    return LibraryImage(path, Path(root).absolute(), kind, text(description), text(model),
                        archived, timestamp, file_identity(path), cropped, review_owned)


def scan_library(workspace: Path, extra_roots=(), cancel: Event | None = None) -> LibraryScan:
    """Індексує результати, а не кеші/кандидатів. Старі абсолютні адреси не потрібні."""
    cancel = cancel or Event()
    workspace = Path(workspace).absolute()
    scan = LibraryScan()
    roots = [workspace / "archive", workspace / "references", workspace / "generated_references"]
    roots += [Path(p).expanduser().absolute() for p in extra_roots if str(p).strip()]
    visited, images, fallbacks = set(), {}, []

    def add(path, root, kind, data):
        if path in images:
            return
        images[path] = image_record(path, root, kind, description=data.get("description"),
            model=data.get("requested_model"), archived=path.is_relative_to(workspace / "archive"),
            cropped=data.get("kind") == "lazyref_crop")

    def read_manifest(path, root):
        if is_link(path) or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Завеликий або непридатний маніфест")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Маніфест має бути об’єктом")
        name = path.name
        if name == "manifest.json" and isinstance(data.get("results"), list):
            rows, key, kind = data["results"], "file", "reference"
        elif name == "generation.json" and data.get("kind") == "ai_generated_pose_guides":
            rows, key, kind = data.get("images"), "file", "generated"
        elif name == "crop.json" and data.get("kind") == "lazyref_crop" and data.get("image_kind") in {"reference", "generated"}:
            rows, key, kind = data.get("images"), "file", data["image_kind"]
        elif name == "import.json" and data.get("kind") == "lazyref_import":
            rows, key, kind = data.get("images"), "file", "reference"
        elif name in {"selection.json", "matrix.json"} and isinstance(data.get("cases"), list):
            rows, key, kind = data["cases"], "image", "generated"
        else:
            return
        if not isinstance(rows, list):
            raise ValueError("Некоректний список зображень")
        for row in rows:
            if cancel.is_set():
                return
            try:
                if not isinstance(row, dict):
                    raise ValueError("Некоректний запис зображення")
                image = relative_image(path.parent, row.get(key))
                add(image, root, kind, row if key == "image" else data)
            except FileNotFoundError:
                # Видалені/переміщені фото не повертаються після оновлення.
                continue
            except (OSError, ValueError) as exc:
                scan.warnings.append(f"{path.name}: {exc}")

    for root in roots:
        if cancel.is_set():
            break
        try:
            # UNC відсікається до будь-якої файлової операції/мережевої автентифікації.
            if str(root).startswith(("\\\\", "//")):
                raise ValueError("Мережеві папки та посилання пропущено")
            try:
                if any(is_link(p) for p in reversed((root, *root.parents))):
                    raise ValueError("Мережеві папки та посилання пропущено")
            except FileNotFoundError:
                continue
            if not root.is_dir():
                raise ValueError("Це не папка")
            root = root.resolve()
            for directory, dirs, files in os.walk(root, followlinks=False,
                    onerror=lambda e: scan.warnings.append(str(e))):
                if cancel.is_set():
                    break
                folder = Path(directory)
                if folder in visited:
                    dirs[:] = []
                    continue
                visited.add(folder)
                allowed = []
                for d in dirs:
                    try:
                        if d not in SKIP_DIRECTORIES and not is_link(folder / d):
                            allowed.append(d)
                    except OSError as exc:
                        scan.warnings.append(str(exc))
                dirs[:] = allowed
                for name in files:
                    if cancel.is_set():
                        break
                    path = folder / name
                    if name in MANIFEST_NAMES:
                        try:
                            read_manifest(path, root)
                        except (OSError, ValueError, RecursionError) as exc:
                            scan.warnings.append(f"{name}: {exc}")
                    # Зберігаємо також результат аварійного запису без готового JSON.
                    elif path.suffix.lower() in IMAGE_SUFFIXES:
                        if folder.name.startswith("AI_pose_"):
                            fallbacks.append((path, root, "generated"))
                        elif (folder.name.startswith("LazyRef_") or
                              folder.name == "partial" and folder.parent.name.startswith("LazyRef_")):
                            if re.fullmatch(r"\d+_(?:close|partial)\.(?:jpg|jpeg|png|webp)", name, re.I):
                                fallbacks.append((path, root, "reference"))
        except (OSError, ValueError) as exc:
            scan.warnings.append(f"{root}: {exc}")
    for path, root, kind in fallbacks:
        if cancel.is_set():
            break
        try:
            add(path, root, kind, {})
        except (OSError, ValueError):
            pass
    scan.images = sorted(images.values(), key=lambda p: (-p.timestamp, str(p.path).casefold()))
    scan.cancelled = cancel.is_set()
    return scan
