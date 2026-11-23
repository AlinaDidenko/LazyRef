"""Opt-in native Windows tests: synthetic pages, loopback canaries, no websites.

Prefer tools/run_native_browser_tests.py for its watchdog and owned cleanup.
Direct pytest requires LAZYREF_NATIVE_BROWSER_TESTS=1. Ordinary offline tests
exclude this module because real Edge verifies the OS boundary.
"""

import base64
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
import time

import pytest
from PIL import Image

from lazyref.access import AccessPolicy, SourceBlocked
from lazyref.capture import BrowserCapture
from lazyref.models import Candidate, Cancelled
from lazyref.network import FetchedResource


pytestmark = [pytest.mark.native_browser, pytest.mark.skipif(
    os.name != "nt" or os.environ.get("LAZYREF_NATIVE_BROWSER_TESTS") != "1",
    reason="Opt-in native AppContainer/Edge tests")]

BASE = "http://capture.fixture.invalid"
IMAGE_URL = BASE + "/target.png"


def image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (420, 320), "#448844").save(buffer, "PNG")
    return buffer.getvalue()


def candidate(path="/page"):
    return Candidate("fixture", "Synthetic photo", image_url=IMAGE_URL, page_url=BASE + path)


def assert_photo(data):
    with Image.open(BytesIO(data)) as image:
        assert abs(image.width - 420) <= 1 and abs(image.height - 320) <= 1
        assert image.getpixel((10, 10))[:3] == (68, 136, 68)


@pytest.fixture(scope="module")
def renderer():
    capture = BrowserCapture(Event())
    try:
        yield capture
    finally:
        capture.close()


@pytest.fixture
def resources(renderer, monkeypatch):
    rows = {IMAGE_URL: FetchedResource(IMAGE_URL, image_bytes(), "image/png")}
    requests = []
    diagnostics = []
    original_context = renderer.context

    def diagnostic_context(**kwargs):
        context = original_context(**kwargs)

        def page_created(page):
            page.on("console", lambda message: diagnostics.append(message.text))
            page.on("pageerror", lambda error: diagnostics.append(str(error)))

        context.on("page", page_created)
        return context

    def fetch(url, **kwargs):
        requests.append((url, kwargs))
        if url not in rows:
            raise ValueError("Fixture forbids unknown resource: " + url)
        return rows[url]

    monkeypatch.setattr(renderer.network, "fetch_browser", fetch)
    monkeypatch.setattr(renderer, "context", diagnostic_context)
    renderer.access.blocked.clear()
    try:
        yield rows, requests
    finally:
        print("Synthetic resource requests:", [url for url, _ in requests])
        print("Synthetic page diagnostics:", diagnostics)


def html(rows, path, body):
    url = BASE + path
    rows[url] = FetchedResource(url, body.encode(), "text/html; charset=utf-8")


def test_real_browser_captures_image_inserted_later_by_javascript(renderer, resources):
    rows, requests = resources
    html(rows, "/late", """<h1>Synthetic gallery</h1><script>
        setTimeout(() => { const image = new Image(); image.src = '/target.png';
            image.width = 420; image.height = 320; document.body.appendChild(image); }, 250);
        </script>""")
    assert_photo(renderer.capture_local(candidate("/late"), deadline=time.monotonic() + 20))
    assert any(url == IMAGE_URL for url, _ in requests)


def test_real_browser_scrolls_target_to_trigger_lazy_image_loading(renderer, resources):
    rows, _ = resources
    html(rows, "/lazy", """<div style="height:1600px">Scroll fixture</div>
        <img width="420" height="320" data-src="/target.png">
        <script>
        const photo = document.querySelector('img');
        const observer = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) {
                photo.src = photo.dataset.src; observer.disconnect();
            }
        }); observer.observe(photo);
        </script>""")
    assert_photo(renderer.capture_local(candidate("/lazy"), deadline=time.monotonic() + 20))


