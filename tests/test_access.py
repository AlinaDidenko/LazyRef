"""CAPTCHA не має зупиняти добірку чи спричиняти повторні спроби доступу."""

from io import BytesIO
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

from lazyref.access import AccessPolicy, SourceBlocked, gate_reason
from lazyref.models import Candidate, Cancelled, SearchRequest
from lazyref.network import Network


@pytest.mark.parametrize("url,body,title", [
    ("https://www.google.com/sorry/index?continue=search", "", ""),
    ("https://photos.example/a", "Our systems have detected unusual traffic from your computer network.", ""),
    ("https://photos.example/a", "<h1>Verify you are human</h1>", ""),
    ("https://photos.example/a", "Cloudflare challenge", "Just a moment..."),
    ("https://photos.example/accounts/login/", "", ""),
    ("https://consent.google.com/m", "", ""),
])
def test_gate_detection(url, body, title):
    assert gate_reason(url, body, title)


def test_normal_mentions_are_not_gates_and_blocks_expire_with_job():
    assert not gate_reason("https://example.org/article", "This article explains CAPTCHA.")
    policy = AccessPolicy()
    with pytest.raises(SourceBlocked):
        policy.block("https://www.example.org/first", "CAPTCHA")
    with pytest.raises(SourceBlocked):
        policy.check("https://example.org/second")
    policy.check("https://another.example/photo")
    AccessPolicy().check("https://example.org/first")


@pytest.mark.parametrize("mode", ["html", "rate_limit", "redirect"])
def test_network_does_not_revisit_blocked_source(monkeypatch, mode):
    monkeypatch.setattr("lazyref.network.public_url", lambda url: url)
    network = Network(Event(), local_transport=True)
    calls = []

    def open_response(request, timeout):
        calls.append(request.full_url)
        if mode == "rate_limit":
            raise HTTPError(request.full_url, 429, "limit", {}, BytesIO())
        if mode == "redirect" and len(calls) == 1:
            raise HTTPError(request.full_url, 302, "redirect",
                            {"location": "https://captcha.example/challenge"}, BytesIO())
        response = BytesIO(b"<h1>Verify you are human</h1>")
        response.headers = {"content-type": "text/html"}
        return response

    monkeypatch.setattr(network.client, "open", open_response)
    try:
        for url in ("https://photos.example/first.jpg", "https://photos.example/second.jpg"):
            with pytest.raises(SourceBlocked):
                network.get(url)
        assert len(calls) == (2 if mode == "redirect" else 1)
    finally:
        network.close()






