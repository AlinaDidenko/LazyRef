"""Windows sandbox ownership contracts with all native APIs replaced."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

import lazyref.browser_sandbox as module
from lazyref.browser_sandbox import BrowserSandbox, PROFILE_PATTERN, PROFILE_PREFIX, cleanup_sandboxes


@pytest.fixture
def windows(monkeypatch):
    state = SimpleNamespace(created=[], deleted=[], permitted=[], freed=[],
                            create_result=0, delete_result=0, permit_error=None)

    def create(name, display, description, capabilities, count, sid):
        state.created.append((name, capabilities, count))
        sid._obj.value = 12345
        return state.create_result

    def delete(name):
        state.deleted.append(name)
        return state.delete_result

    def status(code):
        if code:
            raise OSError(f"synthetic native status {code}")

    def permit(path, sid):
        state.permitted.append((Path(path), sid.value))
        if state.permit_error:
            raise state.permit_error

    api = SimpleNamespace(
        profiles=SimpleNamespace(CreateAppContainerProfile=create, DeleteAppContainerProfile=delete),
        security=SimpleNamespace(FreeSid=lambda sid: state.freed.append(sid.value)),
        status=status, permit_directory=permit)
    monkeypatch.setattr(module, "_Windows", lambda: api)
    state.api = api
    return state


def saved_journal(root, identifier="1" * 32):
    profile = PROFILE_PREFIX + identifier
    directory = root / ("browser-" + identifier)
    directory.mkdir()
    (directory / "fixture.txt").write_text("temporary browser data", encoding="utf-8")
    journal = root / ("sandbox-" + identifier + ".json")
    journal.write_text(json.dumps({"profile": profile, "directory": directory.name}), encoding="utf-8")
    return profile, directory, journal


def test_new_profile_has_zero_capabilities_and_only_its_child_directory_gets_acl(tmp_path, windows):
    untouched = tmp_path / "unrelated.txt"
    untouched.write_text("keep", encoding="utf-8")
    sandbox = BrowserSandbox(tmp_path)

    assert PROFILE_PATTERN.fullmatch(sandbox.profile)
    assert windows.created == [(sandbox.profile, None, 0)]
    assert windows.permitted == [(sandbox.directory, 12345)]
    assert sandbox.directory.parent == tmp_path and sandbox.directory != tmp_path
    assert windows.freed == [12345]
    assert json.loads(sandbox.journal.read_text(encoding="utf-8")) == {
        "profile": sandbox.profile, "directory": sandbox.directory.name}

    sandbox.close()
    sandbox.close()

    assert windows.deleted == [sandbox.profile]
    assert sandbox.closed and not sandbox.directory.exists() and not sandbox.journal.exists()
    assert untouched.read_text(encoding="utf-8") == "keep" and tmp_path.is_dir()


def test_environment_capture_root_owns_sandbox_without_implicit_temp_directory(tmp_path, windows, monkeypatch):
    monkeypatch.setenv("LAZYREF_CAPTURE_ROOT", str(tmp_path))
    sandbox = BrowserSandbox()
    try:
        assert sandbox.root == tmp_path and sandbox._temporary is None
    finally:
        sandbox.close()


@pytest.mark.parametrize("data", [
    {"profile": "Other.Application", "directory": "browser-" + "1" * 32},
    {"profile": PROFILE_PREFIX + "1" * 32, "directory": "../outside"},
    {"profile": PROFILE_PREFIX + "1" * 32, "directory": "browser-" + "2" * 32},
    {"profile": 123, "directory": "browser-fixture"},
    {"profile": PROFILE_PREFIX + "1" * 32 + "\n", "directory": "browser-" + "1" * 32},
])
def test_invalid_journal_cannot_delete_profiles_or_directories(tmp_path, windows, data):
    profile, directory, journal = saved_journal(tmp_path)
    journal.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        cleanup_sandboxes(tmp_path)

    assert not windows.deleted and directory.is_dir() and journal.is_file()


def test_resolved_directory_outside_capture_root_is_rejected_before_native_delete(tmp_path, windows, monkeypatch):
    root = tmp_path / "capture"
    root.mkdir()
    _, directory, journal = saved_journal(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    real_resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        return outside if path == directory else real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="межі"):
        cleanup_sandboxes(root)

    assert not windows.deleted and journal.is_file()
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("native_result", [0, -2147024894])
def test_cleanup_removes_only_recorded_profile_child_and_journal(tmp_path, windows, native_result):
    profile, directory, journal = saved_journal(tmp_path)
    unrelated = tmp_path / "other-data"
    unrelated.mkdir()
    windows.delete_result = native_result

    cleanup_sandboxes(tmp_path)

    assert windows.deleted == [profile]
    assert not directory.exists() and not journal.exists()
    assert unrelated.is_dir() and tmp_path.is_dir()


def test_native_delete_failure_preserves_recovery_journal_and_directory(tmp_path, windows):
    _, directory, journal = saved_journal(tmp_path)
    windows.delete_result = -123

    with pytest.raises(OSError, match="-123"):
        cleanup_sandboxes(tmp_path)

    assert directory.is_dir() and journal.is_file()


def test_directory_delete_failure_preserves_journal_for_retry(tmp_path, windows, monkeypatch):
    _, directory, journal = saved_journal(tmp_path)

    def fail(path):
        assert path == directory
        raise OSError("synthetic locked directory")

    monkeypatch.setattr(module.shutil, "rmtree", fail)
    with pytest.raises(OSError, match="locked directory"):
        cleanup_sandboxes(tmp_path)

    assert directory.is_dir() and journal.is_file()


def test_temporary_windows_delete_lock_retries_only_the_validated_owned_directory(tmp_path, windows, monkeypatch):
    profile, directory, journal = saved_journal(tmp_path)
    untouched = tmp_path / "unrelated"
    untouched.mkdir()
    original = module.shutil.rmtree
    attempts, waits = [], []

    def delete(path):
        attempts.append(path)
        if len(attempts) < 3:
            error = PermissionError("synthetic post-termination cache lock")
            error.winerror = 5
            raise error
        original(path)

    monkeypatch.setattr(module.shutil, "rmtree", delete)
    monkeypatch.setattr(module.time, "sleep", lambda delay: waits.append(delay))
    cleanup_sandboxes(tmp_path)
    assert attempts == [directory] * 3 and waits == [.05, .05]
    assert windows.deleted == [profile] and not journal.exists()
    assert untouched.is_dir() and not directory.exists()


def test_persistent_windows_delete_lock_stops_at_bound_and_keeps_journal(tmp_path, windows, monkeypatch):
    _, directory, journal = saved_journal(tmp_path)
    clock, attempts = [0.0], []
    failure = PermissionError("synthetic permanent sharing violation")
    failure.winerror = 32

    def delete(path):
        attempts.append(path)
        raise failure

    monkeypatch.setattr(module.shutil, "rmtree", delete)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(PermissionError) as caught:
        cleanup_sandboxes(tmp_path)
    assert caught.value is failure and clock[0] == 2.0
    assert len(attempts) <= 42 and set(attempts) == {directory}
    assert directory.is_dir() and journal.is_file()


def test_close_failure_is_retryable_and_not_marked_clean(tmp_path, windows):
    sandbox = BrowserSandbox(tmp_path)
    windows.delete_result = -123
    with pytest.raises(OSError):
        sandbox.close()
    assert not sandbox.closed and sandbox.journal.is_file()
    windows.delete_result = 0
    sandbox.close()
    assert sandbox.closed and not sandbox.journal.exists()


def test_acl_setup_failure_frees_sid_and_cleans_created_profile(tmp_path, windows):
    failure = OSError("synthetic ACL setup failure")
    windows.permit_error = failure
    with pytest.raises(OSError) as caught:
        BrowserSandbox(tmp_path)
    assert caught.value is failure
    assert windows.freed == [12345]
    assert windows.deleted == [windows.created[0][0]]
    assert list(tmp_path.iterdir()) == []


def test_constructor_cleanup_failure_does_not_mask_original_setup_error(tmp_path, windows):
    failure = OSError("original ACL setup failure")
    windows.permit_error = failure
    windows.delete_result = -123

    with pytest.raises(OSError) as caught:
        BrowserSandbox(tmp_path)

    assert caught.value is failure
    assert any("-123" in note for note in caught.value.__notes__)
    assert windows.freed == [12345]
    assert list(tmp_path.glob("sandbox-*.json"))


def test_context_cleanup_failure_preserves_primary_exception(tmp_path, windows):
    original = RuntimeError("original screenshot failure")
    with pytest.raises(RuntimeError) as caught:
        with BrowserSandbox(tmp_path):
            windows.delete_result = -123
            raise original
    assert caught.value is original
    assert any("-123" in note for note in original.__notes__)


def test_launch_uses_only_isolating_wrapper_and_private_profile(tmp_path, windows, monkeypatch):
    edge = tmp_path / "Microsoft Edge/msedge.exe"
    monkeypatch.setattr(module, "_edge_executable", lambda: edge)
    sandbox = BrowserSandbox(tmp_path)
    calls = []
    prepared = []
    monkeypatch.setattr(sandbox, "_prepare_runtime", lambda: prepared.append(True))
    chromium = SimpleNamespace(launch=lambda **kwargs: calls.append(kwargs) or "browser")
    try:
        assert sandbox.launch(chromium, timeout_ms=4321) == "browser"
        assert len(calls) == 1
        options = calls[0]
        assert options["executable_path"] == getattr(sys, "_base_executable", sys.executable)
        assert options["ignore_default_args"] is True and options["timeout"] == 4321
        args = options["args"]
        assert args[:5] == ["-B", str(Path(module.__file__).with_name("browser_driver.py")),
                            sandbox.profile, str(sandbox.directory), str(edge)]
        assert prepared == [True]
        assert "--remote-debugging-port=0" in args
        assert "--remote-debugging-pipe" not in args
        assert "--user-data-dir=" + str(sandbox.directory / "profile") in args
        assert "--headless" in args
        assert args[-2:] == ["--dump-dom", "about:blank"]
        assert "--no-startup-window" not in args
        assert "--no-proxy-server" in args
        assert not any(value.startswith("--remote-allow-origins") for value in args)
        assert options["handle_sigint"] is options["handle_sigterm"] is options["handle_sighup"] is False
    finally:
        sandbox.close()


def test_failed_isolated_launch_has_no_unsandboxed_fallback(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(module, "_edge_executable", lambda: tmp_path / "edge.exe")
    sandbox = BrowserSandbox(tmp_path)
    calls = []
    monkeypatch.setattr(sandbox, "_prepare_runtime", lambda: None)
    failure = OSError("isolated launch failed")

    def fail(**kwargs):
        calls.append(kwargs)
        raise failure

    try:
        with pytest.raises(OSError) as caught:
            sandbox.launch(SimpleNamespace(launch=fail))
        assert caught.value is failure and len(calls) == 1
        assert calls[0]["executable_path"] != str(tmp_path / "edge.exe")
    finally:
        sandbox.close()


def test_instance_close_preserves_another_active_sandbox_in_same_root(tmp_path, windows):
    first, second = BrowserSandbox(tmp_path), BrowserSandbox(tmp_path)
    first.close()
    assert windows.deleted == [first.profile]
    assert second.directory.is_dir() and second.journal.is_file()
    second.close()
    assert windows.deleted == [first.profile, second.profile]


def test_failed_temporary_root_cleanup_keeps_recovery_journal_after_gc(tmp_path, windows, monkeypatch):
    import gc
    monkeypatch.delenv("LAZYREF_CAPTURE_ROOT", raising=False)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    sandbox = BrowserSandbox()
    journal, directory = sandbox.journal, sandbox.directory
    windows.delete_result = -123
    with pytest.raises(OSError):
        sandbox.close()
    del sandbox
    gc.collect()
    assert journal.is_file() and directory.is_dir()
    windows.delete_result = 0
    cleanup_sandboxes(tmp_path)


def test_single_journal_selector_rejects_path_outside_root_before_delete(tmp_path, windows):
    root = tmp_path / "capture"
    root.mkdir()
    _, directory, journal = saved_journal(tmp_path)
    with pytest.raises(ValueError):
        cleanup_sandboxes(root, journal=journal)
    assert not windows.deleted and directory.is_dir() and journal.is_file()


def test_runtime_uses_installed_playwright_node_copy_only(tmp_path, windows, monkeypatch):
    import playwright
    copies = []
    monkeypatch.setattr(module.shutil, "copyfile", lambda source, target: copies.append((source, target)))
    with BrowserSandbox(tmp_path) as sandbox:
        sandbox._prepare_runtime()
        assert copies == [(Path(playwright.__file__).parent / "driver/node.exe", sandbox.directory / "node.exe")]


@pytest.mark.parametrize("content", [
    "0\n/devtools/browser/12345678-1234-1234-1234-123456789abc",
    "65536\n/devtools/browser/12345678-1234-1234-1234-123456789abc",
    "127.0.0.1:1234\n/devtools/browser/12345678-1234-1234-1234-123456789abc",
    "1234\n//outside.invalid/devtools/browser/12345678-1234-1234-1234-123456789abc",
    "1234\n/devtools/browser/12345678-1234-1234-1234-123456789abc?next=outside",
    "1234\n/devtools/browser/12345678-1234-1234-1234-123456789abc\nextra",
    "1234\n/devtools/browser/" + "a" * 600,
])
def test_debug_endpoint_cannot_redirect_relay_or_exceed_bounds(tmp_path, content):
    path = tmp_path / "DevToolsActivePort"
    path.write_text(content, encoding="ascii")
    with pytest.raises(ValueError):
        module._read_endpoint(path)


def test_debug_endpoint_uses_literal_loopback_and_exact_browser_path(tmp_path):
    path = tmp_path / "DevToolsActivePort"
    path.write_text("1234\n/devtools/browser/12345678-1234-1234-1234-123456789abc\n", encoding="ascii")
    assert module._read_endpoint(path) == "ws://127.0.0.1:1234/devtools/browser/12345678-1234-1234-1234-123456789abc"


def test_boxed_runtime_does_not_inherit_proxy_node_hooks_or_crash_targets(tmp_path, monkeypatch):
    ambient = {"Path": "ordinary-path", "SystemRoot": "ordinary-root", "TEMP": "outside",
               "node_options": "fixture", "NODE_EXTRA_CA_CERTS": "fixture", "NODE_USE_ENV_PROXY": "1",
               "HTTP_PROXY": "fixture", "https_proxy": "fixture", "ALL_PROXY": "fixture", "NO_PROXY": "fixture",
               "CHROME_CRASHPAD_PIPE_NAME": "fixture", "CHROME_CRASHPAD_SERVER_URL": "fixture",
               "BREAKPAD_DUMP_LOCATION": "fixture"}
    monkeypatch.setattr(module.os, "environ", ambient)
    result = module._sandbox_environment(tmp_path)
    assert result == {"Path": "ordinary-path", "SystemRoot": "ordinary-root", "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    assert ambient["TEMP"] == "outside" and "node_options" in ambient
