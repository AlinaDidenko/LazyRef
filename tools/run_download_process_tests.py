"""Real owned Python workers; fixed synthetic fixture only, all network blocked."""

import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from uuid import uuid4


def main():
    workspace = Path(__file__).resolve().parents[1]
    fixture = workspace / "tests" / "fixtures" / "download_process_worker.py"
    temporary = (Path(tempfile.gettempdir()) / ("lazyref-download-native-" + uuid4().hex)).resolve()
    temporary.mkdir()
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    sys.dont_write_bytecode = True
    os.environ.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        QT_QPA_PLATFORM="offscreen", LAZYREF_NATIVE_DOWNLOAD_TESTS="1",
        LAZYREF_NATIVE_DOWNLOAD_ROOT=str(temporary))
    # Cache Windows version introspection before prohibiting shell commands.
    platform.uname()
    blocked = []

    def offline(event, args):
        allowed_child = False
        if event == "subprocess.Popen":
            command = args[1]
            directory = Path(args[2]).resolve() if args[2] is not None else workspace
            if directory.is_relative_to(temporary):
                for mode in ("dns", "connect", "body", "success"):
                    expected = [sys.executable, "-B", "-X", "utf8", str(fixture), mode, str(directory)]
                    # Windows audits the final quoted command line; POSIX
                    # audits the original argument list. Match either exactly.
                    if command == expected or command == subprocess.list2cmdline(expected):
                        allowed_child = True
                        break
        forbidden = (event.startswith("socket.") or event == "sqlite3.connect"
                     or event in {"subprocess.Popen", "os.system", "os.spawn", "os.exec"})
        if forbidden and not allowed_child:
            blocked.append(event)
            raise RuntimeError("Native download tests blocked: " + event)

    sys.addaudithook(offline)
    import pytest
    result = pytest.main([str(workspace / "tests" / "test_download_process_native.py"),
        "-q", "--tb=short", "--basetemp", str(temporary / "pytest"), *sys.argv[1:]])
    child_blocks = list(temporary.rglob("blocked-io.txt"))
    if blocked or child_blocks:
        print("Unexpected external I/O was blocked:", sorted(set(blocked)),
              "child reports:", len(child_blocks))
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
