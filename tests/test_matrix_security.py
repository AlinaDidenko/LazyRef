"""Matrix runner regressions; all workers and process trees are in-memory fakes."""

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import run_model_matrix as matrix


class FakeProcessTree:
    """Expose ownership and teardown without starting or inspecting any process."""

    popen_options = {"creationflags": 0x00000004}

    def __init__(self, events, lock, *, close_error=None, **kwargs):
        self.events = events
        self.lock = lock
        self.process = None
        self.closed_cleanly = False
        self._closed = False
        self.close_error = close_error

    def __enter__(self):
        self.events.append("tree_enter")
        return self

    def attach(self, process):
        self.process = process
        self.events.append("tree_attach")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.events.append(("tree_close", self.lock.exists()))
        if self.close_error is not None:
            raise self.close_error
        if self.process is not None:
            # The tree owns descendants even when its original worker has exited.
            self.process.descendant_alive = False
            if self.process.returncode is None:
                self.process.kill()
            self.process.wait(timeout=10)
            self.process = None
        self.closed_cleanly = True

    def __exit__(self, kind, error, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if error is None:
                raise
            error.add_note("Fake process tree cleanup failed: " + str(cleanup))
        return False


@pytest.fixture
def series(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-series"
    frozen_runner = root / "_frozen/tools/run_model_matrix.py"
    frozen_runner.parent.mkdir(parents=True)
    frozen_runner.write_text("# frozen test fixture; never executed\n", encoding="utf-8")
    job = {"id": "01_test", "model": "unused", "profile": "strict", "description": "fixture"}
    config = {
        "workspace": str(tmp_path),
        "source_hashes": {"tools/run_model_matrix.py": matrix.digest(frozen_runner)},
        "cases": [],
        "jobs": [job],
    }
    matrix.write_json(root / "matrix.json", config)
    matrix.write_json(root / "state.json", {
        "status": "ready", "jobs": {job["id"]: {"status": "pending"}},
    })
    events = []
    processes = []
    calls = []
    wake_flags = []
    lock = root / "runner.lock"
    # Replace the OS API as well as Popen: these tests do not affect host sleep.
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(
        SetThreadExecutionState=lambda flags: wake_flags.append(flags) or 1)), raising=False)
    monkeypatch.setattr(matrix, "report", lambda path: events.append("report"))
    monkeypatch.setattr(matrix.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(matrix, "ProcessTree",
                        lambda **kwargs: FakeProcessTree(events, lock, **kwargs), raising=False)
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == lock:
            events.append(("unlock", any(p.descendant_alive or p.returncode is None for p in processes)))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    original_write = matrix.write_json
    controls = SimpleNamespace(running=False, poll_error=None)

    class Process:
        pid = 123456789

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            events.append("spawn")
            self.returncode = None if controls.running else 0
            self.descendant_alive = True
            processes.append(self)
            if not controls.running:
                original_write(root / "runs" / job["id"] / "outcome.json", {
                    "status": "completed", "finished_at": "fixture-finished", "error": "",
                })

        def poll(self):
            if controls.poll_error is not None:
                error, controls.poll_error = controls.poll_error, None
                raise error
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                events.append("wait_timeout")
                raise matrix.subprocess.TimeoutExpired("fake-worker", timeout)
            events.append("wait")
            return self.returncode

    monkeypatch.setattr(matrix.subprocess, "Popen", Process)
    return SimpleNamespace(root=root, runner=frozen_runner, config=config, job=job,
                           events=events, processes=processes, calls=calls, wake_flags=wake_flags,
                           lock=lock, controls=controls, write_json=original_write)


def assert_released(series):
    assert not series.lock.exists()
    assert ("unlock", False) in series.events
    if matrix.os.name == "nt":
        assert series.wake_flags == [0x80000001, 0x80000000]


def test_matrix_executes_verified_frozen_runner_with_owned_process_tree(series):
    matrix.run(series.root)
    assert len(series.calls) == 1
    args, options = series.calls[0]
    assert Path(args[args.index("worker") - 1]).resolve() == series.runner.resolve()
    assert options["creationflags"] == FakeProcessTree.popen_options["creationflags"]
    assert series.events.index("spawn") < series.events.index("tree_attach")
    assert series.events.index(("tree_close", True)) < series.events.index(("unlock", False))
    assert all(not process.descendant_alive for process in series.processes)
    assert matrix.read_json(series.root / "state.json")["status"] == "completed"
    assert_released(series)


@pytest.mark.parametrize("invalid_snapshot", ["changed_script", "missing_runner_hash"])
def test_matrix_rejects_unverified_runner_before_starting_worker(series, invalid_snapshot):
    if invalid_snapshot == "changed_script":
        series.runner.write_text("# altered frozen code\n", encoding="utf-8")
    else:
        series.config["source_hashes"].clear()
        series.write_json(series.root / "matrix.json", series.config)
    with pytest.raises(ValueError):
        matrix.run(series.root)
    assert series.calls == []
    assert not series.lock.exists()


def test_direct_active_worker_cannot_run_archived_snapshot(series, monkeypatch):
    def forbidden_request(*args):
        raise AssertionError("Active code reached worker execution for a different frozen snapshot")

    monkeypatch.setattr(matrix, "request_for", forbidden_request)
    with pytest.raises(ValueError):
        matrix.worker(series.root, series.job["id"])
    assert not (series.root / "runs").exists()
    assert series.calls == []


@pytest.mark.parametrize("failure_point", ["pid_state_write", "poll", "keyboard_interrupt"])
def test_parent_error_stops_worker_tree_before_unlock(series, monkeypatch, failure_point):
    series.controls.running = True
    failure = KeyboardInterrupt("synthetic interrupt") if failure_point == "keyboard_interrupt" else OSError("synthetic parent error")
    if failure_point == "pid_state_write":
        failed = False

        def write_json(path, value):
            nonlocal failed
            if (not failed and Path(path).name == "state.json"
                    and value["jobs"][series.job["id"]].get("pid")):
                failed = True
                raise failure
            return series.write_json(path, value)

        monkeypatch.setattr(matrix, "write_json", write_json)
    else:
        series.controls.poll_error = failure
    with pytest.raises(type(failure)) as caught:
        matrix.run(series.root)
    assert caught.value is failure
    assert "tree_attach" in series.events
    assert series.events.index(("tree_close", True)) < series.events.index(("unlock", False))
    assert series.events.index("kill") < series.events.index("wait") < series.events.index(("unlock", False))
    assert matrix.read_json(series.root / "state.json")["status"] == "interrupted"
    assert_released(series)


@pytest.mark.parametrize("failure_point", ["final_state_write", "final_report"])
def test_final_persistence_error_still_restores_sleep_and_unlocks(series, monkeypatch, failure_point):
    failure = OSError("synthetic final persistence error")

    def write_json(path, value):
        if (failure_point == "final_state_write" and Path(path).name == "state.json"
                and "finished_at" in value):
            raise failure
        return series.write_json(path, value)

    def report(path):
        if failure_point == "final_report" and "finished_at" in matrix.read_json(path / "state.json"):
            raise failure

    monkeypatch.setattr(matrix, "write_json", write_json)
    monkeypatch.setattr(matrix, "report", report)
    with pytest.raises(OSError) as caught:
        matrix.run(series.root)
    assert caught.value is failure
    assert_released(series)


def test_final_report_error_does_not_hide_original_parent_error(series, monkeypatch):
    primary = OSError("original parent failure")
    series.controls.running = True
    series.controls.poll_error = primary

    def report(path):
        if "finished_at" in matrix.read_json(path / "state.json"):
            raise OSError("secondary report failure")

    monkeypatch.setattr(matrix, "report", report)
    with pytest.raises(OSError) as caught:
        matrix.run(series.root)
    assert caught.value is primary
    assert any("secondary report failure" in note for note in getattr(primary, "__notes__", []))
    assert "kill" in series.events and "wait" in series.events
    assert_released(series)


@pytest.mark.parametrize("parent_failed", [False, True])
def test_unconfirmed_tree_cleanup_retains_lock_even_after_worker_exits(series, monkeypatch, parent_failed):
    cleanup_error = TimeoutError("synthetic descendants remain alive")
    tree = FakeProcessTree(series.events, series.lock, close_error=cleanup_error)
    monkeypatch.setattr(matrix, "ProcessTree", lambda: tree)
    primary = OSError("original parent error") if parent_failed else cleanup_error
    if parent_failed:
        series.controls.poll_error = primary
    with pytest.raises(type(primary)) as caught:
        matrix.run(series.root)
    assert caught.value is primary
    assert series.processes[0].returncode == 0
    assert series.processes[0].descendant_alive
    assert tree.closed_cleanly is False
    assert series.lock.is_file()
    assert not any(isinstance(event, tuple) and event[0] == "unlock" for event in series.events)
    assert matrix.read_json(series.root / "state.json")["status"] == "interrupted"
    if matrix.os.name == "nt":
        assert series.wake_flags == [0x80000001, 0x80000000]
    if parent_failed:
        assert any(str(cleanup_error) in note for note in getattr(primary, "__notes__", []))
