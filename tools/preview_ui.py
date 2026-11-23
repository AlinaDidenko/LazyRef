"""Рендер вікна Qt у PNG без керування робочим столом користувача."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
import lazyref.ui as ui
from lazyref.ui import MainWindow, configure_app, ROOT
from lazyref.models import Asset, Assessment, Candidate, SearchResult

# Прев'ю не читає особисті налаштування чи ключі та не змінює їх.
ui.SETTINGS = ROOT / ".lazyref" / "preview-only" / "settings.json"
for name in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "BRAVE_API_KEY"):
    os.environ.pop(name, None)
app = QApplication([])
configure_app(app)
window = MainWindow()
window.save_settings = lambda: None
if len(sys.argv) > 1:
    folder = Path(sys.argv[1]).resolve()
    if (folder / "generation.json").exists():
        from lazyref.generation import GenerationResult
        data = json.loads((folder / "generation.json").read_text(encoding="utf-8"))
        window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
        window.generation_count.setValue(data["requested"])
        window.description.setPlainText(data["description"])
        window.show_generated(GenerationResult(folder, [folder / row["file"] for row in data["images"]], data["requested"],
            usage=data.get("usage") or {}, usage_path=folder / "usage.json" if data.get("usage") else None))
    else:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assets = [Asset(Candidate(**row["source"]), folder / row["file"], folder / row["file"],
            row["width"], row["height"], row["capture_method"], Assessment(**row["assessment"])) for row in data["results"]]
        window.description.setPlainText(data["description"])
        window.count.setValue(data["requested"])
        profile = data.get("search_profile", {}).get("id", "strict")
        review = data.get("review_session") or {}
        window.search_profile.setCurrentIndex(window.search_profile.findData(profile))
        window.show_results(SearchResult(folder, assets, data["requested"], examined=data["statistics"]["examined"], search_profile=profile,
            verify_sources=data.get("verify_sources", True),
            retry_path=Path(review["path"]) if review.get("pending") else None, pending_review=review.get("pending", 0),
            usage=data.get("usage") or {}, usage_path=folder / "usage.json" if data.get("usage") else None))
        if data.get("reference_guide"):
            from PySide6.QtCore import Qt
            window.provider.setCurrentIndex(window.provider.findData("agent_reference"))
            window.guide_thumbnail.setPixmap(ui.QPixmap(data["reference_guide"]["path"]).scaled(65, 65,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            window.guide_banner.show()
            window.variants_button.setEnabled(False)  # Це статичне прев'ю, не запущена генерація.
    window.progress.setValue(100)
window.show()
app.processEvents()
destination = ROOT / ".lazyref" / ("preview-results.png" if len(sys.argv) > 1 else "preview.png")
destination.parent.mkdir(exist_ok=True)
window.grab().save(str(destination))
print(destination)
QTimer.singleShot(0, window.close)
QTimer.singleShot(0, app.quit)
app.exec()
