"""Keep built-in widget text in sync with the selected interface language."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from .i18n import DEFAULT_LANGUAGE, translate


def plain_label(value, name="", wrap=False):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


class UiText:
    def __init__(self, language=DEFAULT_LANGUAGE):
        self.language = language

    def __call__(self, source, **fields):
        return source(self) if callable(source) else translate(source, self.language, **fields)

    def set(self, widget, setter, source, *args, **fields):
        if not hasattr(widget, "_text_bindings"):
            widget._text_bindings = {}
        widget._text_bindings[setter, args] = (source, fields)
        getattr(widget, setter)(*args, self(source, **fields))

    def label(self, source="", name="", wrap=False, **fields):
        widget = plain_label("", name, wrap)
        self.set(widget, "setText", source, **fields)
        return widget

    def button(self, source, **fields):
        widget = QPushButton()
        self.set(widget, "setText", source, **fields)
        return widget

    def add_item(self, combo, source, value):
        index = combo.count()
        combo.addItem("", value)
        self.set(combo, "setItemText", source, index)

    def apply_language(self, language, window):
        if language == self.language:
            return
        self.language = language
        for widget in (window, *window.findChildren(QWidget)):
            if widget.window() != window:
                continue
            for (setter, args), (source, fields) in getattr(widget, "_text_bindings", {}).items():
                getattr(widget, setter)(*args, self(source, **fields))


def text_for(widget):
    while widget is not None:
        if hasattr(widget, "ui_text"):
            return widget.ui_text
        widget = widget.parent()
    return UiText()
