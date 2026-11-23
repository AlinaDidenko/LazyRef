"""Сумісний запуск: у Windows передає керування Python без консолі."""

from lazyref.launcher import main

if __name__ == "__main__":
    raise SystemExit(main(launch_only=True))

