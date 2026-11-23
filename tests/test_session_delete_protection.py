"""Recovery images stay intact; independently saved copies remain removable."""

from pathlib import Path
import shutil
from threading import Event

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication

from lazyref import library_ui, review_recovery, ui
from lazyref.library import image_record
from lazyref.retry_analysis import retry_review
from lazyref.review_session import ReviewSession
from test_review_write_recovery import pending_session
from test_session_security import LocalClient


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def no_model(*args, **kwargs):
    pytest.fail("Recovery must use the already completed assessments")


def session_files(folder):
    return {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["export", "session_write"])
@pytest.mark.parametrize("relocated", [False, True], ids=["standard_session", "imported_session"])
def test_partial_result_cannot_delete_recovery_files_and_can_still_save(
        qt_app, tmp_path, monkeypatch, failure, relocated):
    import lazyref.retry_analysis as retry_module

    session = pending_session(tmp_path, count=2)
    if relocated:
        imported = tmp_path / "opened_review"
        shutil.copytree(session.path.parent, imported)
        session = ReviewSession.load(imported / "review.json")
        assert "_sessions" not in session.path.parts
    client = LocalClient()
    real_export, real_save = retry_module.export_results, ReviewSession.save

    def fail_export(*args, **kwargs):
        raise OSError("synthetic export failure")

    def fail_after_model(self):
        if client.calls:
            raise OSError("synthetic review write failure")
        return real_save(self)

    if failure == "export":
        monkeypatch.setattr(retry_module, "export_results", fail_export)
    else:
        monkeypatch.setattr(ReviewSession, "save", fail_after_model)
    result = retry_review(session.path, Event(), client=client)
    assert result.error and result.export_pending and result.pending_review == 0
    assert len(result.assets) == 2 and all(a.review_owned for a in result.assets)
    if failure == "session_write":
        assert (session.path.parent / "assessment-recovery.json").is_file()
    before = session_files(session.path.parent)

    monkeypatch.setattr(library_ui.QFile, "moveToTrash",
                        lambda *args: pytest.fail("A service copy must never reach the recycle bin"))
    errors, deleted = [], []
    monkeypatch.setattr(library_ui, "show_error", lambda parent, error: errors.append(str(error)))
    window = ui.MainWindow()
    window.save_settings = lambda: None
    try:
        window.show()
        window.show_results(result)
        qt_app.processEvents()
        cards = window.gallery.findChildren(ui.ImageCard)
        assert len(cards) == 2
        card = cards[0]
        actions = card.file_actions
        assert actions.item.review_owned
        actions.deleted.connect(deleted.append)
        menu = actions.menu(card)
        entries = {action.text(): action for action in menu.actions() if not action.isSeparator()}
        assert not entries["Видалити"].isEnabled()
        assert entries["Видалити"].toolTip()
        assert entries["Шукати референси за цим зображенням"].isEnabled()
        assert entries["Обрізати й зберегти копію"].isEnabled()
        menu.deleteLater()
        grid_count = window.grid.count()

        actions.remove()  # A direct invocation must be guarded too.
        with pytest.raises(ValueError):
            library_ui.trash_image(actions.item)

        assert len(errors) == 1 and not deleted
        assert window.grid.count() == grid_count and not card.isHidden()
        assert session_files(session.path.parent) == before
    finally:
        window.close()
        shiboken6.delete(window)

    monkeypatch.setattr(retry_module, "export_results", real_export)
    monkeypatch.setattr(ReviewSession, "save", real_save)
    monkeypatch.setattr(retry_module, "CodexClient", no_model)
    monkeypatch.setattr(review_recovery, "_MEMORY", {})
    recovered = retry_review(session.path, Event(), save_only=True)
    assert not recovered.error and len(recovered.assets) == 2
    assert not recovered.export_pending and not recovered.pending_review
    assert len(client.calls) == 1
    assert all(not asset.review_owned for asset in recovered.assets)


def test_blocked_delete_keeps_resume_limited_to_the_remaining_unassessed_photo(
        qt_app, tmp_path, monkeypatch):
    session = pending_session(tmp_path, count=4)
    client = LocalClient()
    real_save = ReviewSession.save

    def fail_after_first_batch(self):
        if client.calls:
            raise OSError("synthetic write failure after first batch")
        return real_save(self)

    monkeypatch.setattr(ReviewSession, "save", fail_after_first_batch)
    result = retry_review(session.path, Event(), client=client)
    assert result.error and result.pending_review == 1 and len(result.assets) == 3
    assert client.calls == [["0", "1", "2"]]
    assert all(asset.review_owned for asset in result.assets)
    before = session_files(session.path.parent)
    assert Path("assessment-recovery.json") in before
    errors, deleted = [], []
    monkeypatch.setattr(library_ui, "show_error", lambda parent, error: errors.append(str(error)))
    monkeypatch.setattr(library_ui.QFile, "moveToTrash",
                        lambda *args: pytest.fail("A pending session still needs every original"))
    card = ui.ImageCard(result.assets[0], 1, on_deleted=deleted.append)
    try:
        card.file_actions.remove()
        assert errors and not deleted
        assert session_files(session.path.parent) == before
    finally:
        card.close()
        shiboken6.delete(card)

    monkeypatch.setattr(ReviewSession, "save", real_save)
    monkeypatch.setattr(review_recovery, "_MEMORY", {})
    monkeypatch.setattr("lazyref.retry_analysis.CodexClient", no_model)
    remaining_client = LocalClient()
    recovered = retry_review(session.path, Event(), client=remaining_client)
    assert not recovered.error and len(recovered.assets) == 4
    assert remaining_client.calls == [["3"]]
    assert not recovered.pending_review and not recovered.export_pending


