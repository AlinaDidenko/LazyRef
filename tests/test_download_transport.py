"""Direct HTTP regressions with synthetic streams/processes; no network."""

import base64
from http.cookiejar import Cookie
from io import BytesIO
import json
from queue import Queue
from threading import Event, Timer
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from lazyref.access import SourceBlocked
from lazyref.download_budget import DownloadBudget
from lazyref import download_process
from lazyref.models import Cancelled
from lazyref.network import Network


class Response(BytesIO):
    def __init__(self, body=b"OK", **headers):
        super().__init__(body)
        self.headers = headers


def test_read_available_bytes_checks_cancel_before_waiting_for_full_chunk(monkeypatch):
    cancel = Event()
    network = Network(cancel, local_transport=True)

    class Trickle(Response):
        def read(self, size=-1):
            pytest.fail("read(size) can wait to fill the whole 65536-byte buffer")

        def read1(self, size=-1):
            cancel.set()
            return b"x"

    response = Trickle(**{"content-type": "image/png"})
    monkeypatch.setattr(network.client, "open", lambda *args, **kwargs: response)
    try:
        with pytest.raises(Cancelled):
            network.get("https://photo.invalid/image.png")
        assert response.closed
    finally:
        network.close()


def test_split_error_html_still_detects_captcha(monkeypatch):
    network = Network(Event(), local_transport=True)

    class SplitError(BytesIO):
        def read1(self, size=-1):
            return super().read(min(size, 5))

    body = SplitError(b"<h1>Verify you are human</h1>")
    error = HTTPError("https://photo.invalid/error", 503, "error",
                      {"content-type": "text/html"}, body)

    def open_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(network.client, "open", open_error)
    try:
        with pytest.raises(SourceBlocked, match="CAPTCHA"):
            network.get("https://photo.invalid/error")
        assert body.closed
    finally:
        network.close()


