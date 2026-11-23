"""Capture RPC lifecycle: synthetic pipes/processes, no browser or services."""

import base64
from collections import deque
from dataclasses import asdict
from io import BytesIO, StringIO
import json
from pathlib import Path
from queue import Queue as RealQueue
from threading import Event
from types import SimpleNamespace
import sys
import tempfile
import time

import pytest

import lazyref.capture_process as module
from lazyref.access import AccessPolicy, SourceBlocked
from lazyref.capture_process import CaptureProcess
from lazyref.models import Cancelled, Candidate


CANDIDATE = Candidate("test", "Photo", image_url="https://photos.invalid/image.jpg",
                      page_url="https://photos.invalid/page")


def reply(kind="image", **values):
    data = {"kind": kind, "blocked": {}}
    if kind == "image":
        data["image"] = base64.b64encode(b"synthetic PNG bytes").decode("ascii")
    return json.dumps({**data, **values}).encode("utf-8") + b"\n"


class Input:
    def __init__(self):
        self.writes, self.flushes = [], 0
        self.closed = False
        self.before_write = lambda: None

    def write(self, data):
        self.before_write()
        self.writes.append(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class Output:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.limits = []
        self.closed = False

    def readline(self, limit):
        self.limits.append(limit)
        return self.responses.popleft() if self.responses else b""

    def close(self):
        self.closed = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = SimpleNamespace(processes=[], trees=[], commands=[], responses=[reply(), reply()],
                            events=[], on_spawn=lambda process: None, attach_error=None,
                            cleanup_error=None)
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(
        mkdtemp=lambda **kwargs: real_mkdtemp(dir=tmp_path, **kwargs)))

    class Process:
        pid = 123456789

        def __init__(self, args, **kwargs):
            self.stdin, self.stdout = Input(), Output(state.responses)
            self.alive = True
            self.release = Event()
            state.processes.append(self)
            state.commands.append((args, kwargs))
            state.events.append("spawn")
            state.on_spawn(self)

    class Tree:
        popen_options = {"creationflags": 4}

        def __init__(self, timeout, memory_limit):
            assert timeout == 1
            self.memory_limit = memory_limit
            self.process = None
            self.closed_cleanly = False
            self.closed = 0
            state.trees.append(self)

        def attach(self, process):
            self.process = process
            state.events.append("attach")
            if state.attach_error:
                raise state.attach_error

        def close(self):
            self.closed += 1
            state.events.append("stop_tree")
            if self.process:
                self.process.alive = False
                self.process.release.set()
            if state.cleanup_error:
                raise state.cleanup_error
            self.closed_cleanly = True

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "ProcessTree", Tree)
    controller = CaptureProcess(Event(), AccessPolicy())
    state.controller, state.root = controller, tmp_path
    try:
        yield state
    finally:
        state.cleanup_error = None
        # A test deliberately leaves an unconfirmed tree; only fake processes
        # exist, so it is safe to confirm their teardown for fixture cleanup.
        controller._tree_cleanup_failed = False
        controller.close()


def test_success_reuses_worker_and_preserves_deadline_and_blocked_context(harness):
    h = harness
    h.controller.access.blocked["skipped.invalid"] = "CAPTCHA"
    deadline = time.monotonic() + 2

    assert h.controller.capture(CANDIDATE, deadline) == b"synthetic PNG bytes"
    assert h.controller.capture(CANDIDATE, deadline) == b"synthetic PNG bytes"

    assert len(h.processes) == len(h.trees) == 1 and h.processes[0].alive
    requests = [json.loads(raw) for raw in h.processes[0].stdin.writes]
    assert requests == [{"candidate": asdict(CANDIDATE), "deadline": deadline,
                         "blocked": {"skipped.invalid": "CAPTCHA"}}] * 2
    assert h.processes[0].stdout.limits == [module.MAX_REPLY + 1] * 2
    args, options = h.commands[0]
    assert args == [sys.executable, "-B", "-X", "utf8", "-m", "lazyref.capture_process", "worker"]
    assert options["creationflags"] == 4 and options["cwd"] == module.ROOT
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert Path(options["env"]["LAZYREF_CAPTURE_ROOT"]) == h.controller.temporary
    root = h.controller.temporary
    h.controller.close()
    assert not root.exists() and h.trees[0].closed == 1
    assert h.processes[0].stdin.closed and h.processes[0].stdout.closed


def test_worker_environment_drops_external_python_and_node_injection(harness, monkeypatch):
    keys = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PWDEBUG", "DEBUG", "DEBUGP",
            "PLAYWRIGHT_NODEJS_PATH", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS")
    for key in keys:
        monkeypatch.setenv(key, "synthetic override")

    harness.controller.capture(CANDIDATE, time.monotonic() + 2)

    assert not set(keys).intersection(harness.commands[0][1]["env"])


