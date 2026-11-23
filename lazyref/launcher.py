"""Start the project interpreter with CREATE_NO_WINDOW, without hiding shared terminals.

Some Windows venv pythonw redirectors start a console base interpreter. Using
python.exe with explicit flags also keeps its subsequent worker launches quiet.
"""

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback

from .paths import BUNDLED_INTERPRETER, PROGRAM_DIR, data_directory


ROOT = PROGRAM_DIR
BUNDLED_PYTHON = ROOT / BUNDLED_INTERPRETER
APP_PYTHON = BUNDLED_PYTHON if BUNDLED_PYTHON.is_file() else ROOT / ".venv" / "Scripts" / "python.exe"
LOG_LIMIT = 2 * 1024 * 1024


def _record_exception(kind, error, tb):
    """Retain diagnostics when pythonw has no stderr; logging must not mask an error."""
    try:
        directory = data_directory(ROOT) / ".lazyref"
        log = directory / "startup.log"
        previous = directory / "startup.previous.log"
        for path in (directory, log, previous):
            if path.is_symlink() or path.is_junction():
                raise OSError("The diagnostic path is redirected.")
        directory.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size >= LOG_LIMIT:
            log.replace(previous)
        with log.open("a", encoding="utf-8", errors="backslashreplace") as stream:
            stream.write("\n" + datetime.now().astimezone().isoformat() + "\n")
            traceback.print_exception(kind, error, tb, file=stream)
        return log
    except OSError:
        return None


def _show_error(message):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        show = ctypes.windll.user32.MessageBoxW
        show.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        show.restype = ctypes.c_int
        show(None, message, "LazyRef", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def _report_start_failure(error):
    path = _record_exception(type(error), error, error.__traceback__)
    message = "Не вдалося запустити LazyRef.\n" + str(error)
    if path:
        message += "\n\nПодробиці збережено:\n" + str(path)
    _show_error(message)
    return 1


def _run_gui():
    old_hook, old_thread_hook = sys.excepthook, threading.excepthook
    sys.excepthook = _record_exception
    threading.excepthook = lambda args: _record_exception(args.exc_type, args.exc_value, args.exc_traceback)
    try:
        from .ui import run
        return run()
    except Exception as error:
        return _report_start_failure(error)
    finally:
        sys.excepthook, threading.excepthook = old_hook, old_thread_hook


def _has_console():
    import ctypes
    from ctypes import wintypes
    get_console = ctypes.windll.kernel32.GetConsoleWindow
    get_console.restype = wintypes.HWND
    return bool(get_console())


def main(*, launch_only=False):
    if os.name == "nt":
        if not APP_PYTHON.is_file():
            _show_error("Спочатку запусти setup.bat у папці LazyRef, щоб установити середовище програми.")
            return 1
        # .py associations use console Python. Hand off once, then let that
        # launcher exit. Do not call ShowWindow/FreeConsole on a shared terminal.
        if launch_only or Path(sys.executable).resolve() != APP_PYTHON.resolve() or _has_console():
            try:
                entry = ROOT / "app.pyw"
                if not entry.is_file():
                    raise FileNotFoundError("Відсутній app.pyw. Віднови файли LazyRef.")
                subprocess.Popen([str(APP_PYTHON), "-B", "-X", "utf8", str(entry)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
                return 0
            except OSError as error:
                return _report_start_failure(error)
    return _run_gui()
