"""Рамка обрізання у координатах оригіналу, незалежно від масштабу вікна."""

import math
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from .cropping import MIN_CROP_SIDE, crop_box, display_copy_for_resize, display_rgba, load_crop_source, save_crop
from .ui_text import text_for


class CropCanvas(QWidget):
    changed = Signal()

    def __init__(self, pixels, parent=None):
        super().__init__(parent)
        self.image_size = pixels.size
        with display_copy_for_resize(pixels) as preview:
            preview.thumbnail((1600, 1200))
            with display_rgba(preview) as rgba:
                data = rgba.tobytes()
                image = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888)
                self.pixmap = QPixmap.fromImage(image.copy())
        self.selection = None
        self.drag = None
        self.setMinimumSize(420, 260)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def image_rect(self):
        w, h = self.image_size
        scale = min(max(1, self.width() - 24) / w, max(1, self.height() - 24) / h)
        return QRectF((self.width() - w * scale) / 2, (self.height() - h * scale) / 2, w * scale, h * scale)

    def image_point(self, point):
        rect = self.image_rect()
        w, h = self.image_size
        return QPointF(max(0, min(w, (point.x() - rect.x()) * w / rect.width())),
                       max(0, min(h, (point.y() - rect.y()) * h / rect.height())))

    def screen_rect(self, selection):
        rect = self.image_rect()
        scale = rect.width() / self.image_size[0]
        return QRectF(rect.x() + selection.x() * scale, rect.y() + selection.y() * scale,
                      selection.width() * scale, selection.height() * scale)

    def handles(self):
        if self.selection is None:
            return {}
        r = self.screen_rect(self.selection)
        return {"tl": r.topLeft(), "t": QPointF(r.center().x(), r.top()), "tr": r.topRight(),
                "r": QPointF(r.right(), r.center().y()), "br": r.bottomRight(),
                "b": QPointF(r.center().x(), r.bottom()), "bl": r.bottomLeft(),
                "l": QPointF(r.left(), r.center().y())}

    def box(self):
        if self.selection is None:
            return None
        r = self.selection.normalized()
        return (max(0, math.floor(r.left())), max(0, math.floor(r.top())),
                min(self.image_size[0], math.ceil(r.right())), min(self.image_size[1], math.ceil(r.bottom())))

    def reset(self):
        self.selection, self.drag = None, None
        self.changed.emit()
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.image_rect().contains(event.position()):
            return
        point = event.position()
        handle = next((name for name, p in self.handles().items()
                       if abs(p.x() - point.x()) <= 9 and abs(p.y() - point.y()) <= 9), None)
        self.anchor = self.image_point(point)
        self.original = QRectF(self.selection) if self.selection is not None else None
        if handle:
            self.drag = handle
        elif self.selection is not None and self.selection.contains(self.anchor):
            self.drag = "move"
        else:
            self.drag = "new"
            self.selection = QRectF(self.anchor, self.anchor)
        self.update()

    def mouseMoveEvent(self, event):
        if self.drag is None:
            return
        point = self.image_point(event.position())
        if self.drag == "new":
            self.selection = QRectF(self.anchor, point).normalized()
        elif self.drag == "move":
            r = self.original.translated(point - self.anchor)
            r.moveLeft(max(0, min(self.image_size[0] - r.width(), r.left())))
            r.moveTop(max(0, min(self.image_size[1] - r.height(), r.top())))
            self.selection = r
        else:
            r = QRectF(self.original)
            if "l" in self.drag: r.setLeft(point.x())
            if "r" in self.drag: r.setRight(point.x())
            if "t" in self.drag: r.setTop(point.y())
            if "b" in self.drag: r.setBottom(point.y())
            self.selection = r.normalized()
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self.drag = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0c1411"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        image = self.image_rect()
        painter.drawPixmap(image, self.pixmap, QRectF(self.pixmap.rect()))
        if self.selection is not None:
            selected = self.screen_rect(self.selection)
            shade = QColor(0, 0, 0, 140)
            for r in (QRectF(image.left(), image.top(), image.width(), selected.top() - image.top()),
                      QRectF(image.left(), selected.bottom(), image.width(), image.bottom() - selected.bottom()),
                      QRectF(image.left(), selected.top(), selected.left() - image.left(), selected.height()),
                      QRectF(selected.right(), selected.top(), image.right() - selected.right(), selected.height())):
                painter.fillRect(r, shade)
            painter.setPen(QPen(QColor("#c6edae"), 2))
            painter.drawRect(selected)
            for p in self.handles().values():
                painter.fillRect(QRectF(p.x() - 4, p.y() - 4, 8, 8), QColor("#c6edae"))


class CropDialog(QDialog):
    def __init__(self, item, workspace, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.item, self.workspace = item, workspace
        self.saved_item = None
        self.search_after_save = False
        self.setWindowTitle(tr("Обрізати й зберегти копію"))
        self.resize(980, 780)
        layout = QVBoxLayout(self)
        heading = QLabel(tr("Виділи потрібний фрагмент"))
        heading.setObjectName("title")
        layout.addWidget(heading)
        hint = QLabel(tr("Намалюй рамку мишею. Перетягни її або потягни за кути й краї. Оригінал залишиться незмінним."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        with load_crop_source(item) as pixels:
            self.canvas = CropCanvas(pixels)
        layout.addWidget(self.canvas, 1)
        self.dimensions = QLabel()
        layout.addWidget(self.dimensions)
        layout.addWidget(QLabel(tr("Що шукатимемо за фрагментом? (можна дописати пізніше)")))
        self.description = QLineEdit()
        self.description.setMaxLength(2000)
        self.description.setPlaceholderText(tr("Наприклад: черевики в такому ракурсі"))
        layout.addWidget(self.description)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        reset = tr.button("Скинути рамку")
        reset.clicked.connect(self.canvas.reset)
        cancel = tr.button("Скасувати")
        cancel.clicked.connect(self.reject)
        self.save_button = tr.button("Зберегти копію")
        self.save_button.clicked.connect(lambda: self.save_copy(False))
        self.search_button = tr.button("Зберегти й обрати для пошуку")
        self.search_button.setObjectName("primary")
        self.search_button.clicked.connect(lambda: self.save_copy(True))
        for button in (reset, cancel, self.save_button, self.search_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.canvas.changed.connect(self.selection_changed)
        self.selection_changed()

    def selection_changed(self):
        tr = text_for(self)
        box = self.canvas.box()
        valid = False
        try:
            crop_box(box, self.canvas.image_size)
            valid = True
        except ValueError:
            pass
        self.save_button.setEnabled(valid)
        self.search_button.setEnabled(valid)
        self.dimensions.setText(tr("Фрагмент: {width} × {height} px", width=box[2] - box[0], height=box[3] - box[1])
                                if box else tr("Виділи область не менше {side} × {side} px.", side=MIN_CROP_SIDE))
        self.error.clear()

    def save_copy(self, search):
        try:
            self.saved_item = save_crop(self.item, self.canvas.box(), self.workspace, self.description.text())
        except (OSError, ValueError) as exc:
            self.error.setText(text_for(self)(str(exc)))
            return
        self.search_after_save = search
        self.accept()