@pytest.mark.parametrize("partial", [False, True], ids=["complete_export", "partial_export"])
def test_exported_copies_can_be_deleted_without_touching_session(
        qt_app, tmp_path, monkeypatch, partial):
    import lazyref.exporter as exporter

    session = pending_session(tmp_path, count=2)
    client = LocalClient()
    real_copy = exporter.shutil.copyfile
    copied = []

    def one_export_copy(source, target, *args, **kwargs):
        if Path(target).parent.name.startswith("LazyRef_"):
            if copied:
                raise OSError("synthetic second export copy failure")
            copied.append(Path(target))
        return real_copy(source, target, *args, **kwargs)

    if partial:
        monkeypatch.setattr(exporter.shutil, "copyfile", one_export_copy)
    result = retry_review(session.path, Event(), client=client)
    assert len(result.assets) == (1 if partial else 2)
    assert result.export_pending is partial
    assert all(not asset.review_owned for asset in result.assets)
    before = session_files(session.path.parent)
    moved, deleted = [], []
    trash = tmp_path / "fake_recycle_bin.jpg"

    def fake_trash(filename):
        moved.append(filename)
        Path(filename).rename(trash)
        return True, str(trash)

    monkeypatch.setattr(library_ui.QFile, "moveToTrash", fake_trash)
    window = ui.MainWindow()
    window.save_settings = lambda: None
    try:
        window.show_results(result)
        card = window.gallery.findChildren(ui.ImageCard)[0]
        actions = card.file_actions
        actions.deleted.connect(deleted.append)
        menu = actions.menu(card)
        assert menu.actions()[-1].isEnabled()
        menu.deleteLater()
        actions.remove()
        assert moved == deleted == [str(result.assets[0].path)]
        assert trash.is_file() and not result.assets[0].path.exists()
        assert session_files(session.path.parent) == before
        assert all(asset.review_owned for asset in ReviewSession.load(session.path).assets())
    finally:
        window.close()
        shiboken6.delete(window)


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_initial_search_failure_marks_its_visible_session_copies(tmp_path, monkeypatch, strategy):
    from lazyref.models import Candidate, SearchRequest
    from lazyref.pipeline import SearchPipeline
    from test_core import make_asset
    from test_wide_search import Agent, request as wide_request, run as run_wide

    def fail_export(*args, **kwargs):
        raise OSError("synthetic initial export failure")

    client = Agent()
    if strategy == "legacy":
        monkeypatch.setattr("lazyref.pipeline.export_results", fail_export)
        candidates = [Candidate("first", "Photo", page_url="https://photo.invalid/first")]
        pipeline = SearchPipeline(client, finder=lambda *args: (candidates, []),
                                  loader=lambda c, directory, network: make_asset(directory, c.id, 1))
        result = pipeline.run(SearchRequest("Man seated at a bar from behind", 1,
                                            tmp_path / "results", reuse_cache=False), Event())
    else:
        monkeypatch.setattr("lazyref.wide_search.export_results", fail_export)
        result = run_wide(wide_request(tmp_path / "results", reuse_cache=False), client)

    assert result.error and result.export_pending and result.assets
    assert all(asset.review_owned for asset in result.assets)
    assert all(asset.review_owned for asset in ReviewSession.load(result.retry_path).assets())


@pytest.mark.parametrize("copy_kind", ["crop", "import", "generated"])
def test_independent_gallery_images_remain_removable(qt_app, tmp_path, monkeypatch, copy_kind):
    from lazyref.cropping import save_crop
    from lazyref.importing import import_reference
    from PIL import Image

    session = pending_session(tmp_path)
    asset = session.assets()[0]
    protected = image_record(asset.path, asset.path.parent, "reference", review_owned=asset.review_owned)
    before = session_files(session.path.parent)
    if copy_kind == "crop":
        item = save_crop(protected, (0, 0, 100, 100), tmp_path / "workspace")
    elif copy_kind == "import":
        item = import_reference(protected.path, tmp_path / "workspace")
    else:
        generated = tmp_path / "generated.png"
        Image.new("RGB", (100, 100), "green").save(generated)
        item = image_record(generated, tmp_path, "generated")
    assert not item.review_owned
    moved = []
    trash = tmp_path / "fake_recycle_bin.png"

    def fake_trash(filename):
        moved.append(filename)
        Path(filename).rename(trash)
        return True

    monkeypatch.setattr(library_ui.QFile, "moveToTrash", fake_trash)
    library_ui.trash_image(item)
    assert moved == [str(item.path)] and trash.is_file()
    assert session_files(session.path.parent) == before
