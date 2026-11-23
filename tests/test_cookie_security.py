"""Cookie policy regressions using local headers only; no HTTP or DNS calls."""

from email.message import Message
from http.cookiejar import Cookie
from threading import Event
from urllib.request import Request

import pytest

from lazyref.network import Network


class CookieResponse:
    def __init__(self, value, header="Set-Cookie"):
        self.headers = Message()
        self.headers.add_header(header, value)

    def info(self):
        return self.headers


@pytest.fixture
def network():
    session = Network(Event(), local_transport=True)
    try:
        yield session
    finally:
        session.close()


def receive(network, value, *, origin="https://attacker.example/", header="Set-Cookie"):
    network.cookies.extract_cookies(CookieResponse(value, header), Request(origin))


def sent_cookie(network, url):
    request = Request(url)
    network.cookies.add_cookie_header(request)
    return request.get_header("Cookie", "")


@pytest.mark.parametrize("version", ["", "; Version=0", "; Version=-1", "; Version=-123"])
def test_remote_site_cannot_set_cookie_for_an_unrelated_domain(network, version):
    receive(network, "session=poison; Domain=victim.example; Path=/" + version)

    assert sent_cookie(network, "https://victim.example/account") == ""
    assert list(network.cookies) == []


@pytest.mark.parametrize("version", [-1, -123, 2, 999])
def test_unsupported_cookie_version_is_rejected_even_for_same_site(network, version):
    receive(network, f"session=poison; Version={version}; Path=/",
            origin="https://source.example/")

    assert sent_cookie(network, "https://source.example/") == ""
    assert list(network.cookies) == []


@pytest.mark.parametrize("version", [-1, -123, 2, 999])
def test_unsupported_cookie_already_in_jar_is_not_returned(network, version):
    # set_cookie() deliberately bypasses set_ok(); sending must check return_ok too.
    cookie = Cookie(version=version, name="session", value="poison", port=None,
                    port_specified=False, domain="victim.example", domain_specified=False,
                    domain_initial_dot=False, path="/", path_specified=True, secure=True,
                    expires=None, discard=True, comment=None, comment_url=None, rest={})
    network.cookies.set_cookie(cookie)

    assert sent_cookie(network, "https://victim.example/") == ""


@pytest.mark.parametrize("version", ["", "; Version=0"])
def test_same_site_cookie_is_preserved_and_available_to_cookie_value(network, version):
    receive(network, "XSRF-TOKEN=normal%20value; Secure; Path=/" + version,
            origin="https://source.example/")

    assert sent_cookie(network, "https://source.example/api") == "XSRF-TOKEN=normal%20value"
    assert network.cookie_value("XSRF-TOKEN", "https://source.example/api") == "normal value"
    assert sent_cookie(network, "https://unrelated.example/") == ""


def test_supported_version_one_cookie_is_preserved(network):
    # RFC2965 is disabled by the stdlib default. Preserve its behavior when enabled.
    network.cookies._policy.rfc2965 = True
    receive(network, 'session=normal; Version=1; Path="/"',
            origin="https://source.example/", header="Set-Cookie2")

    assert [(cookie.name, cookie.value, cookie.version) for cookie in network.cookies] == [
        ("session", "normal", 1)]
    assert "session=normal" in sent_cookie(network, "https://source.example/api")
    assert sent_cookie(network, "https://unrelated.example/") == ""


def test_network_instances_have_isolated_anonymous_cookie_jars(network):
    second = Network(Event(), local_transport=True)
    try:
        receive(network, "session=first; Path=/", origin="https://source.example/")
        assert sent_cookie(network, "https://source.example/") == "session=first"
        assert sent_cookie(second, "https://source.example/") == ""

        receive(second, "session=second; Path=/", origin="https://source.example/")
        network.close()
        assert list(network.cookies) == []
        assert sent_cookie(second, "https://source.example/") == "session=second"
    finally:
        second.close()
