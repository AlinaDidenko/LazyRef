"""GUI entry point; launcher ensures the project interpreter has no console."""

from lazyref.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
