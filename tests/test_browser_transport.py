"""Browser responses retain HTTP semantics without bypassing pinned connections."""

import gzip
from email.message import Message
from threading import Event
from urllib.parse import urljoin

import pytest

from lazyref.access import SourceBlocked
from lazyref.models import Cancelled
from lazyref.network import Network
from test_network_security import PUBLIC_V4, address, fake_wire


@pytest.fixture
def network():
    instance = Network(Event())
    try:
        yield instance
    finally:
        instance.close()


def wire(monkeypatch, status="200 OK", headers=(), body=b"payload"):
    fields = [f"Content-Length: {len(body)}", *headers]
    response = (f"HTTP/1.1 {status}\r\n" + "\r\n".join(fields) + "\r\n\r\n").encode() + body
    return fake_wire(monkeypatch, lambda host, port, call: [address(PUBLIC_V4, port)], responses=(response,))


def normalized_headers(resource):
    return {key.lower(): value for key, value in resource.headers.items()}


def test_browser_range_response_preserves_status_cors_and_type(network, monkeypatch):
    sockets, dns = wire(monkeypatch, "206 Partial Content", headers=(
        "Content-Type: application/javascript; charset=utf-8", "Content-Range: bytes 0-6/100",
        "Accept-Ranges: bytes", "Access-Control-Allow-Origin: https://photos.invalid",
        "Access-Control-Allow-Credentials: true", "Vary: Origin", "X-Content-Type-Options: nosniff"))
    resource = network.fetch_browser("http://assets.invalid/module.js", headers={
        "Origin": "https://photos.invalid", "Range": "bytes=0-6", "Accept": "*/*",
        "Referer": "https://photos.invalid/gallery"})

    assert resource.status == 206 and resource.body == b"payload"
    assert resource.url == "http://assets.invalid/module.js"
    headers = normalized_headers(resource)
    assert headers["content-type"] == "application/javascript; charset=utf-8"
    assert headers["content-range"] == "bytes 0-6/100"
    assert headers["access-control-allow-origin"] == "https://photos.invalid"
    assert headers["access-control-allow-credentials"] == "true"
    assert headers["vary"] == "Origin" and headers["x-content-type-options"] == "nosniff"
    request = bytes(sockets[0].sent).lower()
    assert b"origin: https://photos.invalid\r\n" in request
    assert b"range: bytes=0-6\r\n" in request
    assert b"referer: https://photos.invalid/gallery\r\n" in request
    assert dns == [("assets.invalid", 80)]


def test_browser_cannot_supply_credentials_host_or_hop_by_hop_headers(network, monkeypatch):
    sockets, _ = wire(monkeypatch)
    network.fetch_browser("http://public.invalid/photo", headers={
        "Authorization": "Bearer synthetic-secret", "Proxy-Authorization": "synthetic-proxy",
        "Cookie": "injected=synthetic", "Host": "internal.invalid", "Connection": "keep-alive",
        "X-Untrusted-Secret": "synthetic-private", "Accept": "image/*"})

    request = bytes(sockets[0].sent).lower()
    assert b"host: public.invalid\r\n" in request
    assert b"accept: image/*\r\n" in request
    for forbidden in (b"synthetic", b"internal.invalid", b"x-untrusted-secret", b"keep-alive"):
        assert forbidden not in request


@pytest.mark.parametrize("name", ["User-Agent", "user-agent", "USER-AGENT"])
def test_browser_user_agent_is_preserved_regardless_of_header_case(network, monkeypatch, name):
    sockets, _ = wire(monkeypatch)
    network.fetch_browser("http://public.invalid/photo", headers={name: "FixtureBrowser/1.0"})
    request = bytes(sockets[0].sent).lower()
    assert request.count(b"user-agent:") == 1
    assert b"user-agent: fixturebrowser/1.0\r\n" in request


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_browser_redirect_is_returned_as_one_hop_with_equivalent_location(network, monkeypatch, status):
    sockets, dns = wire(monkeypatch, f"{status} Redirect", headers=("Location: /new/module.js",), body=b"")
    resource = network.fetch_browser("http://assets.invalid/old/module.js")

    assert resource.status == status
    assert resource.url == "http://assets.invalid/old/module.js"
    assert urljoin(resource.url, normalized_headers(resource)["location"]) == "http://assets.invalid/new/module.js"
    assert len(sockets) == len(dns) == 1