def test_capture_limits_memory_of_the_entire_owned_worker_tree(harness):
    harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    assert harness.trees[0].memory_limit == 1024 * 1024 * 1024


@pytest.mark.parametrize("condition", ["cancelled", "expired", "oversized"])
def test_invalid_or_finished_request_never_starts_worker(harness, condition):
    candidate = CANDIDATE
    deadline = time.monotonic() + 2
    error = Cancelled
    if condition == "cancelled":
        harness.controller.cancel.set()
    elif condition == "expired":
        deadline = time.monotonic() - 1
        error = TimeoutError
    else:
        candidate = Candidate("test", "x" * 500_001)
        error = ValueError

    with pytest.raises(error):
        harness.controller.capture(candidate, deadline)

    assert harness.commands == [] and not list(harness.root.iterdir())


def test_startup_time_consumes_same_deadline(harness, monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    harness.on_spawn = lambda process: setattr(clock, "now", 3)

    with pytest.raises(TimeoutError):
        harness.controller.capture(CANDIDATE, 2)

    assert harness.events == ["spawn", "attach", "stop_tree"]
    assert harness.processes[0].stdin.writes == []
    assert not list(harness.root.iterdir())


def test_response_delivered_after_deadline_is_not_accepted(harness, monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class LateQueue(RealQueue):
        def get(self, *args, **kwargs):
            response = super().get(*args, **kwargs)
            clock.now = 3
            return response

    monkeypatch.setattr(module, "Queue", LateQueue)
    with pytest.raises(TimeoutError):
        harness.controller.capture(CANDIDATE, 2)

    assert not harness.processes[0].alive and harness.controller.process is None


@pytest.mark.parametrize("cause", ["timeout", "cancel"])
def test_blocked_stdin_writer_cannot_block_parent_deadline_or_cancel(harness, cause):
    entered = Event()

    def prepare(process):
        def block():
            entered.set()
            if cause == "cancel":
                harness.controller.cancel.set()
            assert process.release.wait(2), "Owning tree must release the synthetic blocked pipe"
        process.stdin.before_write = block

    harness.on_spawn = prepare
    started = time.monotonic()
    with pytest.raises(TimeoutError if cause == "timeout" else Cancelled):
        harness.controller.capture(CANDIDATE, started + 0.15)

    assert entered.is_set()
    assert time.monotonic() - started < 1
    assert harness.events[-1] == "stop_tree" and not harness.processes[0].alive
    assert harness.processes[0].stdin.closed and harness.processes[0].stdout.closed


def test_captcha_response_propagates_without_retrying_or_solving(harness):
    harness.responses = [reply("blocked", url=CANDIDATE.page_url, reason="CAPTCHA",
                               blocked={"photos.invalid": "CAPTCHA"})]

    with pytest.raises(SourceBlocked) as error:
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)

    assert error.value.url == CANDIDATE.page_url and error.value.reason == "CAPTCHA"
    assert harness.controller.access.blocked == {"photos.invalid": "CAPTCHA"}
    assert len(harness.commands) == 1 and not harness.processes[0].alive


@pytest.mark.parametrize("response,error", [
    (b"", RuntimeError),
    (b'{"kind":"image"}', RuntimeError),
    (b"not json\n", ValueError),
    (b"[]\n", RuntimeError),
    (reply("image", blocked=[]), RuntimeError),
    (reply("image", image="invalid base64"), ValueError),
    (reply("image", image=""), ValueError),
    (reply("timeout", message="shared deadline"), TimeoutError),
    (reply("error", message="isolated browser failed"), RuntimeError),
])
def test_failed_or_malformed_reply_closes_owned_helper(harness, response, error):
    harness.responses = [response]
    with pytest.raises(error):
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    assert not harness.processes[0].alive
    assert harness.controller.process is None and not list(harness.root.iterdir())


def test_reply_size_is_bounded(harness, monkeypatch):
    monkeypatch.setattr(module, "MAX_REPLY", 10)
    harness.responses = [b"x" * 11 + b"\n"]
    with pytest.raises(RuntimeError):
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    assert harness.processes[0].stdout.limits == [11]


def test_tree_construction_failure_cleans_partial_startup(harness, monkeypatch):
    def fail(**kwargs):
        raise OSError("synthetic job setup failure")
    monkeypatch.setattr(module, "ProcessTree", fail)

    with pytest.raises(OSError, match="job setup"):
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)

    assert harness.controller.log is None and harness.controller.temporary is None
    assert not harness.commands and not list(harness.root.iterdir())


