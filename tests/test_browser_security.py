"""Захист фонового браузера без запуску браузера або справжньої мережі."""

import base64
import gzip
import sys
from threading import Event
from types import SimpleNamespace
import zlib

import pytest

from lazyref.browser import BackgroundBrowser
from lazyref.capture import BrowserCapture
from lazyref.access import SourceBlocked
from lazyref.models import Candidate, Cancelled
from lazyref.network import FetchedResource
from test_network_security import PUBLIC_V4, address, fake_wire


class Request:
    def __init__(self, url, method="GET", headers=None, data=None, resource_type="Image", frame_id="main-frame"):
        self.event = {"requestId": str(id(self)), "resourceType": resource_type,
            "frameId": frame_id, "request": {
            "url": url, "method": method, "headers": headers or {}}}
        if data is not None:
            self.event["request"]["postData"] = data
        self.fulfilled = []
        self.aborted = False
        self.direct_calls = []


class Session:
    def __init__(self):
        self.requests = {}
        self.commands = []
        self.closed = False
        self.fail_enable = False

    def on(self, event, callback):
        assert event == "Fetch.requestPaused"
        self.on_request = callback

    def send(self, method, params=None):
        self.commands.append((method, params))
        if self.closed or (method == "Fetch.enable" and self.fail_enable):
            raise RuntimeError("Synthetic CDP session closed")
        if method == "Page.getFrameTree":
            assert params is None
            return {"frameTree": {"frame": {"id": "main-frame"}}}
        elif method == "Fetch.enable":
            assert params == {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
        elif method == "Fetch.fulfillRequest":
            request = self.requests[params["requestId"]]
            request.fulfilled.append({"status": params["responseCode"],
                "body": base64.b64decode(params["body"], validate=True),
                "headers": {item["name"].lower(): item["value"] for item in params["responseHeaders"]}})
        elif method == "Fetch.failRequest":
            assert params["errorReason"] == "Aborted"
            self.requests[params["requestId"]].aborted = True
        else:
            raise AssertionError("Chromium must never send the request itself: " + method)

    def request(self, request):
        self.requests[request.event["requestId"]] = request
        self.on_request(request.event)


class Context:
    def __init__(self):
        self.sessions = {}
        self.pages = []
        self.next_session = None
        self.closed = False

    def on(self, event, callback):
        assert event == "page"
        self.on_page = callback

    def new_cdp_session(self, page):
        session = self.next_session or Session()
        self.next_session = None
        self.sessions[id(page)] = session
        return session

    def new_page(self):
        class Page:
            closed = False
        page = Page()
        page.context = self
        page.close = lambda: setattr(page, "closed", True)
        self.pages.append(page)
        self.on_page(page)
        return page

    def close(self):
        self.closed = True

    def request(self, request, page=None):
        if page is None:
            page = self.pages[0] if self.pages else self.new_page()
        self.sessions[id(page)].request(request)

    def route_web_socket(self, pattern, callback):
        assert pattern == "**/*"
        self.on_websocket = callback


class Browser:
    def __init__(self):
        self.context_options = None
        self.context = Context()

    def new_context(self, **kwargs):
        self.context_options = kwargs
        return self.context

    def close(self):
        pass


@pytest.fixture
def background(monkeypatch):
    background = BrowserCapture(Event())
    background.browser = Browser()
    monkeypatch.setattr(background, "_start", lambda *args, **kwargs: None)
    try:
        yield background
    finally:
        background.close()


def test_context_reports_online_but_keeps_service_worker_and_websocket_guards(background):
    context = background.context()

    assert background.browser.context_options["offline"] is False
    assert background.browser.context_options["java_script_enabled"] is True
    assert background.browser.context_options["service_workers"] == "block"
    assert background.browser.context_options["accept_downloads"] is False
    websocket = SimpleNamespace(closed=False)
    websocket.close = lambda: setattr(websocket, "closed", True)
    context.on_websocket(websocket)
    assert websocket.closed
    assert context.pages == []
    page = context.new_page()
    assert [method for method, _ in context.sessions[id(page)].commands] == ["Page.getFrameTree", "Fetch.enable"]


def test_public_get_is_fulfilled_by_python_transport_only(background, monkeypatch):
    context = background.context()
    calls = []
    resource = FetchedResource("https://photos.invalid/original.jpg", b"picture", "image/jpeg", 206,
                               {"content-range": "bytes 0-6/100", "access-control-allow-origin": "https://photos.invalid"})

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return resource

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    route = Request("https://photos.invalid/original.jpg", headers={"Range": "bytes=0-6"})
    context.request(route)

    assert calls[0][0] == "https://photos.invalid/original.jpg"
    assert calls[0][1]["method"] == "GET" and calls[0][1]["headers"] == {"Range": "bytes=0-6"}
    assert route.fulfilled[0]["status"] == 206 and route.fulfilled[0]["body"] == b"picture"
    assert route.fulfilled[0]["headers"]["content-range"] == "bytes 0-6/100"
    assert route.fulfilled[0]["headers"]["access-control-allow-origin"] == "https://photos.invalid"
    assert route.fulfilled[0]["headers"]["content-type"] == "image/jpeg"
    assert not route.aborted and route.direct_calls == []


def test_cdp_response_preserves_repeated_security_headers(background, monkeypatch):
    context = background.context()
    page = context.new_page()
    pairs = (("Content-Security-Policy", "default-src 'none'"),
             ("Content-Security-Policy", "img-src https://photos.invalid"),
             ("Access-Control-Allow-Origin", "https://one.invalid"),
             ("Access-Control-Allow-Origin", "https://two.invalid"))
    resource = FetchedResource("https://photos.invalid/page", b"fixture", "text/html",
        headers={"Content-Security-Policy": "default-src *"}, header_items=pairs)
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)
    request = Request(resource.url)
    context.request(request, page)
    _, response = context.sessions[id(page)].commands[-1]
    assert response["responseHeaders"] == [
        *[{"name": name, "value": value} for name, value in pairs],
        {"name": "Content-Type", "value": "text/html"}]
    assert request.fulfilled and not request.aborted


