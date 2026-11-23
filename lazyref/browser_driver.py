"""Internal native-browser launcher; its stdio/CDP handles belong to Playwright."""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lazyref.browser_sandbox import run_browser


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit("Internal browser launcher requires profile, directory and executable.")
    result = run_browser(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
    # Windows exception codes may exceed Python's signed process-exit range.
    raise SystemExit(0 if result == 0 else 1)
