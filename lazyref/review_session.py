"""Фото для повторного аналізу: локальний контрольний запис без ключів і команд."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .library import relative_image
from .models import Asset, Assessment, Candidate, SearchRequest
from .priorities import PROFILES
from .validation import validated_candidate, validated_assessment, validated_criteria
from .urls import remote_url
from .review_identity import context_key, row_key
from . import review_recovery


REQUEST_FIELDS = ("description", "count", "provider", "candidate_limit", "screenshots", "model",
                  "artistic_nudity", "search_rounds", "search_profile")
OPTIONAL_REQUEST_FIELDS = ("search_strategy", "discovery_limit", "detail_limit", "reuse_cache", "verify_sources", "reasoning_effort", "reference_mode", "custom_criteria", "enhanced_dedup", "high_quality_only")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, data):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class ReviewSession:
    def __init__(self, path, data):
        self.path, self.data = Path(path), data
        self.persistence_error = None
        self.needs_save = False
        self._uncommitted = set()

    @classmethod
    def create(cls, request, plan, related_generation=None):
        session_id = uuid4().hex
        root = request.output_dir / "_sessions" / session_id
        (root / "images").mkdir(parents=True)
        guide = None
        if request.reference_image:
            source = request.reference_image
            target = root / ("guide" + source.suffix.lower())
            shutil.copyfile(source, target)
            guide = {"file": target.name, "sha256": digest(target)}
        data = {"version": 1, "kind": "lazyref_review_session", "id": session_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "request": {k: getattr(request, k) for k in REQUEST_FIELDS + OPTIONAL_REQUEST_FIELDS},
                "plan": deepcopy(plan), "guide": guide, "assets": [], "statistics": {},
                "usage_history": [], "related_generation": related_generation, "last_result": None,
                "export_pending": False}
        session = cls(root / "review.json", data)
        session.save()
        return session

    @classmethod
    def load(cls, path, *, recover=True):
        path = Path(path).resolve()
        if path.stat().st_size > 20_000_000:
            raise ValueError("Завеликий файл збереженого аналізу.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("kind") != "lazyref_review_session" or not isinstance(data.get("request"), dict)
                or not isinstance(data.get("plan"), dict) or not isinstance(data.get("assets"), list)
                or len(data["assets"]) > 300 or not isinstance(data.get("usage_history"), list)
                or any(k not in data["request"] for k in REQUEST_FIELDS)
                or data["request"].get("search_profile") not in PROFILES
                or not isinstance(data["request"].get("description"), str)):
            raise ValueError("Це не підтримуваний файл збереженого аналізу LazyRef.")
        if type(data.get("export_pending", False)) is not bool:
            raise ValueError("Некоректний стан збереження добірки.")
        if "analysis_settings" in data:
            settings = data["analysis_settings"]
            if (not isinstance(settings, dict) or set(settings) != {"model", "reasoning_effort"}
                    or any(not isinstance(value, str) for value in settings.values())):
                raise ValueError("Некоректні збережені параметри оцінювання.")
            from .reasoning import validate_reasoning
            validate_reasoning(settings["reasoning_effort"])
        criteria = data["plan"].get("criteria")
        if not validated_criteria(criteria):
            raise ValueError("У збереженому плані відсутні критерії пошуку.")
        if data["request"]["search_profile"] == "custom":
            from .custom_priorities import approved_plan
            if criteria != approved_plan(data["request"].get("custom_criteria"))["criteria"]:
                raise ValueError("Збережений план не відповідає підтвердженим умовам.")
        ids = []
        for row in data["assets"]:
            if not isinstance(row, dict) or "assessment" not in row:
                raise ValueError("Некоректний запис збереженого фото.")
            candidate = validated_candidate(row.get("candidate"))
            row["candidate"] = asdict(candidate)
            ids.append(candidate.id)
            if "assessment_key" in row and (not isinstance(row["assessment_key"], str)
                    or len(row["assessment_key"]) != 64
                    or any(c not in "0123456789abcdef" for c in row["assessment_key"])):
                raise ValueError("Некоректна прив'язка збереженої оцінки до умов.")
            if row["assessment"] is not None and "assessment_key" not in row:
                validated_assessment(row["assessment"], criteria, data["request"]["search_profile"],
                                     data["request"].get("verify_sources", True))
            elif row["assessment"] is not None and not isinstance(row["assessment"], dict):
                raise ValueError("Некоректна збережена оцінка.")
        stats = data.get("statistics", {})
        if not isinstance(stats, dict) or not isinstance(stats.get("access_links", []), list):
            raise ValueError("Некоректна статистика збереженої сесії.")
        for link in stats.get("access_links", []):
            if not isinstance(link, dict) or not isinstance(link.get("url"), str):
                raise ValueError("Некоректне посилання збереженої сесії.")
            link["url"] = remote_url(link["url"])
        datetime.fromisoformat(data.get("created_at", ""))
        if len(set(ids)) != len(ids):
            raise ValueError("У збереженому аналізі повторюються ідентифікатори фото.")
        session = cls(path, data)
        if recover:
            session.recover()
        return session

    @classmethod
    def load_metadata(cls, path):
        """Read session metadata without receipts, image checks or hashing."""
        return cls.load(path, recover=False)

    def save(self):
        atomic_json(self.path, self.data)
        if self._uncommitted:
            review_recovery.clear(self.path, self.data["id"], self._uncommitted)
            self._uncommitted.clear()
        self.needs_save = False
        self.persistence_error = None

    def context(self, request):
        """Snapshot the actual analysis settings before the model call."""
        guide = digest(request.reference_image) if request.reference_image else None
        return context_key(request, self.data["plan"], guide)

    def recover(self, request=None):
        """Recover only matching receipts; files are checked before applying them."""
        if not any(row["assessment"] is None for row in self.data["assets"]):
            return  # A stale journal must never replace a committed assessment.
        entries = review_recovery.pending(self.path, self.data["id"])
        if not entries:
            return
        request = request or self.saved_analysis_request()
        context = self.context(request)
        for row in self.data["assets"]:
            entry = entries.get(row["candidate"]["id"])
            if row["assessment"] is not None or not entry or entry["key"] != row_key(context, row):
                continue
            self.owned_file(row["file"], row["sha256"])
            self.owned_file(row["preview"], row["preview_sha256"])
            rating = validated_assessment(entry["assessment"], self.data["plan"].get("criteria"),
                                          request.search_profile, request.verify_sources)
            row["assessment"] = asdict(rating)
            row["assessment_key"] = entry["key"]
            self._uncommitted.add(row["candidate"]["id"])
            self.data["export_pending"] = True
            self.needs_save = True

    @property
    def pending_count(self):
        return sum(row["assessment"] is None for row in self.data["assets"])

    def pending_for(self, request):
        """Compare conditions in the worker; assets() still verifies all files."""
        context, legacy = self.context(request), self.context(self.request())
        return sum(row["assessment"] is None or (
            row["assessment_key"] != row_key(context, row) if "assessment_key" in row
            else context != legacy) for row in self.data["assets"])

    @property
    def export_pending(self):
        return self.data.get("export_pending", False)

    def owned_file(self, filename, checksum):
        """Артефакт не може підкласти інший локальний файл у вкладення моделі."""
        try:
            path = relative_image(self.path.parent, filename)
            if path.stat().st_size <= 20_000_000 and digest(path) == checksum:
                return path
        except (FileNotFoundError, NotADirectoryError):
            pass
        raise ValueError("Збережене фото відсутнє або змінене. Повторний аналіз не запущено.")

    def request(self, *, codex_path="", model=None, reasoning_effort=None):
        values = {k: self.data["request"][k] for k in REQUEST_FIELDS}
        values.update({k: self.data["request"][k] for k in OPTIONAL_REQUEST_FIELDS if k in self.data["request"]})
        # Вихідна папка визначається місцем сесії, не шляхом із JSON.
        root = self.path.parent
        output = root.parent.parent if root.parent.name == "_sessions" else root / "results"
        guide = self.data.get("guide")
        values.update(output_dir=output, codex_path=codex_path,
                      reference_image=self.owned_file(guide["file"], guide["sha256"]) if guide else None)
        # Збережені фото з колишніх бібліотек досі можна оцінити через Codex.
        if values["provider"] == "figure":
            values["artistic_nudity"] = True
        values["provider"] = "agent"
        if model is not None:
            values["model"] = model
        if reasoning_effort is not None:
            values["reasoning_effort"] = reasoning_effort
        request = SearchRequest(**values)
        # Повторна оцінка не потребує API-ключа бібліотеки, бо файли вже локальні.
        request.validate()
        return request

    def saved_analysis_request(self, *, codex_path=""):
        """Use the recorded assessment settings, preserving the original query."""
        return self.request(codex_path=codex_path, **self.data.get("analysis_settings", {}))

    def prepare_analysis(self, request):
        """Commit effective settings and pending rows before buying new ratings."""
        self.data["analysis_settings"] = {
            "model": request.model, "reasoning_effort": request.reasoning_effort,
        }
        self.save()

    def add(self, asset, round_number):
        name = uuid4().hex
        root = self.path.parent
        path, preview = root / "images" / f"{name}.jpg", root / "images" / f"{name}-preview.jpg"
        shutil.copyfile(asset.path, path)
        shutil.copyfile(asset.preview, preview)
        self.data["assets"].append({"candidate": asdict(asset.candidate), "file": path.relative_to(root).as_posix(),
            "preview": preview.relative_to(root).as_posix(), "sha256": digest(path), "preview_sha256": digest(preview),
            "width": asset.width, "height": asset.height, "method": asset.method, "round": round_number,
            "assessment": None})
        self.save()  # Обидва файли та запис існують до відправлення в Codex.
        return replace(asset, path=path, preview=preview, review_owned=True)

    def assets(self, request=None):
        request = request or self.request()
        context = self.context(request)
        legacy_context = self.context(self.request())
        # Validate every file before changing the pending/completed state.
        paths = [(self.owned_file(row["file"], row["sha256"]),
                  self.owned_file(row["preview"], row["preview_sha256"])) for row in self.data["assets"]]
        for row in self.data["assets"]:
            expected = row_key(context, row)
            recorded = row.get("assessment_key")
            if row["assessment"] is not None and (
                    recorded != expected if recorded is not None else context != legacy_context):
                row["assessment"] = None
                row.pop("assessment_key", None)
                self._uncommitted.discard(row["candidate"]["id"])
                self.data["export_pending"] = True
                self.needs_save = True
            elif row["assessment"] is not None and recorded is None:
                # Older sessions have no provenance key. Preserve their
                # validated original-context ratings and pin them on next save.
                row["assessment_key"] = expected
        self.recover(request)
        result = []
        for row, (path, preview) in zip(self.data["assets"], paths):
            rating = (validated_assessment(row["assessment"], self.data["plan"].get("criteria"),
                      request.search_profile, request.verify_sources)
                      if row["assessment"] is not None else None)
            result.append(Asset(validated_candidate(row["candidate"]), path, preview, row["width"], row["height"],
                                row["method"], rating, review_owned=True))
        return result

    def complete(self, batch, ratings, profile, *, context=None):
        ids = {a.candidate.id for a in batch}
        if set(ratings) != ids or any(not isinstance(ratings[i], Assessment) for i in ids):
            raise ValueError("Codex не повернув повну оцінку пакета. Фото залишені для повторної спроби.")
        if any(a.verify_sources != self.data["request"].get("verify_sources", True) for a in ratings.values()):
            raise ValueError("Режим перевірки оцінки не відповідає збереженому запиту.")
        if profile != self.data["request"]["search_profile"]:
            raise ValueError("Профіль оцінки не відповідає збереженому запиту.")
        context = context or self.context(self.request())
        rows = {row["candidate"]["id"]: row for row in self.data["assets"]}
        if not ids.issubset(rows) or len(ids) != len(batch):
            raise ValueError("Оцінка стосується невідомого або повторного фото.")
        entries = {}
        for asset in batch:
            ident = asset.candidate.id
            row = rows[ident]
            self.owned_file(row["file"], row["sha256"])
            self.owned_file(row["preview"], row["preview_sha256"])
            if (asdict(asset.candidate) != row["candidate"]
                    or digest(asset.path) != row["sha256"] or digest(asset.preview) != row["preview_sha256"]):
                raise ValueError("Фото або дані джерела змінилися під час аналізу.")
            rating = validated_assessment(asdict(replace(ratings[ident], search_profile=profile)),
                self.data["plan"].get("criteria"), profile, self.data["request"].get("verify_sources", True))
            entries[ident] = {"key": row_key(context, row), "assessment": asdict(rating)}
        warning = review_recovery.remember(self.path, self.data["id"], entries)
        for row in self.data["assets"]:
            ident = row["candidate"]["id"]
            if ident in ids:
                row["assessment"] = entries[ident]["assessment"]
                row["assessment_key"] = entries[ident]["key"]
        for asset in batch:
            asset.assessment = replace(ratings[asset.candidate.id], search_profile=profile)
        self._uncommitted.update(ids)
        self.needs_save = True
        self.data["export_pending"] = True
        try:
            self.save()
        except OSError as exc:
            location = ("Оцінки залишаються в пам'яті до закриття програми; не закривайте її до повторного збереження."
                        if warning else "Оцінки збережені в журналі відновлення.")
            self.persistence_error = f"Не вдалося зберегти сесію: {exc}. {location} Повторіть локальне збереження."

    def record_usage(self, usage):
        # Історія не вкладається сама в себе й не дублюється після повторного запису.
        entry = {k: deepcopy(usage[k]) for k in ("run_id", "kind", "created_at", "summary", "calls")}
        history = self.data["usage_history"]
        history[:] = [u for u in history if u["run_id"] != entry["run_id"]] + [entry]
        self.save()

    def available_usage(self):
        history = {u["run_id"]: u for u in self.data["usage_history"]}
        # Журнал повтору пишеться під час виклику: зберігає облік навіть при закритті процесу.
        for path in sorted((self.path.parent / "attempts").glob("*.json")):
            if path.stat().st_size > 10_000_000:
                continue
            try:
                u = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(u, dict) and u.get("source") == "codex_exec_jsonl" and isinstance(u.get("calls"), list):
                    history[u["run_id"]] = {k: u[k] for k in ("run_id", "kind", "created_at", "summary", "calls")}
            except (ValueError, KeyError, TypeError, OSError):
                continue
        return list(history.values())

    def finish(self, stats, usage, result_folder=None):
        previous = deepcopy(self.data)
        self.data["statistics"] = {k: deepcopy(v) for k, v in stats.items() if k not in {"usage", "review_session"}}
        self.data["export_pending"] = result_folder is None
        if result_folder is not None:
            self.data["last_result"] = str(result_folder)
        try:
            self.record_usage(usage)
        except OSError:
            self.data = previous
            raise

    def descriptor(self):
        return {"id": self.data["id"], "path": str(self.path.resolve()), "pending": self.pending_count,
                "export_pending": self.export_pending}


def list_pending(output_dir):
    sessions = []
    for path in (Path(output_dir) / "_sessions").glob("*/review.json"):
        try:
            session = ReviewSession.load_metadata(path)
            if session.pending_count or session.export_pending:
                sessions.append(session)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(sessions, key=lambda s: s.data["created_at"], reverse=True)