@pytest.mark.parametrize("encoding,compressed", [
    ("gzip", gzip.compress(b"fixture")),
    ("gzip", gzip.compress(b"fix") + gzip.compress(b"ture")),
    ("deflate", zlib.compress(b"fixture")),
    ("gzip, deflate", zlib.compress(gzip.compress(b"fixture"))),
])
def test_cdp_decodes_wire_compression_and_removes_stale_headers(background, monkeypatch, encoding, compressed):
    context = background.context()
    resource = FetchedResource("https://photos.invalid/script.js", compressed, "application/javascript",
        header_items=(("Content-Encoding", encoding), ("Content-Length", str(len(compressed))),
                      ("Content-Security-Policy", "default-src 'none'")))
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)
    request = Request(resource.url)
    context.request(request)
    result = request.fulfilled[0]
    assert result["body"] == b"fixture"
    assert "content-encoding" not in result["headers"] and "content-length" not in result["headers"]
    assert result["headers"]["content-security-policy"] == "default-src 'none'"


@pytest.mark.parametrize("encoding,compressed", [
    ("gzip", b""), ("gzip", b"invalid"), ("gzip", gzip.compress(b"fixture")[:-4]),
    ("deflate", b""), ("deflate", b"invalid"), ("deflate", zlib.compress(b"fixture")[:-2]),
    ("deflate", zlib.compress(b"fixture") + b"trailing"), ("br", b"unsupported"),
])
def test_cdp_rejects_invalid_truncated_or_unsupported_compression(background, monkeypatch, encoding, compressed):
    context = background.context()
    resource = FetchedResource("https://photos.invalid/script.js", compressed, "application/javascript",
        headers={"Content-Encoding": encoding})
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)
    request = Request(resource.url)
    context.request(request)
    assert request.aborted and not request.fulfilled


