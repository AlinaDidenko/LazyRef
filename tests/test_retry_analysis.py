"""Відновлення після збоїв: тільки локальні фото, без реальних викликів Codex."""

from copy import deepcopy
from pathlib import Path
from threading import Event
import json

import pytest

from lazyref.models import Candidate, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.retry_analysis import ReviewLock, retry_review
from lazyref.review_session import ReviewSession, list_pending
from lazyref.usage import format_usage_report
from test_core import make_asset, picture
from test_search_quality import PLAN, rating


class Client:
    def __init__(self, fail_at=None, cancel_after=None, incomplete=False):
        self.fail_at, self.cancel_after, self.incomplete = fail_at, cancel_after, incomplete
        self.batches, self.guides, self.plans = [], [], 0

    def usage(self, stage, count=0, failed=False):
        call = self.usage_ledger.begin(stage, "test-no-network", count)
        call.observe({"type": "turn.started"})
        if not failed:
            call.observe({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})
        call.finish("timed_out" if failed else "completed")

    def plan(self, *args, **kwargs):
        self.plans += 1
        self.usage("planning")
        return deepcopy(PLAN)

    def assess(self, description, batch, cancel, **kwargs):
        self.batches.append([a.candidate.id for a in batch])
        guide = kwargs.get("reference_image")
        if guide:
            assert guide.is_file()
            self.guides.append(guide)
        failed = len(self.batches) == self.fail_at
        self.usage("assessment", len(batch) + bool(guide), failed)
        if failed:
            raise TimeoutError("Test timeout")
        if len(self.batches) == self.cancel_after:
            cancel.set()
        result = {a.candidate.id: rating(70, failed=("c3",)) for a in batch}
        if self.incomplete:
            result.pop(batch[-1].candidate.id)
        return result


def interrupted(tmp_path, *, count=6, fail_at=1, guide=False):
    reference = tmp_path / "guide.png"
    if guide:
        picture().save(reference)
    request = SearchRequest("Чоловік сидить за баром зі спини", count, tmp_path / "out", candidate_limit=8,
        reference_image=reference if guide else None, search_profile="pose",
        codex_path="DO-NOT-SAVE-EXECUTABLE")
    candidates = [Candidate(str(i), f"Candidate {i}") for i in range(count)]
    def loader(candidate, directory, network):
        asset = make_asset(directory, candidate.id, int(candidate.id))
        asset.candidate = candidate
        return asset
    result = SearchPipeline(Client(fail_at), lambda *a: (candidates, []), loader).run(request, Event())
    assert result.error and result.retry_path and result.retry_path.is_file()
    return result, reference