def test_next_browser_redirect_request_still_rejects_private_dns_destination(network, monkeypatch):
    location = "http://internal.invalid/private"
    response = (f"HTTP/1.1 302 Redirect\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n").encode()
    sockets, _ = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4 if host == "public.invalid" else "127.0.0.1", port)],
        responses=(response,))
    first = network.fetch_browser("http://public.invalid/initial")
    next_url = urljoin(first.url, normalized_headers(first)["location"])
    with pytest.raises(ValueError):
        network.fetch_browser(next_url)
    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "http://[::1]/private",
                                     "https://user:password@public.invalid/private", "file:///C:/private"])
def test_unsafe_redirect_location_is_rejected_before_browser_can_follow_it(network, monkeypatch, location):
    sockets, dns = wire(monkeypatch, "302 Redirect", headers=("Location: " + location,), body=b"")
    with pytest.raises(ValueError):
        network.fetch_browser("http://public.invalid/initial")
    assert dns == [("public.invalid", 80)]
    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]


def test_browser_preserves_compressed_body_and_encoding_together(network, monkeypatch):
    compressed = gzip.compress(b"export const loaded = true;")
    wire(monkeypatch, headers=("Content-Type: application/javascript", "Content-Encoding: gzip"), body=compressed)
    resource = network.fetch_browser("http://assets.invalid/main.js")
    assert resource.body == compressed
    assert normalized_headers(resource)["content-encoding"] == "gzip"


def test_attachment_response_keeps_browser_download_disposition(network, monkeypatch):
    wire(monkeypatch, headers=("Content-Type: text/html", "Content-Disposition: attachment; filename=sample.html"),
         body=b"<script>document.body.append('fixture')</script>")
    resource = network.fetch_browser("http://assets.invalid/attachment")
    assert ("content-disposition", "attachment; filename=sample.html") in resource.header_items


def test_browser_preserves_duplicate_security_headers_without_weakening_them(network, monkeypatch):
    wire(monkeypatch, headers=("Content-Security-Policy: default-src 'none'",
        "Content-Security-Policy: img-src https:",
        "Access-Control-Allow-Origin: https://one.invalid",
        "Access-Control-Allow-Origin: *"))
    resource = network.fetch_browser("http://assets.invalid/page")
    assert resource.header_items == (
        ("content-security-policy", "default-src 'none'"),
        ("content-security-policy", "img-src https:"),
        ("access-control-allow-origin", "https://one.invalid"),
        ("access-control-allow-origin", "*"))


def test_ambiguous_redirect_headers_are_rejected_without_following_either(network, monkeypatch):
    sockets, dns = wire(monkeypatch, "302 Redirect", headers=(
        "Location: /one", "Location: /two"), body=b"")
    with pytest.raises(ValueError, match="Неоднозначна"):
        network.fetch_browser("http://assets.invalid/page")
    assert len(sockets) == 1 and dns == [("assets.invalid", 80)]


@pytest.mark.parametrize("status", [404, 410, 500])
def test_normal_http_errors_are_browser_responses_not_transport_exceptions(network, monkeypatch, status):
    wire(monkeypatch, f"{status} Error", headers=("Content-Type: application/json",), body=b'{"error":"fixture"}')
    resource = network.fetch_browser("http://assets.invalid/api")
    assert resource.status == status and resource.body == b'{"error":"fixture"}'


@pytest.mark.parametrize("status", [401, 403, 419, 429])
def test_access_denial_http_status_keeps_existing_source_blocking(network, monkeypatch, status):
    wire(monkeypatch, f"{status} Blocked", body=b"")
    with pytest.raises(SourceBlocked):
        network.fetch_browser("http://assets.invalid/api")
    assert "assets.invalid" in network.access.blocked


