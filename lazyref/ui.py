"""Вікно LazyRef. Тривалий пошук працює в QThread, не блокуючи кнопки."""

import json
import os
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QThread, Signal, Slot, QUrl, QTimer, QSignalBlocker
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from .models import Asset, SearchRequest
from . import __version__
from .pipeline import SearchPipeline
from .generation import GenerationRequest, GenerationResult, ReferenceGenerator, load_generation
from .guide_ui import GuideDialog
from .i18n import DEFAULT_LANGUAGE, LANGUAGES, message_language
from .usage import format_usage_report
from .priorities import PROFILES
from .review_session import ReviewSession, list_pending
from .retry_analysis import retry_review
from .reasoning import DEFAULT_REASONING_EFFORT, REASONING_CHOICES, REASONING_LABELS, selected_reasoning
from .urls import remote_url
from .library_ui import LibraryDialog, attach_image_actions, show_error
from .library import image_record
from .paths import PROGRAM_DIR, data_directory
from .organization_ui import FavoriteButton, OrganizationController
from .photo_filters import QUALITY_DESCRIPTION
from .settings import load_settings, normalize_settings
from .ui_text import UiText, plain_label as label, text_for

ROOT = data_directory(PROGRAM_DIR)
SETTINGS = ROOT / ".lazyref" / "settings.json"

MODEL_CHOICES = (
    ("З налаштувань Codex", ""),
    ("Sol 6", "gpt-6-sol"),
    ("Sol 6.1", "gpt-6.1-sol"),
    ("Sol 6.2", "gpt-6.2-sol"),
    ("Astra 6", "gpt-6-astra"),
    ("Astra 6.2", "gpt-6.2-astra"),
    ("Terra 6.1", "gpt-6.1-terra"),
    ("Luna 6", "gpt-6-luna"),
)


def model_label(value):
    return next((title for title, model in MODEL_CHOICES if model == value), value)

STYLE = """
QWidget { background: #101a18; color: #eaf1ec; font-family: 'Segoe UI'; font-size: 14px; }
QMainWindow { background: #101a18; }
QLabel { background: transparent; }
QLabel#muted { color: #99ada5; }
QLabel#brand { font-size: 26px; font-weight: 700; letter-spacing: -1px; }
QLabel#eyebrow { color: #bde9aa; font-size: 11px; font-weight: 600; letter-spacing: 2px; }
QLabel#title { font-size: 27px; font-weight: 600; }
QLabel#section { font-size: 16px; font-weight: 600; }
QFrame#sidebar { background: #192622; border: 1px solid #304139; border-radius: 18px; }
QWidget#sidebarFields, QWidget#generationRow { background: transparent; }
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {
    background: #111d19; border: 1px solid #3a4b41; border-radius: 9px;
    padding: 10px; selection-background-color: #5b7950;
}
QFrame#sidebar QLineEdit, QFrame#sidebar QPlainTextEdit, QFrame#sidebar QSpinBox,
QFrame#sidebar QComboBox { background: #111d19; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #bde9aa; }
QSpinBox { padding: 0px 10px; min-height: 42px; font-size: 18px; font-weight: 600; }
QSpinBox QLineEdit { padding: 0px; border: none; background: transparent; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #22332a; selection-background-color: #3d5845; padding: 6px; }
QPushButton { background: #27392f; border: 1px solid #3b5042; border-radius: 9px; padding: 10px 15px; font-weight: 600; }
QPushButton:hover { background: #354b3d; border-color: #78986c; }
QPushButton:pressed { background: #405d43; }
QPushButton#photoFilter:checked { background: #405d43; border-color: #bde9aa; }
QPushButton:disabled { color: #6b7c72; background: #202c25; border-color: #2a3a30; }
QPushButton#primary { background: #c6edae; color: #152615; border: none; font-size: 15px; padding: 15px; }
QPushButton#primary:hover { background: #d9fac7; }
QPushButton#primary:disabled { background: #5e7655; color: #c0cbb9; }
QPushButton#ghost { background: transparent; border: none; color: #a9bcb0; padding: 7px 5px; }
QPushButton#ghost:hover { color: #d5efc8; }
QPushButton#chip { font-size: 12px; font-weight: 400; padding: 7px 11px; border-radius: 14px; }
QFrame#card { background: #1b2923; border: 1px solid #34473a; border-radius: 13px; }
QLabel#badge { background: #32452d; color: #d0efbd; border-radius: 5px; padding: 4px 7px; font-size: 11px; }
QLabel#approx { background: #433d2b; color: #ecd7a8; border-radius: 5px; padding: 4px 7px; font-size: 11px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #425749; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar { border: none; background: #28372d; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #bde9aa; border-radius: 3px; }
QCheckBox, QRadioButton { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #61785e; border-radius: 4px; background: #17241c; }
QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #61785e; border-radius: 8px; background: #17241c; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #bde9aa; }
QToolTip { color: #eef4e9; background: #2e4233; border: 1px solid #66805f; padding: 7px; }
QMenu { background: #22332a; border: 1px solid #526b58; padding: 5px; }
QMenu::item { padding: 9px 20px; }
QMenu::item:selected { background: #3d5845; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #1b2923; padding: 11px 22px; border-bottom: 2px solid #304139; }
QTabBar::tab:selected { color: #d0efbd; border-bottom: 2px solid #bde9aa; }
"""


def open_source_url(url):
    """Зовнішні дані ніколи не запускають локальні/спеціальні URI handlers."""
    try:
        return QDesktopServices.openUrl(QUrl(remote_url(url)))
    except ValueError:
        return False


