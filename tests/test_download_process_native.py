"""Opt-in real ProcessTree regressions with a synthetic, network-blocked child.

Run tools/run_download_process_tests.py; the regular offline suite skips these.
The fixture replaces only socket operations inside the real HTTP worker. No
HTTP server, browser, Codex process or live service is started.
"""

import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time

import pytest

from lazyref import download_process
from lazyref.download_budget import DownloadBudget
from lazyref.models import Cancelled
from lazyref.network import Network


pytestmark = [pytest.mark.native_download, pytest.mark.skipif(
    os.environ.get("LAZYREF_NATIVE_DOWNLOAD_TESTS") != "1",
    reason="Opt-in owned HTTP process tests; use tools/run_download_process_tests.py")]

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "download_process_worker.py"
URL = "http://download.fixture.invalid/photo.png"


@pytest.fixture
def native_worker(monkeypatch, tmp_path):
    original_popen = download_process.subprocess.Popen
    processes, trees, directories = [], [], []
    clients = []

    def create(mode):
        directory = tmp_path / mode
        directory.mkdir()
        directories.append(directory)

        def synthetic_popen(args, **kwargs):
            assert args == [sys.executable, "-B", "-X", "utf8", "-m",
                            "lazyref.download_process", "worker"]
            kwargs["cwd"] = directory
            child = original_popen([sys.executable, "-B", "-X", "utf8", str(FIXTURE),
                                    mode, str(directory)], **kwargs)
            processes.append(child)
            return child

        monkeypatch.setattr(download_process.subprocess, "Popen", synthetic_popen)
        cancel = Event()
        network = Network(cancel)
        client = download_process.DownloadProcess(cancel, network.access, network.cookies)
        original_start = client._start

        def start():
            original_start()
            if client.tree not in trees:
                trees.append(client.tree)

        monkeypatch.setattr(client, "_start", start)
        clients.append((client, network, cancel))
        return client, cancel, directory

    try:
        yield create
    finally:
        for client, network, cancel in clients:
            cancel.set()
            client.close()
            network.close()
        assert all(process.poll() is not None for process in processes)
        assert all(tree.closed_cleanly for tree in trees)
        assert all(not (directory / "blocked-io.txt").exists() for directory in directories), (
            "The synthetic child attempted external I/O")


def wait_until_ready(directory, thread, results, timeout=5):
    deadline = time.monotonic() + timeout
    marker = directory / "ready.json"
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # Writer may still be completing its small local marker.
        if not thread.is_alive():
            pytest.fail(f"Worker returned before its synthetic blocked operation: {results!r}")
        Event().wait(0.01)
    pytest.fail("Synthetic worker did not reach its blocked operation within five seconds")


@pytest.mark.parametrize("stage", ["dns", "connect", "body"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_real_worker_stops_promptly_in_each_blocked_stage(native_worker, stage, stop):
    client, cancel, directory = native_worker(stage)
    clock = [0.0]
    budget = DownloadBudget(lambda: clock[0]) if stop == "deadline" else DownloadBudget()
    results = []

    def fetch():
        try:
            results.append(client.fetch(URL, budget))
        except BaseException as exc:
            results.append(exc)

    thread = Thread(target=fetch, daemon=True)
    thread.start()
    try:
        ready = wait_until_ready(directory, thread, results)
        process, tree = client.process, client.tree
        assert ready["stage"] == stage
        assert type(ready["pid"]) is int and ready["pid"] > 0
        # A Windows venv Python launcher may have its own PID. ProcessTree's
        # real Job Object covers both the launcher and this worker descendant.
        assert process.poll() is None
        stopped_at = time.monotonic()
        if stop == "cancel":
            cancel.set()
        else:
            clock[0] = 45.0
        thread.join(timeout=2)
        elapsed = time.monotonic() - stopped_at
        assert not thread.is_alive(), "HTTP request did not stop promptly"
        assert elapsed < 2
        assert len(results) == 1
        assert isinstance(results[0], Cancelled if stop == "cancel" else TimeoutError)
        assert process.poll() is not None, "Owned HTTP worker survived the stopped request"
        assert tree.closed_cleanly  # Includes waiting for an empty Windows Job.
        assert client.process is None
    finally:
        cancel.set()
        if thread.is_alive():
            client.close()
            thread.join(timeout=2)


def test_real_worker_reuses_process_and_returns_anonymous_cookies(native_worker):
    client, cancel, directory = native_worker("success")
    first = client.fetch(URL, DownloadBudget())
    process, tree = client.process, client.tree
    assert first.body == b"1:"
    assert process.poll() is None
    second = client.fetch(URL, DownloadBudget())
    assert second.body == b"2:anonymous"
    assert client.process is process
    client.close()
    assert process.poll() is not None
    assert tree.closed_cleanly
