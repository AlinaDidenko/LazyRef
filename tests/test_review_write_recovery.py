"""Already purchased ratings survive disk failures without ignoring conditions."""

from copy import deepcopy
from threading import Event

import pytest
from PIL import Image

from lazyref import review_recovery
from lazyref.models import Asset, Candidate, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.retry_analysis import retry_review
from lazyref.review_session import ReviewSession
from test_core import make_asset
from test_session_security import CRITERIA, LocalClient
from test_wide_search import Agent, request as wide_request, run as run_wide


def pending_session(tmp_path, count=1):
    image = tmp_path / "fixture.jpg"
    Image.new("RGB", (200, 200), "blue").save(image)
    request = SearchRequest("A seated person", count, tmp_path / "results", model="fixture-model", reuse_cache=False)
    session = ReviewSession.create(request, {
        "summary": "A seated person", "queries": ["seated person"], "criteria": deepcopy(CRITERIA),
    })
    for number in range(count):
        session.add(Asset(Candidate(str(number), f"Photo {number}", page_url="https://photo.invalid/example"),
                          image, image, 200, 200), 1)
    return session


def no_analysis(*args, **kwargs):
    pytest.fail("The already completed rating must not be bought again")


def test_single_main_write_failure_keeps_rating_and_finishes_by_local_save(tmp_path, monkeypatch):
    session = pending_session(tmp_path)
    original_save = ReviewSession.save
    failures = []

    def fail_once(self):
        if not failures and any(row["assessment"] is not None for row in self.data["assets"]):
            failures.append(True)
            raise OSError("synthetic single session write failure")
        return original_save(self)

    monkeypatch.setattr(ReviewSession, "save", fail_once)
    client = LocalClient()
    result = retry_review(session.path, Event(), client=client)
    assert "single session write failure" in result.error
    assert client.calls == [["0"]]
    assert result.examined == 1 and result.pending_review == 0
    assert result.folder and len(result.assets) == 1
    assert not result.export_pending and result.retry_path is None
    assert ReviewSession.load(session.path).pending_count == 0
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_analysis)
    with pytest.raises(ValueError, match="вже оцінені"):
        retry_review(session.path, Event())


def test_persistent_main_write_failure_recovers_journal_after_memory_loss(tmp_path, monkeypatch):
    session = pending_session(tmp_path)
    original_save = ReviewSession.save

    def fail_main(self):
        if any(row["assessment"] is not None for row in self.data["assets"]):
            raise OSError("synthetic unavailable session file")
        return original_save(self)

    monkeypatch.setattr(ReviewSession, "save", fail_main)
    client = LocalClient()
    first = retry_review(session.path, Event(), client=client)
    assert first.error and first.export_pending and first.pending_review == 0
    assert first.examined == 1 and len(first.assets) == 1
    assert first.assets[0].path.is_file()
    assert client.calls == [["0"]]
    assert (session.path.parent / "assessment-recovery.json").is_file()
    review_recovery._MEMORY.clear()  # Simulate a fresh process with only the durable receipt.
    monkeypatch.setattr(ReviewSession, "save", original_save)
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_analysis)
    second = retry_review(session.path, Event())
    assert not second.error and second.pending_review == 0 and not second.export_pending
    assert second.examined == 1 and len(second.assets) == 1
    assert not (session.path.parent / "assessment-recovery.json").exists()


def test_failed_journal_and_main_writes_keep_ram_and_block_new_model_calls(tmp_path, monkeypatch):
    session = pending_session(tmp_path, count=4)
    original_save = ReviewSession.save
    original_write = review_recovery._write
    blocked = [True]

    def fail_main(self):
        if blocked[0] and any(row["assessment"] is not None for row in self.data["assets"]):
            raise OSError("synthetic session disk error")
        return original_save(self)

    def fail_journal(*args):
        if blocked[0]:
            raise OSError("synthetic receipt disk error")
        return original_write(*args)

    monkeypatch.setattr(ReviewSession, "save", fail_main)
    monkeypatch.setattr(review_recovery, "_write", fail_journal)
    client = LocalClient()
    first = retry_review(session.path, Event(), client=client)
    assert first.error and first.examined == 3 and first.pending_review == 1
    assert len(first.assets) == 3 and all(asset.path.is_file() for asset in first.assets)
    assert client.calls == [["0", "1", "2"]]
    assert "пам'яті" in first.error
    second_client = LocalClient()
    second = retry_review(session.path, Event(), client=second_client)
    assert second.error and second.examined == 3 and second.pending_review == 1
    assert second_client.calls == []  # Save the three receipts before buying the fourth.
    blocked[0] = False
    third_client = LocalClient()
    third = retry_review(session.path, Event(), client=third_client)
    assert not third.error and third.examined == 4 and third.pending_review == 0
    assert third_client.calls == [["3"]]
    assert len(third.assets) == 4


@pytest.mark.parametrize("strategy,expected", [("legacy", 6), ("wide", 3)])
def test_initial_search_preserves_completed_batch_and_stops_further_analysis(tmp_path, monkeypatch, strategy, expected):
    armed = [False]
    original_save = ReviewSession.save

    class ArmedAgent(Agent):
        def assess(self, *args, **kwargs):
            ratings = super().assess(*args, **kwargs)
            armed[0] = True
            return ratings

    def fail_after_model(self):
        if armed[0]:
            raise OSError("synthetic initial analysis write failure")
        return original_save(self)

    monkeypatch.setattr(ReviewSession, "save", fail_after_model)
    client = ArmedAgent()
    if strategy == "legacy":
        candidates = [Candidate(str(i), f"Photo {i}", page_url=f"https://photo.invalid/{i}") for i in range(8)]
        pipeline = SearchPipeline(client, finder=lambda *args: (candidates, []),
            loader=lambda c, directory, network: make_asset(directory, c.id, int(c.id)))
        request = SearchRequest("Man seated at a bar seen from behind", 8, tmp_path / "results",
                                candidate_limit=8, reuse_cache=False)
        result = pipeline.run(request, Event())
    else:
        request = wide_request(tmp_path / "results", reuse_cache=False)
        request.count = 8
        result = run_wide(request, client)
    assert "initial analysis write failure" in result.error
    assert client.assess_calls == 1
    assert result.examined == expected and len(result.assets) == expected
    assert result.export_pending and result.retry_path
    assert all(asset.assessment is not None and asset.path.is_file() for asset in result.assets)
    assert all(asset.path.is_relative_to(result.retry_path.parent) for asset in result.assets)
