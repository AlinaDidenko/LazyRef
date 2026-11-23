"""Monotonic time limits shared by direct downloads and browser fallback.

Each candidate gets one budget. Redirects and alternative URLs share it;
``resource_id`` identifies an individual HTTP response, not the candidate.
"""

from collections import deque
from contextlib import contextmanager
import math
import time
from typing import Callable


DIRECT_DOWNLOAD_SECONDS = 45.0
EXTENDED_DOWNLOAD_SECONDS = 70.0
CANDIDATE_DOWNLOAD_SECONDS = 100.0
PROGRESS_WINDOW_SECONDS = 10.0
MIN_PROGRESS_BYTES = 64 * 1024
MAX_PROGRESS_AGE_SECONDS = 2.0


class DownloadBudget:
    """A parent-side watchdog budget; no waits or network operations occur here.

    Report ``observe(0, content_length, response_id)`` when headers arrive, then
    cumulative bytes for that response. Unknown/invalid lengths do not qualify
    for an extension. Repeated observations without new bytes are not progress.
    """

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock if clock is not None else time.monotonic
        self.started_at = self._clock()
        self._direct_deadline = self.started_at + DIRECT_DOWNLOAD_SECONDS
        self._hard_direct_deadline = self.started_at + EXTENDED_DOWNLOAD_SECONDS
        self._total_deadline = self.started_at + CANDIDATE_DOWNLOAD_SECONDS
        self._extended = False
        self._direct_expired = False
        self._resource_id = object()
        self._received = 0
        self._total: int | None = None
        self._last_progress: float | None = None
        # At the worker's <=10 Hz report rate this holds the entire window.
        # A caller reporting faster can only lose history, never grow memory.
        self._samples: deque[tuple[float, int]] = deque(maxlen=256)

    @property
    def direct_deadline(self) -> float:
        return self._direct_deadline

    @property
    def total_deadline(self) -> float:
        return self._total_deadline

    @property
    def extended(self) -> bool:
        return self._extended

    def _trim_samples(self, now: float) -> None:
        cutoff = now - PROGRESS_WINDOW_SECONDS
        # Keep only observations inside the window. Including an older sample
        # would wrongly count an old burst as recent progress.
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def observe(self, received: int, total: int | None, resource_id: object) -> None:
        """Record cumulative response bytes without changing any deadline."""
        now = self._clock()
        valid_received = isinstance(received, int) and not isinstance(received, bool) and received >= 0
        valid_total = isinstance(total, int) and not isinstance(total, bool) and total > 0
        if not valid_received:
            self._samples.clear()
            self._total = None
            self._last_progress = None
            return
        known_total = total if valid_total and received <= total else None
        if (resource_id != self._resource_id or received < self._received
                or known_total != self._total):
            self._samples.clear()
            self._last_progress = now if received else None
        elif received > self._received:
            self._last_progress = now
        self._resource_id = resource_id
        self._received = received
        self._total = known_total
        self._samples.append((now, received))
        self._trim_samples(now)

    def _useful_progress(self, now: float) -> bool:
        self._trim_samples(now)
        if (self._total is None or self._received < MIN_PROGRESS_BYTES
                or 2 * self._received < self._total or len(self._samples) < 2
                or self._last_progress is None
                or now - self._last_progress > MAX_PROGRESS_AGE_SECONDS):
            return False
        first_time, first_received = self._samples[0]
        recent_bytes = self._received - first_received
        elapsed = now - first_time
        available = self._hard_direct_deadline - now
        if recent_bytes < MIN_PROGRESS_BYTES or elapsed <= 0 or available <= 0:
            return False
        # At the observed rate, the remaining body must fit before t=70.
        return self._total - self._received <= recent_bytes / elapsed * available

    def check_direct(self) -> float:
        """Return remaining direct seconds, or raise a latched TimeoutError.

        At t=45, useful progress can grant one extension to t=70. The extension
        never starts a fresh 25-second timer if the watchdog checks late.
        Expiring direct download still permits the browser's separate budget.
        """
        now = self._clock()
        if not self._direct_expired and now < self._direct_deadline:
            return self._direct_deadline - now
        if (not self._direct_expired and not self._extended
                and now < self._hard_direct_deadline and self._useful_progress(now)):
            self._extended = True
            self._direct_deadline = self._hard_direct_deadline
            return self._direct_deadline - now
        self._direct_expired = True
        raise TimeoutError("Direct image download time budget exhausted")

    def check_total(self) -> float:
        """Return remaining candidate seconds, or raise at the shared limit."""
        remaining = self._total_deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Image download and browser time budget exhausted")
        return remaining

    def capture_deadline(self, seconds: float = 20.0) -> float:
        """Clip the browser's existing per-page limit to this candidate's end."""
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Browser time budget must be finite and positive")
        now = self._clock()
        if now >= self._total_deadline:
            raise TimeoutError("Image download and browser time budget exhausted")
        return min(now + seconds, self._total_deadline)


@contextmanager
def request_budget(network):
    """Share the real network's candidate budget, including injected clients."""
    use_budget = getattr(network, "use_budget", None)
    if use_budget is not None:
        with use_budget() as budget:
            yield budget
    else:
        yield DownloadBudget()
