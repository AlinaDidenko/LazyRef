"""Власний тимчасовий каталог уникає конфліктів Windows sandbox/звичайного запуску."""

from pathlib import Path
import tempfile

import pytest


def pytest_configure(config):
    if not config.option.basetemp:
        root = Path(__file__).resolve().parent.parent / ".lazyref"
        root.mkdir(exist_ok=True)
        config.option.basetemp = tempfile.mkdtemp(prefix="test-run-", dir=root)


@pytest.fixture(autouse=True)
def isolated_ui_storage(request, monkeypatch):
    """Every UI test gets disposable settings, galleries and collection storage."""
    if request.node.get_closest_marker("native_browser"):
        return
    from lazyref import ui
    workspace = request.getfixturevalue("tmp_path") / "ui-workspace"
    monkeypatch.setattr(ui, "ROOT", workspace)
    monkeypatch.setattr(ui, "SETTINGS", workspace / ".lazyref" / "settings.json")
