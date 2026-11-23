"""Відтворювана серія пошуків LazyRef; init/report не викликають моделей.

run запускає лише незавершені тести, по одному в окремому процесі.
Файл STOP у папці серії безпечно зупиняє поточний тест і чергу.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
import traceback

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from lazyref import __version__
from lazyref.codex import CodexClient, find_codex
from lazyref.models import SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.processes import ProcessTree

MODELS = ("gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-luna")
PROFILES = ("strict", "pose")
TERMINAL = {"completed", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    replace_when_available(tmp, path)


def replace_when_available(tmp, path):
    # A Windows viewer can briefly hold the destination without delete sharing.
    for attempt in range(10):
        try:
            tmp.replace(path)
            break
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.4))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_jobs(cases, models=MODELS, profiles=PROFILES):
    jobs = []
    for case_number, case in enumerate(cases, 1):
        for model in models:
            for profile in profiles:
                number = len(jobs) + 1
                jobs.append({"id": f"{number:02d}_p{case_number}_{model}_{profile}",
                    "number": number, "case_number": case_number,
                    "model": model, "profile": profile, "description": case["description"],
                    "guide": case["image"], "guide_sha256": case["sha256"]})
    return jobs


def initialize(root, workspace, selection_dir=None):
    if root.exists():
        raise ValueError("Папка серії вже існує; для продовження використай run.")
    selection_root = (Path(selection_dir).resolve() if selection_dir is not None
                      else workspace / "generated_references/Test_2026-11-06")
    selection_file = selection_root / "selection.json"
    if not selection_file.is_file():
        raise ValueError(f"Не знайдено добірку ШІ-зразків: {selection_file}. "
                         "Для архівованої добірки вкажи --selection-dir із папкою selection.json.")
    selected = read_json(selection_file)
    if [row["id"] for row in selected["cases"]] != ["1D", "2B", "3B"]:
        raise ValueError("Неочікувана добірка ШІ-зразків.")
    return initialize_series(root, workspace, [(row, selection_root / row["image"]) for row in selected["cases"]])


def initialize_series(root, workspace, inputs, *, models=MODELS, profiles=PROFILES, enhanced_dedup=False):
    """Freeze the supplied (metadata, original file) pairs before the first model call."""
    for row, original in inputs:
        if digest(original) != row["sha256"]:
            raise ValueError("ШІ-зразок змінився.")
    root.mkdir(parents=True)
    frozen = root / "_frozen"
    shutil.copytree(workspace / "lazyref", frozen / "lazyref", ignore=shutil.ignore_patterns("__pycache__"))
    (frozen / "tools").mkdir()
    shutil.copy2(Path(__file__), frozen / "tools/run_model_matrix.py")
    cases = []
    for number, (row, original) in enumerate(inputs, 1):
        guide = root / "guides" / f"{number:02d}_AI_pose.png"
        guide.parent.mkdir(exist_ok=True)
        shutil.copy2(original, guide)
        cases.append({**row, "image": guide.relative_to(root).as_posix()})
    settings = read_json(workspace / ".lazyref/settings.json")
    config = {"version": 1, "created_at": now(), "app_version": __version__,
        "workspace": str(workspace), "executable": find_codex(settings.get("codex_path", "")),
        "reasoning_effort": "medium", "service_tier": "default",
        "count": 5, "verify_sources": False, "reuse_cache": False,
        "enhanced_dedup": enhanced_dedup, "high_quality_only": False,
        "search_strategy": "wide", "discovery_limit": 200, "detail_limit": 40,
        "search_rounds": 6, "candidate_limit": 72,
        "screenshots": settings.get("screenshots", True),
        "artistic_nudity": settings.get("artistic_nudity", True),
        "call_timeout_seconds": 500, "concurrency": 1,
        "retry_policy": "One primary attempt per cell; failures remain visible, no automatic fresh searches.",
        "model_reporting": "Explicitly requested CLI identifiers; no inferred model identity.",
        "docs": ["https://learn.chatgpt.com/docs/models", "https://learn.chatgpt.com/docs/developer-settings"],
        "source_hashes": {p.relative_to(frozen).as_posix(): digest(p) for p in sorted(frozen.rglob("*.py"))},
        "cases": cases, "jobs": make_jobs(cases, models, profiles)}
    write_json(root / "matrix.json", config)
    write_json(root / "state.json", {"status": "ready", "created_at": now(), "updated_at": now(),
        "jobs": {job["id"]: {"status": "pending"} for job in config["jobs"]}})
    report(root)
    return config


def verify_frozen(root, config):
    frozen = (root / "_frozen").resolve()
    hashes = config["source_hashes"]
    if not isinstance(hashes, dict) or "tools/run_model_matrix.py" not in hashes:
        raise ValueError("У серії немає контрольної суми замороженого runner.")
    if set(hashes) != {p.relative_to(frozen).as_posix() for p in frozen.rglob("*.py")}:
        raise ValueError("Склад замороженого коду серії змінився.")
    for name, expected in hashes.items():
        path = (frozen / name).resolve()
        if not path.is_relative_to(frozen) or digest(path) != expected:
            raise ValueError("Код серії змінився: " + name)
    for case in config["cases"]:
        if digest(root / case["image"]) != case["sha256"]:
            raise ValueError("Змінився зафіксований ШІ-зразок.")


def request_for(root, config, job):
    return SearchRequest(description=job["description"], count=config["count"],
        output_dir=root / "runs" / job["id"] / "results", model=job["model"],
        codex_path=config["executable"], search_profile=job["profile"],
        reasoning_effort=config.get("reasoning_effort", "medium"),
        enhanced_dedup=config.get("enhanced_dedup", False), high_quality_only=config.get("high_quality_only", False),
        reference_image=root / job["guide"],
        **{k: config[k] for k in ("candidate_limit", "screenshots", "artistic_nudity", "search_rounds",
            "search_strategy", "discovery_limit", "detail_limit", "reuse_cache", "verify_sources")})


def worker(root, identifier):
    if Path(__file__).resolve() != (root / "_frozen/tools/run_model_matrix.py").resolve():
        raise ValueError("Worker має виконуватися із замороженого коду серії. Запусти команду run.")
    config = read_json(root / "matrix.json")
    verify_frozen(root, config)
    job = next(job for job in config["jobs"] if job["id"] == identifier)
    run_dir = root / "runs" / identifier
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "outcome.json").exists():
        raise ValueError("Завершений тест не можна повторно перезаписати.")
    request = request_for(root, config, job)
    request.validate()
    write_json(run_dir / "request.json", {**asdict(request), "reasoning_effort": config["reasoning_effort"],
        "service_tier": config["service_tier"], "reference_sha256": job["guide_sha256"]})
    started, clock_start = now(), time.monotonic()
    cancel, done = Event(), Event()
    def watch_stop():
        while not done.wait(0.5):
            if (root / "STOP").exists():
                cancel.set()
    Thread(target=watch_stop, daemon=True).start()
    def status(message):
        event = {"at": now(), "message": str(message)}
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
        write_json(run_dir / "progress.json", event)
        print(message, flush=True)
    client = CodexClient(config["executable"], job["model"], config["call_timeout_seconds"],
        reasoning_effort=config["reasoning_effort"], service_tier=config["service_tier"])
    outcome = {"job_id": identifier, "started_at": started}
    try:
        result = SearchPipeline(client=client).run(request, cancel, status)
        outcome.update(status="cancelled" if result.cancelled else "failed" if result.error else "completed",
            error=result.error, saved=len(result.assets), discovered=result.discovered,
            screened=result.screened, examined=result.examined, rejected=result.rejected,
            duplicates=result.duplicates, close=sum(a.assessment.is_close for a in result.assets),
            partial=sum(not a.assessment.is_close for a in result.assets),
            reused_assessments=result.reused_assessments, pending_review=result.pending_review,
            retry_path=str(result.retry_path) if result.retry_path else None,
            folder=str(result.folder) if result.folder else None, usage_path=str(result.usage_path),
            usage=result.usage, warnings=result.warnings)
        if result.folder:
            manifest = read_json(result.folder / "manifest.json")
            outcome["stop_reason"] = manifest["statistics"]["stop_reason"]
            outcome["metrics"] = manifest["statistics"].get("metrics", {})
    except Exception as exc:
        outcome.update(status="failed", error=str(exc))
        (run_dir / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
        if client.usage_ledger:
            outcome["usage"] = client.usage_ledger.snapshot()
    finally:
        done.set()
        outcome.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - clock_start, 3))
        write_json(run_dir / "outcome.json", outcome)
    return outcome


def report(root):
    config, state = read_json(root / "matrix.json"), read_json(root / "state.json")
    rows, galleries, summaries = [], [], []
    esc = lambda x: html.escape(str(x))
    for job in config["jobs"]:
        directory = root / "runs" / job["id"]
        outcome_path = directory / "outcome.json"
        outcome = read_json(outcome_path) if outcome_path.exists() else {}
        status = outcome.get("status", state["jobs"][job["id"]]["status"])
        usage = outcome.get("usage", {}).get("summary", {})
        tokens = usage.get("reported_tokens", {}).get("total_tokens")
        seconds = outcome.get("elapsed_seconds")
        details = {**job, **outcome, "status": status, "tokens": tokens,
                   "token_data_complete": usage.get("status") == "complete", "images": []}
        pics = []
        folder = Path(outcome["folder"]) if outcome.get("folder") else None
        if folder and (folder / "manifest.json").exists():
            manifest = read_json(folder / "manifest.json")
            for record in manifest["results"]:
                file = folder / record["file"]
                if not file.is_file():
                    continue
                from PIL import Image
                with Image.open(file) as image:
                    width, height = image.size
                relative = file.relative_to(root).as_posix()
                assessment = record["assessment"]
                details["images"].append({"file": relative, "width": width, "height": height,
                    "bytes": file.stat().st_size, "sha256": digest(file), "match": record["match"],
                    "score": assessment["score"], "reason": assessment["reason"]})
                pics.append(f'<figure><a href="{esc(relative)}"><img loading="lazy" src="{esc(relative)}"></a>'
                    f'<figcaption>{width}×{height} · {esc(record["match"])} · {assessment["score"]}/100'
                    f'<p>{esc(assessment["reason"])}</p></figcaption></figure>')
        summaries.append(details)
        profile = "Точний опис" if job["profile"] == "strict" else "Поза й анатомія"
        elapsed = "—" if seconds is None else f"{int(seconds // 60)}:{int(seconds % 60):02d}"
        values = [job["number"], job["case_number"], job["model"], profile, status,
            outcome.get("discovered", "—"), outcome.get("screened", "—"), outcome.get("examined", "—"),
            outcome.get("saved", "—"), outcome.get("close", "—"), elapsed,
            "—" if tokens is None else f"{tokens:,}" + ("*" if not details["token_data_complete"] else "")]
        rows.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in values) + '</tr>')
        if outcome:
            message = outcome.get("error") or outcome.get("stop_reason", "")
            galleries.append(f'<section><h2>{job["number"]}. Запит {job["case_number"]} · {esc(job["model"])} · {profile}</h2>'
                f'<p>{esc(message)}</p><div class="gallery">{"".join(pics) or "Збережених фото немає."}</div></section>')
    write_json(root / "summary.json", {"updated_at": now(), "state": state["status"], "tests": summaries})
    counts = Counter(row["status"] for row in summaries)
    completed = sum(counts[s] for s in TERMINAL)
    reference_cards = ''.join(f'<figure><img src="{esc(c["image"])}"><figcaption>{i}. {esc(c["description"])}</figcaption></figure>'
                              for i, c in enumerate(config["cases"], 1))
    head = ['№', 'Запит', 'Модель', 'Пріоритети', 'Стан', 'Знайдено', 'Мініатюри', 'Детально', 'Збережено', 'Близьких', 'Час', 'Токени']
    document = '<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    document += '<title>LazyRef — порівняння моделей</title><style>body{font:16px system-ui;margin:32px;background:#f5f5f0;color:#18251d}table{border-collapse:collapse;background:white;width:100%;font-size:14px}th,td{padding:9px;border:1px solid #ccd4ca;text-align:left}th{background:#dfead7}.scroll{overflow:auto}.gallery{display:flex;flex-wrap:wrap;gap:16px}figure{margin:0;width:250px;background:white;padding:12px;border-radius:8px}img{width:100%;height:250px;object-fit:contain}figcaption{font-size:14px}section{margin-top:32px}h1{margin-bottom:8px}</style>'
    total = len(config["jobs"])
    document += f'<h1>Порівняння моделей LazyRef</h1><p>Завершено спроб {completed}/{total} · стан серії {esc(state["status"])} · оновлено {esc(now())} (UTC)</p>'
    document += '<p>Усі тести: Medium · Standard · 5 фото · джерело OFF · повторне використання OFF · до 200 кандидатів / 40 детальних оцінок / 6 циклів. Пошуки послідовні.</p>'
    document += '<p>Посилений відсів повторів ' + ('ON' if config.get('enhanced_dedup') else 'OFF') + ' · лише висока роздільність ' + ('ON' if config.get('high_quality_only') else 'OFF') + '</p>'
    document += '<p>Токени = вхід + вихід. * — неповний облік. Кеш вхідних токенів сервера не є повторним використанням результатів LazyRef. Оцінки відповідності виставляють різні моделі; це не незалежна оцінка якості.</p>'
    document += f'<div class="gallery">{reference_cards}</div><h2>Порівняння</h2><div class="scroll"><table><thead><tr>{"".join("<th>"+h+"</th>" for h in head)}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    document += ''.join(galleries) + '</html>'
    temporary = root / 'report.html.tmp'
    temporary.write_text(document, encoding="utf-8")
    replace_when_available(temporary, root / "report.html")
    return counts


def run(root):
    import ctypes
    config, state = read_json(root / "matrix.json"), read_json(root / "state.json")
    verify_frozen(root, config)
    lock = root / "runner.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    awake = False
    process = None
    tree = None
    try:
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)
        if os.name == "nt":
            awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        state.pop("message", None)
        state.pop("finished_at", None)
        state.setdefault("started_at", now())
        state.update(status="running", pid=os.getpid(), sleep_prevention_active=awake)
        for job in config["jobs"]:
            run_dir = root / "runs" / job["id"]
            outcome_file = run_dir / "outcome.json"
            if outcome_file.exists():
                previous = read_json(outcome_file)
                state["jobs"][job["id"]] = {"status": previous["status"], "finished_at": previous["finished_at"]}
                continue
            if (root / "STOP").exists():
                state["status"] = "stopped"
                break
            if state["jobs"][job["id"]]["status"] == "running":
                state.update(status="interrupted", message="Previous worker was interrupted. Review its saved files before retrying.")
                break
            run_dir.mkdir(parents=True, exist_ok=True)
            state["jobs"][job["id"]] = {"status": "running", "started_at": now()}
            state.update(current_job=job["id"], updated_at=now())
            write_json(root / "state.json", state)
            report(root)
            print(now(), 'START', job["id"], flush=True)
            args = [sys.executable, "-B", "-X", "utf8", "-u", str((root / "_frozen/tools/run_model_matrix.py").resolve()),
                    "worker", "--root", str(root), "--job", job["id"]]
            with (run_dir / "worker.log").open("ab") as log, ProcessTree() as tree:
                process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=config["workspace"], **tree.popen_options)
                tree.attach(process)
                try:
                    state["jobs"][job["id"]]["pid"] = process.pid
                    write_json(root / "state.json", state)
                    while process.poll() is None:
                        state["updated_at"] = now()
                        write_json(root / "state.json", state)
                        time.sleep(2)
                except BaseException as exc:
                    # Спочатку даємо worker зберегти готові оцінки. Контекст
                    # ProcessTree завершує дерево навіть якщо STOP/очікування не вдалися.
                    try:
                        if process.poll() is None:
                            (root / "STOP").touch(exist_ok=True)
                            process.wait(timeout=10)
                    except Exception as cleanup:
                        exc.add_note("Не вдалося дочекатися зупинки worker: " + str(cleanup))
                    raise
            if not outcome_file.exists():
                write_json(outcome_file, {"job_id": job["id"], "status": "failed", "error": f"Worker exited {process.returncode}; see worker.log", "finished_at": now()})
            outcome = read_json(outcome_file)
            state["jobs"][job["id"]].update(status=outcome["status"], finished_at=outcome["finished_at"])
            print(now(), 'END', job["id"], outcome["status"], flush=True)
            state["updated_at"] = now()
            write_json(root / "state.json", state)
            report(root)
            if "Вичерпано ліміт Codex" in outcome.get("error", ""):
                state.update(status="quota_blocked", message="Codex account limit reached; remaining tests have not been started.")
                break
        else:
            statuses = [v["status"] for v in state["jobs"].values()]
            state["status"] = "completed" if all(s == "completed" for s in statuses) else "completed_with_errors"
    except BaseException as exc:
        state.update(status="interrupted", message=str(exc))
        raise
    finally:
        state.update(updated_at=now(), current_job=None, finished_at=now())
        primary = sys.exception()
        errors = []
        def cleanup(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        cleanup(lambda: write_json(root / "state.json", state))
        cleanup(lambda: report(root))
        if awake:
            cleanup(lambda: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000))
        # Не дозволяємо паралельний запуск, якщо ОС не підтвердила зупинку worker.
        if tree is None or tree.closed_cleanly:
            cleanup(lambda: lock.unlink(missing_ok=True))
        if errors:
            if primary is None:
                primary = errors.pop(0)
                for exc in errors:
                    primary.add_note("Помилка завершення серії: " + str(exc))
                raise primary
            for exc in errors:
                primary.add_note("Помилка завершення серії: " + str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "run", "worker", "report"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job")
    parser.add_argument("--selection-dir", type=Path,
                        help="Для init: папка з selection.json та обраними ШІ-зразками, зокрема в архіві")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "init":
        initialize(root, PACKAGE_ROOT, selection_dir=args.selection_dir)
    elif args.command == "run":
        run(root)
    elif args.command == "worker":
        worker(root, args.job)
    else:
        report(root)


if __name__ == "__main__":
    main()
