"""Windows AppContainer for the screenshot browser; no network capabilities.

The trusted Playwright driver stays outside the container. A small Node relay
inside the same container joins its inherited CDP pipes to Edge's private
loopback endpoint. No loopback exemption or network capability is granted.
Only a new temporary browser directory receives an AppContainer ACL.
"""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4


PROFILE_PREFIX = "LazyRef.Capture."
PROFILE_PATTERN = re.compile(r"LazyRef\.Capture\.[0-9a-f]{32}\Z")


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD), ("y", wintypes.DWORD), ("width", wintypes.DWORD),
        ("height", wintypes.DWORD), ("chars_x", wintypes.DWORD), ("chars_y", wintypes.DWORD),
        ("fill", wintypes.DWORD), ("flags", wintypes.DWORD), ("show", wintypes.WORD),
        ("reserved_size", wintypes.WORD), ("reserved_bytes", ctypes.c_void_p),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("thread_id", wintypes.DWORD)]


class _Capabilities(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("capabilities", ctypes.c_void_p),
                ("count", wintypes.DWORD), ("reserved", wintypes.DWORD)]


class _Trustee(ctypes.Structure):
    _fields_ = [("multiple", ctypes.c_void_p), ("operation", ctypes.c_int),
                ("form", ctypes.c_int), ("kind", ctypes.c_int), ("name", ctypes.c_void_p)]


