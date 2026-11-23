"""Повторює лише аналіз локальних кандидатів. Пошук і завантаження тут не запускаються."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from .codex import CodexClient
from .exporter import IncompleteExport, export_results
from .i18n import translate
from .models import Cancelled, SearchResult, check_cancel
from .review_session import ReviewSession
from .usage import UsageLedger
from .result_cleanup import cleanup_result


class ReviewLock:
    """Блокування від другого кліку/вікна; ОС звільняє його і після аварії процесу."""
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        import os
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ValueError("Аналіз цієї добірки вже триває в іншому вікні.") from exc
        return self

    def __exit__(self, *args):
        self.handle.close()


def retry_review(path, cancel, status=lambda text: None, progress=lambda value: None,
                 *, client=None, codex_path="", model=None, reasoning_effort=None, wait_changed=None,
                 save_only=False, prepared=None):
    session = ReviewSession.load_metadata(path)
    with ReviewLock(session.path.parent / "review.lock"):
        # Перечитуємо стан після блокування — інше вікно могло щойно завершити пакет.
        session = ReviewSession.load(path)
        # Metadata cannot tell whether the receipt journal holds all remaining
        # ratings. Resolve automatic UI intent only after verified recovery.
        if save_only is None:
            save_only = session.pending_count == 0 and session.export_pending
        # Saving an existing result must not inherit new model settings. Keep
        # this explicit intent through preflight, including concurrent changes
        # to the saved session; a failed check must never become a model call.
        request = (session.saved_analysis_request(codex_path=codex_path) if save_only
                   else session.request(codex_path=codex_path, model=model, reasoning_effort=reasoning_effort))
        assets = session.assets(request=request)  # Перевірка файлів та умов перед хмарними витратами.
        pending = [a for a in assets if a.assessment is None]
        if save_only and pending:
            raise ValueError("Локальне збереження неможливе: є неоцінені фото або готові оцінки "
                             "не відповідають збереженим умовам. Новий аналіз не запускався.")
        if not pending and not session.export_pending and not session.needs_save:
            raise ValueError("Усі збережені кандидати вже оцінені. Повторний виклик Codex не потрібен.")
        if prepared:
            prepared(request, len(pending), save_only)
        journal = session.path.parent / "attempts" / f"retry_{uuid4().hex}.json"
        ledger = UsageLedger(journal, "assessment_retry", session.data.get("related_generation"), session.available_usage())
        result = SearchResult(None, [], request.count, summary=session.data["plan"].get("summary", ""),
            search_profile=request.search_profile, retry_path=session.path, pending_review=len(pending), usage_path=journal)
        stats = deepcopy(session.data.get("statistics", {}))
        result.search_strategy = request.search_strategy
        result.verify_sources = request.verify_sources
        result.enhanced_dedup = request.enhanced_dedup
        result.high_quality_only = request.high_quality_only
        result.discovered = stats.get("retrieved_unique", 0)
        result.screened = stats.get("screened", 0)
        result.reused_assessments = stats.get("metrics", {}).get("reused_assessments", 0)
        original_folder = session.data.get("last_result")
        reviewed = 0
        cache = None
        if pending and request.search_strategy == "wide" and request.reuse_cache:
            from .search_cache import SearchCache, task_key, assessment_key
            from dataclasses import asdict
            cache = SearchCache(request.output_dir / "_search_cache")
            cache_task = task_key(request)
        try:
            check_cancel(cancel)
            if pending:
                # Persist the actual retry settings together with invalidated
                # rows before any model call. Receipts and a later local export
                # can then recover these settings even after a write failure.
                session.prepare_analysis(request)
            elif session.needs_save:
                # Persist recovered assessments before buying any new ones.
                # A still-unavailable disk keeps this a local recovery attempt.
                session.save()
            if pending:
                client = client or CodexClient(request.codex_path, request.model,
                    reasoning_effort=request.reasoning_effort)
                client.usage_ledger = ledger
                client.wait_changed = wait_changed
            args = {}
            if not request.verify_sources:
                args["verify_sources"] = False
            args.update(request.reference_args)
            if request.search_profile != "strict":
                args["search_profile"] = request.search_profile
            criteria = session.data["plan"].get("criteria", [])
            if criteria:
                args["criteria"] = criteria
            for offset in range(0, len(pending), 3):
                check_cancel(cancel)
                batch = pending[offset:offset + 3]
                status(translate("Повторний аналіз: {batch} фото · завершено {reviewed} із {pending}. Новий пошук не запускається.",
                                 batch=len(batch), reviewed=reviewed, pending=len(pending)))
                context = session.context(request)
                ratings = client.assess(request.search_description, batch, cancel, **args)
                session.complete(batch, ratings, request.search_profile, context=context)
                for asset in batch:
                    asset.assessment = replace(ratings[asset.candidate.id], search_profile=request.search_profile)
                    if cache:
                        cache.put("assessment", assessment_key(cache_task, session.data["plan"], asset), asdict(asset.assessment))
                reviewed += len(batch)
                progress(int(90 * reviewed / len(pending)))
                if session.persistence_error:
                    raise OSError(session.persistence_error)
        except Cancelled:
            result.cancelled = True
        except Exception as exc:
            result.error = str(exc)
        finally:
            if cache:
                if cache.warning:
                    result.warnings.append(cache.warning)
                cleanup_result(result, ("закрити кеш", cache.close))
        result.pending_review = session.pending_count
        result.retry_path = session.path if result.pending_review else None
        result.usage = ledger.snapshot()
        if ledger.write_error:
            result.warnings.append(ledger.write_error)
        completed = [a for a in assets if a.assessment is not None]
        accepted = [a for a in completed if a.assessment.eligible_for(request)]
        result.examined = len(completed)
        result.duplicates = stats.get("duplicates", 0)
        result.rejected = len(completed) - len(accepted) + len(stats.get("precheck_rejections", []))
        result.access_links = stats.get("access_links", [])
        rounds_by_id = {row["candidate"]["id"]: row["round"] for row in session.data["assets"]}
        evaluations = []
        for asset in completed:
            a = asset.assessment
            evaluations.append({"id": asset.candidate.id, "title": asset.candidate.title,
                "source_url": asset.candidate.page_url, "media": a.media, "confidence": a.confidence,
                "score": a.score, "reference_type": a.reference_type, "accepted": a.eligible_for(request),
                "reason": a.reason, "close": a.is_close, "matches": a.matches, "misses": a.misses,
                "criterion_checks": a.criterion_checks, "round": rounds_by_id[asset.candidate.id]})
        stats.update(examined=result.examined, rejected=result.rejected, duplicates=result.duplicates,
            cancelled=result.cancelled, error=result.error, warnings=result.warnings, evaluations=evaluations,
            stop_reason="cancelled" if result.cancelled else "error" if result.error else "saved_candidates_reviewed",
            search_mode="assessment_retry", newly_examined=reviewed, resumed_from=original_folder,
            usage=result.usage, review_session=session.descriptor())
        if request.search_strategy == "wide":
            stats.setdefault("metrics", {})["newly_examined"] = stats.get("metrics", {}).get("newly_examined", 0) + reviewed
        # Початкові цикли/пошукові лічильники — контекст сесії, не нова пошукова робота.
        for round_info in stats.get("rounds", []):
            round_info["examined"] = sum(e["round"] == round_info["round"] for e in evaluations)
        result.assets = list(accepted)
        try:
            session.finish(stats, result.usage)
            status(translate("Зберігаю добірку з попередніми та новими завершеними оцінками…"))
            result.folder, result.assets = export_results(request, accepted, session.data["plan"], stats)
            result.usage_path = result.folder / "usage.json"
            session.finish(stats, result.usage, result.folder)
        except OSError as exc:
            result.error = "\n".join(filter(None, (result.error, str(exc))))
            if isinstance(exc, IncompleteExport):
                result.folder, result.assets = exc.folder, exc.assets
                result.usage_path = result.folder / "usage.json"
        result.export_pending = session.export_pending
        result.duplicates = stats.get("duplicates", result.duplicates)
        result.rejected = stats.get("rejected", result.rejected)
        result.low_resolution_rejected = len(stats.get("low_resolution_ids", []))
        result.retry_path = session.path if result.pending_review or result.export_pending else None
        progress(100)
        return result