def test_retry_after_restart_uses_only_saved_images_and_retains_original_guide(tmp_path, monkeypatch):
    first, guide = interrupted(tmp_path, guide=True)
    original = (first.folder / "manifest.json").read_bytes()
    assert first.pending_review == 6 and first.examined == 0 and not first.assets
    session = ReviewSession.load(first.retry_path)
    assert len(session.assets()) == 6
    assert all(a.path.exists() and a.preview.exists() for a in session.assets())
    assert "DO-NOT-SAVE" not in first.retry_path.read_text(encoding="utf-8")
    assert len(list_pending(tmp_path / "out")) == 1
    guide.unlink()  # Переміщення оригінального пакета не зриває повтор: зразок уже скопійовано.
    monkeypatch.setattr("lazyref.network.Network.__init__", lambda *a, **kw: pytest.fail("No network/download on retry"))
    client = Client()
    second = retry_review(first.retry_path, Event(), client=client)
    assert not second.error and len(second.assets) == 6 and second.examined == 6
    assert [len(b) for b in client.batches] == [3, 3] and client.plans == 0
    assert len(client.guides) == 2 and all(p.parent == first.retry_path.parent for p in client.guides)
    assert second.pending_review == 0 and second.retry_path is None
    assert not list_pending(tmp_path / "out")
    assert second.folder != first.folder and (first.folder / "manifest.json").read_bytes() == original
    manifest = json.loads((second.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["statistics"]["search_mode"] == "assessment_retry"
    assert manifest["statistics"]["newly_examined"] == 6
    assert manifest["reference_guide"]["sha256"] == session.data["guide"]["sha256"]
    assert second.usage["summary"]["reported_tokens"]["total_tokens"] == 240
    assert second.usage["all_attempts_summary"]["reported_tokens"]["total_tokens"] == 360
    assert second.usage["all_attempts_summary"]["incomplete_calls"] == 1
    assert "Попередні спроби" in format_usage_report(second.usage)
    assert len(ReviewSession.load(first.retry_path).available_usage()) == 2  # Дзеркала не подвоюють рахунок.
    with pytest.raises(ValueError, match="вже оцінені"):
        retry_review(first.retry_path, Event(), client=Client())


def test_retry_failure_then_cancel_then_success_never_resends_completed_photos(tmp_path):
    first, _ = interrupted(tmp_path)
    failing = Client(fail_at=1)
    again = retry_review(first.retry_path, Event(), client=failing)
    assert again.error and again.pending_review == 6 and not again.assets
    cancelled = retry_review(first.retry_path, Event(), client=Client(cancel_after=1))
    assert cancelled.cancelled and cancelled.pending_review == 3 and len(cancelled.assets) == 3
    assert ReviewSession.load(first.retry_path).pending_count == 3
    last_client = Client()
    last = retry_review(first.retry_path, Event(), client=last_client)
    assert not last.error and not last.cancelled and len(last.assets) == 6
    assert last_client.batches == [["3", "4", "5"]] and last_client.plans == 0
    assert last.usage["summary"]["reported_tokens"]["total_tokens"] == 120
    assert last.usage["all_attempts_summary"]["reported_tokens"]["total_tokens"] == 360
    assert last.usage["all_attempts_summary"]["incomplete_calls"] == 2
    assert len(last.usage["previous_attempts"]) == 3


def test_previously_accepted_assets_remain_in_combined_results(tmp_path):
    first, _ = interrupted(tmp_path, count=8, fail_at=2)
    assert len(first.assets) == 6 and first.pending_review == 2
    previous_ids = {a.candidate.id for a in first.assets}
    client = Client()
    second = retry_review(first.retry_path, Event(), client=client)
    assert client.batches == [["6", "7"]]
    assert len(second.assets) == 8 and second.examined == 8
    assert previous_ids.issubset({a.candidate.id for a in second.assets})
    assert all(a.path.is_file() for a in first.assets)


def test_partial_model_response_keeps_whole_batch_pending(tmp_path):
    first, _ = interrupted(tmp_path)
    result = retry_review(first.retry_path, Event(), client=Client(incomplete=True))
    assert result.error and result.pending_review == 6 and result.examined == 0
    assert all(a.assessment is None for a in ReviewSession.load(first.retry_path).assets())


@pytest.mark.parametrize("damage", ["missing", "changed", "outside", "guide"])
def test_damaged_or_outside_files_fail_before_model_call(tmp_path, damage):
    first, _ = interrupted(tmp_path, guide=True)
    session = ReviewSession.load(first.retry_path)
    row = session.data["assets"][0]
    if damage == "missing":
        (session.path.parent / row["file"]).unlink()
    elif damage == "changed":
        (session.path.parent / row["preview"]).write_bytes(b"changed")
    elif damage == "outside":
        row["file"] = str(tmp_path / "outside.jpg")
        session.save()
    else:
        (session.path.parent / session.data["guide"]["file"]).unlink()
    client = Client()
    with pytest.raises(ValueError):
        retry_review(first.retry_path, Event(), client=client)
    assert client.batches == [] and client.plans == 0


def test_cancel_before_retry_keeps_all_candidates_and_records_no_model_calls(tmp_path):
    first, _ = interrupted(tmp_path)
    event = Event()
    event.set()
    client = Client()
    result = retry_review(first.retry_path, event, client=client)
    assert result.cancelled and result.pending_review == 6 and not client.batches
    assert result.usage["summary"]["calls"] == 0


def test_lock_rejects_concurrent_resume_and_releases_on_error(tmp_path):
    path = tmp_path / "review.lock"
    with pytest.raises(RuntimeError):
        with ReviewLock(path):
            with pytest.raises(ValueError, match="вже триває"):
                with ReviewLock(path):
                    pytest.fail("Second process must not start")
            raise RuntimeError("simulate failure")
    with ReviewLock(path):
        pass
