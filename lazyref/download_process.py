"""Owned HTTP worker: cancellation also interrupts DNS, TLS and blocked reads.

Only anonymous application cookies cross the bounded JSON protocol. No browser
profile, user cookies, or external executable is loaded by this worker.
"""

import base64
from http.cookiejar import Cookie
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import subprocess
import sys
from threading import Event, Lock, Thread
import time

from .access import SourceBlocked
from .models import check_cancel
from .processes import ProcessTree


ROOT = Path(__file__).resolve().parents[1]
MAX_REQUEST = 1_000_000
MAX_REPLY = 28_000_000
MAX_BODY = 20_000_000
MAX_COOKIE_STATE = 500_000
COOKIE_FIELDS = ("version", "name", "value", "port", "port_specified", "domain",
                 "domain_specified", "domain_initial_dot", "path", "path_specified",
                 "secure", "expires", "discard", "comment", "comment_url", "rfc2109")


def cookie_records(jar):
    records = []
    for cookie in jar:
        record = {name: getattr(cookie, name) for name in COOKIE_FIELDS}
        record["rest"] = dict(cookie._rest)
        records.append(record)
        if len(records) > 1000:
            raise ValueError("Забагато cookies анонімної сесії.")
    if len(json.dumps(records).encode("utf-8")) > MAX_COOKIE_STATE:
        raise ValueError("Завеликий стан cookies анонімної сесії.")
    return records


def restore_cookies(jar, records):
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("Некоректний стан cookies анонімної сесії.")
    if len(json.dumps(records).encode("utf-8")) > MAX_COOKIE_STATE:
        raise ValueError("Завеликий стан cookies анонімної сесії.")
    cookies = [Cookie(**record) for record in records]
    jar.clear()
    for cookie in cookies:
        jar.set_cookie(cookie)


class DownloadProcess:
    def __init__(self, cancel, access, cookies):
        self.cancel, self.access, self.cookies = cancel, access, cookies
        self.process = self.tree = None
        self._cleanup_failed = False
        self._lock = Lock()
        self._serial = 0

    def _start(self):
        if self._cleanup_failed:
            raise RuntimeError("Завершення попереднього HTTP-процесу не підтверджено.")
        if self.process is not None:
            return
        try:
            self.tree = ProcessTree(timeout=1, memory_limit=512 * 1024 * 1024 if os.name == "nt" else None)
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            for key in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONHOME",
                        "PWDEBUG", "DEBUG", "DEBUGP", "PLAYWRIGHT_NODEJS_PATH",
                        "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS"):
                environment.pop(key, None)
            self.process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m",
                "lazyref.download_process", "worker"], cwd=ROOT, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                **self.tree.popen_options)
            self.tree.attach(self.process)
        except BaseException as primary:
            try:
                self.close()
            except Exception as cleanup:
                primary.add_note("Помилка завершення HTTP-процесу: " + str(cleanup))
            raise

    def fetch(self, url, budget, *, params=None, headers=None, max_bytes=3_000_000, data=None):
        # One pipe conversation at a time. Waiting callers remain cancellable.
        while True:
            check_cancel(self.cancel)
            remaining = budget.check_direct()
            if self._lock.acquire(timeout=min(0.05, remaining)):
                break
        try:
            return self._fetch(url, budget, params=params, headers=headers,
                               max_bytes=max_bytes, data=data)
        finally:
            self._lock.release()

    def _fetch(self, url, budget, *, params, headers, max_bytes, data):
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BODY:
            raise ValueError("Некоректне обмеження розміру HTTP-відповіді.")
        request = {"url": url, "params": params, "headers": headers, "max_bytes": max_bytes,
                   "data": None if data is None else base64.b64encode(data).decode("ascii"),
                   "cookies": cookie_records(self.cookies), "blocked": self.access.blocked}
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_REQUEST:
            raise ValueError("Завеликий HTTP-запит.")
        messages, stop_reader = Queue(maxsize=8), Event()
        self._serial += 1
        serial = self._serial

        def deliver(message):
            while not stop_reader.is_set():
                try:
                    messages.put(message, timeout=0.05)
                    return
                except Full:
                    continue

        def exchange(process):
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
                while not stop_reader.is_set():
                    raw = process.stdout.readline(MAX_REPLY + 1)
                    if not raw or len(raw) > MAX_REPLY or not raw.endswith(b"\n"):
                        raise RuntimeError("HTTP-процес не повернув коректний результат.")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise RuntimeError("Некоректна відповідь HTTP-процесу.")
                    deliver(message)
                    if message.get("kind") != "progress":
                        return
            except Exception as exc:
                deliver(exc)

        reader = None
        try:
            check_cancel(self.cancel)
            budget.check_direct()
            self._start()
            check_cancel(self.cancel)
            budget.check_direct()
            reader = Thread(target=exchange, args=(self.process,), daemon=True,
                            name="LazyRef HTTP reply")
            reader.start()
            while True:
                check_cancel(self.cancel)
                try:
                    message = messages.get_nowait()
                except Empty:
                    remaining = budget.check_direct()
                    try:
                        message = messages.get(timeout=min(0.05, remaining))
                    except Empty:
                        continue
                if isinstance(message, Exception):
                    raise message
                if "cookies" in message:
                    restore_cookies(self.cookies, message["cookies"])
                blocked = message.get("blocked", {})
                if not isinstance(blocked, dict):
                    raise RuntimeError("Некоректний стан доступу HTTP-процесу.")
                self.access.blocked.update(blocked)
                if "received" in message:
                    budget.observe(message["received"], message.get("total"),
                                   resource_id=f"{serial}:{message['resource']}")
                kind = message.get("kind")
                # A received access restriction must stay a restriction: do
                # not turn it into a timeout eligible for browser fallback.
                if kind == "blocked":
                    raise SourceBlocked(message["url"], message["reason"])
                budget.check_direct()
                if kind == "progress":
                    continue
                if kind == "value_error":
                    raise ValueError(message["message"])
                if kind == "io_error":
                    # A 20-second socket idle timeout leaves room for another
                    # URL. Only the parent's watchdog exhausts the candidate.
                    raise OSError(message["message"])
                if kind != "resource":
                    raise RuntimeError(message.get("message", "Не вдалося завантажити ресурс."))
                body = base64.b64decode(message["body"], validate=True)
                if len(body) > max_bytes:
                    raise ValueError("Файл перевищує допустимий розмір.")
                from .network import FetchedResource
                check_cancel(self.cancel)
                budget.check_direct()
                return FetchedResource(message["url"], body, message["content_type"])
        except BaseException as primary:
            stop_reader.set()
            try:
                self.close()
            except Exception as cleanup:
                primary.add_note("Помилка завершення HTTP-процесу: " + str(cleanup))
            raise
        finally:
            stop_reader.set()
            if reader is not None and reader.ident is not None:
                reader.join(timeout=0.2)

    def close(self):
        process, tree = self.process, self.tree
        self.process = self.tree = None
        error = None
        if tree is not None:
            try:
                tree.close()
            except BaseException as exc:
                error = exc
            if not tree.closed_cleanly:
                self._cleanup_failed = True
        if self._cleanup_failed and error is None:
            error = RuntimeError("Завершення дерева HTTP-процесу не підтверджено.")

        def close_pipes():
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        if process is not None:
            if self._cleanup_failed:
                # A buffered stream lock may belong to a reader stuck in native
                # I/O. Never wait for that lock on the cancellation thread.
                Thread(target=close_pipes, daemon=True, name="LazyRef HTTP pipe cleanup").start()
            else:
                close_pipes()
        if error is not None:
            raise error


