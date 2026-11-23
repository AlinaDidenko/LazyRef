"""Запис результатів і походження у нову папку кожного пошуку."""

import csv
from dataclasses import asdict, replace
from datetime import datetime
import json
import hashlib
from pathlib import Path
import shutil

from .models import Asset, SearchRequest
from .priorities import PROFILES
from .photo_filters import filter_export, MIN_LONG_SIDE, MIN_SHORT_SIDE


class IncompleteExport(OSError):
    def __init__(self, folder, assets):
        super().__init__("Не всі оцінені фото вдалося скопіювати. Повтори збереження добірки.")
        self.folder, self.assets = folder, assets


def export_results(request: SearchRequest, assets: list[Asset], plan: dict, stats: dict) -> tuple[Path, list[Asset]]:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    folder = request.output_dir / datetime.now().strftime("LazyRef_%Y-%m-%d_%H-%M-%S_%f")
    folder.mkdir(exist_ok=False)
    exported, records = [], []
    eligible = [a for a in assets if a.assessment and a.assessment.eligible_for(request)]
    if request.enhanced_dedup or request.high_quality_only:
        eligible = filter_export(request, eligible, stats)
    ranked = sorted(eligible,
        key=lambda a: (not a.assessment.is_close, -a.assessment.coverage,
                       -a.assessment.preference_coverage if request.search_profile == "custom" else 0,
                       -a.assessment.score, -min(a.width, a.height)))
    for asset in ranked:
        if len(exported) >= request.count:
            break
        index = len(exported) + 1
        label = "close" if asset.assessment.is_close else "partial"
        destination = folder if asset.assessment.is_close else folder / "partial"
        destination.mkdir(exist_ok=True)
        target = destination / f"{index:02d}_{label}.jpg"
        try:
            shutil.copyfile(asset.path, target)
        except OSError:
            target.unlink(missing_ok=True)
            stats.setdefault("warnings", []).append(f"Не вдалося зберегти: {asset.candidate.title}")
            continue
        # The independent exported copy can be removed without breaking review.
        exported.append(replace(asset, path=target, preview=target, review_owned=False))
        records.append({"file": target.relative_to(folder).as_posix(), "match": label, "capture_method": asset.method,
            "width": asset.width, "height": asset.height, "source": asdict(asset.candidate),
            "assessment": asdict(asset.assessment)})
    guide = None
    incomplete = IncompleteExport(folder, exported) if len(exported) < min(request.count, len(ranked)) else None
    if incomplete:
        stats["error"] = "\n".join(filter(None, (stats.get("error", ""), str(incomplete))))
    if request.reference_image:
        guide = {"kind": "visual_reference" if request.reference_mode == "visual" else "ai_generated_pose_guide", "path": str(request.reference_image.resolve()),
                 "sha256": hashlib.sha256(request.reference_image.read_bytes()).hexdigest(),
                 "included_in_results": False}
    manifest = {"version": 8, "created": datetime.now().astimezone().isoformat(),
        "verify_sources": request.verify_sources,
        "photo_filters": {"enhanced_dedup": request.enhanced_dedup, "high_quality_only": request.high_quality_only,
                          "min_long_side": MIN_LONG_SIDE, "min_short_side": MIN_SHORT_SIDE},
        "provenance_status": "assessment_enabled" if request.verify_sources else "unverified",
        "description": request.description, "requested": request.count, "saved": len(exported),
        "requested_model": request.model,
        "requested_reasoning_effort": request.reasoning_effort,
        "reference_mode": request.reference_mode,
        "custom_criteria": request.custom_criteria,
        "close_matches": sum(a.assessment.is_close for a in exported),
        "partial_matches": sum(not a.assessment.is_close for a in exported),
        "provider": request.provider, "candidate_limit": request.candidate_limit,
        "search_rounds": request.search_rounds,
        "search_strategy": request.search_strategy,
        "search_budgets": {"discovery": request.discovery_limit, "detail": request.detail_limit,
                           "reuse_cache": request.reuse_cache} if request.search_strategy == "wide" else None,
        "search_profile": dict(PROFILES[request.search_profile]),
        "reference_guide": guide,
        "usage": stats.get("usage"),
        "review_session": stats.get("review_session"),
        "artistic_nudity": request.allows_artistic_nudity,
        "plan": plan, "statistics": stats, "results": records,
        "authenticity_note": ("Оцінка моделі не підтверджує походження фото. Перевіряй джерело." if request.verify_sources
                              else "Походження не перевірено. Оцінено відповідність пози; зображення може бути згенерованим або намальованим."),
        "license_note": "Ліцензія вказана джерелом; перед використанням перевір її на оригінальній сторінці."}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if stats.get("usage"):
        (folder / "usage.json").write_text(json.dumps(stats["usage"], ensure_ascii=False, indent=2), encoding="utf-8")
    if stats.get("access_links"):
        with (folder / "access_links.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Назва", "Посилання", "Джерело", "Статус"])
            for link in stats["access_links"]:
                row = [link["title"], link["url"], link["source"], link["reason"]]
                writer.writerow(["'" + str(x) if str(x).startswith(("=", "+", "-", "@")) else x for x in row])
    with (folder / "sources.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Файл", "Джерело", "Автор", "Ліцензія", "Посилання на ліцензію", "Відповідність", "Пояснення", "Спосіб збереження", "Перевірка походження"])
        for asset in exported:
            row = [asset.path.relative_to(folder).as_posix(), asset.candidate.page_url or asset.candidate.local_path,
                asset.candidate.creator, asset.candidate.license, asset.candidate.license_url,
                asset.assessment.score, asset.assessment.reason, asset.method,
                "Увімкнена; автентичність не гарантована" if request.verify_sources else "Походження не перевірено"]
            # Зовнішні назви не мають стати формулами, якщо CSV відкриють в Excel.
            writer.writerow(["'" + str(x) if str(x).startswith(("=", "+", "-", "@")) else x for x in row])
    if incomplete:
        raise incomplete
    return folder, exported

