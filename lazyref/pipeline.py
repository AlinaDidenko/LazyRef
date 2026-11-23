"""Послідовність пошуку. Не залежить від графічного інтерфейсу."""

from pathlib import Path
import tempfile
from threading import Event
from uuid import uuid4

from .capture import BrowserCapture
from .access import SourceBlocked
from .codex import CodexClient
from .exporter import IncompleteExport, export_results
from .i18n import translate
from .images import DuplicateIndex, download, prepare
from .models import Cancelled, SearchRequest, SearchResult, check_cancel
from .network import Network
from .search_agent import discover
from .authenticity import GeneratedImage
from .usage import UsageLedger, related_generation_usage
from .priorities import PROFILES
from .review_session import ReviewSession
from .retry_analysis import ReviewLock
from .custom_priorities import make_plan
from .photo_filters import LowResolution, check_resolution, ResultSelection
from .download_budget import request_budget
from .result_cleanup import cleanup_result, temporary_directory


class SearchPipeline:
    def __init__(self, client=None, finder=None, loader=download, *, wait_changed=None, plan_editor=None):
        # Підміна залежностей дає змогу тестувати збої без платних/хмарних викликів.
        self.client = client
        self.finder = finder
        self.loader = loader
        self.wait_changed = wait_changed
        self.plan_editor = plan_editor

    def run(self, request: SearchRequest, cancel: Event, status=lambda text: None, progress=lambda value: None) -> SearchResult:
        if request.search_strategy == "wide" and request.provider == "agent" and self.finder is None:
            from .wide_search import WideSearchPipeline
            return WideSearchPipeline(self.client, self.loader, wait_changed=self.wait_changed, plan_editor=self.plan_editor).run(
                request, cancel, status, progress)
        request.validate()
        request.output_dir.mkdir(parents=True, exist_ok=True)
        # Перевіряємо право запису перед витрачанням хмарного ліміту.
        with tempfile.TemporaryFile(dir=request.output_dir):
            pass
        result = SearchResult(None, [], request.count, search_profile=request.search_profile, verify_sources=request.verify_sources,
                              enhanced_dedup=request.enhanced_dedup, high_quality_only=request.high_quality_only)
        ledger = UsageLedger(request.output_dir / "_usage" / f"search_{uuid4().hex}.json", "search",
            related_generation_usage(request.reference_image))
        result.usage_path = ledger.path
        accepted, plan, evaluations, rounds = [], {}, [], []
        precheck_rejections = []
        pool, attempted, seen_urls = {}, set(), set()
        stop_reason = ""
        network = Network(cancel)
        network.verify_sources = request.verify_sources
        network.high_quality_only = request.high_quality_only
        selection = ResultSelection(request.enhanced_dedup)
        low_resolution_ids = set()
        capture = BrowserCapture(cancel, network.access)
        attempts, screenshot_attempts = 0, 0
        agent_mode = request.provider == "agent" and self.finder is None
        session = None
        session_lock = None
        try:
            with temporary_directory(result, prefix="lazyref-images-") as temporary:
                directory = Path(temporary)
                try:
                    status(translate("Codex уточнює опис і готує пошукові запити…"))
                    client = self.client or CodexClient(request.codex_path, request.model,
                        reasoning_effort=request.reasoning_effort)
                    client.usage_ledger = ledger
                    client.wait_changed = self.wait_changed
                    reference_args = request.reference_args
                    profile_args = {"search_profile": request.search_profile} if request.search_profile != "strict" else {}
                    plan = make_plan(client, request, cancel, self.plan_editor)
                    if not request.verify_sources:
                        profile_args["verify_sources"] = False
                    plan = {**plan, "search_profile": dict(PROFILES[request.search_profile])}
                    session = ReviewSession.create(request, plan, ledger.related_generation)
                    session_lock = ReviewLock(session.path.parent / "review.lock")
                    session_lock.__enter__()
                    ledger.mirror_path = session.path.parent / "attempts" / f"search_{ledger.run_id}.json"
                    ledger.save(strict=True)
                    result.summary = plan["summary"]
                    status(translate("Шукаю: {summary}", summary=result.summary))
                    progress(10)
                    progress(20)
                    unique = DuplicateIndex(allow_larger=request.enhanced_dedup)
                    batch = []
                    criteria = plan.get("criteria", [])
                    def reject_generation(candidate, reason):
                        result.rejected += 1
                        precheck_rejections.append({"id": candidate.id, "title": candidate.title,
                            "page_url": candidate.page_url, "reason": reason, "round": len(rounds)})
                        status(translate("Відхилено позначену генерацію: {title}", title=candidate.title[:90]))
                    def reject_resolution(candidate, reason):
                        low_resolution_ids.add(candidate.id)
                        result.rejected += 1
                        result.low_resolution_rejected = len(low_resolution_ids)
                        precheck_rejections.append({"id": candidate.id, "title": candidate.title,
                            "page_url": candidate.page_url, "reason": str(reason), "round": len(rounds), "filter": "resolution"})
                        status(translate(reason.message, width=reason.width, height=reason.height))
                    def assess_batch():
                        if not batch:
                            return
                        status(translate("Codex переглядає {count} фото · уже оцінено {examined}",
                                         count=len(batch), examined=result.examined))
                        context = session.context(request)
                        ratings = client.assess(request.search_description, batch, cancel,
                            **({"criteria": criteria} if criteria else {}), **reference_args, **profile_args)
                        session.complete(batch, ratings, request.search_profile, context=context)
                        for asset in batch:
                            asset.assessment = ratings[asset.candidate.id]
                            asset.assessment.search_profile = request.search_profile
                            evaluations.append({"id": asset.candidate.id, "title": asset.candidate.title,
                                "source_url": asset.candidate.page_url, "media": asset.assessment.media,
                                "confidence": asset.assessment.confidence, "score": asset.assessment.score,
                                "reference_type": asset.assessment.reference_type,
                                "accepted": asset.assessment.eligible_for(request), "reason": asset.assessment.reason,
                                "close": asset.assessment.is_close, "matches": asset.assessment.matches,
                                "misses": asset.assessment.misses, "criterion_checks": asset.assessment.criterion_checks,
                                "round": len(rounds)})
                            result.examined += 1
                            if asset.assessment.eligible_for(request):
                                previous = len(selection.removed_ids)
                                selection.add(asset, accepted)
                                result.duplicates += len(selection.removed_ids) - previous
                            else:
                                result.rejected += 1
                        close = sum(a.assessment.is_close for a in accepted)
                        status(translate("Оцінено {examined} · близьких {close} · часткових {partial} · повторів {duplicates}",
                                         examined=result.examined, close=close, partial=len(accepted) - close,
                                         duplicates=result.duplicates))
                        progress(20 + int(70 * min(result.examined, request.candidate_limit) / request.candidate_limit))
                        batch.clear()
                        if session.persistence_error:
                            raise OSError(session.persistence_error)
                    def load_candidate(candidate):
                        nonlocal attempts, screenshot_attempts
                        check_cancel(cancel)
                        attempted.add(candidate.id)
                        attempts += 1
                        status(translate("Завантажую кандидат {attempt} · у вибірці {count}…",
                                         attempt=attempts, count=len(pool)))
                        # One acquisition budget covers alternate URLs and the
                        # optional browser fallback, but never model analysis.
                        with request_budget(network) as budget:
                            try:
                                if request.verify_sources and candidate.source_kind == "generated_label":
                                    raise GeneratedImage(candidate.source_notes or "Джерело позначає зображення як згенероване.")
                                if candidate.page_url:
                                    network.access.check(candidate.page_url)
                                asset = self.loader(candidate, directory, network)
                                check_cancel(cancel)
                            except Cancelled:
                                raise
                            except GeneratedImage as exc:
                                reject_generation(candidate, str(exc))
                                return
                            except LowResolution as exc:
                                reject_resolution(candidate, exc)
                                return
                            except SourceBlocked as exc:
                                message = str(exc)
                                if message not in result.warnings:
                                    result.warnings.append(message)
                                    status(message)
                                return
                            except Exception:
                                asset = None
                                if request.verify_sources and request.screenshots and candidate.page_url and screenshot_attempts < 3:
                                    check_cancel(cancel)
                                    try:
                                        budget.check_total()
                                        screenshot_attempts += 1
                                        status(translate("Пряме завантаження не вдалося. Пробую скріншот фото…"))
                                        asset = prepare(capture.capture(candidate, deadline=budget.total_deadline),
                                                        candidate, directory, "screenshot")
                                        check_cancel(cancel)
                                    except Cancelled:
                                        raise
                                    except GeneratedImage as exc:
                                        reject_generation(candidate, str(exc))
                                        return
                                    except SourceBlocked as exc:
                                        message = str(exc)
                                        if message not in result.warnings:
                                            result.warnings.append(message)
                                            status(message)
                                    except Exception:
                                        pass
                                if asset is None:
                                    result.warnings.append(f"Недоступне або непридатне фото: {candidate.title[:100]}")
                                    return
                        try:
                            check_resolution(asset, request.high_quality_only)
                        except LowResolution as exc:
                            reject_resolution(candidate, exc)
                            return
                        if not unique.add(asset.path):
                            result.duplicates += 1
                            return
                        asset = session.add(asset, len(rounds))
                        batch.append(asset)
                        if len(batch) >= 6:
                            assess_batch()
                    stages = [([], 1) for _ in range(request.search_rounds)] if agent_mode else [(plan["queries"], 1)]
                    tranche = max(6, request.candidate_limit // 18 * 6)
                    idle_rounds = 0
                    for stage, (queries, page) in enumerate(stages):
                        check_cancel(cancel)
                        if result.examined >= request.candidate_limit:
                            stop_reason = "assessment_limit"
                            break
                        if sum(a.assessment.is_close for a in accepted) >= request.count:
                            stop_reason = "enough_close_matches"
                            break
                        if agent_mode:
                            status(translate("Цикл агента {stage}/{total} · шукаю нові фото з урахуванням попередніх оцінок…",
                                             stage=stage + 1, total=len(stages)))
                        elif stage:
                            status(translate("Близьких збігів поки недостатньо.") + " " + translate(
                                "Переглядаю наступні сторінки…" if page > 1 else "Шукаю інші формулювання тієї ж сцени…"))
                        before = result.examined
                        agent_report = None
                        if agent_mode:
                            candidates, agent_report = discover(client, request, plan, evaluations, rounds,
                                pool, network, cancel, status)
                            queries = agent_report["queries"]
                            warnings = [agent_report["limitation"]] if agent_report["limitation"] else []
                            status(agent_report["strategy"] or translate("Оцінюю знайдених кандидатів…"))
                        elif self.finder is not None:
                            candidates, warnings = self.finder(request, queries, network, status)
                        else:
                            candidates, warnings = [], []
                        result.warnings.extend(w for w in warnings if w not in result.warnings)
                        added = 0
                        for candidate in candidates:
                            if candidate.id in pool or candidate.image_url and candidate.image_url in seen_urls:
                                continue
                            pool[candidate.id] = candidate
                            if candidate.image_url:
                                seen_urls.add(candidate.image_url)
                            added += 1
                        record = {"round": stage + 1, "queries": queries, "page": page, "new_candidates": added,
                                  "pool_total": len(pool), "examined": 0}
                        if agent_report:
                            record["strategy"] = agent_report["strategy"]
                        rounds.append(record)
                        status(translate("Етап {stage}/{total} · нових кандидатів {added}, загалом {count}. Перевіряю фото…",
                                         stage=stage + 1, total=len(stages), added=added, count=len(pool)))
                        quota = (min(request.candidate_limit, before + 18) if agent_mode else
                            request.candidate_limit if stage == len(stages)-1 else min(request.candidate_limit, before + tranche))
                        pending = [c for c in pool.values() if c.id not in attempted]
                        for candidate in pending:
                            if (result.examined + len(batch) >= quota or attempts >= request.candidate_limit * 4
                                    or sum(a.assessment.is_close for a in accepted) >= request.count):
                                break
                            load_candidate(candidate)
                        assess_batch()
                        record["examined"] = result.examined - before
                        record["close_total"] = sum(a.assessment.is_close for a in accepted)
                        idle_rounds = idle_rounds + 1 if result.examined == before else 0
                        if agent_mode and idle_rounds >= 2:
                            stop_reason = "no_new_usable_candidates"
                            break
                    stop_reason = stop_reason or ("enough_close_matches" if sum(a.assessment.is_close for a in accepted) >= request.count
                        else "assessment_limit" if result.examined >= request.candidate_limit
                        else "download_attempt_limit" if attempts >= request.candidate_limit * 4
                        else "agent_round_limit" if agent_mode else "search_stages_exhausted")
                    if not accepted:
                        result.warnings.append("У перевіреній вибірці немає достатньо відповідних фото. Слабкі збіги не зберігались.")
                    elif not any(a.assessment.is_close for a in accepted):
                        result.warnings.append("Близьких збігів не знайдено. Збережено лише часткові альтернативи з указаними відсутніми ознаками.")
                except Cancelled:
                    result.cancelled = True
                    stop_reason = "cancelled"
                    status(translate("Зупиняю пошук і зберігаю вже оцінені фото…"))
                except Exception as exc:
                    result.error = str(exc)
                    stop_reason = "error"
                # Навіть після зупинки зберігаємо лише завершені оцінки.
                result.access_links = list(network.access_links)
                result.usage = ledger.snapshot()
                if ledger.write_error:
                    result.warnings.append(ledger.write_error)
                if accepted or plan:
                    status(translate("Зберігаю добірку та посилання на джерела…"))
                    for record in rounds:
                        record["examined"] = sum(e["round"] == record["round"] for e in evaluations)
                    stats = {"examined": result.examined, "duplicates": result.duplicates,
                        "enhanced_duplicate_ids": sorted(selection.removed_ids), "low_resolution_ids": sorted(low_resolution_ids),
                        "rejected": result.rejected, "cancelled": result.cancelled,
                        "error": result.error, "warnings": result.warnings, "evaluations": evaluations,
                        "access_links": result.access_links, "rounds": rounds, "stop_reason": stop_reason,
                        "search_mode": "codex_image_agent", "verify_sources": request.verify_sources,
                        "precheck_rejections": precheck_rejections,
                        "retrieval": getattr(network, "search_trace", []), "retrieved_unique": len(pool),
                        "download_attempts": attempts,
                        "usage": result.usage,
                        "candidates": [{"id": c.id, "title": c.title, "source": c.source,
                            "page_url": c.page_url, "query": c.search_query, "page": c.search_page,
                            "source_kind": c.source_kind, "source_notes": c.source_notes,
                            "source_checked": c.source_checked,
                            "attempted": c.id in attempted} for c in pool.values()]}
                    if session:
                        stats["review_session"] = session.descriptor()
                        result.pending_review = session.pending_count
                    # Session images survive the temporary download directory.
                    # Keep completed results visible even if saving fails again.
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
                ("звільнити блокування сесії", (lambda: session_lock.__exit__(None, None, None)) if session_lock else None),
                ("завершити фоновий браузер", capture.close),
                ("завершити мережевий процес", network.close))
        return result