def test_head_response_with_encoding_metadata_does_not_need_compressed_body(background, monkeypatch):
    context = background.context()
    resource = FetchedResource("https://photos.invalid/script.js", b"", "application/javascript",
        headers={"Content-Encoding": "gzip"})
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)
    request = Request(resource.url, method="HEAD")
    context.request(request)
    assert request.fulfilled[0]["body"] == b"" and not request.aborted


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_cdp_bounds_decoded_output_before_fulfilling(background, monkeypatch, encoding, compress):
    monkeypatch.setattr("lazyref.browser.MAX_DECODED_BYTES", 64)
    context = background.context()
    resource = FetchedResource("https://photos.invalid/script.js", compress(b"x" * 1024), "application/javascript",
        headers={"Content-Encoding": encoding})
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)
    request = Request(resource.url)
    context.request(request)
    assert request.aborted and not request.fulfilled


def test_compressed_main_document_captcha_is_blocked_before_script_execution(background, monkeypatch):
    context = background.context()
    page = context.new_page()
    page.set_default_timeout = lambda timeout: None
    url = "https://photos.invalid/page"
    resource = FetchedResource(url, gzip.compress(b"<h1>Verify you are human</h1>"), "text/html",
        headers={"Content-Encoding": "gzip"})
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: resource)

    def goto(url, **kwargs):
        request = Request(url, resource_type="Document")
        context.request(request, page)
        assert request.aborted and not request.fulfilled
        raise RuntimeError("Synthetic net::ERR_FAILED")

    page.goto = goto
    with pytest.raises(SourceBlocked):
        background.navigate(page, url)


@pytest.mark.parametrize("goto_raises", [False, True], ids=["goto-returned", "goto-failed"])
def test_main_document_captcha_after_redirect_preserves_source_block_and_original_host(background, monkeypatch, goto_raises):
    context = background.context()
    page = context.new_page()
    initial, destination = "https://photos.invalid/page", "https://gate.invalid/check"
    page.url = initial
    page.set_default_timeout = lambda timeout: None
    monkeypatch.setattr(context, "new_page", lambda: page)
    monkeypatch.setattr(background, "context", lambda **kwargs: context)
    monkeypatch.setattr(background.access, "check_page", lambda *args: pytest.fail("Blocked document must be raised before page checks"))
    monkeypatch.setattr(background, "capture_element", lambda *args, **kwargs: pytest.fail("CAPTCHA must never be captured"))

    def fetch(url, **kwargs):
        if url == initial:
            return FetchedResource(url, b"", "text/html", 302, {"location": destination})
        background.access.block(url, "CAPTCHA")

    def goto(url, **kwargs):
        first = Request(url, resource_type="Document")
        context.request(first, page)
        assert first.fulfilled[0]["status"] == 302
        redirected = Request(destination, resource_type="Document")
        context.request(redirected, page)
        assert redirected.aborted
        page.url = destination
        if goto_raises:
            raise RuntimeError("Synthetic net::ERR_FAILED")
        return SimpleNamespace(status=200)

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    page.goto = goto
    with pytest.raises(SourceBlocked) as raised:
        background.capture_local(Candidate("fixture", "fixture", page_url=initial))
    assert raised.value.url == destination and raised.value.reason == "CAPTCHA"
    assert background.access.blocked == {"gate.invalid": "CAPTCHA", "photos.invalid": "CAPTCHA"}
    assert context.closed