class _Access(ctypes.Structure):
    _fields_ = [("permissions", wintypes.DWORD), ("mode", ctypes.c_int),
                ("inheritance", wintypes.DWORD), ("trustee", _Trustee)]


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Ізольований запасний скріншот підтримується лише у Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self.profiles = ctypes.WinDLL("userenv", use_last_error=True)
        P, D, H, S = ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, wintypes.LPCWSTR
        signatures = [
            (self.profiles, "CreateAppContainerProfile", [S, S, S, P, D, ctypes.POINTER(P)], ctypes.c_long),
            (self.profiles, "DeriveAppContainerSidFromAppContainerName", [S, ctypes.POINTER(P)], ctypes.c_long),
            (self.profiles, "DeleteAppContainerProfile", [S], ctypes.c_long),
            (self.security, "FreeSid", [P], P),
            (self.security, "GetNamedSecurityInfoW", [S, ctypes.c_int, D, ctypes.POINTER(P),
                ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P)], D),
            (self.security, "SetNamedSecurityInfoW", [S, ctypes.c_int, D, P, P, P, P], D),
            (self.security, "SetEntriesInAclW", [D, ctypes.POINTER(_Access), P, ctypes.POINTER(P)], D),
            (self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                [S, D, ctypes.POINTER(P), P], wintypes.BOOL),
            (self.security, "GetSecurityDescriptorSacl", [P, ctypes.POINTER(wintypes.BOOL),
                ctypes.POINTER(P), ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            (self.security, "OpenProcessToken", [H, D, ctypes.POINTER(H)], wintypes.BOOL),
            (self.security, "GetTokenInformation", [H, ctypes.c_int, P, D, ctypes.POINTER(D)], wintypes.BOOL),
            (self.security, "EqualSid", [P, P], wintypes.BOOL),
            (self.kernel, "LocalFree", [P], P),
            (self.kernel, "CloseHandle", [H], wintypes.BOOL),
            (self.kernel, "GetStartupInfoW", [ctypes.POINTER(_StartupInfo)], None),
            (self.kernel, "InitializeProcThreadAttributeList", [P, D, D, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
            (self.kernel, "UpdateProcThreadAttribute", [P, D, ctypes.c_size_t, P, ctypes.c_size_t, P, P], wintypes.BOOL),
            (self.kernel, "DeleteProcThreadAttributeList", [P], None),
            (self.kernel, "CreateProcessW", [S, wintypes.LPWSTR, P, P, wintypes.BOOL, D, P, S,
                ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
            (self.kernel, "ResumeThread", [H], D),
            (self.kernel, "WaitForSingleObject", [H, D], D),
            (self.kernel, "GetExitCodeProcess", [H, ctypes.POINTER(D)], wintypes.BOOL),
            (self.kernel, "TerminateProcess", [H, wintypes.UINT], wintypes.BOOL),
            (self.kernel, "PeekNamedPipe", [H, P, D, ctypes.POINTER(D), ctypes.POINTER(D),
                ctypes.POINTER(D)], wintypes.BOOL),
        ]
        for dll, name, arguments, result in signatures:
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    @staticmethod
    def status(value):
        if value:
            raise OSError(f"Windows AppContainer: 0x{value & 0xffffffff:08x}")

    def permit_directory(self, path, sid):
        """Preserve existing permissions, granting only the new package access."""
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        new_acl, label = ctypes.c_void_p(), ctypes.c_void_p()
        self.status(self.security.GetNamedSecurityInfoW(str(path), 1, 1 | 4,
            ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor)))
        try:
            entries = (_Access * 2)()
            for entry, identity in zip(entries, (owner, sid)):
                entry.permissions, entry.mode, entry.inheritance = 0x1F01FF, 1, 3
                entry.trustee = _Trustee(None, 0, 0, 0, identity)
            self.status(self.security.SetEntriesInAclW(2, entries, acl, ctypes.byref(new_acl)))
            self.status(self.security.SetNamedSecurityInfoW(str(path), 1, 4, None, None, new_acl, None))
            self.check(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "S:(ML;OICI;NW;;;LW)", 1, ctypes.byref(label), None))
            present, defaulted, label_acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            self.check(self.security.GetSecurityDescriptorSacl(label, ctypes.byref(present),
                ctypes.byref(label_acl), ctypes.byref(defaulted)))
            self.status(self.security.SetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, label_acl))
        finally:
            for value in (label, new_acl, descriptor):
                if value:
                    self.kernel.LocalFree(value)


def _edge_executable():
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
        if base:
            candidate = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if candidate.is_file():
                return candidate
    raise RuntimeError("Для ізольованого запасного скріншота потрібен Microsoft Edge.")


def _remove_sandbox_directory(path):
    # A killed browser's cache can briefly reject deletion even after its Job
    # becomes empty. Retry native access/sharing errors for at most two seconds;
    # keep permissions unchanged and retain the journal if recovery still fails.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            remaining = deadline - time.monotonic()
            if getattr(exc, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                raise
            time.sleep(min(.05, remaining))


def cleanup_sandboxes(root, *, journal=None):
    """Parent recovery after its owned worker tree has completely stopped."""
    root = Path(root).resolve()
    windows = None
    if journal is not None:
        journal = Path(journal).resolve()
        if journal.parent != root or not re.fullmatch(r"sandbox-[0-9a-f]{32}\.json", journal.name):
            raise ValueError("Некоректний контрольний запис ізольованого браузера.")
        journals = [journal] if journal.exists() else []
    else:
        journals = root.glob("sandbox-*.json")
    for journal in journals:
        data = json.loads(journal.read_text(encoding="utf-8"))
        name, directory = data.get("profile", ""), data.get("directory", "")
        if (not isinstance(name, str) or not PROFILE_PATTERN.fullmatch(name)
                or directory != "browser-" + name.removeprefix(PROFILE_PREFIX)):
            raise ValueError("Некоректний контрольний запис ізольованого браузера.")
        path = (root / directory).resolve()
        if path.parent != root:
            raise ValueError("Тимчасовий браузерний каталог виходить за межі свого запуску.")
        windows = windows or _Windows()
        result = windows.profiles.DeleteAppContainerProfile(name)
        if result not in (0, -2147024894):  # A profile may not have been created.
            windows.status(result)
        if path.exists():
            _remove_sandbox_directory(path)
        journal.unlink(missing_ok=True)


class BrowserSandbox:
    def __init__(self, root=None):
        self.windows = _Windows()
        self._temporary = None
        if root is None:
            root = os.environ.get("LAZYREF_CAPTURE_ROOT")
        if root is None:
            self._temporary = Path(tempfile.mkdtemp(prefix="lazyref-capture-"))
            root = self._temporary
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        identifier = uuid4().hex
        self.profile = PROFILE_PREFIX + identifier
        self.directory = self.root / ("browser-" + identifier)
        self.journal = self.root / ("sandbox-" + identifier + ".json")
        self.closed = False
        self.directory.mkdir()
        self.journal.write_text(json.dumps({"profile": self.profile, "directory": self.directory.name}), encoding="utf-8")
        sid = ctypes.c_void_p()
        try:
            self.windows.status(self.windows.profiles.CreateAppContainerProfile(self.profile, "LazyRef screenshot",
                "Temporary browser without network capabilities", None, 0, ctypes.byref(sid)))
            self.windows.permit_directory(self.directory, sid)
        except BaseException as primary:
            try:
                self.close()
            except Exception as exc:
                primary.add_note("Очищення ізольованого браузера: " + str(exc))
            raise
        finally:
            if sid:
                self.windows.security.FreeSid(sid)

    def launch_kwargs(self, timeout_ms=15000):
        args = ["-B", str(Path(__file__).with_name("browser_driver.py")), self.profile,
                str(self.directory), str(_edge_executable()),
                "--headless", "--no-sandbox", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update", "--disable-edgeupdater",
                "--disable-extensions", "--disable-sync", "--disable-gpu", "--disable-breakpad",
                "--disable-crash-reporter", "--disable-features=Translate,AutoDeElevate,msEdgeUpdateLaunchServicesPreferredVersion",
                "--enable-automation", "--edge-skip-compat-layer-relaunch", "--remote-debugging-port=0",
                "--no-proxy-server", "--user-data-dir=" + str(self.directory / "profile"),
                # This finite built-in headless command initializes Edge's
                # renderer inside AppContainer before remote target creation.
                "--dump-dom", "about:blank"]
        # A venv's Windows redirector may not preserve CRT fd 3/4. The helper
        # imports only stdlib/project code, so use the actual Python binary.
        return {"executable_path": getattr(sys, "_base_executable", sys.executable), "ignore_default_args": True, "args": args,
                "env": {**os.environ, "LAZYREF_BROWSER_START_TIMEOUT_MS": str(max(1, int(timeout_ms)))},
                "timeout": timeout_ms, "handle_sigint": False, "handle_sigterm": False, "handle_sighup": False}

    def _prepare_runtime(self):
        # Reuse the installed Playwright runtime; never install or download one.
        import playwright
        source = Path(playwright.__file__).parent / "driver" / "node.exe"
        shutil.copyfile(source, self.directory / "node.exe")

    def launch(self, chromium, timeout_ms=15000):
        self._prepare_runtime()
        return chromium.launch(**self.launch_kwargs(timeout_ms))

    def close(self):
        if self.closed:
            return
        # Do not mark clean on failure; the parent can retry from the journal.
        cleanup_sandboxes(self.root, journal=self.journal)
        if self._temporary:
            self._temporary.rmdir()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, primary, traceback):
        try:
            self.close()
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note("Очищення ізольованого браузера: " + str(exc))


def _read_endpoint(path):
    with Path(path).open("r", encoding="ascii") as stream:
        value = stream.read(513)
    lines = value.splitlines()
    if (len(value) > 512 or len(lines) != 2 or not lines[0].isascii()
            or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535
            or not re.fullmatch(r"/devtools/browser/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", lines[1])):
        raise ValueError("Некоректна приватна адреса керування браузером.")
    return "ws://127.0.0.1:" + lines[0] + lines[1]


def _sandbox_environment(directory):
    excluded = {"NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY", "HTTP_PROXY",
        "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "CHROME_CRASHPAD_PIPE_NAME",
        "CHROME_CRASHPAD_SERVER_URL", "BREAKPAD_DUMP_LOCATION"}
    environment = {name: value for name, value in os.environ.items() if name.upper() not in excluded}
    environment.update(TEMP=str(directory), TMP=str(directory))
    return environment


# Node has WebSocket built in. This endpoint is accessible only to this package
# SID. Chromium's default Origin check rejects cross-origin page/worker access
# to the debugging socket; do not add --remote-allow-origins.
_RELAY_SCRIPT = r"""
const fs = require('node:fs');
const net = require('node:net');
const endpoint = process.argv[1];
if (!/^ws:\/\/127\.0\.0\.1:[0-9]+\/devtools\/browser\/[0-9a-f-]+$/.test(endpoint)) process.exit(1);
const websocket = new WebSocket(endpoint);
// fd 3 is Playwright's overlapped pipe. A pipe stream keeps its read on the
// event loop; fs.createReadStream would leave a blocking thread-pool read that
// prevents Node from exiting after Browser.close.
const input = new net.Socket({fd: 3, readable: true, writable: false});
input.pause();
let pending = Buffer.alloc(0);
let closingId;
const limit = 128 * 1024 * 1024;
websocket.addEventListener('open', () => input.resume());
input.on('data', chunk => {
  pending = Buffer.concat([pending, chunk]);
  if (pending.length > limit) process.exit(1);
  let end;
  while ((end = pending.indexOf(0)) !== -1) {
    const message = pending.subarray(0, end).toString('utf8');
    const request = JSON.parse(message);
    if (request.method === 'Browser.close') closingId = request.id;
    websocket.send(message);
    pending = pending.subarray(end + 1);
  }
});
input.on('end', () => process.exit(0));
input.on('error', () => process.exit(1));
websocket.addEventListener('message', event => {
  if (typeof event.data !== 'string') process.exit(1);
  const data = Buffer.from(event.data + '\0');
  if (data.length > limit) process.exit(1);
  let offset = 0;
  while (offset < data.length) offset += fs.writeSync(4, data, offset, data.length - offset);
  if (closingId !== undefined && JSON.parse(event.data).id === closingId) process.exit(0);
});
websocket.addEventListener('error', () => process.exit(1));
websocket.addEventListener('close', () => process.exit(0));
"""


def _spawn_boxed(windows, sid, job, executable, args, directory, environment, descriptor_count, stdout_handle=None):
    """Both renderer and relay start suspended, enter the Job and validate token."""
    import msvcrt
    import struct
    handles = [msvcrt.get_osfhandle(index) for index in range(descriptor_count)]
    if stdout_handle is not None:
        handles[1] = stdout_handle
    if any(handle in (-1, -2) for handle in handles):
        raise OSError("Playwright не передав потрібні CDP pipes.")
    for handle in set(handles):
        os.set_handle_inheritable(handle, True)
    inherited = (wintypes.HANDLE * len(set(handles)))(*sorted(set(handles)))
    # MSVCRT inherits fd 3/4 through STARTUPINFO.lpReserved2. Node can use
    # these handles inside AppContainer, unlike Edge's own pipe transport.
    reserved = ctypes.create_string_buffer(struct.pack('<I', descriptor_count)
        + bytes([0x41] + [9] * (descriptor_count - 1))
        + b''.join(struct.pack('<Q' if ctypes.sizeof(wintypes.HANDLE) == 8 else '<I', handle) for handle in handles))
    startup = _StartupInfoEx()
    startup.startup.cb = ctypes.sizeof(_StartupInfoEx)
    startup.startup.flags = 0x100
    startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles[:3]
    startup.startup.reserved_size = len(reserved) - 1
    startup.startup.reserved_bytes = ctypes.addressof(reserved)
    capabilities = _Capabilities(sid, None, 0, 0)
    size = ctypes.c_size_t()
    windows.kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    process = _ProcessInfo()
    initialized = success = False
    try:
        windows.check(windows.kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)))
        initialized = True
        startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        windows.check(windows.kernel.UpdateProcThreadAttribute(attributes, 0, 0x20009,
            ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None))
        windows.check(windows.kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002,
            inherited, ctypes.sizeof(inherited), None, None))
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *args]))
        block = ctypes.create_unicode_buffer("\0".join(key + "=" + value
            for key, value in sorted(environment.items())) + "\0\0")
        windows.check(windows.kernel.CreateProcessW(str(executable), command, None, None, True,
            0x80000 | 0x08000000 | 0x400 | 4, block, str(directory), ctypes.byref(startup), ctypes.byref(process)))
        job.attach(SimpleNamespace(_handle=process.process, pid=process.pid))
        token = wintypes.HANDLE()
        windows.check(windows.security.OpenProcessToken(process.process, 8, ctypes.byref(token)))
        try:
            value, length = wintypes.DWORD(), wintypes.DWORD()
            windows.check(windows.security.GetTokenInformation(token, 29, ctypes.byref(value), 4, ctypes.byref(length)))
            if value.value != 1:
                raise RuntimeError("Windows не ізолював браузер; запуск скасовано.")
            info = ctypes.create_string_buffer(4096)
            windows.check(windows.security.GetTokenInformation(token, 30, info, len(info), ctypes.byref(length)))
            if ctypes.cast(info, ctypes.POINTER(wintypes.DWORD)).contents.value != 0:
                raise RuntimeError("AppContainer отримав неочікувані можливості; запуск скасовано.")
            windows.check(windows.security.GetTokenInformation(token, 31, info, len(info), ctypes.byref(length)))
            token_sid = ctypes.cast(info, ctypes.POINTER(ctypes.c_void_p)).contents
            if not windows.security.EqualSid(token_sid, sid):
                raise RuntimeError("Неочікуваний AppContainer SID; запуск скасовано.")
        finally:
            windows.kernel.CloseHandle(token)
        if windows.kernel.ResumeThread(process.thread) != 1:
            raise OSError("Не вдалося запустити ізольований браузер.")
        success = True
        return process.process
    finally:
        if not success and process.process:
            windows.kernel.TerminateProcess(process.process, 1)
            windows.kernel.WaitForSingleObject(process.process, 5000)
            windows.kernel.CloseHandle(process.process)
        if process.thread:
            windows.kernel.CloseHandle(process.thread)
        if initialized:
            windows.kernel.DeleteProcThreadAttributeList(attributes)


