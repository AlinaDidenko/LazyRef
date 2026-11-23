"""Bounded dynamic-photo waiting without a browser, network or real clock delays."""

from threading import Event
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as BrowserError

import lazyref.capture as module
from lazyref.access import SourceBlocked
from lazyref.capture import BrowserCapture, IMAGE_STATE, MAX_SCROLL_STEPS, PAGE_TIMEOUT_SECONDS
from lazyref.models import Cancelled, Candidate


URL = "https://photos.invalid/photo.jpg?id=1"
CANDIDATE = Candidate("target", "Target photo", image_url=URL, page_url="https://photos.invalid/page")


class Node:
    def __init__(self, clock, *, sources=None, current=None, ready=True, visible=True, unobstructed=True):
        self.clock = clock
        self.sources = sources or [URL]
        self.current = current or URL
        self.ready = ready
        self.visible = visible
        self.unobstructed = unobstructed
        self.scrolls = []
        self.screenshots = []
        self.on_scroll = lambda: None
        self.on_screenshot = lambda: None
        self.on_state = lambda: None
        self.box = {"width": 420, "height": 320}

    def evaluate(self, expression, timeout=None):
        assert timeout is not None and 0 < timeout <= 500
        if expression == IMAGE_STATE:
            self.on_state()
            value = lambda item: item() if callable(item) else item
            return {"sources": value(self.sources), "current": value(self.current), "ready": value(self.ready)}
        assert "elementFromPoint" in expression
        return self.unobstructed

    def is_visible(self):
        return self.visible() if callable(self.visible) else self.visible

    def scroll_into_view_if_needed(self, timeout):
        assert 0 < timeout <= 500
        self.scrolls.append(timeout)
        self.on_scroll()

    def bounding_box(self, timeout):
        assert 0 < timeout <= 500
        return self.box

    def screenshot(self, **kwargs):
        self.screenshots.append((self.clock.now, kwargs))
        self.on_screenshot()
        return b"synthetic screenshot"


class Page:
    def __init__(self, clock, nodes=()):
        self.clock, self.nodes = clock, nodes
        self.waits, self.scrolls = [], []
        self.on_wait = lambda: None
        self.on_scroll = lambda: None

    def locator(self, selector):
        assert selector == "img"
        nodes = self.nodes() if callable(self.nodes) else self.nodes
        return SimpleNamespace(count=lambda: len(nodes), nth=lambda index: nodes[index])

    def evaluate(self, expression):
        assert "window.scrollBy" in expression
        self.scrolls.append(self.clock.now)
        self.on_scroll()

    def wait_for_timeout(self, milliseconds):
        assert 0 < milliseconds <= 100
        self.waits.append(milliseconds)
        self.clock.now += milliseconds / 1000
        self.on_wait()

    def is_closed(self):
        return False


