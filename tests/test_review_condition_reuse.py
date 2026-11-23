"""Completed and recoverable ratings belong only to identical analysis inputs."""

from copy import deepcopy
from dataclasses import asdict
from threading import Event

import pytest
from PIL import Image

from lazyref.models import Assessment, Asset, Candidate, SearchRequest
from lazyref.review_identity import row_key
from lazyref.review_recovery import remember
from lazyref.review_session import ReviewSession, digest
from lazyref.retry_analysis import retry_review
from test_session_security import CRITERIA


def rating_for(criteria, profile="strict", verify=True):
    return Assessment(
        "photo" if verify else "unverified", "high" if verify else "not_checked",
        90, "Synthetic matching photo", ["Visible criteria"], [],
        criterion_checks=[{**{k: c[k] for k in ("id", "kind", "label")},
                           **({"priority": c["priority"]} if profile == "custom" else {}),
                           "status": "met", "evidence": "Synthetic visible match"} for c in criteria],
        search_profile=profile, verify_sources=verify,
    )


class RecordingClient:
    def __init__(self):
        self.calls = []

    def assess(self, description, assets, cancel, **kwargs):
        self.calls.append((description, [asset.candidate.id for asset in assets], deepcopy(kwargs)))
        return {asset.candidate.id: rating_for(kwargs["criteria"], kwargs.get("search_profile", "strict"),
                                               kwargs.get("verify_sources", True)) for asset in assets}


def create_session(tmp_path, *, custom=False, guide=False):
    image = tmp_path / "photo.jpg"
    Image.new("RGB", (200, 200), "blue").save(image)
    reference = tmp_path / "guide.png"
    if guide:
        Image.new("RGB", (200, 200), "green").save(reference)
    criteria = deepcopy(CRITERIA)
    if custom:
        for criterion in criteria:
            criterion["priority"] = "required"
    request = SearchRequest(
        "A seated person", 1, tmp_path / "results", model="fixture-model", reasoning_effort="medium",
        search_profile="custom" if custom else "strict", custom_criteria=deepcopy(criteria) if custom else None,
        reference_image=reference if guide else None, reuse_cache=False,
    )
    session = ReviewSession.create(request, {"summary": "Seated person", "queries": ["seated"], "criteria": criteria})
    session.add(Asset(Candidate("photo", "Photo", page_url="https://photos.invalid/view"),
                      image, image, 200, 200), 1)
    return session


def retain_rating(session, storage):
    request = session.request()
    context = session.context(request)
    rating = rating_for(session.data["plan"]["criteria"], request.search_profile, request.verify_sources)
    if storage == "committed":
        session.complete(session.assets(request=request), {"photo": rating}, request.search_profile, context=context)
        assert session.data["assets"][0]["assessment_key"] == row_key(context, session.data["assets"][0])
    else:
        warning = remember(session.path, session.data["id"], {
            "photo": {"key": row_key(context, session.data["assets"][0]), "assessment": asdict(rating)}})
        assert warning is None


@pytest.mark.parametrize("storage", ["receipt", "committed"])
def test_matching_inputs_reuse_rating_for_local_export_without_model(tmp_path, storage):
    session = create_session(tmp_path)
    retain_rating(session, storage)
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client)

    assert not result.error and result.examined == 1 and len(result.assets) == 1
    assert not client.calls and result.pending_review == 0
    assert result.folder and (result.folder / "manifest.json").is_file()


