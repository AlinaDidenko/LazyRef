"""Пошук 0.4: широка вибірка → мініатюри → джерела → детальний аналіз.

Попередній алгоритм залишається в pipeline.py і search_agent.py.
"""

from collections import deque
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
from uuid import uuid4

from .access import SourceBlocked
from .authenticity import GeneratedImage
from .capture import BrowserCapture
from .codex import CodexClient
from .exporter import IncompleteExport, export_results
from .i18n import translate
from .images import DuplicateIndex, download, prepare, screening_preview
from .models import Cancelled, SearchResult, check_cancel
from .network import Network
from .priorities import PROFILES
from .review_session import ReviewSession
from .retry_analysis import ReviewLock
from .search_cache import SearchCache, task_key, key_for, file_hash, assessment_key, cached_rating, SOURCE_TTL
from .usage import UsageLedger, related_generation_usage
from .wide_agent import candidate_from, discover_wide, screen, check_sources
from .custom_priorities import make_plan
from .photo_filters import LowResolution, check_resolution, ResultSelection
from .download_budget import request_budget
from .result_cleanup import cleanup_result, temporary_directory


class WideSearchPipeline:
    def __init__(self, client=None, loader=download, *, preview_loader=screening_preview, wait_changed=None, plan_editor=None):
        self.client, self.loader, self.preview_loader = client, loader, preview_loader
        self.wait_changed = wait_changed
        self.plan_editor = plan_editor

    def run(self, request, cancel, status=lambda text: None, progress=lambda value: None):
        request.validate()
        request.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=request.output_dir):
            pass
        result = SearchResult(None, [], request.count, search_profile=request.search_profile, search_strategy="wide",
                              verify_sources=request.verify_sources, enhanced_dedup=request.enhanced_dedup,
                              high_quality_only=request.high_quality_only)
        ledger = UsageLedger(request.output_dir / "_usage" / f"search_{uuid4().hex}.json", "search",
                             related_generation_usage(request.reference_image))
        result.usage_path = ledger.path
        cache = SearchCache(request.output_dir / "_search_cache", request.reuse_cache)
        network = Network(cancel)
        network.verify_sources = request.verify_sources
        network.high_quality_only = request.high_quality_only
        selection = ResultSelection(request.enhanced_dedup)
        low_resolution_ids = set()
        full_kind = "full" if request.verify_sources else "full_unverified"
        preview_kind = "preview" if request.verify_sources else "preview_unverified"
        network.search_trace, network.source_trace = [], []
        capture = BrowserCapture(cancel, network.access)
        pool, full_images, screening, processed, origin_round = {}, {}, {}, set(), {}
        accepted, evaluations, rounds, precheck, errors = [], [], [], [], []
        plan, session, lock, task, history = {}, None, None, None, {}
        queries, cached_candidates = [], []
        preview_unique = DuplicateIndex()
        full_unique = DuplicateIndex(allow_larger=request.enhanced_dedup)
        settled_full = set()
        metrics = {"preview_downloads": 0, "full_download_attempts": 0, "screenshots": 0,
                   "cached_candidates": 0, "cached_previews": 0, "cached_files": 0,
                   "cached_plan": 0, "cached_screenings": 0, "cached_sources": 0,
                   "newly_screened": 0, "newly_examined": 0, "reused_assessments": 0,
                   "source_checks": 0}
        stop = "error"
        try:
            with temporary_directory(result, prefix="lazyref-wide-") as temporary:
                directory = Path(temporary)
                try:
                    check_cancel(cancel)
                    client = self.client or CodexClient(request.codex_path, request.model,
                        reasoning_effort=request.reasoning_effort)
                    client.usage_ledger, client.wait_changed = ledger, self.wait_changed
                    if request.search_profile == "custom" and request.custom_criteria is None:
                        status(translate("Codex пропонує умови. Потім ти зможеш уточнити їх перед пошуком…"))
                    custom_plan = make_plan(client, request, cancel, self.plan_editor) if request.search_profile == "custom" else None
                    task = task_key(request)
                    history = cache.get("task", task) or {}
                    if not isinstance(history, dict):
                        history = {}
                    cached_candidates = history.get("candidates", [])
                    if not isinstance(cached_candidates, list):
                        cached_candidates = []
                    queries = [q for q in history.get("queries", []) if isinstance(q, str)][-32:]
                    plan = custom_plan or history.get("plan") or {}
                    if not (isinstance(plan, dict) and plan.get("criteria") and plan.get("queries") and plan.get("summary")):
                        status(translate("Codex складає план і описує позу для пошуку…"))
                        plan = make_plan(client, request, cancel, self.plan_editor)
                    elif custom_plan is None:
                        metrics["cached_plan"] = 1
                        status(translate("Використовую збережений план того самого запиту й зразка."))
                    plan = {**plan, "search_profile": dict(PROFILES[request.search_profile])}
                    result.summary = plan["summary"]
                    session = ReviewSession.create(request, plan, ledger.related_generation)
                    lock = ReviewLock(session.path.parent / "review.lock")
                    lock.__enter__()
                    ledger.mirror_path = session.path.parent / "attempts" / f"search_{ledger.run_id}.json"
                    ledger.save(strict=True)
                    reference_args = request.reference_args
                    profile_args = {"search_profile": request.search_profile} if request.search_profile != "strict" else {}
                    if not request.verify_sources:
                        profile_args["verify_sources"] = False

                    def save_history():
                        # Кеш не замінює новий пошук: відхилені раніше фото йдуть у кінець.
                        scores = {e["id"]: (0 if e["close"] else 1 if e["accepted"] else 4) for e in evaluations}
                        current = sorted(pool.values(), key=lambda c: scores.get(c.id,
                            3 if screening.get(c.id, {}).get("decision") == "reject" else 2))
                        merged = {c.image_url: asdict(c) for c in current}
                        for row in cached_candidates:
                            if isinstance(row, dict) and row.get("image_url") and len(merged) < 600:
                                merged.setdefault(row["image_url"], row)
                        cache.put("task", task, {"plan": plan, "queries": list(dict.fromkeys(queries))[-32:],
                                                 "candidates": list(merged.values())})

                    def source_key(c):
                        return key_for([c.page_url, c.image_url])

                    def apply_source(asset, evidence):
                        if isinstance(evidence, dict) and evidence.get("source_kind") in {"unknown", "photo_credit", "generated_label"}:
                            asset.candidate = replace(asset.candidate,
                                **{k: evidence[k] for k in ("source_kind", "source_notes", "source_checked", "creator")})
                            pool[asset.candidate.id] = asset.candidate

                    def record(batch, ratings, reused=False, context=None):
                        if context is None:
                            context = session.context(request)
                        session.complete(batch, ratings, request.search_profile, context=context)
                        for asset in batch:
                            a = replace(ratings[asset.candidate.id], search_profile=request.search_profile)
                            asset.assessment = a
                            processed.add(asset.candidate.id)
                            result.examined += 1
                            metrics["reused_assessments" if reused else "newly_examined"] += 1
                            evaluations.append({"id": asset.candidate.id, "title": asset.candidate.title,
                                "source_url": asset.candidate.page_url, "media": a.media, "confidence": a.confidence,
                                "score": a.score, "reference_type": a.reference_type, "accepted": a.eligible_for(request),
                                "close": a.is_close, "reason": a.reason, "matches": a.matches, "misses": a.misses,
                                "criterion_checks": a.criterion_checks, "round": origin_round.get(asset.candidate.id, 0), "from_cache": reused})
                            if a.eligible_for(request):
                                previous = len(selection.removed_ids)
                                selection.add(asset, accepted)
                                result.duplicates += len(selection.removed_ids) - previous
                            else:
                                result.rejected += 1
                            if not reused:
                                cache.put("assessment", assessment_key(task, plan, asset), asdict(a))
                        save_history()
                        if session.persistence_error:
                            raise OSError(session.persistence_error)

                    def reject(candidate, reason):
                        processed.add(candidate.id)
                        result.rejected += 1
                        precheck.append({"id": candidate.id, "title": candidate.title, "reason": str(reason)[:500]})

                    def restore_rating(c):
                        asset = cache.asset(full_kind, c)
                        evidence = cache.get("source", source_key(c), SOURCE_TTL) if request.verify_sources else {
                            "source_kind": "unknown", "source_notes": "Походження не перевірено", "source_checked": False, "creator": ""}
                        if asset is None or not isinstance(evidence, dict):
                            return False
                        try:
                            check_resolution(asset, request.high_quality_only)
                        except LowResolution:
                            # A cached thumbnail must not hide a now-accessible original.
                            return False
                        apply_source(asset, evidence)
                        if request.verify_sources and asset.candidate.source_kind == "generated_label":
                            reject(c, asset.candidate.source_notes)
                            return True
                        rating = cached_rating(cache.get("assessment", assessment_key(task, plan, asset)),
                                               plan["criteria"], request.search_profile, request.verify_sources)
                        if rating is None:
                            return False
                        duplicate_group, is_new = full_unique.add_with_index(asset.path, preferred=settled_full)
                        if not is_new:
                            processed.add(c.id)
                            result.duplicates += 1
                            return True
                        settled_full.add(duplicate_group)
                        metrics["cached_files"] += 1
                        metrics["cached_sources"] += int(request.verify_sources)
                        saved = session.add(asset, 0)
                        record([saved], {c.id: rating}, reused=True)
                        return True

                    def screen_candidates(candidates):
                        pending = []
                        def flush():
                            if not pending:
                                return
                            # Equal thumbnails may point to different originals.
                            # Preserve those candidates, but inspect identical
                            # thumbnail bytes only once in this batch. Screening
                            # uses pixels/criteria, never source metadata; full
                            # files still get separate source checks and ratings.
                            representatives = {}
                            owners = {}
                            for asset in pending:
                                key = file_hash(asset.preview) if request.enhanced_dedup or request.verify_sources else asset.candidate.id
                                representative = representatives.setdefault(key, asset)
                                owners[asset.candidate.id] = representative.candidate.id
                            batch = list(representatives.values())
                            status(translate("Швидкий відбір: {count} мініатюр · уже переглянуто {screened}",
                                             count=len(batch), screened=len(screening)))
                            decisions = screen(client, request, plan, batch, cancel)
                            metrics["newly_screened"] += len(batch)
                            for asset in pending:
                                decision = {**decisions[owners[asset.candidate.id]], "id": asset.candidate.id}
                                screening[asset.candidate.id] = decision
                                cache.put("screen", key_for([task, plan["criteria"], file_hash(asset.preview)]), decision)
                            pending.clear()
                        for c in candidates:
                            check_cancel(cancel)
                            if c.id in processed:
                                continue
                            try:
                                asset = cache.asset(preview_kind, c)
                                if asset:
                                    metrics["cached_previews"] += 1
                                else:
                                    metrics["preview_downloads"] += 1
                                    # A later full download gets a fresh budget:
                                    # screening/model calls are not network wait.
                                    with request_budget(network):
                                        asset, full = self.preview_loader(c, directory, network)
                                        check_cancel(cancel)
                                    cache.store_asset(preview_kind, asset)
                                    if full:
                                        full_images[c.id] = full
                                        cache.store_asset(full_kind, full)
                                # Identical thumbnails can lead to originals of different sizes.
                                # Verified search also needs alternate sources
                                # if the first page turns out to be inaccessible.
                                if not (request.high_quality_only or request.enhanced_dedup or request.verify_sources) and not preview_unique.add(asset.path):
                                    processed.add(c.id)
                                    result.duplicates += 1
                                    continue
                                decision = cache.get("screen", key_for([task, plan["criteria"], file_hash(asset.preview)]))
                                if (isinstance(decision, dict) and decision.get("decision") in {"keep", "uncertain", "reject"}
                                        and type(decision.get("priority")) is int and 0 <= decision["priority"] <= 100):
                                    screening[c.id] = {**decision, "id": c.id}
                                    metrics["cached_screenings"] += 1
                                else:
                                    pending.append(asset)
                            except Cancelled:
                                raise
                            except GeneratedImage as exc:
                                reject(c, exc)
                            except SourceBlocked as exc:
                                errors.append(str(exc))
                            except (OSError, ValueError, RuntimeError) as exc:
                                errors.append(f"Мініатюра недоступна: {c.title[:80]} ({str(exc)[:100]})")
                            # Keep model failures outside the per-image handler:
                            # a download timeout skips only its candidate, while
                            # a model timeout must preserve the pending review.
                            if len(pending) >= 12:
                                flush()
                        flush()

                    # До третини вибірки — з попереднього пошуку; лишається місце для нових фото.
                    for row in cached_candidates:
                        check_cancel(cancel)
                        if len(pool) >= min(60, request.discovery_limit // 3):
                            break
                        try:
                            c = candidate_from(row, network, verify_sources=request.verify_sources)
                            if c.id not in pool:
                                pool[c.id] = c
                                metrics["cached_candidates"] += 1
                        except (ValueError, TypeError, KeyError, SourceBlocked):
                            continue
                    save_history()
                    for c in list(pool.values()):
                        check_cancel(cancel)
                        restore_rating(c)
                    enough = lambda: sum(a.assessment.is_close for a in accepted) >= request.count
                    if enough():
                        stop = "enough_cached_close_matches"
                    else:
                        screen_candidates(list(pool.values()))
                        idle = 0
                        for index in range(request.search_rounds):
                            check_cancel(cancel)
                            if len(pool) >= request.discovery_limit:
                                break
                            status(translate("Широкий пошук {round}/{total} · кандидатів {count}/{limit}",
                                             round=index + 1, total=request.search_rounds,
                                             count=len(pool), limit=request.discovery_limit))
                            found, report = discover_wide(client, request, plan, pool, queries, evaluations,
                                min(40, request.discovery_limit - len(pool)), network, cancel, status)
                            report["round"] = index + 1
                            queries.extend(report["queries"])
                            pool.update({c.id: c for c in found})
                            origin_round.update({c.id: index+1 for c in found})
                            rounds.append({"round": index+1, "queries": report["queries"], "new_candidates": len(found),
                                           "pool_total": len(pool), "examined": 0, "strategy": report["strategy"]})
                            if report["limitation"]:
                                errors.append(report["limitation"])
                            save_history()  # Адреси переживуть збій першої оцінки.
                            screen_candidates(found)
                            progress(10 + int(50 * (index + 1) / request.search_rounds))
                            idle = idle + 1 if not found else 0
                            if idle >= 2:
                                break
                        # Кожне п'яте місце резервується для сумнівної мініатюри.
                        usable = [c for c in pool.values() if c.id not in processed
                                  and screening.get(c.id, {}).get("decision") in {"keep", "uncertain"}]
                        groups = {label: sorted((c for c in usable if screening[c.id]["decision"] == label),
                            key=lambda c: -screening[c.id]["priority"]) for label in ("keep", "uncertain")}
                        ordered = []
                        while groups["keep"] or groups["uncertain"]:
                            label = "uncertain" if groups["uncertain"] and (len(ordered) % 5 == 4 or not groups["keep"]) else "keep"
                            ordered.append(groups[label].pop(0))
                        # Невдалі завантаження/повтори не витрачають бюджет детальних оцінок.
                        offset = 0
                        while offset < len(ordered) and metrics["newly_examined"] < request.detail_limit and not enough():
                            check_cancel(cancel)
                            source_groups = {}
                            capacity = min(8, request.detail_limit - metrics["newly_examined"])
                            while offset < len(ordered) and len(source_groups) < capacity:
                                c = ordered[offset]
                                offset += 1
                                try:
                                    check_cancel(cancel)
                                    if c.page_url:
                                        network.access.check(c.page_url)
                                    asset = full_images.get(c.id) or cache.asset(full_kind, c)
                                    if asset:
                                        try:
                                            check_resolution(asset, request.high_quality_only)
                                        except LowResolution:
                                            asset = None
                                    if asset:
                                        metrics["cached_files"] += int(c.id not in full_images)
                                    else:
                                        metrics["full_download_attempts"] += 1
                                        status(translate("Завантажую відібране фото: {title}", title=c.title[:80]))
                                        with request_budget(network) as budget:
                                            try:
                                                asset = self.loader(c, directory, network)
                                                check_cancel(cancel)
                                            except (Cancelled, GeneratedImage, SourceBlocked, LowResolution):
                                                raise
                                            except Exception:
                                                check_cancel(cancel)
                                                if not request.verify_sources or not request.screenshots or not c.page_url or metrics["screenshots"] >= 3:
                                                    raise
                                                budget.check_total()
                                                metrics["screenshots"] += 1
                                                asset = prepare(capture.capture(c, deadline=budget.total_deadline),
                                                                c, directory, "screenshot")
                                                check_cancel(cancel)
                                        cache.store_asset(full_kind, asset)
                                    check_resolution(asset, request.high_quality_only)
                                    duplicate_group, _ = full_unique.add_with_index(asset.path, preferred=settled_full)
                                    if duplicate_group in settled_full:
                                        processed.add(c.id)
                                        result.duplicates += 1
                                        continue
                                    source_groups.setdefault(duplicate_group, deque()).append(asset)
                                except Cancelled:
                                    raise
                                except GeneratedImage as exc:
                                    reject(c, exc)
                                except LowResolution as exc:
                                    low_resolution_ids.add(c.id)
                                    result.low_resolution_rejected = len(low_resolution_ids)
                                    reject(c, exc)
                                except Exception as exc:
                                    errors.append(f"Фото недоступне: {c.title[:80]} ({str(exc)[:100]})")
                            if request.enhanced_dedup:
                                # The first (larger) file may be inaccessible.
                                # Prefer the best remaining loaded copy rather
                                # than sealing its group with a smaller fallback.
                                source_groups = {key: deque(sorted(alternatives,
                                    key=lambda a: (min(a.width, a.height), a.width * a.height), reverse=True))
                                    for key, alternatives in source_groups.items()}
                            # Keep loaded alternatives until a source succeeds.
                            # Each wave checks one representative per group;
                            # source evidence and ratings remain candidate-owned.
                            while source_groups and metrics["newly_examined"] < request.detail_limit and not enough():
                                check_cancel(cancel)
                                group = []
                                for duplicate_group, alternatives in list(source_groups.items()):
                                    while alternatives:
                                        check_cancel(cancel)
                                        asset = alternatives.popleft()
                                        if settled_full:
                                            # Another group may have accepted a
                                            # larger copy in the preceding wave.
                                            match, _ = full_unique.add_with_index(asset.path, preferred=settled_full)
                                            if match in settled_full:
                                                processed.add(asset.candidate.id)
                                                result.duplicates += 1
                                                continue
                                        try:
                                            if asset.candidate.page_url:
                                                network.access.check(asset.candidate.page_url)
                                        except SourceBlocked as exc:
                                            errors.append(str(exc))
                                            continue
                                        group.append((duplicate_group, asset))
                                        break
                                    else:
                                        del source_groups[duplicate_group]
                                to_check = []
                                for _, asset in group:
                                    if not request.verify_sources:
                                        apply_source(asset, {"source_kind": "unknown", "source_notes": "Походження не перевірено",
                                                            "source_checked": False, "creator": ""})
                                        continue
                                    evidence = cache.get("source", source_key(asset.candidate), SOURCE_TTL)
                                    if isinstance(evidence, dict):
                                        apply_source(asset, evidence)
                                        metrics["cached_sources"] += 1
                                    else:
                                        to_check.append(asset)
                                if to_check:
                                    status(translate("Перевіряю джерела лише відібраних фото: {count}", count=len(to_check)))
                                    evidence = check_sources(client, to_check, network, cancel, status)
                                    metrics["source_checks"] += len(to_check)
                                    for asset in to_check:
                                        apply_source(asset, evidence[asset.candidate.id])
                                        try:
                                            if asset.candidate.page_url:
                                                network.access.check(asset.candidate.page_url)
                                        except SourceBlocked:
                                            # An inaccessible page is not reusable
                                            # evidence of an available unknown source.
                                            continue
                                        cache.put("source", source_key(asset.candidate), evidence[asset.candidate.id])
                                check_cancel(cancel)
                                pending = []
                                for duplicate_group, asset in group:
                                    c = asset.candidate
                                    try:
                                        if c.page_url:
                                            network.access.check(c.page_url)
                                    except SourceBlocked as exc:
                                        errors.append(str(exc))
                                        continue
                                    # A checked generated label is a rejection,
                                    # not a reason to seek a more permissive source.
                                    if request.enhanced_dedup:
                                        # Later candidates must compare against
                                        # the usable copy, not the blocked original.
                                        full_unique.replace(duplicate_group, asset.path)
                                    settled_full.add(duplicate_group)
                                    alternatives = source_groups.pop(duplicate_group)
                                    result.duplicates += len(alternatives)
                                    processed.update(a.candidate.id for a in alternatives)
                                    if request.verify_sources and c.source_kind == "generated_label":
                                        reject(c, c.source_notes)
                                        continue
                                    asset = session.add(asset, origin_round.get(c.id, 0))
                                    rating = cached_rating(cache.get("assessment", assessment_key(task, plan, asset)),
                                                           plan["criteria"], request.search_profile, request.verify_sources)
                                    if rating:
                                        record([asset], {c.id: rating}, reused=True)
                                    else:
                                        pending.append(asset)
                                for start in range(0, len(pending), 3):
                                    check_cancel(cancel)
                                    batch = pending[start:start+3]
                                    status(translate("Детальний аналіз {count} фото · нових оцінок {examined}/{limit}",
                                                     count=len(batch), examined=metrics["newly_examined"],
                                                     limit=request.detail_limit))
                                    context = session.context(request)
                                    ratings = client.assess(request.search_description, batch, cancel,
                                        criteria=plan["criteria"], **reference_args, **profile_args)
                                    record(batch, ratings, context=context)
                                    progress(65 + int(30 * metrics["newly_examined"] / request.detail_limit))
                        stop = ("enough_close_matches" if enough() else "detail_limit" if metrics["newly_examined"] >= request.detail_limit
                                else "shortlist_exhausted")
                    save_history()
                except Cancelled:
                    result.cancelled, stop = True, "cancelled"
                except Exception as exc:
                    result.error, stop = str(exc), "error"
                finally:
                    if task and plan:
                        # Збереження відбувається і після тайм-ауту, без нових хмарних викликів.
                        try:
                            save_history()
                        except (NameError, UnboundLocalError):
                            pass
                result.discovered, result.screened = len(pool), len(screening)
                result.reused_assessments = metrics["reused_assessments"]
                result.warnings = list(dict.fromkeys(errors))[:50]
                if cache.warning:
                    result.warnings.append(cache.warning)
                if not accepted:
                    result.warnings.append("У перевіреній вибірці немає достатньо відповідних фото.")
                elif not any(a.assessment.is_close for a in accepted):
                    result.warnings.append("Збережено часткові альтернативи; близьких збігів не знайдено.")
                result.usage = ledger.snapshot()
                if ledger.write_error:
                    result.warnings.append(ledger.write_error)
                result.access_links = list(network.access_links)
                if plan:
                    for r in rounds:
                        r["examined"] = sum(e["round"] == r["round"] for e in evaluations)
                        r["screened"] = sum(origin_round.get(i) == r["round"] for i in screening)
                    stats = {"search_mode": "codex_wide_agent", "stop_reason": stop, "examined": result.examined,
                        "enhanced_duplicate_ids": sorted(selection.removed_ids), "low_resolution_ids": sorted(low_resolution_ids),
                        "verify_sources": request.verify_sources,
                        "retrieved_unique": len(pool), "screened": len(screening), "screening": list(screening.values()),
                        "screen_rejected": sum(s["decision"] == "reject" for s in screening.values()),
                        "metrics": metrics, "evaluations": evaluations, "precheck_rejections": precheck,
                        "retrieval": network.search_trace, "source_verification": network.source_trace,
                        "rounds": rounds, "candidates": [asdict(c) for c in pool.values()],
                        "duplicates": result.duplicates, "rejected": result.rejected, "cancelled": result.cancelled,
                        "error": result.error, "warnings": result.warnings, "access_links": result.access_links,
                        "usage": result.usage, "blocked_hosts": network.access.blocked,
                        "download_attempts": metrics["preview_downloads"] + metrics["full_download_attempts"]}
                    if session:
                        stats["review_session"] = session.descriptor()
                        result.pending_review = session.pending_count
                    status(translate("Зберігаю фото та звіт усіх етапів пошуку…"))
                    result.assets = list(accepted)
                    try:
                        if session:
                            session.finish(stats, result.usage)
                        result.folder, result.assets = export_results(request, accepted, plan, stats)
                        result.usage_path = result.folder / "usage.json"
                        if session:
                            session.finish(stats, result.usage, result.folder)
                    except OSError as exc:
                        result.error = "\n".join(filter(None, (result.error, str(exc))))
                        if isinstance(exc, IncompleteExport):
                            result.folder, result.assets = exc.folder, exc.assets
                            result.usage_path = result.folder / "usage.json"
                    if session:
                        result.export_pending = session.export_pending
                        result.retry_path = session.path if result.pending_review or result.export_pending else None
                progress(100)
        finally:
            cleanup_result(result,
                ("звільнити блокування сесії", (lambda: lock.__exit__(None, None, None)) if lock else None),
                ("закрити кеш", cache.close),
                ("завершити фоновий браузер", capture.close),
                ("завершити мережевий процес", network.close))
        return result
