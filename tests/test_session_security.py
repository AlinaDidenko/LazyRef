"""Imported sessions stay data: no external URI handlers or unchecked ratings."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

from lazyref.models import Assessment, Asset, Candidate, SearchRequest
from lazyref.review_session import OPTIONAL_REQUEST_FIELDS, ReviewSession
from lazyref.retry_analysis import retry_review


CRITERIA = [
    {"id": "c1", "label": "Person", "kind": "subject", "keywords": ["person"]},
    {"id": "c2", "label": "Seated pose", "kind": "pose", "keywords": ["seated"]},
]
UNSAFE_URLS = [
    "file:///C:/DO_NOT_EXECUTE.txt",
    "javascript:alert(1)",
    "ms-settings:privacy",
    r"\\untrusted.invalid\share\DO_NOT_OPEN.txt",
    "https://public.invalid\\@127.0.0.1/photo",
]


def valid_rating(profile="strict", verify_sources=True):
    return Assessment(
        "photo" if verify_sources else "unverified",
        "high" if verify_sources else "not_checked",
        90, "Visible seated person", ["Seated"], [],
        criterion_checks=[
            {"id": c["id"], "label": c["label"], "kind": c["kind"],
             "status": "met", "evidence": "Visible"}
            for c in CRITERIA
        ],
        search_profile=profile, verify_sources=verify_sources,
    )


def saved_session(tmp_path, profile="strict", verify_sources=True):
    image = tmp_path / "fixture.jpg"
    Image.new("RGB", (200, 200), "blue").save(image)
    request = SearchRequest(
        "A seated person", 2, tmp_path / "results", search_profile=profile,
        verify_sources=verify_sources, reuse_cache=False,
    )
    session = ReviewSession.create(request, {
        "summary": "Seated person", "queries": ["seated person"],
        "criteria": deepcopy(CRITERIA),
    })
    for identifier in ("completed", "pending"):
        session.add(Asset(
            Candidate(identifier, identifier, page_url="https://example.org/photo"),
            image, image, 200, 200,
        ), 1)
    session.data["assets"][0]["assessment"] = asdict(valid_rating(profile, verify_sources))
    session.save()
    return session


class LocalClient:
    def __init__(self, profile="strict", verify_sources=True):
        self.calls = []
        self.profile, self.verify_sources = profile, verify_sources

    def assess(self, description, assets, cancel, **kwargs):
        self.calls.append([asset.candidate.id for asset in assets])
        return {asset.candidate.id: valid_rating(self.profile, self.verify_sources)
                for asset in assets}


@pytest.mark.parametrize("filename", [
    r"\\storage.invalid\photos\guide.jpg", "//storage.invalid/photos/guide.jpg",
    "C:/photos/guide.jpg", "C:guide.jpg", "../guide.jpg", "images/.. /guide.jpg",
])
def test_session_rejects_nonlocal_names_before_filesystem_access(tmp_path, monkeypatch, filename):
    session = ReviewSession(tmp_path / "review.json", {})

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("A rejected manifest path reached the filesystem")

    with monkeypatch.context() as guard:
        for method in ("resolve", "lstat", "stat"):
            guard.setattr(Path, method, unexpected_lookup)
        with pytest.raises(ValueError):
            session.owned_file(filename, "")


def test_session_stops_at_redirected_directory_before_checking_image(tmp_path, monkeypatch):
    session = ReviewSession(tmp_path / "review.json", {})
    directory = tmp_path / "images"
    checked = []

    def is_link(path):
        checked.append(path)
        return path == directory

    monkeypatch.setattr("lazyref.library.is_link", is_link)
    with pytest.raises(ValueError):
        session.owned_file("images/photo.jpg", "")
    assert checked[-1] == directory
    assert directory / "photo.jpg" not in checked


@pytest.mark.parametrize("criteria", [None, [], "not criteria"])
def test_imported_session_requires_a_valid_plan_before_model_call(tmp_path, criteria):
    session = saved_session(tmp_path)
    session.data["plan"]["criteria"] = criteria
    session.save()
    client = LocalClient()
    with pytest.raises(ValueError):
        retry_review(session.path, Event(), client=client)
    assert not client.calls


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_imported_source_uri_is_rejected_before_model_call(tmp_path, url):
    session = saved_session(tmp_path)
    session.data["assets"][0]["candidate"]["page_url"] = url
    session.save()
    client = LocalClient()
    with pytest.raises(ValueError):
        retry_review(session.path, Event(), client=client)
    assert not client.calls


@pytest.mark.parametrize("profile,verify_sources", [
    ("strict", True), ("pose", True), ("strict", False), ("pose", False),
])
def test_valid_imported_session_preserves_completed_assessment(tmp_path, profile, verify_sources):
    session = saved_session(tmp_path, profile, verify_sources)
    client = LocalClient(profile, verify_sources)
    result = retry_review(session.path, Event(), client=client)
    assert not result.error
    assert client.calls == [["pending"]]
    assert {asset.candidate.id for asset in result.assets} == {"completed", "pending"}
    assert all(asset.assessment.verify_sources is verify_sources for asset in result.assets)


def test_legacy_session_defaults_remain_usable(tmp_path):
    session = saved_session(tmp_path)
    for key in OPTIONAL_REQUEST_FIELDS:
        session.data["request"].pop(key, None)
    session.data["assets"][0]["assessment"].pop("verify_sources")
    session.save()
    client = LocalClient()
    result = retry_review(session.path, Event(), client=client)
    assert not result.error and len(result.assets) == 2
    assert client.calls == [["pending"]]
    assert result.verify_sources is True


@pytest.mark.parametrize("damage", [
    "empty", "missing", "duplicate", "unknown_id", "kind", "label", "status",
    "profile", "mode", "boolean_mode", "boolean_score", "score_range",
    "reason_type", "matches_type", "misses_type",
])
def test_corrupt_saved_assessment_fails_before_model_call(tmp_path, damage):
    session = saved_session(tmp_path)
    rating = session.data["assets"][0]["assessment"]
    checks = rating["criterion_checks"]
    if damage == "empty":
        rating["criterion_checks"] = []
    elif damage == "missing":
        checks.pop()
    elif damage == "duplicate":
        checks[1] = deepcopy(checks[0])
    elif damage == "unknown_id":
        checks[1]["id"] = "not-in-plan"
    elif damage == "kind":
        checks[1]["kind"] = "detail"
    elif damage == "label":
        checks[1]["label"] = "Unrelated criterion"
    elif damage == "status":
        checks[1]["status"] = "unknown"
    elif damage == "profile":
        rating["search_profile"] = "pose"
    elif damage == "mode":
        rating["verify_sources"] = False
    elif damage == "boolean_mode":
        rating["verify_sources"] = 1
    elif damage == "boolean_score":
        rating["score"] = True
    elif damage == "score_range":
        rating["score"] = 101
    elif damage == "reason_type":
        rating["reason"] = ["not text"]
    elif damage == "matches_type":
        rating["matches"] = "not a list"
    elif damage == "misses_type":
        rating["misses"] = {"not": "a list"}
    session.save()
    client = LocalClient()
    with pytest.raises(ValueError):
        retry_review(session.path, Event(), client=client)
    assert not client.calls


@pytest.fixture(scope="module")
def session_qt_app():
    import os
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_image_card_never_opens_unsafe_source_uri(session_qt_app, tmp_path, monkeypatch, url):
    from PySide6.QtWidgets import QPushButton
    import shiboken6
    from lazyref.ui import ImageCard
    image = tmp_path / "fixture.jpg"
    Image.new("RGB", (200, 200), "blue").save(image)
    asset = Asset(Candidate("image", "image", page_url=url), image, image, 200, 200,
                  assessment=valid_rating())
    opened = []
    monkeypatch.setattr("lazyref.ui.QDesktopServices.openUrl", lambda uri: opened.append(uri.toString()))
    monkeypatch.setattr("lazyref.ui.QMessageBox.warning", lambda *args: None)
    monkeypatch.setattr("lazyref.ui.QMessageBox.information", lambda *args: None)
    card = ImageCard(asset, 1)
    try:
        for button in card.findChildren(QPushButton):
            if button.text() == "Джерело":
                button.click()
        assert opened == []
    finally:
        shiboken6.delete(card)


@pytest.mark.parametrize("url,expected", [
    ("file:///C:/DO_NOT_EXECUTE.txt", []),
    ("https://www.deviantart.com/artist/art/study", ["https://www.deviantart.com/artist/art/study"]),
])
def test_manual_access_links_use_same_safe_scheme_policy(session_qt_app, monkeypatch, url, expected):
    from PySide6.QtWidgets import QPushButton
    import shiboken6
    from lazyref.ui import AccessLinksDialog
    opened = []
    monkeypatch.setattr("lazyref.ui.QDesktopServices.openUrl", lambda uri: opened.append(uri.toString()))
    monkeypatch.setattr("lazyref.ui.QMessageBox.warning", lambda *args: None)
    monkeypatch.setattr("lazyref.ui.QMessageBox.information", lambda *args: None)
    dialog = AccessLinksDialog([{"title": "Study", "reason": "Unavailable", "url": url}], None)
    try:
        for button in dialog.findChildren(QPushButton):
            if button.text() == "Відкрити на DeviantArt":
                button.click()
        assert opened == expected
    finally:
        shiboken6.delete(dialog)
