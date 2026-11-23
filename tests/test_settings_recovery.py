"""Malformed local preferences cannot prevent startup or erase their source."""

import json
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication

from lazyref import ui
from lazyref.settings import load_settings, normalize_settings


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "settings.json"
    monkeypatch.setattr(ui, "ROOT", root)
    monkeypatch.setattr(ui, "SETTINGS", path)
    return path


@pytest.mark.parametrize("values", [
    {"search_revision": None}, {"generation_count": None}, {"output_dir": []},
    {"model": ["model"]}, {"generation_model": {"name": "model"}},
    {"codex_path": 12}, {"search_profile": []}, {"search_strategy": {}},
    {"discovery_limit": "200", "detail_limit": None},
    {"candidate_limit": False, "search_rounds": True},
    {"screenshots": "false", "reuse_cache": None, "artistic_nudity": []},
    {"verify_sources": 0, "enhanced_dedup": 1, "high_quality_only": "true"},
    {"language": "pl", "hide_startup_guide": "true"},
    {"generation_count": 2**80, "discovery_limit": -10, "detail_limit": 121},
    {"reasoning_effort": []}, {"output_dir": "\x00bad"},
    {"library_roots": [{"path": "bad"}, "valid-folder", None]},
    ["not a settings object"], None,
])
def test_bad_preferences_boot_both_windows_and_preserve_original(qt_app, preferences, values):
    original = json.dumps(values).encode()
    preferences.write_bytes(original)
    window = ui.MainWindow()
    dialog = ui.SettingsDialog(window.settings, window)
    try:
        window.description.setPlainText("A seated person")
        window.current_search_request().validate()
        assert window.loaded_settings.warning
        assert not window.details.isHidden()
        assert preferences.read_bytes() == original, "Startup must not overwrite preferences"
        window.save_settings()
        backups = list(preferences.parent.glob("settings.invalid-*.json"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        window.save_settings()
        assert list(preferences.parent.glob("settings.invalid-*.json")) == backups
        reopened = load_settings(preferences, preferences.parent)
        assert not reopened.warning and reopened.original is None
    finally:
        shiboken6.delete(dialog)
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("raw", [b'{"generation_count":', b'\xff\xfeinvalid', b'42'])
def test_unparseable_file_gets_exact_backup_before_replacement(qt_app, preferences, raw):
    preferences.write_bytes(raw)
    window = ui.MainWindow()
    try:
        window.save_settings()
        assert window.loaded_settings.backup_path.read_bytes() == raw
        assert isinstance(json.loads(preferences.read_bytes()), dict)
    finally:
        window.close()
        shiboken6.delete(window)


def test_backup_failure_keeps_original_and_current_session_usable(qt_app, preferences, monkeypatch):
    raw = b'{"generation_count": null, "model": "custom-model"}'
    preferences.write_bytes(raw)
    window = ui.MainWindow()
    real_open = Path.open

    def fail_backup(path, *args, **kwargs):
        if ".invalid-" in path.name:
            raise OSError("synthetic backup failure")
        return real_open(path, *args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "open", fail_backup)
            window.save_settings()
            assert preferences.read_bytes() == raw
            assert window.loaded_settings.backup_path is None
            assert "Не вдалося зберегти" in window.status.text()
            window.description.setPlainText("A seated person")
            window.current_search_request().validate()
        window.save_settings()
        assert window.loaded_settings.backup_path.read_bytes() == raw
    finally:
        window.close()
        shiboken6.delete(window)


def test_unreadable_existing_settings_are_never_overwritten(qt_app, preferences, monkeypatch):
    raw = b'{"model": "keep-me"}'
    preferences.write_bytes(raw)
    real_read = Path.read_bytes

    def fail_read(path):
        if path == preferences:
            raise PermissionError("synthetic read denial")
        return real_read(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_bytes", fail_read)
        window = ui.MainWindow()
    try:
        window.save_settings()
        assert preferences.read_bytes() == raw
        assert window.loaded_settings.unreadable and not window.loaded_settings.backup_path
    finally:
        window.close()
        shiboken6.delete(window)


def test_valid_custom_preferences_and_unknown_keys_survive_dialog_and_save(qt_app, preferences):
    values = {"search_revision": 2, "candidate_limit": 37, "search_rounds": 3,
              "discovery_limit": 21, "detail_limit": 17, "generation_count": 8,
              "model": "custom-search-model", "generation_model": "custom-image-model",
              "output_dir": str(preferences.parent / "results"), "screenshots": False,
              "artistic_nudity": False, "reuse_cache": False, "verify_sources": False,
              "enhanced_dedup": True, "high_quality_only": True, "search_strategy": "legacy",
              "search_profile": "pose", "reasoning_effort": "high",
              "future_setting": {"keep": [1, 2, 3]}}
    preferences.write_text(json.dumps(values), encoding="utf-8")
    window = ui.MainWindow()
    dialog = ui.SettingsDialog(window.settings, window)
    try:
        window.apply_settings(dialog.values())
        stored = json.loads(preferences.read_bytes())
        assert all(stored[key] == value for key, value in values.items())
        assert not window.loaded_settings.warning
        assert not list(preferences.parent.glob("settings.invalid-*.json"))
    finally:
        shiboken6.delete(dialog)
        window.close()
        shiboken6.delete(window)


def test_malformed_field_does_not_reset_valid_neighbors_or_library_roots(tmp_path):
    values = {"generation_count": None, "model": "custom-model", "screenshots": False,
              "library_roots": [str(tmp_path), None, {"path": "bad"}], "future": {"x": 1}}
    normalized, invalid = normalize_settings(values, tmp_path)
    assert normalized == {**values, "generation_count": 3, "library_roots": [str(tmp_path)]}
    assert set(invalid) == {"generation_count", "library_roots"}
    assert values["generation_count"] is None


def test_unknown_non_utf8_text_cannot_crash_save_or_truncate_original(qt_app, preferences):
    raw = b'{"future_setting": "\\ud800", "model": "custom-model"}'
    preferences.write_bytes(raw)
    window = ui.MainWindow()
    try:
        window.save_settings()
        assert preferences.read_bytes() == raw
        assert "Не вдалося зберегти" in window.status.text()
    finally:
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("revision,expected", [(None, 72), (1, 72), (2, 24)])
def test_valid_legacy_migration_keeps_previous_semantics(qt_app, preferences, revision, expected):
    values = {"candidate_limit": 24, "model": "old-model"}
    if revision is not None:
        values["search_revision"] = revision
    preferences.write_text(json.dumps(values), encoding="utf-8")
    window = ui.MainWindow()
    try:
        assert window.settings["candidate_limit"] == expected
        assert window.generation_model.value() == "old-model"
        assert not window.loaded_settings.warning
    finally:
        window.close()
        shiboken6.delete(window)
