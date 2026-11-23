"""Windowed startup and diagnostics, without opening a real app or console."""

from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from lazyref import launcher


@pytest.fixture
def launch_env(tmp_path, monkeypatch):
    root = tmp_path / "Lazy Ref & тест"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / "app.pyw").touch()
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "APP_PYTHON", python)
    monkeypatch.setattr(launcher, "_has_console", lambda: False)
    # Only this module's platform is replaced; Path keeps the real host platform.
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(root / "python.exe"))
    calls, messages = [], []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(launcher, "_show_error", messages.append)
    return root, python, calls, messages


def test_console_entry_hands_off_without_shell_or_console(launch_env):
    root, python, calls, messages = launch_env
    assert launcher.main() == 0
    assert not messages and len(calls) == 1
    args, options = calls[0]
    assert args == ([str(python), "-B", "-X", "utf8", str(root / "app.pyw")],)
    assert options["cwd"] == root
    assert options["creationflags"] & 0x08000000
    assert not options.get("shell") and options["close_fds"]
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL


def test_hidden_project_python_runs_gui_once_without_relaunch(launch_env, monkeypatch):
    _, python, calls, _ = launch_env
    monkeypatch.setattr(launcher.sys, "executable", str(python))
    seen = []
    monkeypatch.setattr(launcher, "_run_gui", lambda: seen.append("gui") or 0)
    assert launcher.main() == 0
    assert seen == ["gui"] and not calls


def test_other_pythonw_uses_project_environment(launch_env, monkeypatch):
    root, _, calls, _ = launch_env
    monkeypatch.setattr(launcher.sys, "executable", str(root / "another-python" / "pythonw.exe"))
    assert launcher.main() == 0 and len(calls) == 1


@pytest.mark.parametrize("launch_only,console", [(True, False), (False, True)])
def test_hidden_vbs_launcher_or_console_is_detached_before_gui(launch_env, monkeypatch, launch_only, console):
    _, python, calls, _ = launch_env
    monkeypatch.setattr(launcher.sys, "executable", str(python))
    monkeypatch.setattr(launcher, "_has_console", lambda: console)
    assert launcher.main(launch_only=launch_only) == 0 and len(calls) == 1


def test_missing_environment_explains_setup_without_console_fallback(launch_env):
    _, python, calls, messages = launch_env
    python.unlink()
    assert launcher.main() == 1
    assert not calls and "setup.bat" in messages[0]


def test_missing_windowed_entry_is_reported_before_launch(launch_env):
    root, _, calls, messages = launch_env
    (root / "app.pyw").unlink()
    assert launcher.main() == 1
    assert not calls and "app.pyw" in messages[0]
    assert (root / ".lazyref/startup.log").is_file()


def test_native_start_failure_is_logged_and_explained(launch_env, monkeypatch):
    root, _, _, messages = launch_env
    def fail(*args, **kwargs):
        raise OSError("Synthetic start failure")
    monkeypatch.setattr(launcher.subprocess, "Popen", fail)
    assert launcher.main() == 1
    log = root / ".lazyref/startup.log"
    assert "Synthetic start failure" in log.read_text(encoding="utf-8")
    assert str(log) in messages[0]


def test_gui_error_and_callback_errors_remain_available_without_stderr(launch_env, monkeypatch):
    root, _, _, messages = launch_env
    old_hook, old_thread = sys.excepthook, threading.excepthook
    def run():
        sys.excepthook(ValueError, ValueError("Callback failure"), None)
        threading.excepthook(SimpleNamespace(exc_type=RuntimeError, exc_value=RuntimeError("Thread failure"), exc_traceback=None))
        raise ImportError("Synthetic missing dependency")
    monkeypatch.setitem(sys.modules, "lazyref.ui", SimpleNamespace(run=run))
    monkeypatch.setattr(sys, "stderr", None)
    assert launcher._run_gui() == 1
    assert sys.excepthook is old_hook and threading.excepthook is old_thread
    log = (root / ".lazyref/startup.log").read_text(encoding="utf-8")
    assert all(text in log for text in ("Callback failure", "Thread failure", "Synthetic missing dependency"))
    assert len(messages) == 1


def test_gui_exit_code_is_preserved_and_hooks_restored(launch_env, monkeypatch):
    old_hook, old_thread = sys.excepthook, threading.excepthook
    monkeypatch.setitem(sys.modules, "lazyref.ui", SimpleNamespace(run=lambda: 3))
    assert launcher._run_gui() == 3
    assert sys.excepthook is old_hook and threading.excepthook is old_thread


def test_log_rotates_and_retains_last_error(launch_env, monkeypatch):
    root, _, _, _ = launch_env
    monkeypatch.setattr(launcher, "LOG_LIMIT", 32)
    launcher._record_exception(ValueError, ValueError("First error"), None)
    launcher._record_exception(ValueError, ValueError("Latest error"), None)
    assert "First error" in (root / ".lazyref/startup.previous.log").read_text(encoding="utf-8")
    assert "Latest error" in (root / ".lazyref/startup.log").read_text(encoding="utf-8")


def test_log_failure_does_not_mask_start_failure(launch_env):
    root, _, _, messages = launch_env
    (root / ".lazyref").write_text("Not a directory", encoding="utf-8")
    assert launcher._report_start_failure(ValueError("Original error")) == 1
    assert "Original error" in messages[0]


def test_redirected_log_is_not_written(launch_env, monkeypatch):
    root, _, _, _ = launch_env
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root / ".lazyref" or original(path))
    assert launcher._record_exception(ValueError, ValueError("Test"), None) is None
    assert not (root / ".lazyref").exists()