def test_visible_captcha_text_keeps_existing_source_blocking(network, monkeypatch):
    wire(monkeypatch, headers=("Content-Type: text/html",), body=b"<html><body>Verify you are human</body></html>")
    with pytest.raises(SourceBlocked):
        network.fetch_browser("http://assets.invalid/page")
    assert "assets.invalid" in network.access.blocked


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "CONNECT", "TRACE"])
def test_browser_write_or_tunnel_methods_are_rejected_before_connection(network, monkeypatch, method):
    sockets, dns = wire(monkeypatch)
    with pytest.raises(ValueError):
        network.fetch_browser("http://assets.invalid/api", method=method)
    assert sockets == dns == []


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_browser_read_and_preflight_methods_preserve_method_and_headers(network, monkeypatch, method):
    sockets, _ = wire(monkeypatch, "204 No Content", headers=("Access-Control-Allow-Methods: GET, HEAD",), body=b"")
    resource = network.fetch_browser("http://assets.invalid/api", method=method, headers={
        "Origin": "https://photos.invalid", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "range"})
    assert bytes(sockets[0].sent).startswith((method + " /api HTTP/1.1\r\n").encode())
    assert resource.status == 204 and resource.body == b""
    if method == "OPTIONS":
        request = bytes(sockets[0].sent).lower()
        assert b"access-control-request-method: get\r\n" in request
        assert b"access-control-request-headers: range\r\n" in request


def test_cancel_before_browser_fetch_never_resolves_or_connects(network, monkeypatch):
    sockets, dns = wire(monkeypatch)
    network.cancel.set()
    with pytest.raises(Cancelled):
        network.fetch_browser("http://assets.invalid/photo")
    assert sockets == dns == []


def test_expired_page_deadline_prevents_browser_fetch(network, monkeypatch):
    sockets, dns = wire(monkeypatch)
    monkeypatch.setattr("time.monotonic", lambda: 10.0)
    with pytest.raises(TimeoutError):
        network.fetch_browser("http://assets.invalid/photo", deadline=9.0)
    assert sockets == dns == []


def test_browser_connection_timeout_uses_remaining_page_budget(network, monkeypatch):
    sockets, _ = wire(monkeypatch)
    monkeypatch.setattr("time.monotonic", lambda: 10.0)
    resource = network.fetch_browser("http://assets.invalid/photo", deadline=12.5)
    assert resource.body == b"payload"
    assert 0 < sockets[0].timeout <= 2.5


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
def test_interrupted_browser_response_is_not_accepted(network, monkeypatch, method, interruption):
    clock = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])

    def interrupt():
        if interruption == "deadline":
            clock[0] = 13.0
        else:
            network.cancel.set()

    class Response:
        status = 200
        closed = False
        headers = Message()
        headers["Content-Type"] = "image/png"
        headers["Content-Length"] = "0"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read(self, size):
            interrupt()  # Even EOF may arrive after a blocking socket read.
            return b""

    response = Response()

    def open_response(*args, **kwargs):
        if method == "HEAD":
            interrupt()  # HEAD has no body loop to check cancellation or time.
        return response

    monkeypatch.setattr(network.client, "open", open_response)
    with pytest.raises(TimeoutError if interruption == "deadline" else Cancelled):
        network.fetch_browser("http://assets.invalid/photo", method=method, deadline=12.5)
    assert response.closed


def test_browser_response_size_limit_applies_to_error_bodies_too(network, monkeypatch):
    wire(monkeypatch, "404 Missing", body=b"123456")
    with pytest.raises(ValueError):
        network.fetch_browser("http://assets.invalid/photo", max_bytes=5)


def test_hop_by_hop_and_ambient_cookie_headers_are_not_returned_to_browser(network, monkeypatch):
    wire(monkeypatch, headers=("Set-Cookie: synthetic=value; Path=/", "Connection: keep-alive",
                              "Keep-Alive: timeout=30", "Proxy-Authenticate: Basic", "Alt-Svc: h3=\":443\""))
    resource = network.fetch_browser("http://assets.invalid/photo")
    headers = normalized_headers(resource)
    assert not {"set-cookie", "connection", "keep-alive", "proxy-authenticate", "alt-svc"}.intersection(headers)
