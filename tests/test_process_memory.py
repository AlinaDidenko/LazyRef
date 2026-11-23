"""Memory limits are applied before a child can run; no allocations or OS jobs."""

import ctypes
from types import SimpleNamespace

import pytest

import lazyref.processes as module


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.fixture
def native_api(monkeypatch):
    state = SimpleNamespace(limits=[], closed=[], accepts_limits=True)

    def set_limits(handle, kind, pointer, size):
        limits = pointer._obj
        assert handle == 123 and kind == 9 and size == ctypes.sizeof(limits)
        state.limits.append((limits.BasicLimitInformation.LimitFlags,
                             limits.JobMemoryLimit, limits.ProcessMemoryLimit))
        return state.accepts_limits

    class Api:
        def __init__(self):
            self.CreateJobObjectW = Function(lambda *args: 123)
            self.SetInformationJobObject = Function(set_limits)
            self.CloseHandle = Function(lambda handle: state.closed.append(handle) or 1)

        def __getattr__(self, name):
            function = Function(lambda *args: 1)
            setattr(self, name, function)
            return function

    api = Api()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    return state


@pytest.mark.parametrize("limit,flags", [(None, 0x2000), (1024 ** 3, 0x2200)])
def test_native_job_keeps_kill_on_close_and_only_adds_requested_tree_memory_cap(native_api, limit, flags):
    job = module._WindowsJob(memory_limit=limit)
    try:
        assert native_api.limits == [(flags, limit or 0, 0)]
    finally:
        job.close()
    assert native_api.closed == [123]


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "1024", ctypes.c_size_t(-1).value + 1])
def test_invalid_memory_limit_cannot_wrap_or_silently_disable_the_native_limit(monkeypatch, limit):
    def unexpected_native_call(*args, **kwargs):
        pytest.fail("Invalid memory limit must fail before allocating a native job")
    monkeypatch.setattr(ctypes, "WinDLL", unexpected_native_call)
    with pytest.raises(ValueError, match="SIZE_T"):
        module._WindowsJob(memory_limit=limit)
    with pytest.raises(ValueError, match="SIZE_T"):
        module.ProcessTree(memory_limit=limit)


def test_rejected_native_memory_limit_fails_closed_and_closes_the_job(native_api):
    native_api.accepts_limits = False
    with pytest.raises(OSError):
        module._WindowsJob(memory_limit=1024 ** 3)
    assert native_api.closed == [123]


def test_process_tree_passes_memory_cap_without_changing_the_default(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_WindowsJob", lambda **kwargs: calls.append(kwargs) or object())
    module.ProcessTree()
    module.ProcessTree(memory_limit=1024 ** 3)
    assert calls == [{}, {"memory_limit": 1024 ** 3}]


def test_requested_memory_limit_is_not_ignored_on_an_unsupported_platform(monkeypatch):
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError, match="Windows"):
        module.ProcessTree(memory_limit=1024 ** 3)
