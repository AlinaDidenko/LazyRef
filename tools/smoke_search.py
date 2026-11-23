"""Ручна перевірка справжнього пошуку; використовує ліміт Codex."""

import argparse
import json
from pathlib import Path
import sys
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lazyref.models import SearchRequest
from lazyref.pipeline import SearchPipeline

parser = argparse.ArgumentParser()
parser.add_argument("--description", default="A real person sitting on a bench")
parser.add_argument("--count", type=int, default=3)
parser.add_argument("--candidates", type=int, default=8)
parser.add_argument("--verification", choices=["on", "off"], default="on")
parser.add_argument("--strategy", choices=["wide", "legacy"], default="wide")
parser.add_argument("--discovery", type=int, default=200)
parser.add_argument("--no-cache", action="store_true")
parser.add_argument("--artistic-nudity", action="store_true")
parser.add_argument("--rounds", type=int, default=6)
parser.add_argument("--reference", type=Path, help="Selected AI pose guide for the Codex agent")
args = parser.parse_args()
request = SearchRequest(args.description, args.count, Path(__file__).resolve().parent.parent / "references",
    candidate_limit=args.candidates, search_rounds=args.rounds, screenshots=True,
    reference_image=args.reference.resolve() if args.reference else None,
    artistic_nudity=args.artistic_nudity, search_strategy=args.strategy, discovery_limit=args.discovery,
    detail_limit=args.candidates, reuse_cache=not args.no_cache, verify_sources=args.verification == "on")
result = SearchPipeline().run(request, Event(), lambda s: print(s.encode("ascii", "backslashreplace").decode(), flush=True))
print(json.dumps({"saved": len(result.assets), "examined": result.examined, "duplicates": result.duplicates,
    "close": sum(a.assessment.is_close for a in result.assets),
    "error": result.error, "warnings": result.warnings, "folder": str(result.folder)}, ensure_ascii=True), flush=True)
raise SystemExit(1 if result.error else 0)
