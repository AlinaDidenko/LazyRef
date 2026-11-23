"""Дві локальні галереї та спільне меню файлів. Не викликає Codex або мережу."""

from collections import OrderedDict
from datetime import datetime
import os
from pathlib import Path
from threading import Event

from PIL import Image, ImageOps
from shiboken6 import isValid
from PySide6.QtCore import QFile, QObject, QProcess, Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QScrollArea, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget)

from .library import LibraryImage, LibraryScan, image_record, scan_library
from .cropping import GRAYSCALE_16_MODES, display_copy_for_resize, display_rgba
from .organization_ui import CollectionsDialog, FavoriteButton, OrganizationController, star_icon
from .ui_text import plain_label, text_for


def show_error(parent, message):
    tr = text_for(parent)
    box = QMessageBox(parent)
    box.setWindowTitle(tr("Не вдалося виконати дію"))
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(tr(str(message)))
    box.setIcon(QMessageBox.Icon.Warning)
    box.exec()


def reveal_image(item: LibraryImage):
    path = item.checked()
    if os.name == "nt":
        explorer = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe")
        result = QProcess.startDetached(explorer, ["/select,", str(path)])
        started = result[0] if isinstance(result, tuple) else result
        if started:
            return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))):
        raise OSError("Не вдалося відкрити папку зображення.")


def trash_image(item: LibraryImage):
    if item.deletion_error:
        raise ValueError(item.deletion_error)
    path = item.checked()
    result = QFile.moveToTrash(str(path))
    success = result[0] if isinstance(result, tuple) else result
    if not success:
        raise OSError("Не вдалося перемістити файл до Кошика. Файл залишено на місці.")


class ImageFileActions(QObject):
    deleted = Signal(str)
    search_requested = Signal(object)
    copy_created = Signal(object)

    def __init__(self, item, parent, can_delete=lambda: True, workspace=None, organizer=None):
        super().__init__(parent)
        self.item = item
        self.can_delete = can_delete
        self.workspace = Path(workspace) if workspace is not None else Path(__file__).resolve().parent.parent
        self.organizer = organizer or OrganizationController(self.workspace, self)

    def attach(self, widget):
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(lambda position: self.menu(widget).exec(widget.mapToGlobal(position)))

    def menu(self, parent):
        tr = text_for(parent)
        menu = QMenu(parent)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.setToolTipsVisible(True)
        search = menu.addAction(tr("Шукати референси за цим зображенням"), self.choose_for_search)
        crop = menu.addAction(tr("Обрізати й зберегти копію"), self.crop)
        search.setEnabled(self.can_delete())
        crop.setEnabled(self.can_delete())
        menu.addSeparator()
        favorite = menu.addAction(tr("Прибрати з обраного" if self.organizer.state.favorite(self.item) else "Додати в обране"),
                                  self.toggle_favorite)
        collection = menu.addAction(tr("Додати до добірок…"), self.edit_collections)
        favorite.setEnabled(self.can_delete() and not self.organizer.error)
        collection.setEnabled(self.can_delete() and not self.organizer.error)
        menu.addSeparator()
        menu.addAction(tr("Знайти зображення"), self.reveal)
        remove = menu.addAction(tr("Видалити"), self.remove)
        remove.setToolTip(tr(self.item.deletion_error or "Перемістити цей файл до Кошика Windows"))
        remove.setEnabled(self.can_delete() and not self.item.deletion_error)
        return menu

    def toggle_favorite(self):
        if self.can_delete():
            self.organizer.toggle(self.item, self.parent())

    def edit_collections(self):
        if not self.can_delete():
            return
        try:
            self.item.checked()
        except (OSError, ValueError) as exc:
            show_error(self.parent(), exc)
            return
        # Картки можуть оновитися після створення добірки; діалог належить вікну.
        dialog = CollectionsDialog(self.organizer, self.parent().window(), self.item)
        dialog.exec()
        dialog.deleteLater()

    def choose_for_search(self):
        if not self.can_delete():
            return
        try:
            self.item.checked()
        except (OSError, ValueError) as exc:
            show_error(self.parent(), exc)
        else:
            self.search_requested.emit(self.item)

    def crop(self):
        if not self.can_delete():
            return
        from .crop_ui import CropDialog
        card = self.parent()
        owner = card.window()
        try:
            dialog = CropDialog(self.item, self.workspace, owner)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            show_error(owner, exc)
            return
        # A pending gallery filter can remove the card during the modal loop.
        # Keep both the dialog and its result signals alive until delivery.
        self.setParent(owner)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_item:
                self.copy_created.emit(dialog.saved_item)
                if dialog.search_after_save:
                    self.search_requested.emit(dialog.saved_item)
        finally:
            if isValid(self):
                if isValid(card):
                    self.setParent(card)
                else:
                    self.deleteLater()
            if isValid(dialog):
                dialog.deleteLater()

    def reveal(self):
        try:
            reveal_image(self.item)
        except (OSError, ValueError) as exc:
            show_error(self.parent(), exc)

    def remove(self):
        if not self.can_delete():
            return
        try:
            trash_image(self.item)
        except (OSError, ValueError) as exc:
            show_error(self.parent(), exc)
        else:
            self.deleted.emit(str(self.item.path))