@pytest.mark.parametrize("resource_type,frame_id", [("Image", "main-frame"), ("Document", "child-frame")])
def test_unrelated_resource_gate_does_not_block_main_document(background, monkeypatch, resource_type, frame_id):
    context = background.context()
    page = context.new_page()
    page.url = "https://photos.invalid/page"
    page.set_default_timeout = lambda timeout: None
    checks = []
    monkeypatch.setattr(background.access, "check_page", lambda actual, status: checks.append((actual.url, status)))

    def fetch(url, **kwargs):
        background.access.block(url, "CAPTCHA")

    def goto(url, **kwargs):
        request = Request("https://advert.invalid/banner", resource_type=resource_type, frame_id=frame_id)
        context.request(request, page)
        assert request.aborted
        return SimpleNamespace(status=200)

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    page.goto = goto
    background.navigate(page, page.url)
    assert checks == [(page.url, 200)]
    assert background.access.blocked == {"advert.invalid": "CAPTCHA"}


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_navigation_interruption_takes_priority_over_recorded_document_error(background, monkeypatch, interruption):
    clock = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    context = background.context(deadline=12.0)
    page = context.new_page()
    page.set_default_timeout = lambda timeout: None

    def fetch(url, **kwargs):
        raise SourceBlocked(url, "CAPTCHA")

    def goto(url, **kwargs):
        context.request(Request(url, resource_type="Document"), page)
        if interruption == "cancel":
            background.cancel.set()
        else:
            clock[0] = 13.0
        raise RuntimeError("Synthetic net::ERR_FAILED")

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    page.goto = goto
    with pytest.raises(Cancelled if interruption == "cancel" else TimeoutError):
        background.navigate(page, "https://photos.invalid/page", deadline=12.0)


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_read_and_preflight_requests_use_the_same_pinned_transport(background, monkeypatch, method):
    context = background.context()
    calls = []

    def fetch(url, **kwargs):
        calls.append(kwargs["method"])
        return FetchedResource(url, b"", "text/plain", 204)

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    route = Request("https://photos.invalid/resource", method=method)
    context.request(route)
    assert calls == [method]
    assert route.fulfilled[0]["status"] == 204 and not route.direct_calls


@pytest.mark.parametrize("url,method", [
    ("file:///C:/private.png", "GET"),
    ("ftp://photos.invalid/image.jpg", "GET"),
    ("data:text/html,private", "GET"),
    ("http://127.0.0.1/private", "GET"),
    ("http://[::1]/private", "GET"),
    ("http://localhost/private", "GET"),
    ("https://user:password@photos.invalid/image.jpg", "GET"),
])
def test_disallowed_requests_abort_before_fetch(background, monkeypatch, url, method):
    context = background.context()
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda url, **kwargs: calls.append(url))
    route = Request(url, method)

    context.request(route)

    assert route.aborted and route.fulfilled == []
    assert calls == [] and route.direct_calls == []


def test_write_request_aborts_at_transport_without_connection(background, monkeypatch):
    context = background.context()
    sockets, dns = fake_wire(monkeypatch, lambda host, port, call: [address(PUBLIC_V4, port)])
    route = Request("https://photos.invalid/upload", method="POST")
    context.request(route)
    assert route.aborted and not route.fulfilled and not route.direct_calls
    assert sockets == dns == []


def test_cancelled_route_does_not_fetch(background, monkeypatch):
    context = background.context()
    context.new_page()
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda url, **kwargs: calls.append(url))
    background.cancel.set()
    route = Request("https://photos.invalid/image.jpg")

    context.request(route)

    assert route.aborted and not calls and not route.fulfilled


def test_blocked_source_does_not_reenter_transport(background, monkeypatch):
    context = background.context()
    background.access.blocked["photos.invalid"] = "CAPTCHA"
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda url, **kwargs: calls.append(url))
    route = Request("https://photos.invalid/image.jpg")

    context.request(route)

    assert route.aborted and calls == [] and route.direct_calls == []


def test_navigate_keeps_browser_redirect_semantics_and_rechecks_every_hop(background, monkeypatch):
    context = background.context()
    initial = "https://initial.invalid/redirect"
    final = "https://final.invalid/gallery/page"
    calls, checks, navigations, routes = [], [], [], []

    def fetch(url, **kwargs):
        calls.append(url)
        if url == initial:
            return FetchedResource(url, b"", "text/html", 302, {"location": final})
        return FetchedResource(url, b"<html>photo</html>", "text/html")

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    monkeypatch.setattr(background.access, "check_page", lambda page, status: checks.append((page.url, status)))

    class Page:
        url = "about:blank"

        def set_default_timeout(self, timeout):
            assert 0 < timeout <= 5000

        def goto(self, url, **kwargs):
            self.url = url
            navigations.append((url, kwargs))
            route = Request(url)
            routes.append(route)
            context.request(route)
            assert route.fulfilled[0]["status"] == 302
            self.url = route.fulfilled[0]["headers"]["location"]
            redirected = Request(self.url)
            routes.append(redirected)
            context.request(redirected)
            return SimpleNamespace(status=200)

    background.navigate(Page(), initial)

    assert calls == [initial, final]
    assert navigations[0][0] == initial
    assert navigations[0][1]["wait_until"] == "domcontentloaded"
    assert checks == [(final, 200)]
    assert routes[1].fulfilled[0]["body"] == b"<html>photo</html>"
    assert all(not route.direct_calls for route in routes)


