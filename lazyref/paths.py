"""Keep installed user data separate from replaceable program files."""

import os
from pathlib import Path


PROGRAM_DIR = Path(__file__).resolve().parents[1]
BUNDLED_INTERPRETER = Path("runtime/python.exe")


def data_directory(program_dir: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data and (program_dir / BUNDLED_INTERPRETER).is_file():
        return Path(local_app_data) / "LazyRef"
    return program_dir
