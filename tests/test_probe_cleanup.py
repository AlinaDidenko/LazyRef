"""Diagnostic utilities own their entire process tree on every exit path."""

import importlib.util
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("name", ["probe_codex_image_search", "probe_codex_generation"])
@pytest.mark.parametrize("outcome", ["success", "interrupt", "timeout", "io_error", "attach_error"])
def test_probe_owns_process_before_communication_and_cleans_up(tmp_path, monkeypatch, name, outcome):
    source = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "tools" / (name + ".py")))
    monkeypatch.setattr(module, "find_codex", lambda *args: "never-launched.exe")
    events = []

    class Tree:
        popen_options = {"creationflags": 4}

        def __enter__(self):
            events.append("own")
            return self

        def attach(self, process):
            events.append("attach")
            if outcome == "attach_error":
                raise OSError("synthetic attach failure")

        def __exit__(self, *args):
            events.append("terminate-tree")

    class Process:
        returncode = 0

        def __init__(self, *args, **kwargs):
            assert kwargs["creationflags"] == 4
            events.append("spawn-suspended")

        def communicate(self, *args, **kwargs):
            assert events == ["own", "spawn-suspended", "attach"]
            events.append("communicate")
            if outcome == "interrupt":
                raise KeyboardInterrupt()
            if outcome == "timeout":
                raise subprocess.TimeoutExpired("fake", kwargs["timeout"])
            if outcome == "io_error":
                raise OSError("synthetic pipe failure")

    monkeypatch.setattr(module, "ProcessTree", Tree)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    if outcome == "success":
        assert module.main() == 0
    elif outcome == "timeout" and name == "probe_codex_image_search":
        assert module.main() == 1
    else:
        expected = KeyboardInterrupt if outcome == "interrupt" else subprocess.TimeoutExpired if outcome == "timeout" else OSError
        with pytest.raises(expected):
            module.main()
    assert events[-1] == "terminate-tree"