def attach_image_actions(card, preview, path, on_deleted=None, can_delete=lambda: True, *,
                         kind="reference", description="", on_search=None, on_created=None, workspace=None,
                         organizer=None, review_owned=False):
    """Те саме меню є в результатах поточного пошуку й генерації."""
    try:
        item = image_record(Path(path), Path(path).absolute().parent, kind, description=description,
                            review_owned=review_owned)
    except (OSError, ValueError):
        return None
    actions = ImageFileActions(item, card, can_delete, workspace, organizer)
    if on_search:
        actions.search_requested.connect(on_search)
    if on_created:
        actions.copy_created.connect(on_created)
    actions.attach(card)
    actions.attach(preview)
    def removed(filename):
        card.hide()
        if on_deleted:
            on_deleted(filename)
    actions.deleted.connect(removed)
    return actions


class ThumbnailCache:
    def __init__(self):
        self.cache = OrderedDict()

    def pixmap(self, item):
        item.checked()
        key = (item.path, item.identity)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if item.path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("Зображення завелике для мініатюри")
        with Image.open(item.path) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > 40_000_000:
                raise ValueError("Непідтримуване зображення")
            source.draft("RGB", (360, 230))
            if source.mode in GRAYSCALE_16_MODES:
                with display_copy_for_resize(source) as preview:
                    preview.thumbnail((360, 360))
                    pixels = ImageOps.exif_transpose(preview)
            else:
                # Preserve the JPEG draft path and avoid another full-size
                # copy for ordinary RGB/RGBA images used only as previews.
                source.thumbnail((360, 360))
                pixels = ImageOps.exif_transpose(source)
            with pixels:
                pixels.thumbnail((360, 230))
                with display_rgba(pixels) as rgba:
                    raw = rgba.tobytes()
                    image = QImage(raw, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888)
                    pixmap = QPixmap.fromImage(image.copy())
        self.cache[key] = pixmap
        while len(self.cache) > 96:
            self.cache.popitem(last=False)
        return pixmap


class ThumbnailLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.original = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(230)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_thumbnail(self, pixmap):
        self.original = pixmap
        self.resize_thumbnail()

    def resize_thumbnail(self):
        if not self.original.isNull():
            self.setPixmap(self.original.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_thumbnail()


class DescriptionLabel(QLabel):
    """Два повні рядки з трикрапкою замість обрізаного третього."""
    def __init__(self, description):
        super().__init__()
        self.description = description
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * 2 + 4)
        self.setToolTip(description)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        words = self.description.split()
        first = []
        while words and metrics.horizontalAdvance(" ".join(first + words[:1])) <= self.width():
            first.append(words.pop(0))
        if not first and words:
            first.append(metrics.elidedText(words.pop(0), Qt.TextElideMode.ElideRight, self.width()))
        second = metrics.elidedText(" ".join(words), Qt.TextElideMode.ElideRight, self.width())
        self.setText(" ".join(first) + ("\n" + second if second else ""))