def test_redirects_share_budget_and_close_response_on_expiry(monkeypatch):
    now = [0.0]
    budget = DownloadBudget(clock=lambda: now[0])
    network = Network(Event(), local_transport=True)
    calls = []
    response = Response()

    def open_response(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            now[0] = 25
            raise HTTPError(request.full_url, 302, "redirect",
                            {"location": "https://other.invalid/photo"}, BytesIO())
        now[0] = 46
        return response

    monkeypatch.setattr(network.client, "open", open_response)
    try:
        with network.use_budget(budget), pytest.raises(TimeoutError):
            network.get("https://photo.invalid/photo")
        assert len(calls) == 2
        assert budget.direct_deadline == 45
        assert response.closed
    finally:
        network.close()


def test_budget_context_reuses_nested_budget_and_restores_after_error():
    network = Network(Event(), local_transport=True)
    outer, inner = DownloadBudget(), DownloadBudget()
    try:
        with network.use_budget(outer):
            with network.use_budget() as shared:
                assert shared is outer
            with pytest.raises(ValueError):
                with network.use_budget(inner) as chosen:
                    assert chosen is inner
                    raise ValueError("synthetic")
            assert network._budget.get() is outer
        assert network._budget.get() is None
    finally:
        network.close()


@pytest.mark.parametrize("local_transport", [False, True])
def test_socket_idle_timeout_does_not_exhaust_shared_budget(monkeypatch, local_transport):
    now = [0.0]
    budget = DownloadBudget(clock=lambda: now[0])
    network = Network(Event(), local_transport=local_transport)
    if local_transport:
        def open_response(request, **kwargs):
            if request.full_url.endswith("original"):
                now[0] = 20
                raise TimeoutError("synthetic idle timeout")
            return Response()
        monkeypatch.setattr(network.client, "open", open_response)
    else:
        def respond(process, command):
            if command["url"].endswith("original"):
                now[0] = 20
                return [{"kind": "io_error", "message": "synthetic idle timeout"}]
            return [result()]
        fake_processes(monkeypatch, respond)
    try:
        with network.use_budget(budget):
            with pytest.raises(OSError) as caught:
                network.get("https://photo.invalid/original")
            assert not isinstance(caught.value, TimeoutError)
            assert budget.check_direct() == 25
            assert network.get("https://photo.invalid/thumbnail") == b"OK"
    finally:
        network.close()


def cookie():
    return Cookie(version=0, name="XSRF-TOKEN", value="normal%20value", port=None,
                  port_specified=False, domain="photo.invalid", domain_specified=False,
                  domain_initial_dot=False, path="/", path_specified=True, secure=True,
                  expires=None, discard=True, comment=None, comment_url=None, rest={})


def result(body=b"OK", **extra):
    return {"kind": "resource", "url": "https://photo.invalid/final",
            "content_type": "image/png", "body": base64.b64encode(body).decode("ascii"),
            "blocked": {}, **extra}


def fake_processes(monkeypatch, respond):
    processes, trees, starts = [], [], []

    class Output:
        def __init__(self):
            self.messages, self.closed = Queue(), False

        def readline(self, size):
            return self.messages.get()

        def close(self):
            self.closed = True

    class Input(BytesIO):
        def __init__(self, process):
            super().__init__()
            self.process = process

        def flush(self):
            command = json.loads(self.getvalue())
            self.seek(0)
            self.truncate()
            self.process.requests.append(command)
            for message in respond(self.process, command):
                raw = message if isinstance(message, bytes) else json.dumps(message).encode() + b"\n"
                self.process.stdout.messages.put(raw)

    class Process:
        def __init__(self, argv, **options):
            self.requests, self.alive = [], True
            self.stdout = Output()
            self.stdin = Input(self)
            processes.append(self)
            starts.append((argv, options))

    class Tree:
        def __init__(self, **kwargs):
            self.closed_cleanly = False
            self.process = None
            self.popen_options = {"creationflags": 1234}
            trees.append(self)

        def attach(self, process):
            self.process = process

        def close(self):
            if self.process:
                self.process.alive = False
                self.process.stdout.messages.put(b"")
            self.closed_cleanly = True

    monkeypatch.setattr(download_process, "ProcessTree", Tree)
    monkeypatch.setattr(download_process.subprocess, "Popen", Process)
    return processes, trees, starts


def test_default_network_uses_persistent_owned_worker_and_cookies(monkeypatch):
    network = Network(Event())
    network.cookies.set_cookie(cookie())
    records = download_process.cookie_records(network.cookies)
    processes, trees, starts = fake_processes(monkeypatch,
        lambda process, command: [result(cookies=records)])
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setattr(network.client, "open", lambda *a, **k: pytest.fail("local I/O forbidden"))
    try:
        assert network.get("https://photo.invalid/photo") == b"OK"
        fetched = network.fetch("https://photo.invalid/other")
        assert fetched.url == "https://photo.invalid/final"
        assert network.cookie_value("XSRF-TOKEN", "https://photo.invalid/") == "normal value"
        assert len(processes) == 1
        assert len(processes[0].requests) == 2
        assert processes[0].requests[1]["cookies"] == records
        assert starts[0][1]["creationflags"] == 1234
        assert "PYTHONPATH" not in starts[0][1]["env"]
        assert trees[0].process is processes[0]
    finally:
        network.close()
    assert not processes[0].alive
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_cancel_interrupts_worker_without_reply_and_preserves_cookie_snapshot(monkeypatch):
    cancel, network = Event(), None
    network = Network(cancel)
    network.cookies.set_cookie(cookie())
    records = download_process.cookie_records(network.cookies)

    def reply(process, command):
        Timer(0.03, cancel.set).start()
        return [{"kind": "progress", "received": 0, "total": 100,
                 "resource": "headers", "cookies": records}]

    processes, trees, _ = fake_processes(monkeypatch, reply)
    try:
        with pytest.raises(Cancelled):
            network.get("https://photo.invalid/photo")
        assert not processes[0].alive and trees[0].closed_cleanly
        assert network.cookie_value("XSRF-TOKEN", "https://photo.invalid/") == "normal value"
    finally:
        network.close()


def test_deadline_kills_worker_even_without_any_progress(monkeypatch):
    now = [0.0]
    budget = DownloadBudget(clock=lambda: now[0])
    network = Network(Event())

    def stalled(process, command):
        now[0] = 46
        return []

    processes, trees, _ = fake_processes(monkeypatch, stalled)
    try:
        with network.use_budget(budget), pytest.raises(TimeoutError):
            network.get("https://photo.invalid/photo")
        assert not processes[0].alive and trees[0].closed_cleanly
    finally:
        network.close()


def test_parent_rpc_progress_extends_at_45_but_stops_dripping_worker_at_70(monkeypatch):
    now = [0.0]
    timestamps = {0: 33.0, 600_000: 40.0, 700_000: 44.0,
                  710_000: 45.0, 711_000: 70.0}
    observed = []

    class TimedBudget(DownloadBudget):
        def observe(self, received, total, resource_id):
            # Advance only as the real parent consumes each queued RPC frame;
            # worker scheduling cannot race the simulated clock.
            now[0] = timestamps[received]
            observed.append(now[0])
            if now[0] == 70:
                assert self.extended and self.direct_deadline == 70
            super().observe(received, total, resource_id)

    budget = TimedBudget(clock=lambda: now[0])
    network = Network(Event())
    frames = [{"kind": "progress", "received": received, "total": 1_000_000,
               "resource": "same-response"} for received in timestamps]
    processes, trees, _ = fake_processes(monkeypatch, lambda *args: frames)
    try:
        with network.use_budget(budget), pytest.raises(TimeoutError):
            network.get("https://photo.invalid/slow-but-progressing")
        assert observed == [33, 40, 44, 45, 70]
        assert budget.extended and budget.direct_deadline == 70
        assert not processes[0].alive and trees[0].closed_cleanly
    finally:
        network.close()


def test_blocked_source_survives_deadline_and_restart_keeps_anonymous_cookies(monkeypatch):
    now = [0.0]
    budget = DownloadBudget(clock=lambda: now[0])
    network = Network(Event())
    network.cookies.set_cookie(cookie())
    records = download_process.cookie_records(network.cookies)
    original_observe = budget.observe

    def observe(*args, **kwargs):
        original_observe(*args, **kwargs)
        now[0] = 46

    monkeypatch.setattr(budget, "observe", observe)

    def respond(process, command):
        if command["url"].endswith("blocked"):
            return [{"kind": "blocked", "url": command["url"], "reason": "CAPTCHA",
                     "blocked": {"photo.invalid": "CAPTCHA"}, "cookies": records,
                     "received": 0, "resource": "blocked"}]
        return [result()]

    processes, _, _ = fake_processes(monkeypatch, respond)
    try:
        with network.use_budget(budget), pytest.raises(SourceBlocked, match="CAPTCHA"):
            network.get("https://photo.invalid/blocked")
        assert network.access.blocked["photo.invalid"] == "CAPTCHA"
        assert network.get("https://other.invalid/photo") == b"OK"
        assert len(processes) == 2
        assert processes[1].requests[0]["cookies"] == records
    finally:
        network.close()


def test_real_worker_protocol_keeps_redirect_cookie_and_does_not_repeat_params(monkeypatch):
    from test_network_security import PUBLIC_V4, address, fake_wire

    redirect = (b"HTTP/1.1 302 Found\r\nLocation: /final\r\n"
                b"Set-Cookie: session=anonymous; Path=/\r\nContent-Length: 0\r\n\r\n")
    ok = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 2\r\n\r\nOK"
    sockets, _ = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4, port)], responses=(redirect, ok))
    command = {"url": "http://photo.invalid/start", "params": {"query": "model pose"},
               "headers": None, "data": None, "max_bytes": 100, "cookies": [], "blocked": {}}
    outgoing = BytesIO()
    monkeypatch.setattr(download_process.sys, "stdin",
                        SimpleNamespace(buffer=BytesIO(json.dumps(command).encode() + b"\n")))
    monkeypatch.setattr(download_process.sys, "stdout", SimpleNamespace(buffer=outgoing))

    download_process.worker()

    frames = [json.loads(raw) for raw in outgoing.getvalue().splitlines()]
    assert frames[-1]["kind"] == "resource"
    assert base64.b64decode(frames[-1]["body"]) == b"OK"
    assert frames[-1]["cookies"][0]["value"] == "anonymous"
    assert b"GET /start?query=model%20pose HTTP" in sockets[0].sent
    assert b"GET /final HTTP" in sockets[1].sent
    assert b"Cookie: session=anonymous" in sockets[1].sent
    assert any(frame["kind"] == "progress" and frame.get("total") == 2 for frame in frames)