def worker():
    from .network import Network
    network = Network(Event(), local_transport=True)

    def send(message):
        raw = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > MAX_REPLY:
            raise ValueError("Завелика відповідь HTTP-процесу.")
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()

    try:
        while True:
            raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
            if not raw:
                break
            if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                break
            last_sent = 0.0
            latest = {}
            previous_cookies = None

            def progress(received, total, resource_id):
                nonlocal last_sent, latest, previous_cookies
                latest = {"received": received, "total": total, "resource": resource_id}
                now = time.monotonic()
                # Include headers immediately (cookies/known body length), then
                # report at most ten times per second while streaming.
                if received and now - last_sent < 0.1:
                    return
                state = cookie_records(network.cookies)
                message = {"kind": "progress", **latest}
                if state != previous_cookies:
                    message["cookies"] = state
                    previous_cookies = state
                send(message)
                last_sent = now

            try:
                command = json.loads(raw)
                restore_cookies(network.cookies, command["cookies"])
                network.access.blocked.update(command["blocked"])
                data = command.get("data")
                resource = network._get_local(command["url"], params=command.get("params"),
                    headers=command.get("headers"), max_bytes=command["max_bytes"],
                    data=None if data is None else base64.b64decode(data, validate=True),
                    metadata=True, progress=progress)
                response = {"kind": "resource", "url": resource.url,
                            "content_type": resource.content_type,
                            "body": base64.b64encode(resource.body).decode("ascii")}
            except SourceBlocked as exc:
                response = {"kind": "blocked", "url": exc.url, "reason": exc.reason}
            except TimeoutError as exc:
                response = {"kind": "io_error", "message": str(exc)[:1500]}
            except ValueError as exc:
                response = {"kind": "value_error", "message": str(exc)[:1500]}
            except Exception as exc:
                response = {"kind": "error", "message": str(exc)[:1500]}
            response.update(latest)
            response["blocked"] = network.access.blocked
            try:
                response["cookies"] = cookie_records(network.cookies)
            except ValueError:
                # The existing parent snapshot stays intact if a hostile
                # response tries to grow cookie state beyond the protocol cap.
                network.cookies.clear()
            send(response)
    finally:
        network.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["worker"]:
        raise SystemExit(2)
    worker()