class LibraryCard(QFrame):
    def __init__(self, item, cache, on_deleted, on_search, on_created, workspace, organizer, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.item = item
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout()
        badge = "ШІ-зображення" if item.kind == "generated" else "Референс"
        row.addWidget(plain_label(tr(badge) + (tr(" · фрагмент") if item.cropped else ""), "badge"))
        row.addStretch()
        row.addWidget(tr.label("Архів" if item.archived else "Поточні", "muted"))
        self.favorite_button = FavoriteButton(item, organizer, self)
        row.addWidget(self.favorite_button)
        layout.addLayout(row)
        picture = ThumbnailLabel()
        try:
            picture.set_thumbnail(cache.pixmap(item))
        except (OSError, ValueError, Image.DecompressionBombError):
            picture.setText(tr("Мініатюра недоступна"))
        picture.setToolTip(tr("Права кнопка: обрати для пошуку, добірки, обрізати, знайти файл або видалити"))
        layout.addWidget(picture)
        title = DescriptionLabel(item.description or item.path.name)
        layout.addWidget(title)
        names = organizer.state.names(item)
        self.collection_names = plain_label(tr("Добірки: {names}", names=", ".join(names)) if names else "", "muted")
        self.collection_names.setToolTip("\n".join(names))
        self.collection_names.setVisible(bool(names))
        # Довгі назви доступні повністю в підказці, без розширення картки.
        self.collection_names.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.collection_names)
        date = datetime.fromtimestamp(item.timestamp).strftime("%d.%m.%Y · %H:%M")
        layout.addWidget(plain_label(date + (" · " + item.model if item.model else ""), "muted"))
        filename = plain_label(item.path.name, "muted")
        filename.setToolTip(str(item.path))
        layout.addWidget(filename)
        button = tr.button("Відкрити зображення")
        button.setObjectName("ghost")
        button.clicked.connect(self.open_image)
        layout.addWidget(button)
        self.file_actions = ImageFileActions(item, self, workspace=workspace, organizer=organizer)
        self.file_actions.attach(self)
        self.file_actions.attach(picture)
        self.file_actions.deleted.connect(on_deleted)
        self.file_actions.search_requested.connect(on_search)
        self.file_actions.copy_created.connect(on_created)
        use = tr.button("Обрати для пошуку")
        use.clicked.connect(self.file_actions.choose_for_search)
        layout.addWidget(use)

    def open_image(self):
        try:
            path = self.item.checked()
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise OSError("Не вдалося відкрити зображення.")
        except (OSError, ValueError) as exc:
            show_error(self, exc)


