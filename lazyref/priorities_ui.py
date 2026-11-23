"""Локальне редагування умов між плануванням і пошуком."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLineEdit, QPushButton,
                              QScrollArea, QVBoxLayout, QWidget)

from .custom_priorities import PRIORITY_CHOICES, draft_choices, validate_choices
from .library import image_record
from .library_ui import ThumbnailCache
from .ui_text import text_for


class PrioritiesDialog(QDialog):
    def __init__(self, plan, reference=None, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.selected_criteria = None
        self.rows = []
        self.setWindowTitle(tr("Власні пріоритети пошуку"))
        self.resize(960, 650)
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.addWidget(tr.label("Що важливо знайти?", "title"))
        layout.addWidget(tr.label("Обери обов’язкові умови, бажані деталі й те, що можна не враховувати. Формулювання можна змінити.", "muted", True))
        body = QHBoxLayout()
        if reference:
            preview = tr.label()
            preview.setFixedWidth(220)
            preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            try:
                pixmap = ThumbnailCache().pixmap(image_record(reference, reference.parent, "reference"))
                preview.setPixmap(pixmap.scaled(220, 260, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            except (OSError, ValueError):
                preview.setText(tr("Зразок недоступний для попереднього перегляду"))
                preview.setWordWrap(True)
            body.addWidget(preview)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.rows_layout = QVBoxLayout(content)
        self.rows_layout.setContentsMargins(0, 4, 8, 4)
        self.rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(content)
        body.addWidget(scroll, 1)
        layout.addLayout(body, 1)
        self.add_button = tr.button("+ Додати умову")
        self.add_button.clicked.connect(lambda: self.add_row({"label": "", "kind": "detail", "priority": "required", "keywords": []}))
        layout.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(tr.label("Обов’язково — визначає близький збіг. Бажано — допомагає обрати кращі фото. Не враховувати — не впливає на пошук та оцінку.", "muted", True))
        self.error = tr.label("", "muted", True)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        cancel = tr.button("Скасувати пошук")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addStretch()
        self.start_button = tr.button("Почати пошук із цими умовами")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.confirm)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        for row in draft_choices(plan):
            self.add_row(row)

    def add_row(self, criterion):
        if len(self.rows) >= 10:
            return
        tr = text_for(self)
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        text = QLineEdit(criterion["label"])
        text.setMaxLength(240)
        text.setMinimumHeight(42)
        text.setPlaceholderText(tr("Наприклад: черевик спирається на носок"))
        text.setAccessibleName(tr("Умова пошуку"))
        text.setToolTip(criterion["label"])
        text.textChanged.connect(text.setToolTip)
        text.setCursorPosition(0)
        priority = QComboBox()
        for title, value in PRIORITY_CHOICES:
            priority.addItem(tr(title), value)
        priority.setCurrentIndex(priority.findData(criterion["priority"]))
        priority.setMinimumHeight(42)
        priority.setAccessibleName(tr("Важливість умови"))
        remove = QPushButton("×")
        remove.setFixedWidth(35)
        remove.setStyleSheet("padding: 0; font-size: 20px;")
        remove.setAccessibleName(tr("Прибрати умову"))
        remove.setToolTip(tr("Прибрати цю умову зі списку"))
        row.addWidget(text, 1)
        row.addWidget(priority)
        row.addWidget(remove)
        item = {"widget": container, "text": text, "priority": priority, "original": dict(criterion)}
        remove.clicked.connect(lambda: self.remove_row(item))
        self.rows.append(item)
        self.rows_layout.addWidget(container)
        self.add_button.setEnabled(len(self.rows) < 10)

    def remove_row(self, item):
        self.rows.remove(item)
        self.rows_layout.removeWidget(item["widget"])
        item["widget"].deleteLater()
        self.add_button.setEnabled(True)

    def values(self):
        return validate_choices([{"id": f"c{i+1}", "label": row["text"].text().strip(),
            "kind": row["original"]["kind"], "priority": row["priority"].currentData(),
            "keywords": row["original"].get("keywords", []) if row["text"].text().strip() == row["original"]["label"] else []}
            for i, row in enumerate(self.rows)])

    def confirm(self):
        try:
            self.selected_criteria = self.values()
        except ValueError as exc:
            self.error.setText(text_for(self)(str(exc)))
            return
        self.accept()