def app_icon() -> QIcon:
    image = QPixmap(64, 64)
    image.fill(QColor("#c6edae"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#18301e"), 4))
    painter.drawRoundedRect(14, 13, 35, 37, 5, 5)
    painter.drawEllipse(22, 21, 7, 7)
    painter.drawLine(18, 43, 29, 33)
    painter.drawLine(29, 33, 36, 39)
    painter.drawLine(36, 39, 45, 29)
    painter.end()
    return QIcon(image)


class EmptyArt(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(185)
        self.setMaximumHeight(210)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        for x, y, angle, fill in [(-70, 5, -12, "#20372b"), (65, -3, 12, "#29392a"), (0, -7, 0, "#344a32")]:
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            painter.setBrush(QColor(fill))
            painter.setPen(QPen(QColor("#60745b"), 1))
            painter.drawRoundedRect(-53, -66, 106, 132, 10, 10)
            painter.setPen(QPen(QColor("#c8e5b8"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawEllipse(-10, -43, 20, 20)
            painter.drawLine(0, -19, -7, 10)
            painter.drawLine(-7, 10, -29, 26)
            painter.drawLine(-29, 26, -9, 43)
            painter.drawLine(-7, 10, 17, 28)
            painter.drawLine(17, 28, 31, 42)
            painter.drawLine(0, -12, 27, 3)
            painter.drawLine(27, 3, 37, -7)
            painter.restore()
        painter.end()


class ReferenceCount(QSpinBox):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Qt 6.12 зі стилем NoButtons іноді лишає редактор завширшки 1 px.
        # Явна область зберігає введення й доступність стандартного QSpinBox.
        self.lineEdit().setGeometry(11, 1, max(1, self.width() - 22), max(1, self.height() - 2))


class ModelPicker(QComboBox):
    """Людські назви в списку, точні ідентифікатори у виклику Codex."""
    def __init__(self, value="", parent=None, *, text=None):
        super().__init__(parent)
        self.ui_text = text or text_for(self)
        for title, model in MODEL_CHOICES:
            self.ui_text.add_item(self, title, model)
        self.set_value(value)
        self.setMinimumWidth(160)
        self.currentIndexChanged.connect(self.update_tooltip)
        self.update_tooltip()

    def value(self):
        return self.currentData() or ""

    def set_value(self, value):
        value = (value or "").strip()
        index = self.findData(value)
        if index < 0:
            # Не підміняємо власну модель зі старих налаштувань.
            self.addItem(value, value)
            index = self.count() - 1
        self.setCurrentIndex(index)

    def update_tooltip(self):
        model = self.value()
        self.ui_text.set(self, "setToolTip", "{model}" if model else "LazyRef використовує модель із налаштувань Codex.",
                         model=model)


class ReasoningPicker(QComboBox):
    def __init__(self, value=DEFAULT_REASONING_EFFORT, parent=None, *, text=None):
        super().__init__(parent)
        tr = text or text_for(self)
        for title, effort in REASONING_CHOICES:
            self.addItem(title, effort)
        self.set_value(value)
        tr.set(self, "setAccessibleName", "Рівень міркування")
        self.setMinimumWidth(120)
        tr.set(self, "setToolTip", "Спільний рівень для нової генерації, пошуку й повторного аналізу. За замовчуванням — Medium.")

    def value(self):
        return self.currentData()

    def set_value(self, value):
        self.setCurrentIndex(self.findData(selected_reasoning(value)))


class SearchWorker(QThread):
    status = Signal(str)
    progress = Signal(int)
    completed = Signal(object)
    long_wait = Signal(bool)
    plan_ready = Signal(object)

    def __init__(self, request, *, language=DEFAULT_LANGUAGE):
        super().__init__()
        self.request = request
        self.language = language
        self.cancel = Event()
        self.plan_answer = Event()
        self.plan_choice = None

    def edit_plan(self, draft, cancel):
        from .models import check_cancel
        self.plan_answer.clear()
        self.plan_choice = None
        self.plan_ready.emit(draft)
        while not self.plan_answer.wait(0.1):
            check_cancel(cancel)
        check_cancel(cancel)
        return self.plan_choice

    def submit_plan(self, choices):
        self.plan_choice = deepcopy(choices)
        self.plan_answer.set()

    def run(self):
        try:
            with message_language(self.language):
                result = SearchPipeline(wait_changed=self.long_wait.emit, plan_editor=self.edit_plan).run(
                    self.request, self.cancel, self.status.emit, self.progress.emit)
        except Exception as exc:
            from .models import SearchResult
            result = SearchResult(None, [], self.request.count, error=str(exc),
                search_profile=self.request.search_profile, search_strategy=self.request.search_strategy,
                verify_sources=self.request.verify_sources)
        self.completed.emit(result)


class ReviewWorker(QThread):
    status = Signal(str)
    progress = Signal(int)
    completed = Signal(object)
    long_wait = Signal(bool)
    prepared = Signal(object, int, bool)

    def __init__(self, path, codex_path="", reasoning_effort=DEFAULT_REASONING_EFFORT, *,
                 save_only=False, language=DEFAULT_LANGUAGE):
        super().__init__()
        self.path, self.codex_path = path, codex_path
        self.reasoning_effort = reasoning_effort
        self.save_only = save_only
        self.language = language
        self.cancel = Event()

    def run(self):
        try:
            with message_language(self.language):
                result = retry_review(self.path, self.cancel, self.status.emit, self.progress.emit,
                                      codex_path=self.codex_path, reasoning_effort=self.reasoning_effort,
                                      wait_changed=self.long_wait.emit, save_only=self.save_only,
                                      prepared=self.prepared.emit)
        except Exception as exc:
            from .models import SearchResult
            result = SearchResult(None, [], 0, error=str(exc))
            try:
                session = ReviewSession.load_metadata(self.path)
                result.requested = session.data["request"]["count"]
                result.search_profile = session.data["request"]["search_profile"]
                result.verify_sources = session.data["request"].get("verify_sources", True)
                result.retry_path, result.pending_review = session.path, session.pending_count
                result.export_pending = session.export_pending
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.completed.emit(result)


class PendingReviewsDialog(QDialog):
    def __init__(self, output_dir, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.selected_path = None
        self.selected_save_only = None
        self.setWindowTitle(tr("Збережені кандидати та добірки"))
        self.resize(750, 470)
        layout = QVBoxLayout(self)
        layout.addWidget(tr.label("Продовжити аналіз фото", "title"))
        layout.addWidget(tr.label("Збережений опис, пріоритети й зразок залишаються тими самими. "
            "Спочатку перевіримо фото й відновимо готові оцінки у фоні. "
            "Якщо всі оцінки готові, повторюється лише збереження файлів.", "muted", True))
        self.items = QListWidget()
        layout.addWidget(self.items, 1)
        from datetime import datetime
        for session in list_pending(output_dir):
            request = session.data["request"]
            date = datetime.fromisoformat(session.data["created_at"]).astimezone().strftime("%d.%m %H:%M")
            state = (tr("очікує перевірки: {count}", count=session.pending_count)
                     if session.pending_count else tr("очікує збереження"))
            profile = tr(PROFILES[request["search_profile"]]["label"])
            item = QListWidgetItem(f"{date} · {state} · {profile}\n" + request["description"][:150])
            item.setData(Qt.ItemDataRole.UserRole, str(session.path))
            item.setData(Qt.ItemDataRole.UserRole + 1, True if session.pending_count == 0 else None)
            self.items.addItem(item)
        if not self.items.count():
            layout.addWidget(tr.label("У цій папці немає незавершених оцінок або добірок, що очікують збереження.", "muted", True))
        buttons = QHBoxLayout()
        browse = tr.button("Відкрити review.json…")
        browse.clicked.connect(lambda: self.choose_file(output_dir))
        buttons.addWidget(browse)
        buttons.addStretch()
        cancel = tr.button("Закрити")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.resume = tr.button("Продовжити")
        self.resume.setObjectName("primary")
        self.resume.setEnabled(False)
        self.resume.clicked.connect(self.choose_current)
        self.items.currentItemChanged.connect(lambda item, previous: self.resume.setEnabled(item is not None))
        buttons.addWidget(self.resume)
        layout.addLayout(buttons)

    def choose_current(self):
        if self.items.currentItem():
            self.selected_path = Path(self.items.currentItem().data(Qt.ItemDataRole.UserRole))
            self.selected_save_only = self.items.currentItem().data(Qt.ItemDataRole.UserRole + 1)
            self.accept()

    def choose_file(self, output_dir):
        tr = text_for(self)
        path, _ = QFileDialog.getOpenFileName(self, tr("Обери збережений аналіз"),
            str(Path(output_dir) / "_sessions"), tr("Аналіз LazyRef (review.json)"))
        if path:
            self.selected_path = Path(path)
            self.selected_save_only = None
            self.accept()


class UsageDialog(QDialog):
    def __init__(self, data, path=None, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.setWindowTitle(tr("Витрати Codex"))
        self.resize(740, 580)
        self.setMinimumSize(550, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(tr.label("Облік токенів", "title"))
        self.report = QPlainTextEdit(format_usage_report(data, language=tr.language))
        self.report.setReadOnly(True)
        layout.addWidget(self.report, 1)
        row = QHBoxLayout()
        if path:
            open_report = tr.button("Відкрити файл звіту")
            open_report.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
            row.addWidget(open_report)
        row.addStretch()
        close = tr.button("Закрити")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class GenerationWorker(SearchWorker):
    def run(self):
        try:
            with message_language(self.language):
                self.result = ReferenceGenerator().run(self.request, self.cancel, self.status.emit, self.progress.emit)
        except Exception as exc:
            self.result = GenerationResult(None, requested=self.request.count, error=str(exc))
        # Показуємо вибір після finished: попередній QThread уже безпечно завершений.


class GeneratedCard(QFrame):
    def __init__(self, path, index, choose, parent=None, *, on_deleted=None, can_delete=lambda: True,
                 on_search=None, on_created=None, description="", organizer=None):
        super().__init__(parent)
        tr = text_for(self)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        header = QHBoxLayout()
        header.addWidget(tr.label("ШІ-ЗРАЗОК ПОЗИ · ВАРІАНТ {index}", "approx", index=index), 1)
        layout.addLayout(header)
        preview = QLabel()
        preview.setFixedHeight(240)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setPixmap(QPixmap(str(path)).scaled(320, 240, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(preview)
        enlarge = tr.button("Відкрити зразок")
        enlarge.setObjectName("ghost")
        enlarge.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        layout.addWidget(enlarge)
        self.choose = tr.button("Шукати фото з цією позою  →")
        tr.set(self.choose, "setAccessibleName", "Обрати ШІ-варіант {index}", index=index)
        self.choose.clicked.connect(lambda: choose(path))
        layout.addWidget(self.choose)
        self.file_path = Path(path)
        self.file_actions = attach_image_actions(self, preview, path, on_deleted, can_delete,
            kind="generated", description=description, on_search=on_search, on_created=on_created, workspace=ROOT, organizer=organizer)
        if self.file_actions:
            self.favorite_button = FavoriteButton(self.file_actions.item, self.file_actions.organizer, self, can_delete)
            header.addWidget(self.favorite_button)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        settings, _ = normalize_settings(settings, ROOT)
        tr = UiText(settings.get("language", DEFAULT_LANGUAGE))
        self.setWindowTitle(tr("Налаштування LazyRef"))
        self.setMinimumWidth(570)
        self.resize(610, 680)
        self.setMinimumHeight(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(tr.label("Налаштування LazyRef", "title"))
        general = QFormLayout()
        self.language = QComboBox()
        for code, name in LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(self.language.findData(tr.language))
        self.language.setAccessibleName(tr("Мова інтерфейсу"))
        general.addRow(tr("Мова інтерфейсу"), self.language)
        self.guide_button = tr.button("Відкрити інструкцію")
        self.guide_button.clicked.connect(self.open_guide)
        general.addRow("", self.guide_button)
        layout.addLayout(general)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 8, 12, 8)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        body.addWidget(tr.label("Пошук і підключення", "section"))
        form = QFormLayout()
        form.setVerticalSpacing(15)
        self.enhanced_dedup = QPushButton()
        self.high_quality_only = QPushButton()
        for button, key, title, explanation in (
            (self.enhanced_dedup, "enhanced_dedup", "Посилений відсів повторів",
             "Додатково порівнює перевірені фото з помірним обрізанням або водяним знаком. Локально, без окремих викликів Codex."),
            (self.high_quality_only, "high_quality_only", "Шукати тільки високоякісні фото",
             tr(QUALITY_DESCRIPTION) + " " + tr("Це поріг роздільності, а не гарантія різкості.")),
        ):
            title, explanation = tr(title), tr(explanation)
            button.setCheckable(True)
            button.setObjectName("photoFilter")
            button.setChecked(settings.get(key, False) is True)
            button.setAccessibleName(title)
            button.setMinimumHeight(42)
            button.setToolTip(explanation)
            def update_filter(checked, control=button, caption=title):
                control.setText(caption + (": ON" if checked else ": OFF"))
            button.toggled.connect(update_filter)
            update_filter(button.isChecked())
            form.addRow(button)
            form.addRow(label(explanation, "muted", True))
        self.strategy = QComboBox()
        self.strategy.addItem(tr("Широкий пошук · новий"), "wide")
        self.strategy.addItem(tr("Попередній пошук · 0.3.4"), "legacy")
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(settings.get("search_strategy", "wide"))))
        self.strategy.setAccessibleName(tr("Алгоритм пошуку"))
        form.addRow(tr("Алгоритм агента"), self.strategy)
        self.discovery_limit = QSpinBox()
        self.discovery_limit.setRange(20, 300)
        self.discovery_limit.setSingleStep(20)
        self.discovery_limit.setValue(settings.get("discovery_limit", 200))
        form.addRow(tr("Новий: максимум кандидатів"), self.discovery_limit)
        self.detail_limit = QSpinBox()
        self.detail_limit.setRange(8, 120)
        self.detail_limit.setValue(settings.get("detail_limit", 40))
        form.addRow(tr("Новий: нових детальних оцінок"), self.detail_limit)
        self.reuse_cache = QCheckBox(tr("Повторно використовувати попередні результати"))
        self.reuse_cache.setChecked(settings.get("reuse_cache", True))
        self.reuse_cache.setToolTip(tr("Фото й оцінки для того самого запиту зберігаються локально. Для незалежного порівняння режимів вимкни кеш."))
        form.addRow(self.reuse_cache)
        self.limit = QComboBox()
        for value, text in [(8, "8 — короткий тест"), (24, "24 — швидкий пошук"), (48, "48 — середня глибина"), (72, "72 — рекомендовано"), (96, "96 — більше варіантів"), (120, "120 — найбільша добірка")]:
            self.limit.addItem(tr(text), value)
        index = self.limit.findData(settings.get("candidate_limit", 72))
        if index < 0:
            value = settings["candidate_limit"]
            self.limit.addItem(str(value), value)
            index = self.limit.count() - 1
        self.limit.setCurrentIndex(max(0, index))
        form.addRow(tr("Попередній алгоритм: перевірок"), self.limit)
        def strategy_changed():
            wide = self.strategy.currentData() == "wide"
            for control in (self.discovery_limit, self.detail_limit, self.reuse_cache):
                control.setEnabled(wide)
        self.strategy.currentIndexChanged.connect(strategy_changed)
        strategy_changed()
        self.rounds = QComboBox()
        for value in (1, 2, 4, 6, 8):
            self.rounds.addItem(str(value), value)
        index = self.rounds.findData(settings.get("search_rounds", 6))
        if index < 0:
            value = settings["search_rounds"]
            self.rounds.addItem(str(value), value)
            index = self.rounds.count() - 1
        self.rounds.setCurrentIndex(index)
        form.addRow(tr("Максимум циклів агента"), self.rounds)
        self.path = QLineEdit(settings.get("codex_path", ""))
        self.path.setPlaceholderText(tr("Знайти codex.exe автоматично"))
        form.addRow("Codex CLI", self.path)
        self.model = ModelPicker(settings.get("model", ""), text=tr)
        self.model.setAccessibleName(tr("Модель пошуку"))
        form.addRow(tr("Модель пошуку"), self.model)
        self.generation_model = ModelPicker(settings.get("generation_model", settings.get("model", "")), text=tr)
        self.generation_model.setAccessibleName(tr("Модель генерації ШІ-зразків"))
        form.addRow(tr("Модель генерації ШІ-зразків"), self.generation_model)
        self.reasoning = ReasoningPicker(settings.get("reasoning_effort"), text=tr)
        form.addRow(tr("Рівень міркування"), self.reasoning)
        self.screenshots = QCheckBox(tr("Скріншот, якщо фото не завантажується"))
        self.screenshots.setChecked(settings.get("screenshots", True))
        form.addRow(self.screenshots)
        self.artistic_nudity = QCheckBox(tr("Дозволяти художню натуру дорослих (18+)"))
        self.artistic_nudity.setChecked(settings.get("artistic_nudity", True))
        self.artistic_nudity.setToolTip(tr("Дозволяє нееротичні анатомічні референси дорослих в обох режимах перевірки."))
        form.addRow(self.artistic_nudity)
        body.addLayout(form)
        body.addWidget(tr.label("Новий алгоритм: збір кандидатів → короткий відбір мініатюр → перевірка джерел і детальний огляд найкращих. Ліміти — максимуми, а не гарантована кількість фото. Попередній алгоритм доступний для порівняння й повернення до старого пошуку.", "muted", True))
        body.addWidget(tr.label("Перевірка джерела ON/OFF перемикається в головному вікні. OFF оцінює відповідність пози без перевірки походження. Скріншот зі сторінки доступний лише при ON. Навіть ON не гарантує автентичність.", "muted", True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Зберегти"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Скасувати"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # QFormLayout стискає рядки при довгих поясненнях; padding стилю потребує висоти.
        for widget_type in (QLineEdit, QComboBox, QSpinBox):
            for field in self.findChildren(widget_type):
                field.setMinimumHeight(42)

    def values(self):
        return {"candidate_limit": self.limit.currentData(), "search_rounds": self.rounds.currentData(), "codex_path": self.path.text().strip(),
            "search_strategy": self.strategy.currentData(), "discovery_limit": self.discovery_limit.value(),
            "detail_limit": self.detail_limit.value(), "reuse_cache": self.reuse_cache.isChecked(),
            "enhanced_dedup": self.enhanced_dedup.isChecked(), "high_quality_only": self.high_quality_only.isChecked(),
            "model": self.model.value(), "generation_model": self.generation_model.value(),
            "reasoning_effort": self.reasoning.value(), "language": self.language.currentData(),
            "screenshots": self.screenshots.isChecked(), "artistic_nudity": self.artistic_nudity.isChecked()}

    def open_guide(self):
        guide = GuideDialog(self.language.currentData(), self)
        guide.exec()
        guide.deleteLater()


class AccessLinksDialog(QDialog):
    def __init__(self, links, parent):
        super().__init__(parent)
        tr = text_for(self)
        self.setWindowTitle(tr("Посилання для ручного перегляду"))
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(tr.label("Роботи з обмеженим доступом", "title"))
        layout.addWidget(tr.label("Ці зображення не завантажені й не оцінені. Відкрий роботу у своєму браузері; для 18+ на DeviantArt може знадобитися вхід і Browsing Mode → Mature.", "muted", True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        for link in links:
            card = QFrame()
            card.setObjectName("card")
            row = QVBoxLayout(card)
            row.addWidget(label(link["title"], "section", True))
            row.addWidget(tr.label(link["reason"], "muted", True))
            button = tr.button("Відкрити на DeviantArt")
            button.clicked.connect(lambda checked=False, url=link["url"]: open_source_url(url))
            row.addWidget(button)
            rows.addWidget(card)
        rows.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText(tr("Закрити"))
        close.rejected.connect(self.reject)
        layout.addWidget(close)


class ImageCard(QFrame):
    def __init__(self, asset: Asset, index: int, *, on_deleted=None, can_delete=lambda: True,
                 on_search=None, on_created=None, description="", organizer=None, parent=None):
        super().__init__(parent)
        tr = text_for(self)
        self.setObjectName("card")
        self.setMinimumWidth(235)
        self.setMaximumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 13, 13, 13)
        picture = QLabel()
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        picture.setFixedHeight(215)
        picture.setPixmap(QPixmap(str(asset.path)).scaled(290, 210, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        tr.set(picture, "setToolTip", "Відкрити фото кнопкою нижче. Пропорції збережено.")
        layout.addWidget(picture)
        row = QHBoxLayout()
        row.addWidget(label(f"{index:02d}", "muted"))
        row.addStretch()
        close = asset.assessment.is_close
        row.addWidget(tr.label("Близький збіг" if close else "Часткова альтернатива", "badge" if close else "approx"))
        layout.addLayout(row)

        self.file_path = asset.path
        self.file_actions = attach_image_actions(self, picture, asset.path, on_deleted, can_delete,
            description=description, on_search=on_search, on_created=on_created, workspace=ROOT, organizer=organizer,
            review_owned=asset.review_owned)
        if self.file_actions:
            self.favorite_button = FavoriteButton(self.file_actions.item, self.file_actions.organizer, self, can_delete)
            row.addWidget(self.favorite_button)
        if not asset.assessment.verify_sources:
            layout.addWidget(tr.label("Походження не перевірено", "approx", True))
        title = label(asset.candidate.source, "section")
        layout.addWidget(title)
        if asset.candidate.creator:
            author = tr.label("Фото: {creator}", "muted", True, creator=asset.candidate.creator)
            author.setToolTip(asset.candidate.license)
            layout.addWidget(author)
        reason = label(asset.assessment.reason, "muted", True)
        reason.setMinimumHeight(62)
        reason.setMaximumHeight(92)
        tr.set(reason, "setToolTip", "{reason}\n\nВідмінності: {misses}", reason=asset.assessment.reason,
               misses="; ".join(asset.assessment.misses))
        layout.addWidget(reason)
        if asset.assessment.unmet_criteria:
            layout.addWidget(tr.label("Не підтверджено: {criteria}", "muted", True,
                                      criteria="; ".join(asset.assessment.unmet_criteria)))
        if asset.assessment.unmet_preferences:
            layout.addWidget(tr.label("Відмінності в деталях: {details}", "muted", True,
                                      details="; ".join(asset.assessment.unmet_preferences)))
        row = QHBoxLayout()
        open_file = tr.button("Відкрити фото")
        open_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(asset.path))))
        row.addWidget(open_file)
        if asset.candidate.page_url:
            source = tr.button("Джерело")
            source.setObjectName("ghost")
            if asset.candidate.source_notes:
                tr.set(source, "setToolTip", "{notes}\nВідомості джерела не гарантують автентичність.",
                       notes=asset.candidate.source_notes)
            source.clicked.connect(lambda: open_source_url(asset.candidate.page_url))
            row.addWidget(source)
        layout.addLayout(row)


def format_result_summary(tr, result, close_count, request):
    count = len(result.assets)
    if count:
        text = tr("Близьких: {close} · часткових: {partial} · запитано: {requested} · оцінено: {examined}",
                  close=close_count, partial=count - close_count, requested=result.requested, examined=result.examined)
    else:
        text = tr("Збережено {count} із {requested} · оцінено {examined} · повторів {duplicates}",
                  count=count, requested=result.requested, examined=result.examined, duplicates=result.duplicates)
    if result.access_links:
        text += tr(" · посилань без фото: {count}", count=len(result.access_links))
    if result.cancelled:
        text = tr("Пошук зупинено.") + " " + text
    if result.error:
        text = tr("Пошук не завершено.") + " " + text
    lines = [text]
    if result.search_strategy == "wide":
        lines.append(tr("Кандидатів: {discovered} · відбір мініатюр: {screened} · оцінок із кешу: {reused}",
                        discovered=result.discovered, screened=result.screened, reused=result.reused_assessments))
    lines.append(tr("Перевірка джерела: {state}",
                    state="ON" if result.verify_sources else tr("OFF · Походження не перевірено")))
    if result.enhanced_dedup or result.high_quality_only:
        filters = tr("Посилений відсів повторів: {dedup} · повторів: {duplicates} · Висока роздільність: {quality}",
                     dedup="ON" if result.enhanced_dedup else "OFF", duplicates=result.duplicates,
                     quality="ON" if result.high_quality_only else "OFF")
        if result.high_quality_only:
            filters += tr(" · замалих фото відсіяно: {count}", count=result.low_resolution_rejected)
        lines.append(filters)
    profile = tr("Пріоритети: {profile}", profile=tr(PROFILES[result.search_profile]["label"]))
    if request is not None:
        model = tr("Модель цієї спроби: {model} · {effort}", model=tr(model_label(request.model)),
                   effort=REASONING_LABELS[request.reasoning_effort])
        profile = model + " · " + profile
    lines.append(profile)
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(app_icon())
        self.resize(1260, 860)
        self.setMinimumSize(1060, 730)
        self.worker = None
        self._busy = False
        self.organizer = OrganizationController(ROOT, self)
        self.generation_result = None
        self.selected_library_image = None
        self.usage_data = {}
        self.usage_path = None
        self.retry_path = None
        self.pending_review = 0
        self.export_pending = False
        self.generation_search_request = None
        self.last_search_request = None
        self.last_folder = None
        self.priorities_dialog = None
        self.access_links = []
        self.close_when_done = False
        self.loaded_settings = load_settings(SETTINGS, ROOT)
        self.settings = self.loaded_settings.data
        tr = self.ui_text = UiText(self.settings.get("language", DEFAULT_LANGUAGE))
        tr.set(self, "setWindowTitle", "LazyRef — фотореференси для художників")
        # Оновлюємо тільки старий типовий ліміт; інші свідомо вибрані значення зберігаємо.
        if self.settings.get("search_revision", 1) < 2:
            if self.settings.get("candidate_limit", 24) == 24:
                self.settings["candidate_limit"] = 72
            self.settings["search_revision"] = 2
        # Особиста версія: користувачка явно дозволила художні референси 18+.
        self.settings.setdefault("artistic_nudity", True)
        self.settings.setdefault("search_strategy", "wide")
        # До 0.6.0 одна модель керувала обома етапами. Зберігаємо цей вибір при міграції.
        self.settings.setdefault("generation_model", self.settings.get("model", ""))
        self.settings["reasoning_effort"] = selected_reasoning(self.settings.get("reasoning_effort"))
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(27, 22, 27, 22)
        outer.setSpacing(22)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(35, 35))
        header.addWidget(logo)
        header.addSpacing(7)
        header.addWidget(label("LazyRef", "brand"))
        header.addSpacing(20)
        header.addWidget(tr.label("Знайди позу. Почни малювати.", "muted"))
        header.addStretch()
        self.library_button = tr.button("Галереї")
        self.library_button.setObjectName("ghost")
        tr.set(self.library_button, "setToolTip", "Усі збережені референси й ШІ-зображення, включно з архівами")
        self.library_button.clicked.connect(self.open_library)
        header.addWidget(self.library_button)
        self.folder_button = tr.button("Відкрити папку")
        self.folder_button.setObjectName("ghost")
        tr.set(self.folder_button, "setToolTip", "Відкрити папку поточної добірки; до першого пошуку — папку збереження")
        self.folder_button.clicked.connect(self.open_folder)
        header.addWidget(self.folder_button)
        settings = tr.button("Налаштування")
        settings.setObjectName("ghost")
        settings.clicked.connect(self.edit_settings)
        self.settings_button = settings
        header.addWidget(settings)
        outer.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(28)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(355)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        fields = QWidget()
        fields.setObjectName("sidebarFields")
        left = QVBoxLayout(fields)
        left.setContentsMargins(22, 24, 22, 10)
        left.setSpacing(10)
        left.addWidget(tr.label("НОВА ДОБІРКА", "eyebrow"))
        left.addWidget(tr.label("Що шукаємо?", "title", True))
        left.addWidget(tr.label("Опиши позу, ракурс і важливі деталі.", "muted", True))
        self.description = QPlainTextEdit()
        tr.set(self.description, "setAccessibleName", "Опис референсу")
        tr.set(self.description, "setPlaceholderText", "Чоловік навпочіпки, корпус у ¾, руки витягнуті до камери біля підлоги…")
        self.description.setMinimumHeight(90)
        self.description.setMaximumHeight(110)
        left.addWidget(self.description)
        left.addWidget(tr.label("Кількість референсів"))
        self.count = ReferenceCount()
        self.count.setRange(1, 40)
        self.count.setValue(6)
        self.count.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tr.set(self.count, "setAccessibleName", "Кількість референсів")
        count_row = QHBoxLayout()
        minus = QPushButton("−")
        plus = QPushButton("+")
        minus.setFixedWidth(44)
        plus.setFixedWidth(44)
        tr.set(minus, "setAccessibleName", "Менше референсів")
        tr.set(plus, "setAccessibleName", "Більше референсів")
        minus.clicked.connect(self.count.stepDown)
        plus.clicked.connect(self.count.stepUp)
        count_row.addWidget(minus)
        count_row.addWidget(self.count, 1)
        count_row.addWidget(plus)
        self.count_buttons = (minus, plus)
        left.addLayout(count_row)
        left.addWidget(tr.label("Режим пошуку"))
        self.provider = QComboBox()
        tr.add_item(self.provider, "Агент Codex · пошук зображень", "agent")
        tr.add_item(self.provider, "ШІ-зразок → реальні фото", "agent_reference")
        tr.add_item(self.provider, "Зображення → референси", "image_reference")
        self.provider.currentIndexChanged.connect(self.source_changed)
        tr.set(self.provider, "setAccessibleName", "Джерело фотографій")
        left.addWidget(self.provider)
        self.source_hint = label("", "muted", True)
        self.source_hint.setStyleSheet("font-size:12px;")
        left.addWidget(self.source_hint)
        self.pick_image_button = tr.button("Обрати зображення з галереї")
        self.pick_image_button.clicked.connect(self.open_library)
        left.addWidget(self.pick_image_button)
        self.import_image_button = tr.button("Обрати фото з комп’ютера")
        self.import_image_button.clicked.connect(self.import_image)
        left.addWidget(self.import_image_button)
        self.generation_model_row = QWidget()
        self.generation_model_row.setObjectName("generationRow")
        generation_model_layout = QVBoxLayout(self.generation_model_row)
        generation_model_layout.setContentsMargins(0, 0, 0, 0)
        generation_model_layout.addWidget(tr.label("Модель генерації ШІ-зразків"))
        self.generation_model = ModelPicker(self.settings["generation_model"], text=tr)
        tr.set(self.generation_model, "setAccessibleName", "Модель генерації ШІ-зразків")
        generation_model_layout.addWidget(self.generation_model)
        generation_model_hint = tr.label("Модель агента Codex, який викликає генератор зображень. Рівень міркування — над галереєю.", "muted", True)
        generation_model_hint.setStyleSheet("font-size:12px;")
        generation_model_layout.addWidget(generation_model_hint)
        left.addWidget(self.generation_model_row)
        self.verify_sources = QPushButton()
        self.verify_sources.setCheckable(True)
        self.verify_sources.setChecked(self.settings.get("verify_sources", True) is not False)
        tr.set(self.verify_sources, "setAccessibleName", "Перевірка джерела ON OFF")
        tr.set(self.verify_sources, "setToolTip", "ON: перевіряти походження й ознаки генерації. OFF: оцінювати позу; джерело необов’язкове, результати неперевірені.")
        self.verification_hint = label("", "muted", True)
        self.verification_hint.setStyleSheet("font-size:12px;")
        self.verify_sources.toggled.connect(self.verification_changed)
        self.verification_changed()
        left.addWidget(self.verify_sources)
        left.addWidget(self.verification_hint)
        left.addWidget(tr.label("Пріоритети пошуку"))
        self.search_profile = QComboBox()
        for key, profile in PROFILES.items():
            tr.add_item(self.search_profile, profile["label"], key)
        tr.set(self.search_profile, "setAccessibleName", "Пріоритети пошуку")
        self.search_profile.setCurrentIndex(max(0, self.search_profile.findData(self.settings.get("search_profile", "strict"))))
        left.addWidget(self.search_profile)
        self.profile_hint = label("", "muted", True)
        self.profile_hint.setStyleSheet("font-size:12px;")
        left.addWidget(self.profile_hint)
        self.search_profile.currentIndexChanged.connect(self.profile_changed)
        self.profile_changed()
        self.generation_row = QWidget()
        self.generation_row.setObjectName("generationRow")
        generation_layout = QHBoxLayout(self.generation_row)
        generation_layout.setContentsMargins(0, 0, 0, 0)
        generation_layout.addWidget(tr.label("ШІ-варіантів", wrap=True), 1)
        self.generation_count = ReferenceCount()
        self.generation_count.setRange(1, 8)
        self.generation_count.setValue(max(1, min(8, self.settings.get("generation_count", 3))))
        tr.set(self.generation_count, "setAccessibleName", "Кількість ШІ-варіантів")
        self.generation_count.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.generation_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.generation_count.setFixedWidth(58)
        fewer = QPushButton("−")
        more = QPushButton("+")
        fewer.setFixedWidth(38)
        more.setFixedWidth(38)
        tr.set(fewer, "setAccessibleName", "Менше ШІ-варіантів")
        tr.set(more, "setAccessibleName", "Більше ШІ-варіантів")
        fewer.clicked.connect(self.generation_count.stepDown)
        more.clicked.connect(self.generation_count.stepUp)
        generation_layout.addWidget(fewer)
        generation_layout.addWidget(self.generation_count)
        generation_layout.addWidget(more)
        left.addWidget(self.generation_row)
        self.open_generation_button = tr.button("Відкрити ШІ-зразки")
        tr.set(self.open_generation_button, "setToolTip", "Відкрити збережену добірку без повторної генерації")
        self.open_generation_button.clicked.connect(self.open_saved_generation)
        left.addWidget(tr.label("Зберегти до"))
        folder_row = QHBoxLayout()
        self.output = QLineEdit(self.settings.get("output_dir", str(ROOT / "references")))
        tr.set(self.output, "setAccessibleName", "Папка збереження")
        self.output.setToolTip(self.output.text())
        folder_row.addWidget(self.output)
        browse = QPushButton("…")
        browse.setFixedWidth(40)
        tr.set(browse, "setAccessibleName", "Обрати папку збереження")
        browse.clicked.connect(self.choose_output)
        folder_row.addWidget(browse)
        left.addLayout(folder_row)
        left.addStretch()
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form_scroll.setWidget(fields)
        sidebar_layout.addWidget(form_scroll, 1)
        actions = QVBoxLayout()
        actions.setContentsMargins(22, 6, 22, 18)
        self.search = tr.button("Знайти референси  →")
        self.search.setObjectName("primary")
        self.search.clicked.connect(self.start_search)
        actions.addWidget(self.search)
        actions.addWidget(self.open_generation_button)
        self.stop = tr.button("Зупинити пошук")
        self.stop.clicked.connect(self.cancel_search)
        self.stop.hide()
        actions.addWidget(self.stop)
        cloud_note = tr.label("Через твій Codex. Описи й зображення обробляються у хмарі.", "muted", True)
        cloud_note.setStyleSheet("font-size:12px;")
        actions.addWidget(cloud_note)
        sidebar_layout.addLayout(actions)
        left_column = QVBoxLayout()
        left_column.setSpacing(22)
        left_column.addWidget(sidebar, 1)
        left_column.addWidget(label("LAZYREF  /  " + __version__, "eyebrow"))
        body.addLayout(left_column)
        right = QVBoxLayout()
        right.setSpacing(13)
        gallery_header = QHBoxLayout()
        gallery_header.addWidget(tr.label("Референси", "title"))
        gallery_header.addStretch()
        right.addLayout(gallery_header)
        model_row = QGridLayout()
        model_row.addWidget(tr.label("Модель пошуку"), 0, 0)
        model_row.addWidget(tr.label("Рівень міркування"), 0, 1)
        self.search_model = ModelPicker(self.settings.get("model", ""), text=tr)
        tr.set(self.search_model, "setAccessibleName", "Модель пошуку")
        model_row.addWidget(self.search_model, 1, 0)
        self.reasoning = ReasoningPicker(self.settings["reasoning_effort"], text=tr)
        self.reasoning.setMaximumWidth(170)
        model_row.addWidget(self.reasoning, 1, 1)
        model_row.setColumnStretch(0, 1)
        self.repeat_search_button = tr.button("Повторити пошук")
        tr.set(self.repeat_search_button, "setToolTip", "Новий пошук обраною моделлю й рівнем міркування за попереднім описом і зразком. Попередня добірка залишається у своїй папці.")
        self.repeat_search_button.clicked.connect(self.repeat_last_search)
        self.repeat_search_button.hide()
        model_row.addWidget(self.repeat_search_button, 1, 2)
        right.addLayout(model_row)
        self.info_title = "Інформація про пошук"
        self.info_button = QPushButton()
        self.info_button.setObjectName("ghost")
        self.info_button.setCheckable(True)
        tr.set(self.info_button, "setAccessibleName", "Розгорнути або згорнути інформацію про пошук")
        self.info_button.toggled.connect(self.toggle_search_info)
        self.info_button.hide()
        right.addWidget(self.info_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.info_content = QWidget()
        self.info_content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        info_layout = QVBoxLayout(self.info_content)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(10)
        self.info_content.hide()
        self.toggle_search_info(False)
        right.addWidget(self.info_content)
        self.repeat_hint = tr.label("Обери модель й рівень та повтори пошук. Опис і зразок залишаться тими самими; попередні результати не використовуються.", "muted", True)
        self.repeat_hint.setStyleSheet("font-size:12px;")
        self.repeat_hint.hide()
        info_layout.addWidget(self.repeat_hint)
        self.subtitle = label("", "muted", True)
        self.subtitle.hide()
        info_layout.addWidget(self.subtitle)
        self.long_wait_panel = QFrame()
        self.long_wait_panel.setObjectName("longWait")
        self.long_wait_panel.setStyleSheet("QFrame#longWait { border: 1px solid #9d854c; border-radius: 10px; background: #283027; }")
        wait_layout = QHBoxLayout(self.long_wait_panel)
        wait_text = QVBoxLayout()
        wait_text.addWidget(tr.label("Це займе більше часу. Хочете перервати пошук?", "section", True))
        wait_text.addWidget(tr.label("Очікування триває. Автоматична зупинка — після 500 секунд поточного виклику.", "muted", True))
        wait_layout.addLayout(wait_text, 1)
        self.long_wait_stop = tr.button("Перервати пошук")
        self.long_wait_stop.clicked.connect(self.cancel_search)
        wait_layout.addWidget(self.long_wait_stop)
        self.long_wait_panel.hide()
        right.addWidget(self.long_wait_panel)
        self.retry_row = QWidget()
        retry_layout = QHBoxLayout(self.retry_row)
        retry_layout.setContentsMargins(0, 0, 0, 0)
        self.retry_button = tr.button("Повторити аналіз")
        self.retry_button.setObjectName("primary")
        self.retry_button.clicked.connect(self.retry_current_review)
        retry_layout.addWidget(self.retry_button)
        self.retry_hint = label("", "muted", True)
        retry_layout.addWidget(self.retry_hint, 1)
        self.retry_row.hide()
        right.addWidget(self.retry_row)
        self.guide_banner = QWidget()
        guide_layout = QHBoxLayout(self.guide_banner)
        guide_layout.setContentsMargins(0, 0, 0, 0)
        self.guide_thumbnail = QLabel()
        self.guide_thumbnail.setFixedSize(65, 65)
        guide_layout.addWidget(self.guide_thumbnail)
        self.guide_caption = tr.label("Обраний зразок\nУ добірці нижче — знайдені референси.", "muted", True)
        guide_layout.addWidget(self.guide_caption, 1)
        self.variants_button = tr.button("Обрати інший зразок")
        self.variants_button.clicked.connect(lambda: self.show_generated(self.generation_result))
        guide_layout.addWidget(self.variants_button)
        self.guide_banner.hide()
        info_layout.addWidget(self.guide_banner)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.gallery = QWidget()
        self.grid = QGridLayout(self.gallery)
        self.grid.setContentsMargins(0, 10, 5, 10)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.gallery)
        # Лише одна сторінка видима: підказка, очікування або результати.
        self.content = QStackedWidget()
        self.content.addWidget(self.scroll)
        self.blank = QWidget()
        self.content.addWidget(self.blank)
        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.addStretch(1)
        self.empty_art = EmptyArt()
        empty_layout.addWidget(self.empty_art)
        empty_title = tr.label("Почни з того, що уявляєш", "title")
        self.empty_title = empty_title
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        caption = tr.label("Опиши референс — агент шукатиме зображення,\nперевірятиме їх і уточнюватиме пошук до добірки.", "muted", True)
        self.empty_caption = caption
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(caption)
        empty_layout.addSpacing(16)
        self.empty_examples = QWidget()
        examples = QHBoxLayout(self.empty_examples)
        examples.setContentsMargins(0, 0, 0, 0)
        examples.addStretch()
        for text, prompt in [("Поза навпочіпки", "Людина сидить навпочіпки, видно все тіло, ракурс збоку"), ("Руки в перспективі", "Людина простягає руку до камери, кисть на передньому плані")]:
            button = tr.button(text)
            button.setObjectName("chip")
            button.clicked.connect(lambda checked=False, p=prompt: self.description.setPlainText(tr(p)))
            examples.addWidget(button)
        examples.addStretch()
        empty_layout.addWidget(self.empty_examples)
        empty_layout.addStretch(2)
        self.content.addWidget(self.empty)
        self.content.setCurrentWidget(self.empty)
        right.addWidget(self.content, 1)
        self.status = tr.label("Готова до пошуку", "muted", True)
        right.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setValue(0)
        right.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(115)
        self.details.hide()
        right.addWidget(self.details)
        self.details_button = tr.button("Показати перебіг пошуку")
        self.details_button.setObjectName("ghost")
        self.details_button.clicked.connect(lambda: self.details.setVisible(not self.details.isVisible()))
        report_buttons = QHBoxLayout()
        report_buttons.addWidget(self.details_button)
        self.usage_button = tr.button("Витрати Codex")
        self.usage_button.setObjectName("ghost")
        self.usage_button.setEnabled(False)
        self.usage_button.clicked.connect(self.show_usage)
        report_buttons.addWidget(self.usage_button)
        provenance_note = tr.label("Оцінка ШІ не є підтвердженням походження фото.", "muted", True)
        provenance_note.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        provenance_note.setStyleSheet("font-size:12px;")
        report_buttons.addWidget(provenance_note, 1)
        right.addLayout(report_buttons)
        self.access_links_button = tr.button("Посилання для ручного перегляду")
        self.access_links_button.setObjectName("ghost")
        self.access_links_button.clicked.connect(self.show_access_links)
        self.access_links_button.hide()
        right.addWidget(self.access_links_button, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)
        self.source_changed()
        self.search_model.currentIndexChanged.connect(self.execution_settings_changed)
        self.generation_model.currentIndexChanged.connect(self.execution_settings_changed)
        self.reasoning.currentIndexChanged.connect(self.execution_settings_changed)
        if self.loaded_settings.warning:
            self.log(self.loaded_settings.warning)
            self.details.show()

    def source_changed(self):
        tr = self.ui_text
        provider = self.provider.currentData()
        self.generation_row.setVisible(provider == "agent_reference")
        self.generation_model_row.setVisible(provider == "agent_reference")
        self.open_generation_button.setVisible(provider == "agent_reference")
        self.pick_image_button.setVisible(provider == "image_reference")
        self.import_image_button.setVisible(provider == "image_reference")
        tr.set(self.search, "setText",
               "Створити ШІ-варіанти  →" if provider == "agent_reference" else "Знайти референси  →")
        hint = ("Створи ШІ-варіанти, обери позу й шукай референси через Codex." if provider == "agent_reference" else
                "Обери фото з комп’ютера або галереї та опиши, що потрібно знайти. Власне фото збережеться в галереї як копія." if provider == "image_reference" else
                "Codex шукає зображення у вебі за описом. Окремі бібліотеки й API-ключі не потрібні.")
        strategy = "Широкий пошук." if self.settings.get("search_strategy", "wide") == "wide" else "Попередній алгоритм."
        hint = tr(hint) + " " + tr(strategy)
        self.source_hint.setText(hint)
        self.provider.setToolTip(hint)

    def verification_changed(self):
        enabled = self.verify_sources.isChecked()
        self.ui_text.set(self.verify_sources, "setText", "Перевірка джерела: {state}", state="ON" if enabled else "OFF")
        self.verify_sources.setStyleSheet("background:#344d35; border-color:#7da46a;" if enabled else
                                         "background:#463b28; border-color:#b39358;")
        self.ui_text.set(self.verification_hint, "setText",
                         "Перевірка походження та фотографічності." if enabled else
                         "Лише відповідність пози. Походження не перевірено.")

    def profile_changed(self):
        self.ui_text.set(self.profile_hint, "setText",
            "Перед пошуком з’явиться список умов: обери обов’язкові, бажані й ті, що можна не враховувати."
            if self.search_profile.currentData() == "custom" else
            "Головне — поза, ракурс і опора. Одяг, оголеність та оточення — побажання. Для обов’язкових деталей обери «Точний опис»."
            if self.search_profile.currentData() == "pose" else
            "Попередні правила: поза, ракурс, одяг і сцена з опису враховуються як обов’язкові умови.")

    def show_access_links(self):
        AccessLinksDialog(self.access_links, self).exec()

    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, self.ui_text("Куди зберігати референси"), self.output.text())
        if folder:
            self.output.setText(folder)
            self.output.setToolTip(folder)

    def edit_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.apply_settings(dialog.values())
        dialog.deleteLater()

    def show_startup_guide(self):
        if self.settings.get("hide_startup_guide", False):
            return
        guide = GuideDialog(self.ui_text.language, self, startup=True)
        guide.language_changed.connect(lambda language: self.apply_settings({"language": language}))
        guide.exec()
        if guide.dont_show_again.isChecked():
            self.settings["hide_startup_guide"] = True
            self.save_settings()
        guide.deleteLater()

    def apply_settings(self, values):
        self.settings.update(values)
        self.settings["reasoning_effort"] = selected_reasoning(self.settings.get("reasoning_effort"))
        with QSignalBlocker(self.search_model), QSignalBlocker(self.generation_model), QSignalBlocker(self.reasoning):
            self.search_model.set_value(self.settings.get("model", ""))
            self.generation_model.set_value(self.settings.get("generation_model", ""))
            self.reasoning.set_value(self.settings["reasoning_effort"])
        self.ui_text.apply_language(self.settings.get("language", DEFAULT_LANGUAGE), self)
        self.search_model.update_tooltip()
        self.generation_model.update_tooltip()
        self.save_settings()
        self.source_changed()

    def execution_settings_changed(self):
        self.settings.update(model=self.search_model.value(), generation_model=self.generation_model.value(),
                             reasoning_effort=self.reasoning.value())
        self.save_settings()

    def save_settings(self):
        data = {k: v for k, v in self.settings.items() if not k.endswith("_key") and k != "remember_api_keys"}
        data["output_dir"] = self.output.text()
        data["generation_count"] = self.generation_count.value()
        data["search_profile"] = self.search_profile.currentData()
        data["verify_sources"] = self.verify_sources.isChecked()
        roots = self.settings.get("library_roots", [])
        roots = [p for p in roots if isinstance(p, str) and p.strip()] if isinstance(roots, list) else []
        if self.output.text().strip():
            roots.append(str(Path(self.output.text()).expanduser().absolute()))
        data["library_roots"] = list(dict.fromkeys(roots))
        self.settings["library_roots"] = data["library_roots"]
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            previous_backup = self.loaded_settings.backup_path
            self.loaded_settings.preserve_original(SETTINGS)
            temporary = SETTINGS.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(SETTINGS)
            if self.loaded_settings.backup_path and previous_backup is None:
                self.log("Початкові налаштування збережено: {path}", path=self.loaded_settings.backup_path)
        except (OSError, UnicodeError):
            self.log("Не вдалося зберегти налаштування; поточний пошук доступний.")

    def current_search_request(self):
        chosen = self.selected_library_image if self.provider.currentData() == "image_reference" else None
        return SearchRequest(description=self.description.toPlainText().strip(), count=self.count.value(),
            output_dir=Path(self.output.text()).expanduser().resolve(),
            provider="agent",
            reference_image=chosen.path if chosen else None,
            reference_mode="visual" if chosen else "pose",
            candidate_limit=self.settings.get("candidate_limit", 72), screenshots=self.settings.get("screenshots", True),
            codex_path=self.settings.get("codex_path", ""), model=self.settings.get("model", ""),
            reasoning_effort=self.settings["reasoning_effort"],
            artistic_nudity=self.settings.get("artistic_nudity", True),
            search_rounds=self.settings.get("search_rounds", 6), search_profile=self.search_profile.currentData(),
            search_strategy=self.settings.get("search_strategy", "wide"),
            discovery_limit=self.settings.get("discovery_limit", 200), detail_limit=self.settings.get("detail_limit", 40),
            enhanced_dedup=self.settings.get("enhanced_dedup", False), high_quality_only=self.settings.get("high_quality_only", False),
            reuse_cache=self.settings.get("reuse_cache", True), verify_sources=self.verify_sources.isChecked())

    def start_search(self):
        if self.worker and self.worker.isRunning():
            return
        tr = self.ui_text
        if not self.output.text().strip():
            QMessageBox.information(self, tr("Папка збереження"), tr("Обери папку для результатів."))
            return
        if self.provider.currentData() == "image_reference":
            if not self.selected_library_image:
                self.open_library()
                return
            try:
                self.selected_library_image.checked()
            except (OSError, ValueError) as exc:
                QMessageBox.information(self, tr("Обери зображення повторно"), tr(str(exc)))
                return
        request = self.current_search_request()
        try:
            request.validate()
        except ValueError as exc:
            QMessageBox.information(self, tr("Перевір параметри"), tr(str(exc)))
            return
        self.save_settings()
        if self.provider.currentData() == "agent_reference":
            self.start_generation(request)
            return
        self.launch_search(request)

    def launch_search(self, request):
        if self.worker and self.worker.isRunning():
            return
        tr = self.ui_text
        self.last_search_request = replace(request)
        self.last_folder = request.output_dir
        self.repeat_search_button.hide()
        self.repeat_hint.hide()
        self.set_retry(None, 0)
        self.set_usage({}, None)
        self.clear_gallery()
        self.guide_banner.setVisible(bool(request.reference_image))
        tr.set(self.guide_caption, "setText", "Обраний зразок\nУ добірці нижче — знайдені референси.")
        if request.reference_image:
            self.guide_thumbnail.setPixmap(QPixmap(str(request.reference_image)).scaled(65, 65,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.variants_button.setVisible(bool(self.generation_result and request.reference_image in self.generation_result.images))
        self.access_links = []
        self.access_links_button.hide()
        self.details.clear()
        self.progress.setValue(0)
        self.set_subtitle("Пошук триває. Складні пози можуть потребувати кількох хвилин.")
        self.set_busy(True)
        tr.set(self.search, "setText", "Шукаю референси…")
        self.worker = SearchWorker(request, language=tr.language)
        self.worker.plan_ready.connect(self.edit_custom_priorities)
        self.worker.long_wait.connect(self.show_long_wait)
        self.worker.status.connect(self.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.completed.connect(self.show_results)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()
        self.log("Модель Codex: {model}", model=request.model or tr("з налаштувань Codex; у LazyRef не вказана"))
        self.log("Рівень міркування: {effort}", effort=REASONING_LABELS[request.reasoning_effort])
        self.log("Пріоритети: {profile}", profile=tr(PROFILES[request.search_profile]["label"]))
        self.log("Перевірка джерела: {state}", state="ON" if request.verify_sources else tr("OFF — походження не перевірено"))
        if request.provider == "agent":
            self.log("Алгоритм: {strategy}", strategy=tr("широкий пошук · новий" if request.search_strategy == "wide"
                                                       else "попередній пошук · 0.3.4"))

    def repeat_last_search(self):
        if not self.last_search_request or self.worker and self.worker.isRunning():
            return
        # Окремий запит: історичну спробу та її результати не переписуємо.
        request = replace(self.last_search_request, model=self.settings.get("model", ""),
            enhanced_dedup=self.settings.get("enhanced_dedup", False), high_quality_only=self.settings.get("high_quality_only", False),
            codex_path=self.settings.get("codex_path", ""), reasoning_effort=self.settings["reasoning_effort"], reuse_cache=False)
        try:
            request.validate()
        except ValueError as exc:
            QMessageBox.information(self, self.ui_text("Не вдалося повторити пошук"), self.ui_text(str(exc)))
            return
        self.description.setPlainText(request.description)
        self.count.setValue(request.count)
        self.output.setText(str(request.output_dir))
        if request.reference_image and request.reference_mode == "visual":
            self.selected_library_image = image_record(request.reference_image, request.reference_image.parent,
                                                      "reference", description=request.description)
        mode = "image_reference" if request.reference_mode == "visual" else "agent_reference"
        self.provider.setCurrentIndex(self.provider.findData(mode if request.reference_image else "agent"))
        self.search_profile.setCurrentIndex(self.search_profile.findData(request.search_profile))
        self.verify_sources.setChecked(request.verify_sources)
        self.save_settings()
        self.launch_search(request)
        self.log("Повторний пошук: попередній опис і зразок, без використання попередніх результатів.")

    def edit_custom_priorities(self, draft):
        from .priorities_ui import PrioritiesDialog
        worker = self.sender()
        if not isinstance(worker, SearchWorker) or worker is not self.worker or worker.cancel.is_set():
            if isinstance(worker, SearchWorker):
                worker.submit_plan(None)
            return
        choices = None
        try:
            self.log("Уточни умови пошуку. Поки вікно відкрите, Codex не працює.")
            dialog = PrioritiesDialog(draft, worker.request.reference_image, self)
            self.priorities_dialog = dialog
            if dialog.exec() == QDialog.DialogCode.Accepted:
                choices = dialog.selected_criteria
                if self.last_search_request:
                    self.last_search_request = replace(self.last_search_request, custom_criteria=deepcopy(choices))
            dialog.deleteLater()
        finally:
            self.priorities_dialog = None
            worker.submit_plan(choices)

    def start_generation(self, search_request):
        self.last_search_request = None
        self.repeat_search_button.hide()
        self.repeat_hint.hide()
        self.set_retry(None, 0)
        self.set_usage({}, None)
        self.generation_search_request = search_request
        self.generation_result = None
        self.guide_banner.hide()
        self.clear_gallery()
        self.set_subtitle("ШІ-варіантів: {variants} · реальних фото після вибору: {photos}",
                          variants=self.generation_count.value(), photos=search_request.count)
        self.details.clear()
        self.progress.setValue(0)
        self.access_links_button.hide()
        self.last_folder = None
        request = GenerationRequest(search_request.description, self.generation_count.value(),
            ROOT / "generated_references", search_request.codex_path, self.settings.get("generation_model", ""),
            reasoning_effort=search_request.reasoning_effort)
        self.worker = GenerationWorker(request, language=self.ui_text.language)
        self.worker.status.connect(self.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self.worker_finished)
        self.set_busy(True)
        self.ui_text.set(self.search, "setText", "Створюю ШІ-варіанти…")
        self.worker.start()
        self.log("Модель генерації ШІ-зразків: {model} · {effort}",
                 model=self.ui_text(model_label(request.model)), effort=REASONING_LABELS[request.reasoning_effort])

    def open_saved_generation(self):
        if self.worker and self.worker.isRunning():
            return
        tr = self.ui_text
        path, _ = QFileDialog.getOpenFileName(self, tr("Обери збережені ШІ-зразки"),
            str(ROOT / "generated_references"), tr("ШІ-зразки LazyRef (generation.json)"))
        if not path:
            return
        try:
            result, description = load_generation(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.information(self, tr("Не вдалося відкрити зразки"), tr(str(exc)))
            return
        self.provider.setCurrentIndex(self.provider.findData("agent_reference"))
        self.description.setPlainText(description)
        self.generation_count.setValue(result.requested)
        self.generation_search_request = self.current_search_request()
        self.last_search_request = None
        self.set_retry(None, 0)
        self.access_links_button.hide()
        self.details.clear()
        self.progress.setValue(min(100, int(100 * len(result.images) / result.requested)))
        self.show_generated(result)
        self.log("Відкрито збережені ШІ-зразки. Нових викликів генератора немає.")

    def show_generated(self, result):
        if result is None or self.worker and self.worker.isRunning():
            return
        self.generation_result = result
        self.repeat_search_button.hide()
        self.repeat_hint.hide()
        self.set_usage(result.usage, result.usage_path)
        self.guide_banner.hide()
        self.clear_gallery()
        self.last_folder = result.folder
        self.show_content(self.scroll if result.images else self.empty)
        count_text = ("ШІ-варіантів доступно: {count} · запитано: {requested}. Обери зразок для пошуку фото."
            if len(result.images) > result.requested else
            "ШІ-варіантів створено: {count} із {requested}. Обери зразок для пошуку фото.")
        self.set_subtitle(count_text, count=len(result.images), requested=result.requested)
        tr = self.ui_text
        self.grid.addWidget(tr.label("Згенеровані зразки · це не справжні фотографії", "section", True), 0, 0, 1, 2)
        for index, path in enumerate(result.images):
            self.grid.addWidget(GeneratedCard(path, index+1, self.select_generated,
                on_deleted=self.image_deleted, can_delete=self.can_delete_image,
                on_search=self.choose_library_image, on_created=self.crop_created,
                description=self.description.toPlainText(), organizer=self.organizer, parent=self.gallery),
                1 + index // 2, index % 2)
        if not result.images:
            tr.set(self.empty_title, "setText", "ШІ-зразки поки не створено")
            tr.set(self.empty_caption, "setText", "Можна повторити генерацію або перейти до пошуку за описом.")
        for note in result.warnings:
            self.log(note)
        if result.error:
            self.log(result.error)
            self.details.show()
        elif result.cancelled:
            self.log("Генерацію зупинено. Уже створені варіанти збережено; можна обрати будь-який із них.")
        else:
            self.log("Обери найточнішу позу. ШІ-зразок не буде включено до знайдених фото.")

    def select_generated(self, path):
        if self.worker and self.worker.isRunning():
            return
        if not self.generation_search_request or not self.generation_result or path not in self.generation_result.images:
            return
        tr = self.ui_text
        if not self.output.text().strip():
            QMessageBox.information(self, tr("Папка збереження"), tr("Обери папку для результатів."))
            return
        # Зразок можна використовувати повторно після зміни або очищення моделі.
        # Параметри нового пошуку беремо з поточних налаштувань, не зі старого запиту.
        request = replace(self.current_search_request(), provider="agent", reference_image=path, reference_mode="pose")
        try:
            request.validate()
        except ValueError as exc:
            QMessageBox.information(self, tr("Перевір зразок"), tr(str(exc)))
            return
        self.save_settings()
        self.launch_search(request)
        self.log("ШІ-зразок: {path} · опис: {description}", path=path, description=request.description)

    def set_busy(self, busy):
        self._busy = busy
        if busy and self.content.currentWidget() is self.empty:
            self.show_content(self.blank)
        self.long_wait_panel.hide()
        self.search.setEnabled(not busy)
        self.ui_text.set(self.search, "setText", "Шукаю референси…" if busy else
            "Створити ШІ-варіанти  →" if self.provider.currentData() == "agent_reference" else "Знайти референси  →")
        self.settings_button.setEnabled(not busy)
        self.search_model.setEnabled(not busy)
        self.generation_model.setEnabled(not busy)
        self.reasoning.setEnabled(not busy)
        self.repeat_search_button.setEnabled(not busy and self.last_search_request is not None)
        self.variants_button.setEnabled(not busy)
        self.usage_button.setEnabled(not busy and bool(self.usage_data))
        self.retry_button.setEnabled(not busy and bool(self.retry_path) and (self.pending_review > 0 or self.export_pending))
        self.library_button.setEnabled(not busy)
        self.pick_image_button.setEnabled(not busy)
        self.import_image_button.setEnabled(not busy)
        self.generation_row.setEnabled(not busy)
        self.open_generation_button.setEnabled(not busy)
        for control in (self.description, self.count, self.provider, self.output, self.generation_count, self.search_profile, self.verify_sources):
            control.setEnabled(not busy)
        for button in self.count_buttons:
            button.setEnabled(not busy)
        self.stop.setVisible(busy)
        self.stop.setEnabled(busy)
        for button in self.gallery.findChildren(FavoriteButton):
            button.refresh()

    def cancel_search(self):
        if self.worker:
            self.worker.cancel.set()
            if self.priorities_dialog is not None:
                self.priorities_dialog.reject()
            self.long_wait_panel.hide()
            self.long_wait_stop.setEnabled(False)
            self.stop.setEnabled(False)
            self.log("Зупиняю… Уже завершені результати буде збережено.")

    def show_long_wait(self, waiting):
        if self.sender() is not None and self.sender() is not self.worker:
            return
        active = bool(self.worker and self.worker.isRunning() and not self.worker.cancel.is_set())
        self.long_wait_panel.setVisible(waiting and active)
        self.long_wait_stop.setEnabled(waiting and active)

    def worker_finished(self):
        self.set_busy(False)
        if self.close_when_done:
            self.close()
        elif isinstance(self.worker, GenerationWorker):
            self.show_generated(self.worker.result)

    def show_content(self, page):
        # Стартові приклади й малюнок не використовуються як фон після початку роботи.
        # Якщо фото немає, залишаємо тільки повідомлення про стан/помилку.
        self.empty_art.hide()
        self.empty_examples.hide()
        self.content.setCurrentWidget(page)

    def clear_gallery(self):
        self.show_content(self.blank)
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def show_results(self, result):
        tr = self.ui_text
        self.repeat_search_button.setVisible(self.last_search_request is not None)
        self.repeat_hint.setVisible(self.last_search_request is not None)
        self.set_retry(result.retry_path, result.pending_review, result.export_pending)
        self.set_usage(result.usage, result.usage_path)
        self.clear_gallery()
        self.last_folder = result.folder
        self.access_links = result.access_links
        self.access_links_button.setVisible(bool(self.access_links))
        tr.set(self.access_links_button, "setText", "Переглянути збережені посилання ({count})",
               count=len(self.access_links))
        self.show_content(self.scroll if result.assets else self.empty)
        if not result.assets:
            tr.set(self.empty_title, "setText", "Відповідних фото поки не знайдено")
            tr.set(self.empty_caption, "setText",
                   "Переглянута вибірка не дала достатніх збігів.\nСлабкі результати не додано до добірки; подробиці — у звіті пошуку.")
            if result.pending_review:
                tr.set(self.empty_title, "setText", "Кандидати збережені, аналіз не завершено")
                tr.set(self.empty_caption, "setText",
                       "Фото вже є на комп’ютері. Натисни «Повторити аналіз»,\nщоб оцінити їх без нового пошуку.")
            elif result.export_pending:
                tr.set(self.empty_title, "setText", "Оцінки збережені, добірка очікує експорту")
                tr.set(self.empty_caption, "setText",
                       "Натисни «Повторити збереження», щоб створити добірку\nз уже оцінених фото без нового виклику Codex.")
        close_assets = [a for a in result.assets if a.assessment.is_close]
        partial_assets = [a for a in result.assets if not a.assessment.is_close]
        grid_row, index = 0, 0
        close_heading = "Близькі пози — оточення й одяг можуть відрізнятися" if result.search_profile == "pose" else "Близькі збіги — усі ключові ознаки"
        for heading, group in ((close_heading, close_assets),
                               ("Часткові альтернативи — частина умов не виконана", partial_assets)):
            if not group:
                continue
            self.grid.addWidget(tr.label(heading, "section", True), grid_row, 0, 1, 2)
            grid_row += 1
            for offset, asset in enumerate(group):
                index += 1
                self.grid.addWidget(ImageCard(asset, index, on_deleted=self.image_deleted,
                    can_delete=self.can_delete_image, on_search=self.choose_library_image,
                    on_created=self.crop_created, description=self.description.toPlainText(),
                    organizer=self.organizer, parent=self.gallery), grid_row + offset // 2, offset % 2)
            grid_row += (len(group) + 1) // 2
        count = len(result.assets)
        requested = result.requested
        summary = partial(format_result_summary, result=result, close_count=len(close_assets),
                          request=self.last_search_request)
        self.set_subtitle(summary, collapsed=True,
                          summary=lambda tr: tr("Результати: {count} із {requested}", count=count, requested=requested))
        for warning in result.warnings:
            self.details.appendPlainText("• " + tr(warning))
        if result.error:
            self.log(result.error)
            self.details.show()
        elif result.cancelled:
            self.log("Зупинено. Уже оцінені фото й звіт збережено." if result.folder else "Пошук зупинено.")
        elif close_assets:
            message = ("Знайдено близьких збігів: {count}. Часткові альтернативи — окремо в partial." if partial_assets
                       else "Знайдено близьких збігів: {count}. Посилання й пояснення — у звіті.")
            self.log(message, count=len(close_assets))
        elif partial_assets:
            self.log("Близьких збігів не знайдено. Нижче лише часткові альтернативи; файли — у папці partial.")
        elif self.access_links:
            self.log("Фото не завантажені. Збережено посилання на роботи для ручного перегляду.")
        else:
            self.log("Збігів недостатньо. Спробуй ширший опис або інше джерело.")

    def set_retry(self, path, count, export_pending=False):
        self.retry_path, self.pending_review = path, count
        self.export_pending = export_pending
        available = bool(path) and (count > 0 or export_pending)
        self.retry_row.setVisible(available)
        tr = self.ui_text
        tr.set(self.retry_button, "setText", "Повторити аналіз ({count})" if count else "Повторити збереження",
               count=count)
        self.retry_button.setEnabled(available and not (self.worker and self.worker.isRunning()))
        tr.set(self.retry_hint, "setText", "Лише неоцінені фото, по три за раз. Повторна оцінка використовує Codex." if count
               else "Зберегти вже оцінені фото та звіт. Новий виклик Codex не потрібен.")

    def open_pending_reviews(self):
        if self.worker and self.worker.isRunning():
            return
        dialog = PendingReviewsDialog(Path(self.output.text()).expanduser(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_path:
            self.launch_review(dialog.selected_path, save_only=dialog.selected_save_only)

    def retry_current_review(self):
        if self.retry_path and not (self.worker and self.worker.isRunning()):
            self.launch_review(self.retry_path, save_only=self.pending_review == 0 and self.export_pending)

    def launch_review(self, path, *, save_only=None):
        if self.worker and self.worker.isRunning():
            return
        self.last_search_request = None
        self.last_folder = None
        self.repeat_search_button.hide()
        self.repeat_hint.hide()
        tr = self.ui_text
        try:
            session = ReviewSession.load_metadata(path)
            saved_request = session.data["request"]
            if save_only is None and session.pending_count == 0 and session.export_pending:
                save_only = True
            # Restore only display metadata here. Guide validation, receipt
            # recovery and the effective request belong to ReviewWorker.
            if type(saved_request["count"]) is not int or not 1 <= saved_request["count"] <= 40:
                raise ValueError("Кількість у збереженому запиті має бути від 1 до 40.")
            if type(saved_request.get("verify_sources", True)) is not bool:
                raise ValueError("Некоректний режим перевірки джерел у збереженому запиті.")
            self.description.setPlainText(saved_request["description"])
            self.count.setValue(saved_request["count"])
            self.verify_sources.setChecked(saved_request.get("verify_sources", True))
            self.search_profile.setCurrentIndex(self.search_profile.findData(saved_request["search_profile"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, tr("Не вдалося відкрити збережений аналіз"), tr(str(exc)))
            return
        self.set_usage({}, None)
        self.guide_banner.hide()
        self.progress.setValue(0)
        self.set_busy(True)
        tr.set(self.search, "setText", "Перевіряю збережені фото…")
        self.set_subtitle("Перевіряю збережені фото й відновлюю готові оцінки у фоні.")
        self.worker = ReviewWorker(path, self.settings.get("codex_path", ""), self.settings["reasoning_effort"],
                                   save_only=save_only, language=tr.language)
        self.worker.prepared.connect(self.review_prepared)
        self.worker.long_wait.connect(self.show_long_wait)
        self.worker.status.connect(self.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.completed.connect(self.show_results)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    @Slot(object, int, bool)
    def review_prepared(self, request, pending_review, save_only):
        # This slot runs in the UI thread and receives a request whose files
        # and assessment conditions have already passed worker validation.
        self.last_search_request = request
        self.last_folder = request.output_dir
        self.description.setPlainText(request.description)
        self.count.setValue(request.count)
        self.verify_sources.setChecked(request.verify_sources)
        self.search_profile.setCurrentIndex(self.search_profile.findData(request.search_profile))
        self.ui_text.set(self.search, "setText", "Аналізую збережені фото…" if pending_review else "Зберігаю добірку…")
        self.set_subtitle("Продовжую аналіз за збереженим описом, пріоритетами й зразком." if pending_review
                         else "Повторюю збереження вже оцінених фото без виклику Codex.")

    def set_usage(self, data, path):
        self.usage_data, self.usage_path = data, path
        self.usage_button.setEnabled(bool(data) and not (self.worker and self.worker.isRunning()))
        if data:
            summary = data["summary"]
            total = summary["reported_tokens"]["total_tokens"]
            def usage_caption(tr):
                amount = f"{total:,}".replace(",", " ") if total is not None else tr("немає даних")
                return tr("Витрати Codex") + " · " + amount + (tr(" · неповні") if summary["incomplete_calls"] else "")
            self.ui_text.set(self.usage_button, "setText", usage_caption)
        else:
            self.ui_text.set(self.usage_button, "setText", "Витрати Codex")

    def show_usage(self):
        UsageDialog(self.usage_data, self.usage_path, self).exec()

    def toggle_search_info(self, expanded):
        self.info_content.setVisible(expanded)
        title = self.info_title
        suffix = " · Згорнути" if expanded else " · Розгорнути"
        self.ui_text.set(self.info_button, "setText", lambda tr: tr(title) + tr(suffix))

    def set_subtitle(self, text, *, collapsed=False, summary="Інформація про пошук", **fields):
        self.ui_text.set(self.subtitle, "setText", text, **fields)
        self.subtitle.setVisible(bool(text))
        self.info_title = summary
        self.info_button.setVisible(bool(text))
        self.info_button.setChecked(bool(text) and not collapsed)
        self.toggle_search_info(self.info_button.isChecked())

    def log(self, text, **fields):
        self.ui_text.set(self.status, "setText", text, **fields)
        self.details.appendPlainText(self.status.text())

    def open_folder(self):
        try:
            folder = self.last_folder
            if folder is None:
                if self.provider.currentData() == "agent_reference":
                    folder = ROOT / "generated_references"
                else:
                    output = self.output.text().strip()
                    folder = Path(output).expanduser() if output else ROOT / "references"
            folder = Path(folder).resolve()
            folder.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError(self.ui_text("Windows не вдалося відкрити папку: {path}", path=folder))
        except (OSError, ValueError) as exc:
            show_error(self, exc)

    def can_delete_image(self):
        return not self._busy and not (self.worker and self.worker.isRunning())

    def open_library(self):
        if not self.can_delete_image():
            return
        self.save_settings()
        dialog = LibraryDialog(ROOT, self.settings.get("library_roots", []), self, organizer=self.organizer)
        dialog.file_deleted.connect(self.image_deleted)
        dialog.exec()
        selected = dialog.selected_image
        dialog.deleteLater()
        if selected:
            self.choose_library_image(selected)

    def import_image(self):
        if not self.can_delete_image():
            return
        tr = self.ui_text
        filename, _ = QFileDialog.getOpenFileName(self, tr("Обери фото для пошуку референсів"), "",
            tr("Зображення (*.png *.jpg *.jpeg *.webp)"))
        if not filename:
            return
        try:
            from .importing import import_reference
            item = import_reference(Path(filename), ROOT, self.description.toPlainText().strip())
        except (OSError, ValueError) as exc:
            show_error(self, tr("Не вдалося імпортувати фото: {error}", error=tr(str(exc))))
            return
        self.choose_library_image(item)
        self.log("Фото скопійовано в галерею. Оригінал залишився на місці.")

    def choose_library_image(self, item):
        if not self.can_delete_image():
            return
        tr = self.ui_text
        try:
            item.checked()
            from .generation import validate_reference
            validate_reference(item.path, min_side=32)
        except (OSError, ValueError) as exc:
            QMessageBox.information(self, tr("Не вдалося обрати зразок"), tr(str(exc)))
            return
        self.selected_library_image = item
        self.last_search_request = None
        self.set_retry(None, 0)
        self.clear_gallery()
        self.show_content(self.empty)
        tr.set(self.empty_title, "setText", "Зображення готове до пошуку")
        tr.set(self.empty_caption, "setText", "Уточни, що саме знайти за зразком,\nі натисни «Знайти референси».")
        self.set_subtitle("Новий пошук за обраним зображенням або фрагментом.")
        self.last_folder = item.path.parent
        self.provider.setCurrentIndex(self.provider.findData("image_reference"))
        self.description.setPlainText(item.description)
        tr.set(self.description, "setPlaceholderText", "Наприклад: черевики в такому ракурсі, як на обраному фрагменті")
        self.description.setFocus()
        self.guide_thumbnail.setPixmap(QPixmap(str(item.path)).scaled(65, 65,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        tr.set(self.guide_caption, "setText", "Обрано для нового пошуку: {filename}\nУточни опис і натисни «Знайти референси».",
               filename=item.path.name)
        self.guide_banner.show()
        self.variants_button.hide()
        self.repeat_search_button.hide()
        self.repeat_hint.hide()
        self.log("Зразок обрано з галереї. Генерація не потрібна; пошук почнеться після натискання кнопки.")

    def crop_created(self, item):
        self.log("Обрізану копію збережено в галереї: {path}", path=item.path)

    def image_deleted(self, filename):
        path = Path(filename)
        for index in reversed(range(self.grid.count())):
            card = self.grid.itemAt(index).widget()
            if getattr(card, "file_path", None) == path:
                self.grid.takeAt(index)
                card.hide()
                card.deleteLater()
        if self.generation_result:
            self.generation_result.images = [p for p in self.generation_result.images if p != path]
        if self.selected_library_image and self.selected_library_image.path == path:
            self.selected_library_image = None
            self.guide_banner.hide()
        self.log("Зображення переміщено до Кошика: {filename}", filename=path.name)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.close_when_done = True
            self.cancel_search()
            event.ignore()
        else:
            self.save_settings()
            event.accept()


def configure_app(app):
    # Доступний у Windows шрифт також працює під час offscreen-перевірок Qt.
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("segoeui.ttf", "segoeuib.ttf", "segoeuisl.ttf", "seguisym.ttf"):
        font = font_dir / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setApplicationName("LazyRef")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)


def run():
    app = QApplication.instance() or QApplication([])
    configure_app(app)
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window, window.show_startup_guide)
    return app.exec()

