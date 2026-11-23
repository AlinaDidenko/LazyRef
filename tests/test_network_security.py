"""Мережева межа: справжній urllib, але DNS, TCP і TLS повністю підмінені."""

from io import BytesIO
import socket
import ssl
from threading import Event

import pytest

from lazyref.network import Network


PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2606:4700:4700::1111"
OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: image/jpeg\r\n\r\nOK"


def address(ip, port):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    target = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", target


def fake_wire(monkeypatch, resolver, responses=(OK,), fail_connections=()):
    """Жоден fixture не створює справжнього socket або DNS-запиту."""
    sockets, dns_calls = [], []

    def resolve(host, port, *, type):
        assert type == socket.SOCK_STREAM
        dns_calls.append((host, port))
        return resolver(host, port, len(dns_calls))

    class FakeSocket:
        def __init__(self, family, kind, protocol):
            self.family, self.kind, self.protocol = family, kind, protocol
            self.index = len(sockets)
            self.target = None
            self.closed = False
            self.timeout = None
            self.sent = bytearray()
            sockets.append(self)

        def settimeout(self, value):
            self.timeout = value

        def setsockopt(self, *args):
            pass

        def connect(self, target):
            self.target = target
            if self.index in fail_connections:
                raise OSError("synthetic connection failure")

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, mode):
            assert mode == "rb"
            return BytesIO(responses[min(self.index, len(responses) - 1)])

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", FakeSocket)
    return sockets, dns_calls


def fetch(url):
    network = Network(Event(), local_transport=True)
    try:
        return network.get(url)
    finally:
        network.close()


def test_dns_rebinding_cannot_replace_the_validated_connection_address(monkeypatch):
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4 if call == 1 else "127.0.0.1", port)])

    assert fetch("http://photos.invalid/image.jpg") == b"OK"

    assert dns == [("photos.invalid", 80)]
    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]
    assert b"Host: photos.invalid\r\n" in sockets[0].sent
    assert b"GET /image.jpg HTTP/1.1\r\n" in sockets[0].sent


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "fe80::1"])
def test_any_private_dns_answer_rejects_the_whole_set_before_connect(monkeypatch, private):
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4, port), address(private, port)])

    with pytest.raises(ValueError, match="приватні"):
        fetch("http://mixed.invalid/image.jpg")

    assert dns == [("mixed.invalid", 80)]
    assert sockets == []


def test_empty_dns_answer_is_rejected_without_connect(monkeypatch):
    sockets, _ = fake_wire(monkeypatch, lambda *args: [])
    with pytest.raises(ValueError, match="приватні"):
        fetch("http://empty.invalid/image.jpg")
    assert sockets == []


def test_public_ipv6_sockaddr_and_host_header_are_preserved(monkeypatch):
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V6, port)])

    assert fetch(f"http://[{PUBLIC_V6}]/image.jpg") == b"OK"

    assert dns == [(PUBLIC_V6, 80)]
    assert sockets[0].family == socket.AF_INET6
    assert sockets[0].target == (PUBLIC_V6, 80, 0, 0)
    assert f"Host: [{PUBLIC_V6}]\r\n".encode() in sockets[0].sent


def test_failed_public_address_is_closed_before_using_another_validated_address(monkeypatch):
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V6, port), address(PUBLIC_V4, port)],
        fail_connections=(0,))

    assert fetch("http://fallback.invalid/image.jpg") == b"OK"

    assert dns == [("fallback.invalid", 80)]
    assert sockets[0].closed
    assert [s.target for s in sockets] == [(PUBLIC_V6, 80, 0, 0), (PUBLIC_V4, 80)]
    assert all(s.timeout == 20 for s in sockets)


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "http://internal.invalid/private"])
def test_redirect_to_private_destination_never_connects_to_it(monkeypatch, location):
    redirect = ("HTTP/1.1 302 Found\r\nLocation: " + location + "\r\nContent-Length: 0\r\n\r\n").encode()
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4 if host == "photos.invalid" else "127.0.0.1", port)],
        responses=(redirect, OK))

    with pytest.raises(ValueError):
        fetch("http://photos.invalid/image.jpg")

    assert [s.target for s in sockets] == [(PUBLIC_V4, 80)]
    assert all(host in {"photos.invalid", "internal.invalid"} for host, _ in dns)


def test_https_pins_tcp_but_keeps_original_tls_hostname_and_certificate_checks(monkeypatch):
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4, port)])
    tls = []

    def wrap_socket(context, sock, *, server_hostname):
        tls.append((sock, server_hostname, context.check_hostname, context.verify_mode))
        return sock

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    assert fetch("https://secure.invalid/image.jpg") == b"OK"

    assert dns == [("secure.invalid", 443)]
    assert sockets[0].target == (PUBLIC_V4, 443)
    assert tls == [(sockets[0], "secure.invalid", True, ssl.CERT_REQUIRED)]
    assert b"Host: secure.invalid\r\n" in sockets[0].sent


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_environment_proxy_cannot_reresolve_or_tunnel_the_destination(monkeypatch, scheme):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://untrusted-proxy.invalid:8080")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    sockets, dns = fake_wire(monkeypatch,
        lambda host, port, call: [address(PUBLIC_V4, port)])
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", lambda context, sock, **kwargs: sock)

    assert fetch(f"{scheme}://photos.invalid/image.jpg") == b"OK"

    port = 443 if scheme == "https" else 80
    assert dns == [("photos.invalid", port)]
    assert sockets[0].target == (PUBLIC_V4, port)
    assert b"CONNECT " not in sockets[0].sent
    assert b"GET /image.jpg HTTP/1.1\r\n" in sockets[0].sent