class LibraryPage(QWidget):
    PAGE_SIZE = 24

    def __init__(self, kind, cache, on_deleted, on_search, on_created, workspace, organizer, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.kind, self.cache, self.on_deleted = kind, cache, on_deleted
        self.on_search, self.on_created, self.workspace = on_search, on_created, workspace
        self.organizer = organizer
        self.favorites_only, self.collection_id = False, None
        self.images, self.filtered = [], []
        self.page = 0
        self.query, self.scope = "", "all"
        layout = QVBoxLayout(self)
        self.counter = plain_label("", "muted")
        layout.addWidget(self.counter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        container = QWidget()
        self.grid = QGridLayout(container)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid.setSpacing(14)
        for col in range(3):
            self.grid.setColumnStretch(col, 1)
        self.scroll.setWidget(container)
        layout.addWidget(self.scroll, 1)
        self.empty = tr.label("Збережених зображень поки немає.", "muted", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        controls = QHBoxLayout()
        self.previous = tr.button("← Назад")
        self.next = tr.button("Далі →")
        self.pages = plain_label("", "muted")
        self.previous.clicked.connect(lambda: self.navigate(-1))
        self.next.clicked.connect(lambda: self.navigate(1))
        controls.addWidget(self.previous)
        controls.addStretch()
        controls.addWidget(self.pages)
        controls.addStretch()
        controls.addWidget(self.next)
        layout.addLayout(controls)

    def set_images(self, images):
        self.images = [i for i in images if i.kind == self.kind]
        self.apply_filter(self.query, self.scope, reset=False)

    def apply_filter(self, query, scope, reset=True):
        self.query, self.scope = query.casefold().strip(), scope
        words = self.query.split()
        self.filtered = [i for i in self.images if all(w in i.search_text for w in words)
                         and (scope == "all" or i.archived == (scope == "archive"))
                         and (not self.favorites_only or self.organizer.state.favorite(i))
                         and (self.collection_id is None or self.collection_id in self.organizer.state.assigned(i))]
        self.page = 0 if reset else min(self.page, max(0, (len(self.filtered) - 1) // self.PAGE_SIZE))
        self.render(preserve_scroll=not reset)

    def navigate(self, delta):
        self.page += delta
        self.render()

    def render(self, preserve_scroll=False):
        tr = text_for(self)
        position = self.scroll.verticalScrollBar().value() if preserve_scroll else 0
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        start = self.page * self.PAGE_SIZE
        for n, item in enumerate(self.filtered[start:start + self.PAGE_SIZE]):
            self.grid.addWidget(LibraryCard(item, self.cache, self.on_deleted, self.on_search,
                                          self.on_created, self.workspace, self.organizer,
                                          parent=self.grid.parentWidget()), n // 3, n % 3,
                                Qt.AlignmentFlag.AlignTop)
        count = len(self.filtered)
        self.counter.setText(tr("Зображень: {count} із {total} · в архіві: {archived}",
                                count=count, total=len(self.images), archived=sum(i.archived for i in self.filtered)))
        pages = max(1, (count + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.pages.setText(tr("Сторінка {page} із {pages}", page=self.page + 1, pages=pages))
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page + 1 < pages)
        self.empty.setVisible(not count)
        self.empty.setText(tr("За цим фільтром нічого не знайдено." if self.query or self.scope != "all" or self.favorites_only or self.collection_id
                              else "Збережених зображень поки немає."))
        self.scroll.setVisible(bool(count))
        self.scroll.verticalScrollBar().setValue(position)


class LibraryWorker(QThread):
    def __init__(self, workspace, roots, parent):
        super().__init__(parent)
        self.workspace, self.roots = workspace, roots
        self.cancel = Event()
        self.result = LibraryScan()

    def run(self):
        try:
            self.result = scan_library(self.workspace, self.roots, self.cancel)
        except Exception as exc:
            self.result = LibraryScan(warnings=[str(exc)])


class LibraryDialog(QDialog):
    file_deleted = Signal(str)

    def __init__(self, workspace, roots=(), parent=None, *, autoload=True, organizer=None):
        super().__init__(parent)
        tr = text_for(self)
        self.workspace, self.roots = workspace, roots
        self.organizer = organizer or OrganizationController(workspace, self)
        self.organizer.reload()
        self.worker = None
        self.images = []
        self.selected_image = None
        self.closing = False
        self.setWindowTitle(tr("LazyRef — галереї"))
        self.resize(1160, 840)
        self.setMinimumSize(940, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        heading = QHBoxLayout()
        heading.addWidget(tr.label("Твої зображення", "title"))
        heading.addStretch()
        self.manage_collections = tr.button("Мої добірки…")
        self.manage_collections.clicked.connect(self.open_collections)
        heading.addWidget(self.manage_collections)
        self.refresh_button = tr.button("Оновити")
        self.refresh_button.clicked.connect(self.refresh)
        heading.addWidget(self.refresh_button)
        layout.addLayout(heading)
        layout.addWidget(tr.label("Збережені добірки й ШІ-зразки з поточних папок та архівів.", "muted"))
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Знайти за описом, моделлю або назвою файла…"))
        self.search.setAccessibleName(tr("Пошук у галереях"))
        self.scope = QComboBox()
        for title, value in [("Усі папки", "all"), ("Поточні", "current"), ("Архів", "archive")]:
            self.scope.addItem(tr(title), value)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.scope)
        layout.addLayout(filters)
        organized = QHBoxLayout()
        self.favorites = tr.button("Лише обране")
        self.favorites.setCheckable(True)
        self.favorites.setIcon(star_icon(False))
        self.favorites.setStyleSheet("QPushButton:checked { background: #433d2b; color: #ecd7a8; border-color: #b39358; }")
        self.favorites.setAccessibleName(tr("Показати лише обрані зображення"))
        self.favorites.setToolTip(tr("Зірочки з обох галерей, включно з архівними фото"))
        organized.addWidget(self.favorites)
        self.collection_filter = QComboBox()
        self.collection_filter.setAccessibleName(tr("Фільтр за добіркою"))
        organized.addWidget(self.collection_filter, 1)
        layout.addLayout(organized)
        self.tabs = QTabWidget()
        cache = ThumbnailCache()
        self.references = LibraryPage("reference", cache, self.image_deleted, self.choose_image, self.add_copy,
                                      workspace, self.organizer, parent=self)
        self.generated = LibraryPage("generated", cache, self.image_deleted, self.choose_image, self.add_copy,
                                     workspace, self.organizer, parent=self)
        self.tabs.addTab(self.references, tr("Референси"))
        self.tabs.addTab(self.generated, tr("ШІ-зображення"))
        layout.addWidget(self.tabs, 1)
        self.status = plain_label("", "muted", True)
        layout.addWidget(self.status)
        note = tr.label("Зірочка — обране. ПКМ → добірки, пошук за фото, обрізання, знайти файл або видалити до Кошика.", "muted", True)
        note.setStyleSheet("font-size:12px;")
        layout.addWidget(note)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(200)
        self.filter_timer.timeout.connect(self.filter_changed)
        self.search.textChanged.connect(lambda: self.filter_timer.start())
        self.scope.currentIndexChanged.connect(self.filter_changed)
        self.favorites.toggled.connect(self.filter_changed)
        self.collection_filter.currentIndexChanged.connect(self.filter_changed)
        self.organizer.changed.connect(self.organization_changed)
        self.organization_changed()
        if autoload:
            QTimer.singleShot(0, self.refresh)

    def filter_changed(self):
        self.favorites.setIcon(star_icon(self.favorites.isChecked()))
        for page in (self.references, self.generated):
            page.favorites_only = self.favorites.isChecked()
            page.collection_id = self.collection_filter.currentData()
            page.apply_filter(self.search.text(), self.scope.currentData())

    def organization_changed(self):
        tr = text_for(self)
        selected = self.collection_filter.currentData()
        if self.organizer.error:
            selected = None
            self.favorites.setChecked(False)
        self.collection_filter.blockSignals(True)
        self.collection_filter.clear()
        self.collection_filter.addItem(tr("Усі зображення"), None)
        if not self.organizer.error:
            for key, name in self.organizer.state.collections.items():
                self.collection_filter.addItem(name, key)
        self.collection_filter.setCurrentIndex(max(0, self.collection_filter.findData(selected)))
        self.collection_filter.blockSignals(False)
        self.manage_collections.setEnabled(not self.organizer.error)
        self.favorites.setEnabled(not self.organizer.error)
        self.collection_filter.setEnabled(not self.organizer.error)
        for page in (self.references, self.generated):
            page.collection_id = self.collection_filter.currentData()
            page.apply_filter(self.search.text(), self.scope.currentData(), reset=False)
        if self.organizer.error:
            self.status.setText(tr("Обране й добірки недоступні: {error}", error=tr(self.organizer.error)))

    def open_collections(self):
        dialog = CollectionsDialog(self.organizer, self)
        dialog.exec()
        dialog.deleteLater()

    def refresh(self):
        if self.worker is not None or self.closing:
            return
        self.organizer.reload()
        self.organizer.changed.emit()
        self.refresh_button.setEnabled(False)
        self.tabs.setEnabled(False)
        self.status.setText(text_for(self)("Переглядаю збережені добірки й архіви…"))
        self.worker = LibraryWorker(self.workspace, self.roots, self)
        self.worker.finished.connect(self.scan_finished)
        self.worker.start()

    def scan_finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        if self.closing:
            super().reject()
            return
        self.set_scan(worker.result)
        self.refresh_button.setEnabled(True)
        self.tabs.setEnabled(True)

    def choose_image(self, item):
        if self.worker is not None:
            return
        self.selected_image = item
        self.accept()

    def add_copy(self, item):
        self.images = [item] + [i for i in self.images if i.path != item.path]
        self.filter_timer.stop()
        self.search.clear()
        self.scope.setCurrentIndex(self.scope.findData("all"))
        self.favorites.setChecked(False)
        self.collection_filter.setCurrentIndex(0)
        self.update_pages()
        self.filter_changed()
        self.tabs.setCurrentIndex(1 if item.kind == "generated" else 0)
        self.status.setText(text_for(self)("Обрізану копію збережено. Можна обрати її для пошуку; оригінал незмінний."))

    def set_scan(self, result):
        tr = text_for(self)
        self.images = result.images
        self.update_pages()
        status = [tr("Знайдено {count} зображень.", count=len(self.images))]
        if result.warnings:
            status.append(tr("Пропущених записів або недоступних папок: {count}.", count=len(result.warnings)))
        if self.organizer.error:
            status.append(tr("Обране й добірки недоступні: {error}", error=tr(self.organizer.error)))
        self.status.setText(" ".join(status))
        self.status.setToolTip("\n".join(tr(warning) for warning in result.warnings[:20]))

    def update_pages(self):
        tr = text_for(self)
        for index, page in enumerate((self.references, self.generated)):
            page.set_images(self.images)
            title = tr("Референси" if index == 0 else "ШІ-зображення")
            self.tabs.setTabText(index, f"{title} · {len(page.images)}")

    def image_deleted(self, path):
        self.images = [i for i in self.images if i.path != Path(path)]
        self.update_pages()
        self.status.setText(text_for(self)("Зображення переміщено до Кошика. Після відновлення натисни «Оновити»."))
        self.file_deleted.emit(path)

    def reject(self):
        if self.worker is not None:
            self.closing = True
            self.worker.cancel.set()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