def run_browser(profile, directory, executable, args):
    """Only the two zero-capability processes share a private CDP listener."""
    import msvcrt
    from .processes import _WindowsJob
    if not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError("Некоректний профіль ізольованого браузера.")
    directory = Path(directory).resolve()
    if directory.name != "browser-" + profile.removeprefix(PROFILE_PREFIX) or not directory.is_dir():
        raise ValueError("Некоректний тимчасовий каталог браузера.")
    windows = _Windows()
    sid = ctypes.c_void_p()
    job = _WindowsJob()
    children = []
    bootstrap_read = bootstrap_write = None
    try:
        windows.status(windows.profiles.DeriveAppContainerSidFromAppContainerName(profile, ctypes.byref(sid)))
        environment = _sandbox_environment(directory)
        timeout = max(1, min(60000, int(environment.get("LAZYREF_BROWSER_START_TIMEOUT_MS", "15000")))) / 1000
        deadline = time.monotonic() + timeout
        bootstrap_read, bootstrap_write = os.pipe()
        edge = _spawn_boxed(windows, sid, job, executable, args, directory, environment, 3,
            stdout_handle=msvcrt.get_osfhandle(bootstrap_write))
        children.append(edge)
        os.close(bootstrap_write)
        bootstrap_write = None
        # The fixed about:blank command emits its DOM once its renderer works.
        # Wait without a blocking read; startup shares Playwright's deadline.
        rendered = b""
        while b"</html>" not in rendered:
            if time.monotonic() >= deadline:
                raise TimeoutError("Ізольований рендерер не запустився вчасно.")
            available = wintypes.DWORD()
            windows.check(windows.kernel.PeekNamedPipe(msvcrt.get_osfhandle(bootstrap_read),
                None, 0, None, ctypes.byref(available), None))
            if available.value:
                rendered += os.read(bootstrap_read, min(available.value, 4096))
                if len(rendered) > 65536:
                    raise RuntimeError("Неочікувана відповідь стартового рендерера.")
            elif windows.kernel.WaitForSingleObject(edge, 0) == 0:
                raise RuntimeError("Ізольований рендерер завершився під час запуску.")
            else:
                time.sleep(.01)
        active_port = directory / "profile" / "DevToolsActivePort"
        while True:
            if windows.kernel.WaitForSingleObject(edge, 0) == 0:
                raise RuntimeError("Ізольований браузер завершився до встановлення зв'язку.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Ізольований браузер не запустився вчасно.")
            try:
                endpoint = _read_endpoint(active_port)
                break
            except (OSError, ValueError):
                # Edge creates the file before finishing/releasing its writer.
                # Retry only inside the same startup deadline.
                pass
            time.sleep(.01)
        relay = _spawn_boxed(windows, sid, job, directory / "node.exe",
            ["--no-warnings", "-e", _RELAY_SCRIPT, endpoint], directory, environment, 5)
        children.append(relay)
        if windows.kernel.WaitForSingleObject(relay, 0xFFFFFFFF) != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        windows.check(windows.kernel.GetExitCodeProcess(relay, ctypes.byref(code)))
        return code.value
    finally:
        primary = sys.exception()
        errors = []
        for action in (job.terminate, lambda: job.wait_empty(10), job.close):
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        for handle in children:
            windows.kernel.CloseHandle(handle)
        for descriptor in (bootstrap_read, bootstrap_write):
            if descriptor is not None:
                os.close(descriptor)
        if sid:
            windows.security.FreeSid(sid)
        if errors:
            if primary is None:
                primary = errors.pop(0)
                for exc in errors:
                    primary.add_note("Очищення ізольованого браузера: " + str(exc))
                raise primary
            for exc in errors:
                primary.add_note("Очищення ізольованого браузера: " + str(exc))
