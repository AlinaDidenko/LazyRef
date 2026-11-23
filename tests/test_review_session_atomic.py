"""The main session survives interrupted writes; UI counts avoid image reads."""

from dataclasses import replace
import json

import pytest

from lazyref import review_session
from test_review_condition_reuse import create_session, retain_rating


@pytest.mark.parametrize("failure", ["write", "flush", "replace"])
def test_failed_session_commit_preserves_previous_json_and_removes_temporary(tmp_path, monkeypatch, failure):
    path = tmp_path / "review.json"
    review_session.atomic_json(path, {"original": True})
    before = path.read_bytes()

    def fail_write(data, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError("synthetic interrupted write")

    def fail(*args, **kwargs):
        raise OSError("synthetic commit failure")

    if failure == "write":
        monkeypatch.setattr(review_session.json, "dump", fail_write)
    elif failure == "flush":
        monkeypatch.setattr(review_session.os, "fsync", fail)
    else:
        monkeypatch.setattr(type(path), "replace", fail)
    with pytest.raises(OSError):
        review_session.atomic_json(path, {"replacement": True})
    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8")) == {"original": True}
    assert list(tmp_path.iterdir()) == [path]


def test_ui_pending_preview_checks_conditions_without_reading_candidate_images(tmp_path, monkeypatch):
    session = create_session(tmp_path)
    retain_rating(session, "committed")
    request = session.request()

    def forbid(*args, **kwargs):
        pytest.fail("Full image integrity checks belong in the review worker")

    monkeypatch.setattr(session, "owned_file", forbid)
    monkeypatch.setattr(review_session, "digest", forbid)
    assert session.pending_for(request) == 0
    assert session.pending_for(replace(request, reasoning_effort="high")) == 1
    assert session.pending_for(replace(request, model="different-model")) == 1
