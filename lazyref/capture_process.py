"""Окремий процес для скріншота: спільний дедлайн для запуску, DNS і JS.

Нативний запуск враховується в бюджеті після повернення; завершення дерева
процесів і очищення тимчасових даних можуть потребувати додаткового часу.

Python тут є довіреним мережевим посередником. Фоновий браузер запускається
в AppContainer без мережевих capabilities через окремий browser_driver.
"""

import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time

from .access import SourceBlocked
from .models import Candidate, check_cancel
from .processes import ProcessTree


MAX_REPLY = 28_000_000
CAPTURE_MEMORY_LIMIT = 1024 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def remaining_seconds(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Вичерпано час запасного скріншота. Переходжу до наступного кандидата.")
    return remaining


class CaptureProcess:
    def __init__(self, cancel, access):
        self.cancel, self.access = cancel, access
        self.process = self.tree = self.temporary = self.log = None
        self._tree_cleanup_failed = False

    def _start(self):
        if self.process is not None:
            return
        if self.temporary is not None:
            self.close()
        if os.name != "nt":
            raise RuntimeError("Ізольований запасний скріншот підтримується лише у Windows.")
        try:
            # Explicit cleanup retains the recovery journal if native teardown
            # fails; TemporaryDirectory's finalizer could otherwise erase it.
            self.temporary = Path(tempfile.mkdtemp(prefix="lazyref-capture-")).resolve()
            self.log = (self.temporary / "worker.log").open("wb")
            self.tree = ProcessTree(timeout=1, memory_limit=CAPTURE_MEMORY_LIMIT)
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["LAZYREF_CAPTURE_ROOT"] = str(self.temporary)
            for key in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PWDEBUG", "DEBUG", "DEBUGP",
                        "PLAYWRIGHT_NODEJS_PATH", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS"):
                environment.pop(key, None)
            self.process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m",
                "lazyref.capture_process", "worker"], cwd=ROOT, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                **self.tree.popen_options)
            self.tree.attach(self.process)
        except BaseException as primary:
            try:
                self.close()
            except Exception as cleanup:
                primary.add_note("Помилка очищення браузера: " + str(cleanup))
            raise

    def capture(self, candidate, deadline):
        check_cancel(self.cancel)
        remaining_seconds(deadline)
        result = Queue(maxsize=1)
        request = json.dumps({"candidate": asdict(candidate), "deadline": deadline,
                              "blocked": self.access.blocked}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(request) > 500_000:
            raise ValueError("Завеликий запит запасного скріншота.")

        def exchange(process):
            try:
                process.stdin.write(request)
                process.stdin.flush()
                raw = process.stdout.readline(MAX_REPLY + 1)
                if not raw or len(raw) > MAX_REPLY or not raw.endswith(b"\n"):
                    raise RuntimeError("Ізольований браузер не повернув результат.")
                result.put(json.loads(raw))
            except Exception as exc:
                result.put(exc)

        reader = None
        try:
            self._start()
            check_cancel(self.cancel)
            remaining_seconds(deadline)
            reader = Thread(target=exchange, args=(self.process,), daemon=True, name="LazyRef capture reply")
            reader.start()
            while True:
                check_cancel(self.cancel)
                remaining = remaining_seconds(deadline)
                try:
                    response = result.get(timeout=min(0.05, remaining))
                    break
                except Empty:
                    continue
            check_cancel(self.cancel)
            remaining_seconds(deadline)
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict) or not isinstance(response.get("blocked"), dict):
                raise RuntimeError("Некоректна відповідь ізольованого браузера.")
            self.access.blocked.update(response["blocked"])
            if response.get("kind") == "blocked":
                raise SourceBlocked(response["url"], response["reason"])
            if response.get("kind") == "timeout":
                raise TimeoutError(response["message"])
            if response.get("kind") != "image":
                raise RuntimeError(response.get("message", "Не вдалося зробити запасний скріншот."))
            image = base64.b64decode(response["image"], validate=True)
            if not image or len(image) > 20_000_000:
                raise ValueError("Некоректний розмір запасного скріншота.")
            check_cancel(self.cancel)
            remaining_seconds(deadline)
            return image
        except BaseException as primary:
            # Навіть заблокований DNS або нескінченний JS не утримує пошук.
            try:
                self.close()
            except Exception as cleanup:
                primary.add_note("Помилка очищення браузера: " + str(cleanup))
            raise
        finally:
            if reader is not None and reader.ident is not None and self.process is None:
                reader.join(timeout=0.2)

    def close(self):
        process, tree = self.process, self.tree
        self.process = self.tree = None
        errors = []

        def cleanup(action):
            try:
                action()
            except BaseException as exc:
                errors.append(exc)

        if tree:
            cleanup(tree.close)
            if not tree.closed_cleanly:
                self._tree_cleanup_failed = True
        if self._tree_cleanup_failed and not errors:
            errors.append(RuntimeError("Завершення дерева браузера не підтверджено; тимчасові дані збережено."))
        if process:
            if self._tree_cleanup_failed:
                # Buffered pipe.close() takes the same lock as the RPC thread's
                # read/write. If native termination failed, EOF is not assured;
                # never wait for that lock on the search/cancellation thread.
                # The daemon retains the streams until they can be closed, and
                # the original tree error remains visible to the caller.
                primary = errors[0]
                primary.add_note("Закриття каналів браузера відкладено: завершення процесів не підтверджено.")

                def deferred_pipe_close():
                    for stream in (process.stdin, process.stdout):
                        if stream:
                            try:
                                stream.close()
                            except BaseException as exc:
                                primary.add_note("Помилка відкладеного закриття каналу браузера: " + str(exc))

                cleanup(Thread(target=deferred_pipe_close, daemon=True,
                               name="LazyRef deferred pipe cleanup").start)
            else:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        cleanup(stream.close)
        if self.log:
            cleanup(self.log.close)
            self.log = None
        if self.temporary and not self._tree_cleanup_failed:
            root = self.temporary
            def close_temporary():
                # root was created by this instance; never follow a replaced
                # directory link or remove an arbitrary path from a journal.
                if root.resolve() != root or not root.name.startswith("lazyref-capture-"):
                    raise ValueError("Некоректний тимчасовий каталог браузера.")
                if any(root.glob("sandbox-*.json")):
                    from .browser_sandbox import cleanup_sandboxes
                    cleanup_sandboxes(root)
                if root.exists():
                    shutil.rmtree(root)
                self.temporary = None
            cleanup(close_temporary)
        if errors:
            primary = errors[0]
            for error in errors[1:]:
                primary.add_note("Додаткова помилка очищення браузера: " + str(error))
            raise primary


def worker():
    from .capture import BrowserCapture
    browser = BrowserCapture(Event())
    try:
        for raw in sys.stdin.buffer:
            response = {}
            try:
                command = json.loads(raw)
                candidate = Candidate(**command["candidate"])
                browser.access.blocked.update(command.get("blocked", {}))
                image = browser.capture_local(candidate, deadline=command["deadline"])
                response = {"kind": "image", "image": base64.b64encode(image).decode("ascii")}
            except SourceBlocked as exc:
                response = {"kind": "blocked", "url": exc.url, "reason": exc.reason}
            except TimeoutError as exc:
                response = {"kind": "timeout", "message": str(exc)}
            except Exception as exc:
                response = {"kind": "error", "message": str(exc)[:1500]}
            response["blocked"] = browser.access.blocked
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    finally:
        browser.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["worker"]:
        worker()
    else:
        raise SystemExit("Unknown capture helper mode")
