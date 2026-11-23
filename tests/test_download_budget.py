"""Download deadlines and useful progress, with a fake clock and no I/O."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lazyref.download_budget import DownloadBudget, MIN_PROGRESS_BYTES, request_budget


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def qualifying_progress(budget, clock, *, total=256 * 1024, resource="response"):
    clock.now = 35
    budget.observe(0, total, resource)
    clock.now = 44
    budget.observe(128 * 1024, total, resource)
    clock.now = 45


def test_direct_deadline_includes_time_before_headers():
    clock = Clock(20)
    budget = DownloadBudget(clock)
    assert budget.check_direct() == 45
    assert budget.direct_deadline == 65
    assert budget.total_deadline == 120
    clock.now = 64.999
    assert budget.check_direct() == pytest.approx(0.001)
    clock.now = 65
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_useful_progress_grants_one_extension_to_absolute_seventy_seconds():
    clock = Clock()
    budget = DownloadBudget(clock)
    qualifying_progress(budget, clock)
    assert budget.check_direct() == 25
    assert budget.extended
    assert budget.direct_deadline == 70
    clock.now = 69.999
    budget.observe(255 * 1024, 256 * 1024, "response")
    assert budget.check_direct() == pytest.approx(0.001)
    clock.now = 70
    with pytest.raises(TimeoutError):
        budget.check_direct()
    assert budget.total_deadline == 100


def test_late_watchdog_check_never_restarts_extension_timer():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 40
    budget.observe(0, 256 * 1024, "response")
    clock.now = 49
    budget.observe(128 * 1024, 256 * 1024, "response")
    clock.now = 50
    assert budget.check_direct() == 20
    assert budget.direct_deadline == 70


@pytest.mark.parametrize("total", [None, 0, -1, "262144", True, 1.5])
def test_unknown_or_invalid_size_cannot_extend(total):
    clock = Clock()
    budget = DownloadBudget(clock)
    qualifying_progress(budget, clock, total=total)
    with pytest.raises(TimeoutError):
        budget.check_direct()


@pytest.mark.parametrize("received,total", [
    (64 * 1024 - 1, 100 * 1024),  # Too few useful bytes.
    (128 * 1024, 512 * 1024),  # Less than half downloaded.
    (128 * 1024, 64 * 1024),  # Length is inconsistent with actual body.
])
def test_extension_requires_enough_bytes_and_a_valid_half_downloaded(received, total):
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 35
    budget.observe(0, total, "response")
    clock.now = 44
    budget.observe(received, total, "response")
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_slow_recent_rate_cannot_extend_even_when_half_downloaded():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 35
    budget.observe(7 * 64 * 1024, 16 * 64 * 1024, "response")
    clock.now = 44
    budget.observe(8 * 64 * 1024, 16 * 64 * 1024, "response")
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_old_burst_followed_by_one_byte_drip_cannot_extend():
    clock = Clock()
    budget = DownloadBudget(clock)
    budget.observe(0, 256 * 1024, "response")
    clock.now = 34.9
    budget.observe(128 * 1024, 256 * 1024, "response")
    for second in range(35, 45):
        clock.now = second
        budget.observe(128 * 1024 + second, 256 * 1024, "response")
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_stalled_response_and_repeated_observations_do_not_count_as_progress():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 35
    budget.observe(0, 256 * 1024, "response")
    clock.now = 42.9
    budget.observe(128 * 1024, 256 * 1024, "response")
    clock.now = 44.9
    budget.observe(128 * 1024, 256 * 1024, "response")
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_redirect_changes_progress_but_never_resets_deadlines():
    clock = Clock()
    budget = DownloadBudget(clock)
    qualifying_progress(budget, clock)
    budget.observe(0, 256 * 1024, "redirected-response")
    assert budget.direct_deadline == 45
    assert budget.total_deadline == 100
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_alternate_response_cannot_grant_a_second_extension():
    clock = Clock()
    budget = DownloadBudget(clock)
    qualifying_progress(budget, clock)
    budget.check_direct()
    clock.now = 60
    budget.observe(0, 256 * 1024, "alternate-response")
    clock.now = 69
    budget.observe(128 * 1024, 256 * 1024, "alternate-response")
    clock.now = 70
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_expired_direct_budget_cannot_be_revived_by_later_progress():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()
    budget.observe(0, 256 * 1024, "response")
    clock.now = 46
    budget.observe(128 * 1024, 256 * 1024, "response")
    with pytest.raises(TimeoutError):
        budget.check_direct()
    assert budget.capture_deadline() == 66


def test_body_completed_before_first_deadline_can_finish_processing():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 35
    budget.observe(0, 128 * 1024, "response")
    clock.now = 44.9
    budget.observe(128 * 1024, 128 * 1024, "response")
    clock.now = 45
    assert budget.check_direct() == 25


def test_browser_keeps_its_page_limit_and_is_clipped_to_shared_limit():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 45
    with pytest.raises(TimeoutError):
        budget.check_direct()
    assert budget.capture_deadline() == 65
    clock.now = 90
    assert budget.capture_deadline() == 100
    assert budget.check_total() == 10
    clock.now = 99.999
    assert budget.check_total() == pytest.approx(0.001)
    clock.now = 100
    with pytest.raises(TimeoutError):
        budget.check_total()
    with pytest.raises(TimeoutError):
        budget.capture_deadline()


def test_next_candidate_starts_fresh_budget():
    clock = Clock()
    first = DownloadBudget(clock)
    clock.now = 90
    second = DownloadBudget(clock)
    assert first.check_total() == 10
    assert second.check_direct() == 45
    assert second.check_total() == 100
    assert second.capture_deadline() == 110


def test_progress_at_hard_deadline_does_not_revive_download():
    clock = Clock()
    budget = DownloadBudget(clock)
    clock.now = 60
    budget.observe(0, 256 * 1024, "response")
    clock.now = 69
    budget.observe(128 * 1024, 256 * 1024, "response")
    clock.now = 70
    with pytest.raises(TimeoutError):
        budget.check_direct()
    assert not budget.extended


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_browser_budget_is_rejected(seconds):
    with pytest.raises(ValueError):
        DownloadBudget(Clock()).capture_deadline(seconds)


def test_progress_history_is_bounded_even_for_an_excessively_chatty_worker():
    clock = Clock()
    budget = DownloadBudget(clock)
    for received in range(10000):
        clock.now += 0.0001
        budget.observe(received, 2 * MIN_PROGRESS_BYTES, "response")
    assert len(budget._samples) <= 256


def test_regressing_byte_count_cannot_reuse_previous_progress():
    clock = Clock()
    budget = DownloadBudget(clock)
    qualifying_progress(budget, clock)
    budget.observe(1, 256 * 1024, "response")
    with pytest.raises(TimeoutError):
        budget.check_direct()


def test_request_budget_uses_network_context_and_releases_it_on_error():
    budget = DownloadBudget(Clock())
    entered = []

    @contextmanager
    def use_budget():
        entered.append("enter")
        try:
            yield budget
        finally:
            entered.append("exit")

    with pytest.raises(ValueError, match="synthetic failure"):
        with request_budget(SimpleNamespace(use_budget=use_budget)) as active:
            assert active is budget
            raise ValueError("synthetic failure")
    assert entered == ["enter", "exit"]


def test_request_budget_supports_injected_clients_without_budget_context():
    with request_budget(SimpleNamespace()) as first:
        assert isinstance(first, DownloadBudget)
    with request_budget(SimpleNamespace()) as second:
        assert isinstance(second, DownloadBudget)
        assert first is not second