def test_attach_failure_stops_suspended_helper_and_preserves_original(harness):
    harness.attach_error = OSError("synthetic assignment failure")
    with pytest.raises(OSError, match="assignment failure"):
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    assert harness.events == ["spawn", "attach", "stop_tree"]
    assert not harness.processes[0].alive and not list(harness.root.iterdir())


def test_cleanup_failure_preserves_primary_and_recovery_directory(harness):
    harness.responses = [reply("timeout", message="original deadline")]
    harness.cleanup_error = OSError("synthetic tree cleanup failure")

    with pytest.raises(TimeoutError, match="original deadline") as error:
        harness.controller.capture(CANDIDATE, time.monotonic() + 2)

    assert "synthetic tree cleanup failure" in error.value.__notes__[0]
    assert harness.controller.temporary.is_dir()


def test_unconfirmed_tree_cannot_block_parent_on_buffered_pipe_close(harness):
    entered, release, finished = Event(), Event(), Event()
    harness.responses = [reply("timeout", message="original deadline")]
    harness.cleanup_error = OSError("synthetic tree cleanup failure")

    def prepare(process):
        def blocked_close():
            entered.set()
            assert release.wait(2), "Parent must return before blocked pipe cleanup"
            raise OSError("synthetic secondary pipe failure")
        def finish_close():
            process.stdout.closed = True
            finished.set()
        process.stdin.close = blocked_close
        process.stdout.close = finish_close

    harness.on_spawn = prepare
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="original deadline") as error:
            harness.controller.capture(CANDIDATE, started + 2)
        assert time.monotonic() - started < 1
        assert entered.wait(1) and not finished.is_set()
        assert "synthetic tree cleanup failure" in error.value.__notes__[0]
        assert harness.controller.temporary.is_dir()
    finally:
        release.set()
        assert finished.wait(1)
    assert harness.processes[0].stdout.closed
    assert any("synthetic secondary pipe failure" in note
               for note in harness.cleanup_error.__notes__)


def test_sandbox_journal_is_cleaned_only_after_tree_stops(harness, monkeypatch):
    harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    root = harness.controller.temporary
    (root / "sandbox-fixture.json").write_text("{}", encoding="utf-8")

    def sandbox_cleanup(actual_root):
        assert actual_root == root
        assert harness.trees[0].closed_cleanly and not harness.processes[0].alive
        harness.events.append("sandbox_cleanup")

    monkeypatch.setitem(sys.modules, "lazyref.browser_sandbox",
                        SimpleNamespace(cleanup_sandboxes=sandbox_cleanup))
    harness.controller.close()

    assert harness.events[-2:] == ["stop_tree", "sandbox_cleanup"]
    assert not root.exists()


def test_sandbox_cleanup_failure_retains_journal_for_later_cleanup(harness, monkeypatch):
    harness.controller.capture(CANDIDATE, time.monotonic() + 2)
    root = harness.controller.temporary
    journal = root / "sandbox-fixture.json"
    journal.write_text("{}", encoding="utf-8")

    def fail(actual_root):
        raise OSError("synthetic profile cleanup failure")

    fake = SimpleNamespace(cleanup_sandboxes=fail)
    monkeypatch.setitem(sys.modules, "lazyref.browser_sandbox", fake)
    with pytest.raises(OSError, match="profile cleanup"):
        harness.controller.close()

    assert journal.is_file() and harness.controller.temporary == root
    fake.cleanup_sandboxes = lambda actual_root: None
    harness.controller.close()
    assert not root.exists()


def test_worker_reuses_local_browser_and_passes_absolute_parent_deadlines(monkeypatch):
    created = []

    class Browser:
        def __init__(self, cancel):
            self.access = AccessPolicy()
            self.calls, self.closed = [], False
            created.append(self)

        def capture_local(self, candidate, deadline):
            self.calls.append((candidate, deadline))
            return b"image"

        def close(self):
            self.closed = True

    monkeypatch.setattr("lazyref.capture.BrowserCapture", Browser)
    commands = [{"candidate": asdict(CANDIDATE), "deadline": value, "blocked": {"denied.invalid": "CAPTCHA"}}
                for value in (123.4, 125.6)]
    data = b"".join(json.dumps(command).encode() + b"\n" for command in commands)
    output = StringIO()
    monkeypatch.setattr(module, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=BytesIO(data)), stdout=output))

    module.worker()

    assert len(created) == 1 and created[0].closed
    assert created[0].calls == [(CANDIDATE, 123.4), (CANDIDATE, 125.6)]
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert all(response["kind"] == "image" and response["blocked"] == {"denied.invalid": "CAPTCHA"}
               for response in responses)
