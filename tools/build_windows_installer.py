"""Build the Windows x64 installer and its release files with Inno Setup 7."""

import argparse
import hashlib
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "installer"
PYTHON_ARCHIVE = "python-3.14.10-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/3.14.10/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
NODE_VERSION = "24.22.3"
NODE_DISTRIBUTION = f"node-v{NODE_VERSION}-win-x64"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_DISTRIBUTION}.zip"
NODE_SHA256 = "e612af99f626ab415ea225af6bce5c971feb448cb5cae250bb1c3fa50c5f7d9f"
QT_SOURCE_HASHES = {
    "qtbase-everywhere-src-6.12.0.tar.xz": "a951bd163c7b80fc6b8c88d7668fb56abf91c152373e13c10666763238131307",
    "qtimageformats-everywhere-src-6.12.0.tar.xz": "0943132b5db8de6b6a7f1d162682173e8763c7ed94e80fd39113292845f67762",
    "pyside-setup-everywhere-src-6.12.0.tar.xz": "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751",
}

MAKE_ICON = """
from io import BytesIO
import sys

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication
from lazyref.ui import app_icon

app = QApplication([])
buffer = QBuffer()
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
assert app_icon().pixmap(64, 64).save(buffer, "PNG")
with Image.open(BytesIO(bytes(buffer.data()))) as image:
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        sys.argv[1], sizes=[(16, 16), (32, 32), (48, 48), (256, 256)]
    )
"""


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_archive(staging, archive, url, checksum):
    if archive is None:
        archive = staging / url.rsplit("/", 1)[-1]
        with urlopen(url, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
    if sha256(archive) != checksum:
        raise ValueError(f"{archive.name} does not match the upstream SHA-256 checksum.")
    return archive


def stage_runtime(staging, payload, archive, wheel_dir, node_archive):
    runtime = payload / "runtime"
    with ZipFile(archive) as distribution:
        distribution.extractall(runtime)
    (runtime / "python314._pth").write_text(
        "python314.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="ascii"
    )
    wheels = staging / "installed-wheels"
    package_index = ["--index-url", "https://pypi.org/simple"] if wheel_dir is None else [
        "--no-index", "--find-links", str(wheel_dir)
    ]
    subprocess.run([
        sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
        *package_index, "--target", str(wheels), "--no-compile", "--only-binary=:all:",
        "--platform", "win_amd64", "--implementation", "cp", "--python-version", "3.14",
        "--abi", "cp314", "--require-hashes", "--requirement", str(CONFIG / "requirements-win-x64.txt"),
    ], check=True)

    packages = runtime / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    for entry in wheels.iterdir():
        # pip's unused entry-point launchers point at the build interpreter.
        if entry.name in {"bin", "PySide6"}:
            continue
        if entry.is_dir():
            shutil.copytree(entry, packages / entry.name)
        else:
            shutil.copy2(entry, packages)
    stage_qt(wheels / "PySide6", packages / "PySide6")

    # Write the pinned Node.js release and its matching license into the
    # directory used by Playwright and the isolated browser relay.
    driver = packages / "playwright" / "driver"
    with ZipFile(node_archive) as node:
        for name in ("node.exe", "LICENSE"):
            with node.open(f"{NODE_DISTRIBUTION}/{name}") as source, (driver / name).open("wb") as output:
                shutil.copyfileobj(source, output)


def stage_qt(source, destination):
    # LazyRef uses Qt Core, GUI and Widgets, with PNG, JPEG and WebP rendering.
    # Include the Windows and offscreen platforms and their required runtimes.
    files = [
        "__init__.py", "_config.py", "_git_pyside_version.py", "pyside6.abi3.dll",
        "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd",
        "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
        "plugins/platforms/qwindows.dll", "plugins/platforms/qoffscreen.dll",
        "plugins/imageformats/qjpeg.dll", "plugins/imageformats/qwebp.dll",
    ]
    files.extend(item.name for item in source.glob("*.dll")
                 if item.name.startswith(("msvcp", "vcruntime", "concrt")))
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    shutil.copytree(source / "support", destination / "support")


def stage_sources(payload):
    for name in ("app.py", "app.pyw"):
        shutil.copy2(ROOT / name, payload)
    (payload / "lazyref").mkdir()
    for module in (ROOT / "lazyref").glob("*.py"):
        shutil.copy2(module, payload / "lazyref")
    shutil.copy2(CONFIG / "THIRD_PARTY_NOTICES.txt", payload)


def stage_qt_licenses(source_dir, payload, version, dist):
    source_archive = dist / f"LazyRef-{version}-Qt-sources.zip"
    with ZipFile(source_archive, "w") as source_bundle:
        for name, checksum in QT_SOURCE_HASHES.items():
            upstream = source_dir / name
            if sha256(upstream) != checksum:
                raise ValueError(f"The Qt source archive checksum did not match for {name}.")
            source_bundle.write(upstream, name)
            with tarfile.open(upstream, "r:xz") as archive:
                # Pick only top-level license files. Keep each project's licenses together.
                for member in archive:
                    parts = member.name.split("/")
                    if member.isfile() and len(parts) == 3 and parts[1] == "LICENSES":
                        folder = payload / "licenses" / name.removesuffix(".tar.xz")
                        folder.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as license_file:
                            (folder / Path(parts[2]).name).write_bytes(license_file.read())
        source_bundle.write(CONFIG / "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt")
    if not list((payload / "licenses").glob("*/LGPL-3.0-only.txt")):
        raise ValueError("The Qt source archives must contain the LGPLv3 license text.")
    return source_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", required=True, type=Path, help="Inno Setup 7 ISCC.exe")
    parser.add_argument("--qt-source-dir", required=True, type=Path, help="Directory with the three Qt source archives")
    parser.add_argument("--python-archive", type=Path, help="Use a local copy of the verified Python embeddable ZIP")
    parser.add_argument("--node-archive", type=Path, help="Use a local copy of the verified Node.js Windows x64 ZIP")
    parser.add_argument("--wheel-dir", type=Path, help="Use existing wheels instead of downloading from PyPI")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Build the installer on Windows.")

    compiler = args.iscc.resolve(strict=True)
    version = runpy.run_path(str(ROOT / "lazyref" / "__init__.py"))["__version__"]
    build, dist = ROOT / "build", ROOT / "dist"
    build.mkdir(exist_ok=True)
    dist.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="installer-", dir=build))
    payload = staging / "payload"
    payload.mkdir()
    python = verified_archive(staging, args.python_archive, PYTHON_URL, PYTHON_SHA256)
    node = verified_archive(staging, args.node_archive, NODE_URL, NODE_SHA256)
    stage_runtime(staging, payload, python, args.wheel_dir, node)
    stage_sources(payload)
    qt_source = stage_qt_licenses(args.qt_source_dir, payload, version, dist)
    subprocess.run([
        str(payload / "runtime" / "python.exe"), "-B", "-X", "utf8", "-c", MAKE_ICON,
        str(payload / "LazyRef.ico"),
    ], check=True, cwd=payload, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    subprocess.run([
        str(compiler), "/Q", f"/DAppVersion={version}", f"/DPayload={payload}", f"/DOutputDir={dist}",
        str(CONFIG / "LazyRef.iss"),
    ], check=True)
    installer = dist / f"LazyRef-{version}-Windows-x64-Setup.exe"
    (dist / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in (installer, qt_source)), encoding="ascii"
    )
    print(f"Installer: {installer}\nBuild payload: {payload}")


if __name__ == "__main__":
    main()
