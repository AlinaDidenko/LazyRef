"""Run synthetic Windows browser tests in an owned, time-limited process tree."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    if os.name != "nt":
        print("Native browser tests require Windows and installed Microsoft Edge.")
        return 1
    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace))
    from lazyref.browser_sandbox import cleanup_sandboxes
    from lazyref.processes import ProcessTree

    root = Path(tempfile.mkdtemp(prefix="lazyref-native-")).resolve()
    browser_root = root / "browser"
    environment = os.environ.copy()
    environment.update(LAZYREF_NATIVE_BROWSER_TESTS="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1", LAZYREF_CAPTURE_ROOT=str(browser_root),
        TEMP=str(root), TMP=str(root))
    # The production capture helper has its own memory limit. The complete
    # pytest tree can legitimately contain several concurrent browser fixtures.
    tree = ProcessTree(timeout=5)
    code, errors = 1, []
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "pytest",
            "-q", "-p", "no:cacheprovider", "--tb=short", "tests/test_dynamic_browser.py", *sys.argv[1:],
            "--basetemp", str(root / "pytest")], cwd=workspace, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **tree.popen_options)
        tree.attach(process)
        try:
            output, _ = process.communicate(timeout=120)
            code = process.returncode
        except subprocess.TimeoutExpired:
            print("Native tests exceeded the 120-second watchdog.", flush=True)
            tree.close()
            output, _ = process.communicate(timeout=5)
            code = 124
        print(output.decode("utf-8", errors="replace"), end="", flush=True)
    except Exception as exc:
        errors.append(exc)
    finally:
        try:
            tree.close()
        except Exception as exc:
            errors.append(exc)
        if tree.closed_cleanly:
            try:
                # All paths are children of this invocation's unique TEMP root.
                # Never remove journals while an owned process might still run.
                if root.resolve() != root or not root.name.startswith("lazyref-native-"):
                    raise ValueError("Unexpected native-test temporary directory.")
                if browser_root.exists():
                    if browser_root.resolve() != browser_root:
                        raise ValueError("Unexpected browser-test temporary directory.")
                    cleanup_sandboxes(browser_root)
                for child in root.iterdir():
                    if not re.fullmatch(r"lazyref-capture-[A-Za-z0-9_-]+", child.name):
                        continue
                    if child.resolve().parent != root or child.is_symlink() or not child.is_dir():
                        raise ValueError("Unexpected capture-helper temporary directory.")
                    cleanup_sandboxes(child)
                shutil.rmtree(root)
            except Exception as exc:
                errors.append(exc)
        else:
            errors.append(RuntimeError("Owned browser process termination was not confirmed."))
    if errors:
        for error in errors:
            print("Native-test cleanup/error:", error, file=sys.stderr)
        if root.exists():
            print("Retained native-test recovery data:", root, file=sys.stderr)
        return code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