def test_real_browser_preserves_module_redirect_relative_import_and_cors(renderer, resources):
    rows, requests = resources
    html(rows, "/modules", '<script type="module" src="/old/main.js"></script>')
    rows[BASE + "/old/main.js"] = FetchedResource(BASE + "/old/main.js", b"", "text/plain", 302,
                                                   {"location": BASE + "/new/main.js"})
    script = b"""import {load} from './loader.js'; await load();"""
    rows[BASE + "/new/main.js"] = FetchedResource(BASE + "/new/main.js", gzip.compress(script),
                                                  "application/javascript", 200, {"content-encoding": "gzip"})
    loader = b"""export async function load() {
        const response = await fetch('http://data.fixture.invalid/photo.json');
        if (response.status !== 200) throw new Error('Bad response');
        const payload = await response.json(); const image = new Image();
        image.src = payload.url; image.width = 420; image.height = 320;
        document.body.appendChild(image);
    }"""
    rows[BASE + "/new/loader.js"] = FetchedResource(BASE + "/new/loader.js", loader, "application/javascript")
    rows["http://data.fixture.invalid/photo.json"] = FetchedResource(
        "http://data.fixture.invalid/photo.json", json.dumps({"url": IMAGE_URL}).encode(), "application/json", 200,
        {"access-control-allow-origin": BASE})
    assert_photo(renderer.capture_local(candidate("/modules"), deadline=time.monotonic() + 20))
    urls = [url for url, _ in requests]
    assert BASE + "/old/main.js" in urls and BASE + "/new/main.js" in urls
    assert BASE + "/new/loader.js" in urls and BASE + "/old/loader.js" not in urls
    api_call = next(kwargs for url, kwargs in requests if url == "http://data.fixture.invalid/photo.json")
    assert {name.lower(): value for name, value in api_call["headers"].items()}.get("origin") == BASE


def test_real_browser_reports_online_to_dynamic_page_using_the_checked_broker(renderer, resources):
    rows, _ = resources
    html(rows, "/online", """<body><script>if (navigator.onLine) {
        const image = new Image(); image.src = '/target.png';
        image.width = 420; image.height = 320; document.body.appendChild(image);
    }</script>""")
    assert_photo(renderer.capture_local(candidate("/online"), deadline=time.monotonic() + 5))


def test_real_browser_still_skips_captcha_and_overlay(renderer, resources):
    rows, _ = resources
    html(rows, "/captcha", '<h1>Verify you are human</h1><img src="/target.png" width="420" height="320">')
    with pytest.raises(SourceBlocked):
        renderer.capture_local(candidate("/captcha"), deadline=time.monotonic() + 5)
    renderer.access.blocked.clear()
    html(rows, "/overlay", '<img src="/target.png" width="420" height="320">'
         '<div style="position:fixed;inset:0;background:white;z-index:99999">Overlay fixture</div>')
    with pytest.raises(ValueError, match="перекриття"):
        renderer.capture_local(candidate("/overlay"), deadline=time.monotonic() + 5)


class Canary:
    def __init__(self, family):
        host = "127.0.0.1" if family == socket.AF_INET else "::1"
        self.stop = Event()
        self.received = []
        self.threads = []
        self.sockets = []
        try:
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                listener = socket.socket(family, kind)
                self.sockets.append(listener)
                listener.bind((host, 0))
                listener.settimeout(0.1)
                if kind == socket.SOCK_STREAM:
                    listener.listen()
                thread = Thread(target=self.listen, args=(listener, kind), daemon=True)
                thread.start()
                self.threads.append(thread)
        except OSError:
            self.close()
            raise

    def listen(self, listener, kind):
        while not self.stop.is_set():
            try:
                if kind == socket.SOCK_STREAM:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(0.2)
                        self.received.append(("tcp", connection.recv(4096)))
                        try:
                            connection.sendall(b"HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 2\r\n\r\nOK")
                        except OSError:
                            pass
                else:
                    packet, _ = listener.recvfrom(4096)
                    self.received.append(("udp", packet))
            except socket.timeout:
                pass
            except OSError:
                if not self.stop.is_set():
                    self.received.append(("listener-error", b""))
                return

    def positive_control(self):
        with socket.socket(self.sockets[0].family, socket.SOCK_STREAM) as probe:
            probe.connect(self.sockets[0].getsockname())
            probe.sendall(b"positive-tcp")
            probe.recv(4096)
        with socket.socket(self.sockets[1].family, socket.SOCK_DGRAM) as probe:
            probe.sendto(b"positive-udp", self.sockets[1].getsockname())
        limit = time.monotonic() + 2
        while len(self.received) < 2 and time.monotonic() < limit:
            time.sleep(0.01)
        assert sorted(self.received) == [("tcp", b"positive-tcp"), ("udp", b"positive-udp")]
        self.received.clear()

    def close(self):
        self.stop.set()
        for listener in self.sockets:
            listener.close()
        for thread in self.threads:
            thread.join(timeout=1)


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6], ids=["IPv4", "IPv6"])
def test_real_appcontainer_blocks_direct_tcp_workers_and_webrtc_udp(renderer, family):
    try:
        canary = Canary(family)
    except OSError:
        if family == socket.AF_INET6:
            pytest.skip("IPv6 loopback unavailable")
        raise
    context = None
    try:
        canary.positive_control()
        renderer._start(deadline=time.monotonic() + 20)
        # A fresh context has neither CDP Fetch nor WebSocket interception.
        # This verifies the OS boundary with browser networking fully enabled.
        context = renderer.browser.new_context(java_script_enabled=True,
            service_workers="block", offline=False)
        page = context.new_page()
        page.set_content("<h1>Local network isolation probe</h1>")
        host = "127.0.0.1" if family == socket.AF_INET else "[::1]"
        tcp_url = f"http://{host}:{canary.sockets[0].getsockname()[1]}/probe"
        stun_url = f"stun:{host}:{canary.sockets[1].getsockname()[1]}"
        flags = page.evaluate("""async ({url, stun}) => {
            const flags = {mainStarted: false, workerStarted: false, rtcOffered: false};
            window.testPeer = new RTCPeerConnection({iceServers: [{urls: stun}]});
            window.testPeer.createDataChannel('isolation');
            await window.testPeer.setLocalDescription(await window.testPeer.createOffer());
            flags.rtcOffered = true;
            flags.mainStarted = true;
            fetch(url, {signal: AbortSignal.timeout(800)}).then(() => flags.mainReached = true)
                .catch(error => flags.mainError = error.name);
            const code = `postMessage('started'); fetch(${JSON.stringify(url)}, {signal: AbortSignal.timeout(800)})
                .then(() => postMessage('reached')).catch(error => postMessage(error.name));`;
            window.testWorker = new Worker(URL.createObjectURL(new Blob([code], {type:'application/javascript'})));
            window.testWorker.onmessage = event => {
                if (event.data === 'started') flags.workerStarted = true;
                else if (event.data === 'reached') flags.workerReached = true;
                else flags.workerError = event.data;
            };
            await new Promise(resolve => setTimeout(resolve, 1200));
            window.testPeer.close(); window.testWorker.terminate();
            return flags;
        }""", {"url": tcp_url, "stun": stun_url})
        assert flags["mainStarted"] and flags["workerStarted"] and flags["rtcOffered"]
        assert not flags.get("mainReached") and not flags.get("workerReached")
        assert flags.get("mainError") and flags.get("workerError")
        assert canary.received == []
    finally:
        if context:
            context.close()
        canary.close()


