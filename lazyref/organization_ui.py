"""Зірочки й редактори добірок; усі дії локальні."""

from math import cos, sin, pi

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit,
                              QListWidget, QListWidgetItem, QPushButton, QVBoxLayout)

from .library_organization import OrganizationSnapshot, OrganizationStore
from .ui_text import text_for


class OrganizationController(QObject):
    changed = Signal()

    def __init__(self, workspace, parent=None):
        super().__init__(parent)
        self.store = OrganizationStore(workspace)
        self.state = OrganizationSnapshot()
        self.error = ""
        self.reload()

    def reload(self):
        try:
            self.state = self.store.snapshot()
            self.error = ""
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def change(self, operation, parent):
        from .library_ui import show_error
        try:
            operation()
        except (OSError, ValueError) as exc:
            show_error(parent, exc)
            self.reload()
            self.changed.emit()
            return False
        self.reload()
        self.changed.emit()
        if self.error:
            show_error(parent, self.error)
        return not self.error

    def toggle(self, item, parent):
        favorite = not self.state.favorite(item)
        return self.change(lambda: self.store.set_favorite(item, favorite), parent)


def star_icon(selected):
    # Векторна зірочка не залежить від підтримки символів у шрифті Windows.
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#e6c66b" if selected else "#99ada5")
    painter.setPen(QPen(color, 1.6))
    painter.setBrush(color if selected else Qt.BrushStyle.NoBrush)
    points = [QPointF(14 + (11 if i % 2 == 0 else 5) * cos(-pi / 2 + i * pi / 5),
                     14 + (11 if i % 2 == 0 else 5) * sin(-pi / 2 + i * pi / 5)) for i in range(10)]
    painter.drawPolygon(QPolygonF(points))
    painter.end()
    return QIcon(pixmap)


class FavoriteButton(QPushButton):
    def __init__(self, item, organizer, parent=None, allowed=lambda: True):
        super().__init__(parent)
        self.item, self.organizer, self.allowed = item, organizer, allowed
        self.setFixedSize(36, 36)
        self.setIconSize(star_icon(False).actualSize(self.size()))
        self.setStyleSheet("padding: 3px; border: none;")
        self.setCheckable(True)
        self.clicked.connect(self.toggle_favorite)
        organizer.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        selected = self.organizer.state.favorite(self.item)
        self.setChecked(selected)
        self.setIcon(star_icon(selected))
        title = "Прибрати з обраного" if selected else "Додати в обране"
        tr = text_for(self)
        tr.set(self, "setAccessibleName", title)
        tr.set(self, "setToolTip", self.organizer.error or title)
        self.setEnabled(not self.organizer.error and self.allowed())

    def toggle_favorite(self):
        if self.allowed():
            self.organizer.toggle(self.item, self)
        self.refresh()


class CollectionsDialog(QDialog):
    """Без item — керування назвами; з item — вибір кількох добірок для фото."""
    def __init__(self, organizer, parent=None, item=None):
        super().__init__(parent)
        tr = text_for(self)
        self.organizer, self.item = organizer, item
        self.setWindowTitle(tr("Добірки для зображення" if item else "Мої добірки"))
        self.resize(570, 570)
        self.setMinimumSize(470, 470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.addWidget(tr.label("Додати до добірок" if item else "Мої добірки", "title"))
        layout.addWidget(tr.label("Познач усі потрібні добірки. Файл залишиться один." if item else
            "Створюй добірки для проєктів і деталей: руки, черевики, персонажі…", "muted", True))
        self.list = QListWidget()
        self.list.setAccessibleName(tr("Добірки зображень"))
        self.list.setStyleSheet("QListWidget { background: #111d19; border: 1px solid #3a4b41; border-radius: 9px; padding: 6px; }"
            "QListWidget::item { padding: 9px 7px; border-radius: 5px; }"
            "QListWidget::item:selected { background: #354b3d; color: #eaf1ec; }"
            "QListWidget::indicator { width: 18px; height: 18px; }")
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.name.setPlaceholderText(tr("Назва нової добірки"))
        self.name.setAccessibleName(tr("Назва добірки"))
        self.name.setMinimumHeight(42)
        self.create = tr.button("Створити")
        self.create.clicked.connect(self.create_collection)
        row.addWidget(self.name, 1)
        row.addWidget(self.create)
        layout.addLayout(row)
        if not item:
            actions = QHBoxLayout()
            self.rename = tr.button("Перейменувати")
            self.remove = tr.button("Видалити добірку")
            self.remove.setToolTip(tr("Видаляє лише добірку. Фото залишаються на місці."))
            self.rename.clicked.connect(self.rename_collection)
            self.remove.clicked.connect(self.delete_collection)
            actions.addWidget(self.rename)
            actions.addWidget(self.remove)
            layout.addLayout(actions)
            self.list.currentItemChanged.connect(self.selection_changed)
        layout.addWidget(tr.label("Зняття позначки або видалення добірки не видаляє фотографій.", "muted", True))
        self.empty = tr.label("", "muted", True)
        layout.addWidget(self.empty)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
            if item else QDialogButtonBox.StandardButton.Close)
        if item:
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Зберегти"))
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Скасувати"))
            self.buttons.accepted.connect(self.save_memberships)
        else:
            self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("Закрити"))
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.load(organizer.state.assigned(item) if item else set())

    def checked(self):
        return {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked}

    def load(self, checked=None, selected=None):
        checked = checked or set()
        self.list.clear()
        for key, name in self.organizer.state.collections.items():
            row = QListWidgetItem(name, self.list)
            row.setData(Qt.ItemDataRole.UserRole, key)
            if self.item:
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            if key == selected:
                self.list.setCurrentItem(row)
        self.empty.setText(text_for(self)("Створи першу добірку вище.") if not self.list.count() else "")
        if not self.item:
            self.selection_changed(self.list.currentItem())

    def selection_changed(self, current, previous=None):
        self.rename.setEnabled(current is not None)
        self.remove.setEnabled(current is not None)
        if current:
            self.name.setText(current.text())

    def create_collection(self):
        selected = self.checked()
        created = []
        if self.organizer.change(lambda: created.append(self.organizer.store.create_collection(self.name.text())), self):
            self.load(selected | set(created) if self.item else set(), created[0])
            self.name.clear()

    def rename_collection(self):
        current = self.list.currentItem()
        if current:
            key = current.data(Qt.ItemDataRole.UserRole)
            if self.organizer.change(lambda: self.organizer.store.rename_collection(key, self.name.text()), self):
                self.load(selected=key)

    def delete_collection(self):
        current = self.list.currentItem()
        if current and self.organizer.change(lambda: self.organizer.store.delete_collection(current.data(Qt.ItemDataRole.UserRole)), self):
            self.load()
            self.name.clear()

    def save_memberships(self):
        if self.organizer.change(lambda: self.organizer.store.set_collections(self.item, self.checked()), self):
            self.accept()
