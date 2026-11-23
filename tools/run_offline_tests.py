"""Run tests with real network and child processes blocked; no dependencies installed."""

import os
from pathlib import Path
import platform
import sys
import tempfile
from uuid import uuid4
from urllib.parse import unquote


def main():
    workspace = Path(__file__).resolve().parents[1]
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # Windows platform introspection may run local `cmd /c ver`. Cache it
    # before blocking child processes; no application, model or service is run.
    platform.uname()
    blocked = []

    def offline(event, args):
        if event == "sqlite3.connect":
            database = unquote(os.fspath(args[0])).replace("\\", "/").casefold()
            roots = (workspace.as_posix().casefold() + "/", workspace.as_uri().casefold() + "/")
            if database.startswith(roots):
                blocked.append("sqlite3.connect: workspace database")
                raise RuntimeError("Offline tests blocked a working project database")
        if (event in {"subprocess.Popen", "os.system", "os.spawn", "os.exec",
                      "socket.connect", "socket.bind", "socket.getaddrinfo",
                      "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                      "socket.sendto", "socket.sendmsg"}):
            blocked.append(event)
            raise RuntimeError("Offline tests blocked: " + event)

    sys.addaudithook(offline)
    import pytest
    temporary = Path(tempfile.gettempdir()) / ("lazyref-offline-" + uuid4().hex)
    result = pytest.main([*(sys.argv[1:] or ["tests"]), "-q",
                         "--ignore=" + str(workspace / "tests/test_capture.py"),
                         "--ignore=" + str(workspace / "tests/test_dynamic_browser.py"),
                         "--basetemp=" + str(temporary)])
    if blocked:
        # A caught exception must not hide an attempted external operation.
        print("Blocked external operations:", ", ".join(sorted(set(blocked))))
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