def test_attach_failure_closes_suspended_process_and_never_starts_request(monkeypatch):
    network = Network(Event())
    processes, trees, _ = fake_processes(monkeypatch, lambda *args: [result()])
    tree_type = download_process.ProcessTree
    attach = tree_type.attach

    def fail_attach(self, process):
        attach(self, process)
        raise OSError("synthetic Job assignment failure")

    monkeypatch.setattr(tree_type, "attach", fail_attach)
    try:
        with pytest.raises(OSError, match="Job assignment"):
            network.get("https://photo.invalid/photo")
        assert not processes[0].alive and trees[0].closed_cleanly
        assert not processes[0].requests
    finally:
        network.close()


def test_unconfirmed_cleanup_does_not_wait_on_pipe_lock_or_restart(monkeypatch):
    network = Network(Event())
    entered, release, finished = Event(), Event(), Event()
    processes, _, _ = fake_processes(monkeypatch,
        lambda *args: [{"kind": "io_error", "message": "original socket timeout"}])

    def fail_cleanup(self):
        def blocked_close():
            entered.set()
            assert release.wait(2)
            finished.set()
        self.process.stdin.close = blocked_close
        raise OSError("synthetic tree cleanup failure")

    monkeypatch.setattr(download_process.ProcessTree, "close", fail_cleanup)
    try:
        with pytest.raises(OSError, match="original socket timeout") as caught:
            network.get("https://photo.invalid/photo")
        assert entered.wait(1) and not finished.is_set()
        assert "cleanup failure" in caught.value.__notes__[0]
        with pytest.raises(RuntimeError, match="не підтверджено"):
            network.get("https://photo.invalid/next")
        assert len(processes) == 1
    finally:
        release.set()
        assert finished.wait(1)
        with pytest.raises(RuntimeError, match="не підтверджено"):
            network.close()


@pytest.mark.parametrize("reply", [b"not json\n", b"{}", b"[]\n", b"x" * (download_process.MAX_REPLY + 1)])
def test_invalid_or_oversized_rpc_reply_closes_owned_worker(monkeypatch, reply):
    network = Network(Event())
    processes, trees, _ = fake_processes(monkeypatch, lambda *args: [reply])
    try:
        with pytest.raises((RuntimeError, ValueError)):
            network.get("https://photo.invalid/photo")
        assert not processes[0].alive and trees[0].closed_cleanly
    finally:
        network.close()