@pytest.fixture
def capture(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    instance = BrowserCapture(Event())
    checks = []
    instance.access.check_page = lambda page: checks.append(clock.now)
    try:
        yield instance, clock, checks
    finally:
        instance.close()


def test_ready_photo_returns_immediately_without_polling(capture):
    browser, clock, checks = capture
    node = Node(clock)
    page = Page(clock, [node])

    assert browser.capture_element(page, CANDIDATE, deadline=2) == b"synthetic screenshot"

    assert not page.waits and not page.scrolls
    assert clock.now == 0 and len(node.scrolls) == 1
    assert node.screenshots == [(0, {"type": "png", "timeout": 2000, "animations": "disabled"})]
    assert checks == [0, 0]


def test_late_inserted_target_is_seen_without_waiting_for_full_budget(capture):
    browser, clock, _ = capture
    node = Node(clock)
    page = Page(clock, lambda: [node] if clock.now >= 0.3 else [])

    assert browser.capture_element(page, CANDIDATE, deadline=3) == b"synthetic screenshot"

    assert 0.3 <= clock.now < 0.5
    assert len(node.screenshots) == 1


def test_scroll_triggers_lazy_load_and_capture_finishes_as_soon_as_ready(capture):
    browser, clock, _ = capture
    node = Node(clock, ready=False)
    node.on_scroll = lambda: setattr(node, "ready", True)
    page = Page(clock, [node])

    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"

    assert not page.waits and not page.scrolls
    assert len(node.screenshots) == 1


def test_placeholder_with_matching_data_source_is_never_captured(capture):
    browser, clock, _ = capture
    placeholder = "https://photos.invalid/placeholder.png"
    node = Node(clock, sources=[placeholder, URL],
                current=lambda: URL if clock.now >= 0.3 else placeholder)
    page = Page(clock, [node])

    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"

    assert len(node.screenshots) == 1 and node.screenshots[0][0] >= 0.3


def test_incomplete_photo_waits_without_awaiting_an_unbounded_decode_promise(capture):
    browser, clock, _ = capture
    node = Node(clock, ready=lambda: clock.now >= 0.2)
    page = Page(clock, [node])

    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"

    assert len(page.waits) == 2 and node.screenshots[0][0] == pytest.approx(0.2)


def test_hidden_target_may_become_visible_after_scripts_finish(capture):
    browser, clock, _ = capture
    node = Node(clock, visible=lambda: clock.now >= 0.2)
    page = Page(clock, [node])

    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"

    assert node.screenshots[0][0] >= 0.2


def test_viewport_scroll_can_trigger_late_dom_insertion(capture):
    browser, clock, _ = capture
    node = Node(clock)
    page = Page(clock)
    page.on_scroll = lambda: setattr(page, "nodes", [node])

    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"

    assert page.scrolls == [0] and clock.now == pytest.approx(0.1)


def test_missing_target_obeys_remaining_budget_and_bounds_scrolls(capture):
    browser, clock, _ = capture
    page = Page(clock)

    with pytest.raises(TimeoutError, match="час запасного скріншота"):
        browser.capture_element(page, CANDIDATE, deadline=4)

    assert 4 <= clock.now <= 4.001
    assert len(page.scrolls) == MAX_SCROLL_STEPS


def test_existing_partial_budget_is_not_reset_for_polling(capture):
    browser, clock, _ = capture
    clock.now = 19.75
    page = Page(clock)

    with pytest.raises(TimeoutError):
        browser.capture_element(page, CANDIDATE, deadline=20)

    assert 20 <= clock.now <= 20.001
    assert sum(page.waits) <= 251


def test_cancellation_stops_on_next_short_poll(capture):
    browser, clock, _ = capture
    page = Page(clock)
    page.on_wait = browser.cancel.set

    with pytest.raises(Cancelled):
        browser.capture_element(page, CANDIDATE, deadline=20)

    assert page.waits == [100] and clock.now == pytest.approx(0.1)


def test_visible_overlay_is_rejected_without_waiting_or_removal(capture):
    browser, clock, _ = capture
    node = Node(clock, unobstructed=False)
    page = Page(clock, [node])

    with pytest.raises(ValueError, match="перекриття"):
        browser.capture_element(page, CANDIDATE, deadline=20)

    assert not page.waits and not node.screenshots


def test_different_content_query_cannot_replace_target(capture):
    browser, clock, _ = capture
    wrong = URL.replace("id=1", "id=2")
    node = Node(clock, sources=[wrong], current=wrong)

    with pytest.raises(TimeoutError):
        browser.capture_element(Page(clock, [node]), CANDIDATE, deadline=0.2)

    assert not node.screenshots


def test_captcha_policy_still_stops_before_capture(capture):
    browser, clock, _ = capture
    node = Node(clock)
    page = Page(clock, [node])

    def captcha(page):
        raise SourceBlocked(CANDIDATE.page_url, "CAPTCHA")

    browser.access.check_page = captcha
    with pytest.raises(SourceBlocked):
        browser.capture_element(page, CANDIDATE, deadline=20)
    assert not node.screenshots and not page.waits


def test_captcha_appearing_during_wait_is_checked_before_screenshot(capture):
    browser, clock, _ = capture
    node = Node(clock)
    page = Page(clock, lambda: [node] if clock.now >= 0.2 else [])

    def captcha(page):
        if clock.now >= 0.2:
            raise SourceBlocked(CANDIDATE.page_url, "CAPTCHA")

    browser.access.check_page = captcha
    with pytest.raises(SourceBlocked):
        browser.capture_element(page, CANDIDATE, deadline=20)
    assert not node.screenshots and clock.now == pytest.approx(0.2)


def test_replaced_dom_node_is_retried_within_same_budget(capture):
    browser, clock, _ = capture
    node = Node(clock)
    calls = []

    def detached_once():
        calls.append(True)
        if len(calls) == 1:
            raise BrowserError("Element is not attached")

    node.on_state = detached_once
    page = Page(clock, [node])
    assert browser.capture_element(page, CANDIDATE, deadline=1) == b"synthetic screenshot"
    assert clock.now == pytest.approx(0.1) and len(node.screenshots) == 1


@pytest.mark.parametrize("failure", ["deadline", "cancel"])
def test_screenshot_completion_rechecks_deadline_and_cancellation(capture, failure):
    browser, clock, _ = capture
    node = Node(clock)
    node.on_screenshot = (lambda: setattr(clock, "now", 2)) if failure == "deadline" else browser.cancel.set

    with pytest.raises(TimeoutError if failure == "deadline" else Cancelled):
        browser.capture_element(Page(clock, [node]), CANDIDATE, deadline=1)


def test_capture_passes_one_deadline_through_start_navigation_and_image(capture):
    browser, clock, _ = capture
    calls = []
    page = object()
    context = SimpleNamespace(new_page=lambda: page, close=lambda: calls.append("close"))

    def create_context(*, deadline):
        calls.append(("context", deadline))
        clock.now += 3
        return context

    def navigate(actual_page, url, *, deadline):
        assert actual_page is page and url == CANDIDATE.page_url
        calls.append(("navigate", deadline))
        clock.now += 10

    def capture_image(actual_page, candidate, *, deadline):
        assert actual_page is page and candidate is CANDIDATE
        assert clock.now == 13
        calls.append(("image", deadline))
        return b"screenshot"

    browser.context, browser.navigate, browser.capture_element = create_context, navigate, capture_image

    assert browser.capture_local(CANDIDATE) == b"screenshot"
    assert calls == [("context", PAGE_TIMEOUT_SECONDS), ("navigate", PAGE_TIMEOUT_SECONDS),
                     ("image", PAGE_TIMEOUT_SECONDS), "close"]


def test_cleanup_failure_cannot_replace_deadline_error(capture):
    browser, clock, _ = capture

    def close():
        raise RuntimeError("Context already killed")

    context = SimpleNamespace(new_page=lambda: object(), close=close)
    browser.context = lambda *, deadline: context

    def navigate(*args, **kwargs):
        raise TimeoutError("page deadline")

    browser.navigate = navigate
    with pytest.raises(TimeoutError, match="page deadline") as failure:
        browser.capture_local(CANDIDATE)
    assert "Context already killed" in failure.value.__notes__[0]