@pytest.mark.parametrize("public_origin", [False, True], ids=["opaque-origin", "public-origin"])
def test_real_page_and_worker_cannot_connect_to_browser_control_websocket(renderer, record_property, public_origin):
    """Even a disclosed same-container CDP URL must reject untrusted origins."""
    renderer._start(deadline=time.monotonic() + 20)
    active_port = renderer.sandbox.directory / "profile" / "DevToolsActivePort"
    lines = active_port.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].isdigit()
    port = int(lines[0])
    assert 1 <= port <= 65535
    assert lines[1].startswith("/devtools/browser/")
    assert all(char.isalnum() or char in "/-" for char in lines[1])
    websocket_url = f"ws://127.0.0.1:{port}{lines[1]}"
    # Use the real sandboxed browser with no Fetch/WebSocket interception and
    # networking enabled, so the check cannot pass because of broker routing.
    context = renderer.browser.new_context(java_script_enabled=True,
        service_workers="block", offline=False)
    console = []
    try:
        page = context.new_page()
        page.on("console", lambda message: console.append(message.text))
        content = "<h1>Local browser-control isolation probe</h1>"
        if public_origin:
            fixture_url = BASE + "/browser-control-isolation"
            page.route("**/*", lambda route: route.fulfill(body=content, content_type="text/html")
                       if route.request.url == fixture_url else route.abort())
            page.goto(fixture_url, wait_until="domcontentloaded")
            page.unroute("**/*")
            assert page.evaluate("location.origin") == BASE
        else:
            page.set_content(content)
        flags = page.evaluate("""async url => {
            function probe(address) {
                return new Promise(resolve => {
                    const result = {started: true, opened: false, replied: false, error: false};
                    let socket;
                    const timer = setTimeout(() => {
                        result.timedOut = true; if (socket) socket.close(); resolve(result);
                    }, 2500);
                    try {
                        socket = new WebSocket(address);
                        socket.onopen = () => {
                            result.opened = true;
                            socket.send(JSON.stringify({id: 1, method: 'Browser.getVersion'}));
                        };
                        socket.onmessage = event => {
                            try {
                                const message = JSON.parse(event.data);
                                if (message.id === 1) result.replied = true;
                            } catch (_) { result.unexpectedMessage = true; }
                            socket.close();
                        };
                        socket.onerror = () => { result.error = true; };
                        socket.onclose = event => {
                            result.closeCode = event.code;
                            clearTimeout(timer); resolve(result);
                        };
                    } catch (error) {
                        result.error = true; result.errorName = error.name;
                        clearTimeout(timer); resolve(result);
                    }
                });
            }
            const source = 'const probe = ' + probe.toString() + ';' +
                'postMessage({started:true}); probe(' + JSON.stringify(url) +
                ').then(result => postMessage(result));';
            const blob = URL.createObjectURL(new Blob([source], {type: 'application/javascript'}));
            const worker = new Worker(blob);
            let workerStarted = false;
            const workerResult = new Promise(resolve => {
                worker.onmessage = event => {
                    if (!('opened' in event.data)) workerStarted = event.data.started;
                    else resolve(event.data);
                };
                worker.onerror = event => resolve({workerError: event.message});
                setTimeout(() => resolve({timedOut: true}), 3500);
            });
            const [main, child] = await Promise.all([probe(url), workerResult]);
            worker.terminate(); URL.revokeObjectURL(blob);
            return {main, worker: child, workerStarted};
        }""", websocket_url)
        # Browser console normally reports handshake 403 for its forbidden
        # Origin. Keep the exact local diagnostic when available for failures.
        console = [message.replace(websocket_url, "[local CDP endpoint]") for message in console]
        record_property("cdp_websocket_console", "\n".join(console))
        assert flags["workerStarted"], flags
        for role in ("main", "worker"):
            result = flags[role]
            assert result.get("started") and result.get("error"), (role, flags, console)
            assert not result.get("timedOut"), (role, flags, console)
            assert not result.get("opened") and not result.get("replied"), (role, flags, console)
            assert not result.get("unexpectedMessage"), (role, flags, console)
    finally:
        context.close()


