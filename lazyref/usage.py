"""Локальний облік статистики Codex JSONL. Жодних мережевих викликів.

Зберігаємо лише лічильники й ідентифікатори запусків, без промптів або ключів.
Невідоме значення — None, не нуль. Кеш і міркування вже входять у вхід/вихід.
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
import json
from pathlib import Path
from uuid import uuid4

from .i18n import DEFAULT_LANGUAGE, translate

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens")
STAGES = {"generation": "Генерація ШІ-зразків", "planning": "Планування",
          "search": "Пошук зображень", "screening": "Попередній відбір мініатюр",
          "source_check": "Перевірка джерел", "assessment": "Аналіз фото", "other": "Інші виклики"}
NOTE = ("Це токени, повідомлені Codex CLI. Витрати інструмента генерації зображень та інших "
        "сервісів можуть не мати окремого лічильника в цьому звіті. Це не відсоток ліміту й не сума грошей.")


def now():
    return datetime.now(timezone.utc).isoformat()


def token_values(raw):
    raw = raw if isinstance(raw, dict) else {}
    values = {k: raw[k] if type(raw.get(k)) is int and raw[k] >= 0 else None for k in FIELDS}
    values["total_tokens"] = (values["input_tokens"] + values["output_tokens"]
        if values["input_tokens"] is not None and values["output_tokens"] is not None else None)
    return values


def summarize(calls):
    turns = [turn for call in calls for turn in call["turns"]]
    totals = {}
    for key in FIELDS:
        known = [t["usage"][key] for t in turns if t["usage"][key] is not None]
        totals[key] = sum(known) if known else None
    totals["total_tokens"] = (totals["input_tokens"] + totals["output_tokens"]
        if totals["input_tokens"] is not None and totals["output_tokens"] is not None else None)
    complete = sum(call["status"] != "running" and bool(call["turns"]) and
        all(t["status"] == "completed" and t["usage"]["total_tokens"] is not None for t in call["turns"])
        for call in calls)
    return {"calls": len(calls), "turns": len(turns), "complete_calls": complete, "incomplete_calls": len(calls) - complete,
        "status": "no_calls" if not calls else "complete" if complete == len(calls) else "incomplete",
        "reported_tokens": totals,
        "reported_turns_by_field": {key: sum(t["usage"][key] is not None for t in turns) for key in FIELDS}}


class UsageLedger:
    def __init__(self, path: Path, kind: str, related_generation=None, previous_attempts=None):
        self.path = path
        self.mirror_path = None
        self.calls = []
        self.run_id = str(uuid4())
        self.kind, self.created_at = kind, now()
        self.related_generation = related_generation
        self.previous_attempts = list({u["run_id"]: u for u in (previous_attempts or [])}.values())
        self.write_error = ""
        self.save(strict=True)  # Перевіряємо збереження до першого хмарного виклику.

    def snapshot(self):
        return deepcopy({"version": 1, "run_id": self.run_id, "kind": self.kind,
            "created_at": self.created_at, "source": "codex_exec_jsonl",
            "summary": summarize(self.calls),
            "by_stage": {stage: summarize([c for c in self.calls if c["stage"] == stage]) for stage in STAGES},
            "calls": self.calls, "related_generation": self.related_generation,
            "previous_attempts": [{"run_id": u["run_id"], "summary": u["summary"]} for u in self.previous_attempts],
            "all_attempts_summary": summarize([c for u in self.previous_attempts for c in u["calls"]] + self.calls),
            "write_error": self.write_error, "note": NOTE})

    def save(self, strict=False):
        try:
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
            for path in [self.path] + ([self.mirror_path] if self.mirror_path else []):
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(path)
        except OSError:
            self.write_error = "Не вдалося записати облік витрат на диск. Показано дані з пам’яті."
            if strict:
                raise OSError(self.write_error)

    def begin(self, stage, model, image_count):
        record = {"id": str(uuid4()), "stage": stage, "requested_model": model or None,
            "image_count": image_count, "started_at": now(), "finished_at": None,
            "status": "running", "thread_id": None, "turns": []}
        self.calls.append(record)
        self.save()
        return UsageCall(self, record)


class UsageCall:
    def __init__(self, ledger, record):
        self.ledger, self.record = ledger, record

    def observe(self, event):
        event_type = event.get("type")
        if event_type == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str):
                self.record["thread_id"] = value[:100]
        elif event_type == "turn.started":
            self.record["turns"].append({"status": "running", "usage": token_values(None)})
        elif event_type in {"turn.completed", "turn.failed"}:
            turns = self.record["turns"]
            if not turns:
                turns.append({"status": "running", "usage": token_values(None)})
            if turns[-1]["status"] != "running":
                return  # Повторна кінцева подія не подвоює витрати.
            turns[-1].update(status="completed" if event_type == "turn.completed" else "failed",
                             usage=token_values(event.get("usage")))
        else:
            return
        self.ledger.save()

    def finish(self, status):
        self.record.update(status=status, finished_at=now())
        self.ledger.save()


def related_generation_usage(reference: Path | None):
    if reference is None:
        return None
    path = reference.parent / "usage.json"
    missing = {"status": "unavailable", "path": str(path.resolve())}
    try:
        if path.stat().st_size > 2_000_000:
            return missing
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("kind") != "generation" or not isinstance(data.get("calls"), list):
            return missing
        return {"status": "available", "path": str(path.resolve()), "run_id": data["run_id"],
                "summary": summarize(data["calls"])}
    except (OSError, ValueError, KeyError, TypeError):
        return missing


def format_usage_report(data, *, language=DEFAULT_LANGUAGE):
    tr = partial(translate, language=language)
    if not data:
        return tr("Для цього запуску статистику токенів не збережено.")

    def number(value):
        return tr("немає даних") if value is None else f"{value:,}".replace(",", " ")

    def describe(title, summary):
        if summary["status"] == "no_calls":
            return tr("{title}: викликів не було.", title=title)
        total = summary["reported_tokens"]
        def detail(key):
            value = number(total[key])
            if total[key] is not None and summary["reported_turns_by_field"][key] < summary["turns"]:
                value += tr(" (неповні дані)")
            return value
        lines = [
            tr("{title}: {total} токенів за отриманими лічильниками", title=title, total=number(total["total_tokens"])),
            tr("  Вхід: {input} · вихід: {output}", input=number(total["input_tokens"]), output=number(total["output_tokens"])),
            tr("  Із входу — кеш: {cached}; із виходу — міркування: {reasoning}",
               cached=detail("cached_input_tokens"), reasoning=detail("reasoning_output_tokens")),
            tr("  Запусків Codex: {count} · з повними лічильниками: {complete}",
               count=summary["calls"], complete=summary["complete_calls"]),
        ]
        if summary["incomplete_calls"]:
            lines.append(tr("  Облік неповний: невідома витрата незавершених/неповністю звітованих запусків не дорівнює нулю."))
        return "\n".join(lines)

    lines = [describe(tr("Поточний запуск"), data["summary"])]
    for stage, summary in data["by_stage"].items():
        if summary["calls"]:
            lines += ["", describe(tr(STAGES[stage]), summary)]
    if data.get("previous_attempts"):
        lines += ["", tr("Попередні спроби цієї добірки (не входять у поточний запуск):")]
        for i, previous in enumerate(data["previous_attempts"], 1):
            lines.append(describe(tr("Спроба {index}", index=i), previous["summary"]))
        lines += ["", describe(tr("Разом усі спроби цієї добірки"), data["all_attempts_summary"])]
    related = data.get("related_generation")
    if related:
        lines += ["", tr("Генерація обраного пакета зразків — окремо від поточного пошуку:")]
        if related["status"] == "available":
            lines.append(describe(tr("Раніше створені ШІ-варіанти"), related["summary"]))
            lines.append(tr("Це весь пакет варіантів. При повторному пошуку за ним генерація не нараховується заново."))
        else:
            lines.append(tr("Даних немає: для цього пакета облік не збережено."))
    if data.get("write_error"):
        lines += ["", tr(data["write_error"])]
    lines += ["", tr("Разом = вхід + вихід. Кеш і міркування повторно не додаються."), tr(NOTE)]
    return "\n".join(lines)