def test_navigate_uses_only_remaining_page_budget(background, monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 10.0)
    timeouts = []
    monkeypatch.setattr(background.access, "check_page", lambda *args: None)

    class Page:
        url = "https://photos.invalid/page"

        def set_default_timeout(self, timeout):
            timeouts.append(timeout)

        def goto(self, url, **kwargs):
            timeouts.append(kwargs["timeout"])
            return SimpleNamespace(status=200)

    background.navigate(Page(), Page.url, deadline=12.5)
    assert len(timeouts) >= 2 and all(0 < value <= 2500 for value in timeouts)


def test_expired_page_budget_aborts_route_before_network_fetch(background, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    context = background.context(deadline=11.0)
    context.new_page()
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: calls.append(True))
    clock[0] = 12.0
    route = Request("https://photos.invalid/image.jpg")
    context.request(route)
    assert route.aborted and not calls and not route.fulfilled


def test_popup_gets_its_own_interception_session(background, monkeypatch):
    context = background.context()
    first, popup = context.new_page(), context.new_page()
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return FetchedResource(url, b"fixture", "text/html")

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    original, opened = Request("https://first.invalid/page"), Request("https://popup.invalid/page")
    context.request(original, first)
    context.request(opened, popup)
    assert context.sessions[id(first)] is not context.sessions[id(popup)]
    assert calls == ["https://first.invalid/page", "https://popup.invalid/page"]
    assert original.fulfilled and opened.fulfilled


def test_failed_interception_closes_page_without_allowing_navigation(background):
    context = background.context()
    context.next_session = Session()
    context.next_session.fail_enable = True
    page = context.new_page()
    with pytest.raises(RuntimeError, match="Synthetic CDP session closed"):
        background.navigate(page, "https://photos.invalid/page")
    assert context.pages[0].closed


def test_navigation_waits_for_cdp_interception_ack_before_opening_url(background, monkeypatch):
    context = background.context()
    page = context.new_page()
    state = background._page_attachments[page]
    state["ready"] = False
    events = []
    page.set_default_timeout = lambda timeout: None
    page.url = "https://photos.invalid/page"
    monkeypatch.setattr(background.access, "check_page", lambda *args: None)

    def wait(timeout):
        assert 0 < timeout <= 10
        events.append("wait")
        state["ready"] = True

    def goto(url, **kwargs):
        assert state["ready"]
        events.append("navigate")
        return SimpleNamespace(status=200)

    page.wait_for_timeout, page.goto = wait, goto
    background.navigate(page, page.url)
    assert events == ["wait", "navigate"]


def test_cdp_attachment_wait_obeys_shared_deadline(background, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    context = background.context(deadline=10.005)
    page = context.new_page()
    background._page_attachments[page]["ready"] = False

    def wait(timeout):
        assert 0 < timeout <= 5
        clock[0] = 11.0

    page.wait_for_timeout = wait
    with pytest.raises(TimeoutError):
        background.navigate(page, "https://photos.invalid/page", deadline=10.005)


@pytest.mark.parametrize("failure", ["deadline", "cancel", "closed"])
def test_request_finishing_after_deadline_or_close_is_not_fulfilled(background, monkeypatch, failure):
    clock = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    context = background.context(deadline=11.0)
    page = context.new_page()
    session = context.sessions[id(page)]

    def fetch(url, **kwargs):
        if failure == "deadline":
            clock[0] = 12.0
        elif failure == "cancel":
            background.cancel.set()
        else:
            session.closed = True
        return FetchedResource(url, b"fixture", "image/png")

    monkeypatch.setattr(background.network, "fetch_browser", fetch)
    request = Request("https://photos.invalid/image.png")
    context.request(request, page)
    assert not request.fulfilled
    assert session.commands[-1][0] == "Fetch.failRequest"


def test_request_body_cannot_bypass_read_only_transport(background, monkeypatch):
    context = background.context()
    sockets, dns = fake_wire(monkeypatch, lambda host, port, call: [address(PUBLIC_V4, port)])
    request = Request("https://photos.invalid/upload", method="GET", data="side=effect")
    context.request(request)
    assert request.aborted and not request.fulfilled
    assert sockets == dns == []


def test_cancel_before_navigation_does_not_fetch_or_open_page(background, monkeypatch):
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda url, **kwargs: calls.append(url))
    background.cancel.set()

    with pytest.raises(Cancelled):
        background.navigate(None, "https://photos.invalid/page")

    assert calls == []