def fixture_worker_script():
    png = base64.b64encode(image_bytes()).decode("ascii")
    return f"""
import base64
import os
from pathlib import Path
from lazyref.network import Network, FetchedResource
from lazyref.capture_process import worker
PNG = base64.b64decode({png!r})
def fetch(self, url, **kwargs):
    if url.endswith('/target.png'):
        return FetchedResource(url, PNG, 'image/png')
    if url.endswith('/started'):
        (Path(os.environ['LAZYREF_CAPTURE_ROOT']) / 'js-started').write_text('started', encoding='ascii')
        return FetchedResource(url, b'ok', 'text/plain')
    if url.endswith('/ready'):
        body = b'<img src="/target.png" width="420" height="320">'
    elif url.endswith('/hang'):
        body = b'''<script>const signal = new XMLHttpRequest();
            signal.open('GET', '/started', false); signal.send(); while (true) {{}}</script>'''
    else:
        raise ValueError('Unknown offline fixture')
    return FetchedResource(url, body, 'text/html')
Network.fetch_browser = fetch
worker()
"""


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
def test_public_capture_process_stops_hung_javascript_and_cleans_owned_tree(monkeypatch, interruption):
    import lazyref.capture_process as module
    actual_popen = module.subprocess.Popen
    script = fixture_worker_script()

    def popen(args, **kwargs):
        assert "lazyref.capture_process" in args
        return actual_popen([sys.executable, "-B", "-X", "utf8", "-c", script], **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    cancel = Event()
    controller = module.CaptureProcess(cancel, AccessPolicy())
    observer = None
    observer_stop, script_started = Event(), Event()
    cancelled_at = []
    try:
        assert_photo(controller.capture(candidate("/ready"), time.monotonic() + 20))
        process, tree = controller.process, controller.tree
        temporary = Path(controller.temporary)
        marker = temporary / "js-started"

        def observe_script():
            while not observer_stop.wait(0.01):
                if marker.is_file():
                    script_started.set()
                    if interruption == "cancel" and not observer_stop.wait(0.25):
                        cancelled_at.append(time.monotonic())
                        cancel.set()
                    return

        observer = Thread(target=observe_script, daemon=True)
        observer.start()
        started = time.monotonic()
        budget = 6 if interruption == "timeout" else 20
        with pytest.raises(TimeoutError if interruption == "timeout" else Cancelled) as interrupted:
            controller.capture(candidate("/hang"), started + budget)
        assert script_started.is_set(), "The test must observe JavaScript execution before interruption"
        if interruption == "timeout":
            assert time.monotonic() - started < budget + 3
        else:
            assert cancelled_at and time.monotonic() - cancelled_at[0] < 3
        assert process.poll() is not None and tree.closed_cleanly
        assert controller.process is controller.tree is None
        assert not temporary.exists(), getattr(interrupted.value, "__notes__", [])
    finally:
        observer_stop.set()
        if observer:
            observer.join(timeout=1)
        controller.close()
