"""Quick start for the startup dialog and the Settings window."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QRadioButton, QScrollArea, QVBoxLayout, QWidget

from .i18n import LANGUAGES
from .ui_text import UiText


class GuideDialog(QDialog):
    language_changed = Signal(str)

    def __init__(self, language, parent=None, *, startup=False):
        super().__init__(parent)
        tr = self.ui_text = UiText(language)
        tr.set(self, "setWindowTitle", "Інструкція LazyRef")
        self.resize(640, 610 if startup else 570)
        self.setMinimumSize(470, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(tr.label("Як користуватися LazyRef", "title", True))
        if startup:
            languages = QHBoxLayout()
            languages.addWidget(tr.label("Мова інтерфейсу", "muted"))
            for code, name in LANGUAGES.items():
                choice = QRadioButton(name, self)
                choice.setChecked(code == language)
                choice.clicked.connect(lambda _checked, code=code: self.change_language(code))
                languages.addWidget(choice)
            languages.addStretch()
            layout.addLayout(languages)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 12, 0)
        body.setSpacing(8)
        body.addWidget(tr.label("Опиши позу, ракурс і важливі деталі. Потім обери режим пошуку.", "muted", True))
        for heading, instructions in (
            ("Агент Codex · пошук зображень",
             "Вкажи кількість референсів і натисни «Знайти референси». Наприклад, введи «Людина сидить навпочіпки, видно все тіло, ракурс збоку»."),
            ("ШІ-зразок → реальні фото",
             "Натисни «Створити ШІ-варіанти» й обери зразок із потрібною позою. LazyRef шукатиме за ним справжні фотографії."),
            ("Зображення → референси",
             "Обери фото з комп’ютера або галереї. Уточни, що знайти за зразком, і натисни «Знайти референси»."),
            ("Результати й галереї",
             "Кнопка «Відкрити папку» покаже файли поточної добірки. У «Галереях» можна переглянути збережені фото й ШІ-зразки."),
        ):
            body.addSpacing(6)
            body.addWidget(tr.label(heading, "section", True))
            body.addWidget(tr.label(instructions, wrap=True))
        body.addSpacing(6)
        body.addWidget(tr.label("Для пошуку й генерації потрібен Codex CLI. За потреби вкажи шлях до codex.exe в налаштуваннях.",
                                "muted", True))
        body.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.dont_show_again = QCheckBox() if startup else None
        if self.dont_show_again is not None:
            tr.set(self.dont_show_again, "setText", "Не показувати більше")
            footer.addWidget(self.dont_show_again)
        footer.addStretch()
        close = tr.button("Зрозуміло" if startup else "Закрити")
        close.setObjectName("primary")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def change_language(self, language):
        self.ui_text.apply_language(language, self)
        self.language_changed.emit(language)