@pytest.mark.parametrize("destination", ["http://127.0.0.1/private", "http://internal.invalid/private"])
def test_browser_resource_redirect_uses_pinned_transport_and_blocks_private_destination(background, monkeypatch, destination):
    context = background.context()
    redirect = ("HTTP/1.1 302 Found\r\nLocation: " + destination + "\r\nContent-Length: 0\r\n\r\n").encode()
    sockets, _ = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4 if host == "photos.invalid" else "127.0.0.1", port)],
        responses=(redirect,))
    route = Request("http://photos.invalid/image.jpg")

    context.request(route)

    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]
    if "internal.invalid" in destination:
        assert route.fulfilled[0]["status"] == 302
        redirected = Request(route.fulfilled[0]["headers"]["location"])
        context.request(redirected)
        assert redirected.aborted and not redirected.fulfilled and not redirected.direct_calls
    else:
        assert route.aborted and route.fulfilled == []
    assert route.direct_calls == []
    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "http://[::1]/private", "file:///C:/private"])
def test_unsafe_document_url_is_rejected_before_browser_navigation(background, monkeypatch, url):
    calls = []
    monkeypatch.setattr(background.network, "fetch_browser", lambda *args, **kwargs: calls.append(True))
    # None guarantees no browser operation is reached.
    with pytest.raises(ValueError):
        background.navigate(None, url)
    assert calls == []


def test_isolation_failure_closes_resources_without_unrestricted_browser_fallback(monkeypatch):
    import playwright.sync_api
    events = []

    class Sandbox:
        def launch(self, chromium, **kwargs):
            events.append("isolated-launch")
            raise OSError("Synthetic isolation unavailable")

        def close(self):
            events.append("sandbox-close")

    chromium = SimpleNamespace(launch=lambda **kwargs: pytest.fail("Unrestricted launch must never be attempted"))
    runtime = SimpleNamespace(chromium=chromium, stop=lambda: events.append("driver-stop"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: runtime))
    monkeypatch.setitem(sys.modules, "lazyref.browser_sandbox", SimpleNamespace(BrowserSandbox=Sandbox))
    background = BackgroundBrowser(Event())
    try:
        with pytest.raises(RuntimeError, match="Synthetic isolation unavailable"):
            background._start()
        assert events == ["isolated-launch", "driver-stop", "sandbox-close"]
        assert background.browser is background.runtime is background.sandbox is None
    finally:
        background.close()


def test_isolated_browser_is_reused_within_search_and_launch_uses_page_budget(monkeypatch):
    import playwright.sync_api
    events = []
    browser = Browser()
    chromium = SimpleNamespace(launch=lambda **kwargs: pytest.fail("Unrestricted launch must never be attempted"))

    class Sandbox:
        def launch(self, actual, timeout_ms):
            assert actual is chromium
            events.append(("isolated-launch", timeout_ms))
            return browser

        def close(self):
            events.append("sandbox-close")

    runtime = SimpleNamespace(chromium=chromium, stop=lambda: events.append("driver-stop"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: runtime))
    monkeypatch.setitem(sys.modules, "lazyref.browser_sandbox", SimpleNamespace(BrowserSandbox=Sandbox))
    monkeypatch.setattr("time.monotonic", lambda: 10.0)
    background = BackgroundBrowser(Event())
    try:
        background._start(deadline=12.5)
        background._start(deadline=12.5)
        assert events == [("isolated-launch", 2500)]
        assert background.browser is browser
    finally:
        background.close()
    assert events[-2:] == ["driver-stop", "sandbox-close"]
