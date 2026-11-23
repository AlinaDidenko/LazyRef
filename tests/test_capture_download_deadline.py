"""The browser keeps its own cap inside the candidate's total budget."""

from threading import Event

import pytest

from lazyref.capture import BrowserCapture
from lazyref.models import Candidate, Cancelled


@pytest.mark.parametrize("now,total,expected", [
    (5, None, 25),
    (45, 100, 65),
    (70, 100, 90),
    (93, 100, 100),
])
def test_browser_deadline_is_shorter_of_page_cap_and_candidate_budget(monkeypatch, now, total, expected):
    calls = []

    class Process:
        def __init__(self, cancel, access):
            pass

        def capture(self, candidate, deadline):
            calls.append(deadline)
            return b"synthetic screenshot"

        def close(self):
            pass

    monkeypatch.setattr("lazyref.capture.time.monotonic", lambda: now)
    monkeypatch.setattr("lazyref.capture_process.CaptureProcess", Process)
    browser = BrowserCapture(Event())
    try:
        candidate = Candidate("test", "Photo", page_url="https://photos.invalid/photo")
        assert browser.capture(candidate, deadline=total) == b"synthetic screenshot"
        assert calls == [expected]
    finally:
        browser.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_expired_or_cancelled_candidate_does_not_start_browser(monkeypatch, cancelled):
    monkeypatch.setattr("lazyref.capture.time.monotonic", lambda: 100)
    monkeypatch.setattr("lazyref.capture_process.CaptureProcess",
                        lambda *args: pytest.fail("An expired/cancelled attempt must not launch a worker"))
    cancel = Event()
    if cancelled:
        cancel.set()
    browser = BrowserCapture(cancel)
    try:
        with pytest.raises(Cancelled if cancelled else TimeoutError):
            browser.capture(Candidate("test", "Photo", page_url="https://photos.invalid/photo"), deadline=100)
    finally:
        browser.close()
