"""Process ownership and prompt transport regressions; no real CLI/network."""

from pathlib import Path
from threading import Event

import pytest

from lazyref.codex import CodexClient
from lazyref.models import Cancelled
from lazyref.processes import ProcessTree


class FakeProcessTree:
    """Explicit owner for tests that replace Popen with an in-memory process."""
    popen_options = {"creationflags": 0}

    def __enter__(self):
        self.process = None
        self.closed_cleanly = False
        return self

    def attach(self, process):
        self.process = process

    def __exit__(self, *args):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.closed_cleanly = True


def test_large_prompt_is_a_seekable_file_not_a_blocking_pipe(monkeypatch):
    prompt = "Український промпт\n" * 10000
    seen = []

    class NeverWrite:
        def write(self, value):
            pytest.fail("Parent must not write to a child pipe")

    class Process:
        def __init__(self, args, stdin, **kwargs):
            assert stdin.seekable()
            seen.append(stdin.read())
            self.stdin, self.returncode = NeverWrite(), 0
            Path(args[args.index("-o") + 1]).write_text('{"ok":true}', encoding="utf-8")

        def poll(self):
            return self.returncode

    monkeypatch.setattr("lazyref.codex.find_codex", lambda path: "fake-codex.exe")
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    assert CodexClient().request(prompt, {}, Event()) == {"ok": True}
    assert seen == [prompt.encode("utf-8")]


def test_process_startup_time_counts_towards_deadline(monkeypatch):
    clock = [0]
    killed = []

    class Process:
        def __init__(self, *args, **kwargs):
            clock[0] = 2
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            killed.append(True)
            self.returncode = -1

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr("lazyref.codex.find_codex", lambda path: "fake-codex.exe")
    monkeypatch.setattr("lazyref.codex.ProcessTree", FakeProcessTree)
    monkeypatch.setattr("lazyref.codex.subprocess.Popen", Process)
    monkeypatch.setattr("lazyref.codex.time.monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        CodexClient(timeout=1).request("prompt", {}, Event())
    assert killed == [True]


class FakeJob:
    def __init__(self):
        self.events = []

    def attach(self, process):
        self.events.append("attach")

    def resume(self, process):
        self.events.append("resume")

    def terminate(self):
        self.events.append("terminate-tree")

    def wait_empty(self, timeout):
        self.events.append("wait-all")

    def close(self):
        self.events.append("close-job")


class FakeChild:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.events = []

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append("kill-parent")
        self.returncode = -1

    def wait(self, timeout):
        self.events.append("wait-parent")
        return self.returncode


@pytest.fixture
def fake_job(monkeypatch):
    # Windows is the supported platform. No Win32 calls occur in these tests.
    import lazyref.processes as module
    monkeypatch.setattr(module.os, "name", "nt")
    job = FakeJob()
    monkeypatch.setattr(module, "_WindowsJob", lambda: job)
    return job


@pytest.mark.parametrize("parent_returncode", [None, 0])
def test_tree_is_owned_before_resume_and_all_descendants_are_stopped(fake_job, parent_returncode):
    child = FakeChild(parent_returncode)
    with ProcessTree() as tree:
        assert tree.popen_options["creationflags"] & 0x4  # CREATE_SUSPENDED
        assert tree.popen_options["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
        tree.attach(child)
        assert fake_job.events == ["attach", "resume"]
    assert fake_job.events == ["attach", "resume", "terminate-tree", "wait-all", "close-job"]
    assert child.events == ["wait-parent"]
    assert tree.closed_cleanly


def test_failed_job_assignment_never_resumes_child_and_kills_it(fake_job):
    def fail(process):
        fake_job.events.append("attach-failed")
        raise OSError("Cannot assign")
    fake_job.attach = fail
    child = FakeChild()
    with pytest.raises(OSError, match="Cannot assign"):
        with ProcessTree() as tree:
            tree.attach(child)
    assert fake_job.events == ["attach-failed", "close-job"]
    assert child.events == ["kill-parent", "wait-parent"]


def test_cleanup_error_does_not_replace_cancellation(fake_job):
    def fail():
        raise OSError("Synthetic termination failure")
    fake_job.terminate = fail
    with pytest.raises(Cancelled) as error:
        with ProcessTree() as tree:
            tree.attach(FakeChild())
            raise Cancelled()
    assert "Synthetic termination failure" in error.value.__notes__[0]
    assert fake_job.events[-1] == "close-job"
    assert not tree.closed_cleanly


def test_cleanup_error_without_primary_error_is_reported(fake_job):
    def fail(timeout):
        raise TimeoutError("Synthetic descendant hang")
    fake_job.wait_empty = fail
    with pytest.raises(TimeoutError, match="Synthetic descendant hang"):
        with ProcessTree() as tree:
            tree.attach(FakeChild(0))
    assert fake_job.events[-1] == "close-job"
    assert not tree.closed_cleanly
    tree.close()
    assert not tree.closed_cleanly  # Повторне закриття не приховує першої помилки.