@pytest.mark.parametrize("storage", ["receipt", "committed"])
@pytest.mark.parametrize("change", ["description", "criteria", "priority", "guide", "full", "preview", "source"])
def test_changed_conditions_or_image_do_not_reuse_old_rating(tmp_path, storage, change):
    session = create_session(tmp_path, custom=change == "priority", guide=change == "guide")
    retain_rating(session, storage)
    row = session.data["assets"][0]
    if change == "description":
        session.data["request"]["description"] = "A standing person seen from the front"
    elif change == "criteria":
        session.data["plan"]["criteria"][1]["label"] = "Standing pose"
    elif change == "priority":
        session.data["request"]["custom_criteria"][1]["priority"] = "preferred"
        session.data["plan"]["criteria"][1]["priority"] = "preferred"
    elif change == "guide":
        path = session.path.parent / session.data["guide"]["file"]
        Image.new("RGB", (200, 200), "red").save(path)
        session.data["guide"]["sha256"] = digest(path)
    elif change in {"full", "preview"}:
        field, checksum = ("file", "sha256") if change == "full" else ("preview", "preview_sha256")
        path = session.path.parent / row[field]
        Image.new("RGB", (200, 200), "red").save(path)
        row[checksum] = digest(path)
    else:
        row["candidate"]["source_notes"] = "New source evidence"
        row["candidate"]["source_checked"] = True
    session.save()
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client)

    assert not result.error and len(result.assets) == 1
    assert len(client.calls) == 1 and client.calls[0][1] == ["photo"]
    assert result.pending_review == 0


@pytest.mark.parametrize("storage", ["receipt", "committed"])
@pytest.mark.parametrize("override", [{"model": "different-model"}, {"reasoning_effort": "high"}])
def test_effective_model_or_reasoning_override_requires_fresh_analysis(tmp_path, storage, override):
    session = create_session(tmp_path)
    retain_rating(session, storage)
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client, **override)

    assert not result.error and result.examined == 1
    assert len(client.calls) == 1 and client.calls[0][1] == ["photo"]
    # Original session conditions remain historical; an override must not
    # relabel its new rating as if the original model/settings produced it.
    saved = ReviewSession.load(session.path)
    assert saved.data["request"]["model"] == "fixture-model"
    assert saved.data["request"]["reasoning_effort"] == "medium"
    overridden = saved.request(**override)
    assert saved.data["assets"][0]["assessment_key"] == row_key(saved.context(overridden), saved.data["assets"][0])


@pytest.mark.parametrize("storage", ["receipt", "committed"])
@pytest.mark.parametrize("target", ["full", "preview", "guide"])
def test_changed_file_without_matching_checksum_blocks_before_any_model_call(tmp_path, storage, target):
    session = create_session(tmp_path, guide=True)
    retain_rating(session, storage)
    row = session.data["assets"][0]
    filename = session.data["guide"]["file"] if target == "guide" else row["file" if target == "full" else "preview"]
    (session.path.parent / filename).write_bytes(b"synthetic damaged file")
    client = RecordingClient()

    with pytest.raises(ValueError):
        retry_review(session.path, Event(), client=client)

    assert not client.calls


def test_failed_session_commit_keeps_rating_and_retries_only_local_save(tmp_path, monkeypatch):
    session = create_session(tmp_path)
    request = session.request()
    assets = session.assets(request=request)
    context = session.context(request)
    real_save = session.save

    def fail_write():
        raise OSError("synthetic review.json write failure")

    monkeypatch.setattr(session, "save", fail_write)
    session.complete(assets, {"photo": rating_for(session.data["plan"]["criteria"])},
                     request.search_profile, context=context)
    assert session.needs_save and "synthetic" in session.persistence_error
    assert assets[0].assessment is not None
    monkeypatch.setattr(session, "save", real_save)
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client)

    assert not result.error and result.examined == 1 and len(result.assets) == 1
    assert not client.calls and result.pending_review == 0


@pytest.mark.parametrize("override", [{}, {"model": "different-model"}, {"reasoning_effort": "high"}])
def test_legacy_rating_is_only_reused_for_original_conditions(tmp_path, override):
    session = create_session(tmp_path)
    # Simulate a session written before provenance keys existed.
    session.data["assets"][0]["assessment"] = asdict(rating_for(session.data["plan"]["criteria"]))
    session.data["export_pending"] = True
    session.save()
    client = RecordingClient()

    result = retry_review(session.path, Event(), client=client, **override)

    assert not result.error and result.examined == 1
    assert len(client.calls) == (1 if override else 0)
