"""240/500 секунд перевіряються віртуальним годинником, без очікування або хмари."""

from io import BytesIO
import json
from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient, PLAN_SCHEMA, REQUEST_TIMEOUT_SECONDS
from lazyref.models import Cancelled
from lazyref.usage import UsageLedger
from test_process_security import FakeProcessTree


class Clock:
    def __init__(self):
        self.now = 0


class FastEvent(Event):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.times = iter((120, 239, 240, 300, 499, 500, 550, 600))

    def wait(self, timeout=None):
        self.clock.now = next(self.times)
        return self.is_set()


def setup_client(tmp_path, monkeypatch, finish_at):
    clock = Clock()
    processes = []
    monkeypatch.setattr("lazyref.codex.find_codex", lambda configured: "codex.exe")
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    monkeypatch.setattr("lazyref.codex.time.monotonic", lambda: clock.now)
    class Process:
        def __init__(self, args, stdout, **kwargs):
            self.stdin, self.returncode, self.killed_at = BytesIO(), None, None
            self.answer = Path(args[args.index("-o") + 1])
            self.stdout = stdout
            stdout.write(b'{"type":"turn.started"}\n')
            stdout.flush()
            processes.append(self)

        def poll(self):
            if self.returncode is None and clock.now >= finish_at:
                self.answer.write_text('{"ok":true}', encoding="utf-8")
                self.stdout.write(json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 100, "output_tokens": 20}}).encode())
                self.stdout.flush()
                self.returncode = 0
            return self.returncode

        def kill(self):
            self.killed_at, self.returncode = clock.now, -1

        def wait(self, **kwargs):
            return self.returncode
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    client = CodexClient()
    client.usage_ledger = UsageLedger(tmp_path / "usage.json", "search")
    return client, clock, processes


def test_warning_at_240_keeps_same_process_running_and_accepts_later_answer(tmp_path, monkeypatch):
    client, clock, processes = setup_client(tmp_path, monkeypatch, finish_at=300)
    notifications = []
    event = FastEvent(clock)
    client.wait_changed = lambda active: notifications.append((active, clock.now))
    assert client.timeout == REQUEST_TIMEOUT_SECONDS == 500
    assert client.request("test", PLAN_SCHEMA, event) == {"ok": True}
    assert notifications == [(True, 240), (False, 300)]
    assert not event.is_set() and len(processes) == 1 and processes[0].killed_at is None
    usage = client.usage_ledger.snapshot()
    assert usage["summary"]["calls"] == 1 and usage["summary"]["reported_tokens"]["total_tokens"] == 120
    # Наступний виклик отримує власні пороги й окремо скидає повідомлення.
    clock.now = 0
    assert client.request("next", PLAN_SCHEMA, FastEvent(clock)) == {"ok": True}
    assert notifications == [(True, 240), (False, 300), (True, 240), (False, 300)]
    assert len(processes) == 2


def test_fast_completion_never_displays_warning(tmp_path, monkeypatch):
    client, clock, processes = setup_client(tmp_path, monkeypatch, finish_at=239)
    notifications = []
    client.wait_changed = notifications.append
    client.request("test", PLAN_SCHEMA, FastEvent(clock))
    assert notifications == [False] and processes[0].killed_at is None


def test_no_response_is_killed_at_500_not_240_and_retains_unknown_usage(tmp_path, monkeypatch):
    client, clock, processes = setup_client(tmp_path, monkeypatch, finish_at=600)
    notifications = []
    client.wait_changed = lambda active: notifications.append((active, clock.now))
    with pytest.raises(TimeoutError, match="500 с"):
        client.request("test", PLAN_SCHEMA, FastEvent(clock))
    assert processes[0].killed_at == 500 and len(processes) == 1
    assert notifications == [(True, 240), (False, 500)]
    usage = client.usage_ledger.snapshot()
    assert usage["calls"][0]["status"] == "timed_out"
    assert usage["summary"]["reported_tokens"]["total_tokens"] is None


def test_user_can_cancel_from_warning_before_hard_deadline(tmp_path, monkeypatch):
    client, clock, processes = setup_client(tmp_path, monkeypatch, finish_at=600)
    event = FastEvent(clock)
    notifications = []
    def on_waiting(active):
        notifications.append(active)
        if active:
            event.set()
    client.wait_changed = on_waiting
    with pytest.raises(Cancelled):
        client.request("test", PLAN_SCHEMA, event)
    assert processes[0].killed_at == 240 and notifications == [True, False]
    assert client.usage_ledger.snapshot()["calls"][0]["status"] == "cancelled"


def test_explicit_generation_timeout_still_applies_without_search_warning(tmp_path, monkeypatch):
    client, clock, processes = setup_client(tmp_path, monkeypatch, finish_at=550)
    notifications = []
    client.wait_changed = notifications.append
    assert client.request("test", {}, FastEvent(clock), image_generation=True, timeout=600) == {"ok": True}
    assert notifications == [False] and processes[0].killed_at is None
