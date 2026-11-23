"""Прев'ю вікна обліку на вигаданих числах. Codex не запускається."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from lazyref.ui import UsageDialog, configure_app
from lazyref.usage import UsageLedger

ledger = UsageLedger(ROOT / ".lazyref/preview-only/usage-demo.json", "search")
for stage, inputs, outputs in (("planning", 12000, 500), ("search", 32000, 4000), ("assessment", 24000, 1800)):
    call = ledger.begin(stage, "demo-no-model-call", 0)
    call.observe({"type": "turn.completed", "usage": {"input_tokens": inputs, "cached_input_tokens": inputs//2,
        "output_tokens": outputs, "reasoning_output_tokens": outputs//4}})
    call.finish("completed")
call = ledger.begin("search", "demo-no-model-call", 0)
call.finish("cancelled")
app = QApplication([])
configure_app(app)
dialog = UsageDialog(ledger.snapshot(), ledger.path)
dialog.setWindowTitle("Витрати Codex — демонстраційні дані")
dialog.show()
app.processEvents()
target = ROOT / ".lazyref/preview-usage.png"
dialog.grab().save(str(target))
print(target)
dialog.close()
