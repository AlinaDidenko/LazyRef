"""Перевірка генерації через власний Codex; використовує його ліміти."""
import argparse
import json
from pathlib import Path
import sys
from threading import Event

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lazyref.generation import GenerationRequest, ReferenceGenerator

parser = argparse.ArgumentParser()
parser.add_argument("--description", default="Чоловік сидить за барною стійкою спиною до камери")
parser.add_argument("--count", type=int, default=2)
args = parser.parse_args()
result = ReferenceGenerator().run(GenerationRequest(args.description, args.count, ROOT / "generated_references"),
    Event(), lambda s: print(s.encode("ascii", "backslashreplace").decode(), flush=True))
print(json.dumps({"folder": str(result.folder), "images": [str(p) for p in result.images],
    "requested": result.requested, "error": result.error, "cancelled": result.cancelled}, ensure_ascii=True), flush=True)
raise SystemExit(1 if result.error else 0)
