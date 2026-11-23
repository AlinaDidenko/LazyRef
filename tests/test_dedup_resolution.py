"""Larger originals survive early dedup without changing OFF behavior or budgets."""

from copy import deepcopy
import json
from threading import Event

import pytest
from PIL import Image

from lazyref.images import DuplicateIndex
from lazyref.models import Asset, Candidate, SearchRequest
from lazyref.pipeline import SearchPipeline
from lazyref.retry_analysis import retry_review
from lazyref.review_session import ReviewSession
from lazyref.wide_agent import SCREEN_SCHEMA, SOURCE_SCHEMA
from lazyref.wide_search import WideSearchPipeline
from test_core import picture
from test_search_quality import PLAN, rating


class PairClient:
    def __init__(self, reject=(), fail=False):
        self.assessed, self.screened, self.sources = [], [], []
        self.reject, self.fail = set(reject), fail

    def plan(self, *args, **kwargs):
        return deepcopy(PLAN)

    def request(self, prompt, schema, cancel, **kwargs):
        data = json.loads(prompt.split("Task data: ", 1)[1])
        if schema is SCREEN_SCHEMA:
            self.screened.extend(row["id"] for row in data["attachments"])
            return {"images": [{"id": row["id"], "decision": "keep", "priority": 80,
                                "reason": "Visible pose"} for row in data["attachments"]]}
        assert schema is SOURCE_SCHEMA
        self.sources.extend(row["id"] for row in data)
        kwargs["trace"].extend({"action": "open_page", "url": row["page_url"]} for row in data)
        return {"blocked_sources": [], "images": [{"id": row["id"], "source_kind": "photo_credit",
            "source_checked": True, "source_notes": "Synthetic evidence", "creator": "Test"} for row in data]}

    def assess(self, description, batch, cancel, **kwargs):
        self.assessed.extend(a.candidate.title for a in batch)
        if self.fail:
            raise TimeoutError("Synthetic interrupted assessment")
        return {a.candidate.id: rating(20, failed=("c1", "c2")) if a.candidate.title in self.reject
                else rating() for a in batch}


def write_asset(c, directory, image):
    path = directory / (c.id + ".jpg")
    preview = directory / (c.id + "-preview.jpg")
    image.save(path, quality=94)
    small = image.copy()
    small.thumbnail((1100, 1100))
    small.save(preview, quality=88)
    return Asset(c, path, preview, *image.size)


def pair_run(tmp_path, monkeypatch, strategy, *, enhanced=True, hq=False, reverse=False,
             reuse_cache=False, client=None, images=None, output=None):
    source = picture(size=(1200, 900))
    images = images or {"small": source, "large": source.resize((2400, 1800), Image.Resampling.LANCZOS)}
    candidates = [Candidate(name, name, image_url=f"https://img.example.org/{name}.jpg",
                            page_url=f"https://example.org/{name}") for name in images]
    if reverse:
        candidates.reverse()
    client = client or PairClient()
    req = SearchRequest("A person seated at a bar", 2, output or tmp_path / "out",
                        model="offline-test", search_strategy=strategy, search_rounds=1,
                        candidate_limit=8, discovery_limit=20, detail_limit=8,
                        enhanced_dedup=enhanced, high_quality_only=hq, reuse_cache=reuse_cache)

    def loader(c, directory, network):
        return write_asset(c, directory, images[c.title])

    def discover(*args):
        seen = {c.image_url for c in args[3].values()}
        return [c for c in candidates if c.image_url not in seen], {
            "queries": ["synthetic query"], "strategy": "test", "limitation": ""}

    def preview(c, directory, network):
        # An identical preview can point to originals of different resolution.
        path = directory / (c.id + "-screen.jpg")
        source.resize((240, 180)).save(path, quality=80)
        return Asset(c, path, path, 240, 180, "screening_thumbnail"), None

    monkeypatch.setattr("lazyref.wide_search.discover_wide", discover)
    pipeline = (SearchPipeline(client, lambda *a: (candidates, []), loader) if strategy == "legacy"
                else WideSearchPipeline(client, loader, preview_loader=preview))
    result = pipeline.run(req, Event())
    return result, client, req


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("hq", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_larger_original_wins_without_assessing_smaller_later_repeats(tmp_path, monkeypatch, strategy, hq, reverse):
    result, client, req = pair_run(tmp_path, monkeypatch, strategy, hq=hq, reverse=reverse)
    assert not result.error
    assert [(a.candidate.title, a.width, a.height) for a in result.assets] == [("large", 2400, 1800)]
    assert client.assessed == (["large"] if reverse else ["small", "large"])
    assert result.examined == len(client.assessed) and result.duplicates == 1
    if strategy == "wide":
        assert len(client.screened) == 1  # Exact same thumbnail needs only one coarse check.
        assert result.screened == 2
        assert len(client.sources) == len(client.assessed)
    assert req.candidate_limit == req.detail_limit == 8


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
@pytest.mark.parametrize("hq", [False, True])
def test_disabled_enhanced_dedup_keeps_first_copy_as_before(tmp_path, monkeypatch, strategy, hq):
    result, client, _ = pair_run(tmp_path, monkeypatch, strategy, enhanced=False, hq=hq)
    assert not result.error and client.assessed == ["small"]
    assert [(a.width, a.height) for a in result.assets] == [(1200, 900)]


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_larger_copy_requires_its_own_eligible_assessment(tmp_path, monkeypatch, strategy):
    result, client, _ = pair_run(tmp_path, monkeypatch, strategy, client=PairClient(reject={"large"}))
    assert not result.error and client.assessed == ["small", "large"]
    assert [a.candidate.title for a in result.assets] == ["small"]
    assert result.rejected == 1


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_interrupted_variants_survive_retry_with_independent_ratings(tmp_path, monkeypatch, strategy):
    result, _, _ = pair_run(tmp_path, monkeypatch, strategy, client=PairClient(fail=True))
    assert result.error and result.pending_review == 2
    recovered = PairClient()
    retried = retry_review(result.retry_path, Event(), client=recovered)
    assert not retried.error and recovered.assessed == ["small", "large"]
    assert [(a.width, a.height) for a in retried.assets] == [(2400, 1800)]
    session = ReviewSession.load(result.retry_path)
    assert session.pending_count == 0


def test_cached_full_ratings_keep_larger_copy_without_new_assessments(tmp_path, monkeypatch):
    first, _, req = pair_run(tmp_path, monkeypatch, "wide", reuse_cache=True)
    assert not first.error
    second, client, _ = pair_run(tmp_path, monkeypatch, "wide", reuse_cache=True, output=req.output_dir)
    assert not second.error and client.assessed == []
    assert second.reused_assessments == 2
    assert [(a.width, a.height) for a in second.assets] == [(2400, 1800)]


def test_larger_mode_still_rejects_exact_or_lower_resolution_copies(tmp_path):
    base = picture(size=(1200, 900))
    paths = []
    for name, size in [("small", (600, 450)), ("large", (1200, 900)),
                       ("same", (1200, 900)), ("middle", (800, 600))]:
        path = tmp_path / (name + ".png")
        base.resize(size).save(path)
        paths.append(path)
    index = DuplicateIndex(allow_larger=True)
    assert [index.add(path) for path in paths] == [True, True, False, False]


@pytest.mark.parametrize("strategy", ["legacy", "wide"])
def test_larger_variants_do_not_expand_assessment_budget(tmp_path, monkeypatch, strategy):
    source = picture(size=(600, 450))
    images = {f"copy{i}": source.resize((600 + 40*i, 450 + 30*i)) for i in range(12)}
    # Rejecting assessments prevent a result-count stop from hiding the budget check.
    result, client, req = pair_run(tmp_path, monkeypatch, strategy, images=images,
                                   client=PairClient(reject=set(images)))
    assert not result.error and not result.assets
    assert result.examined == len(client.assessed) == 8
    assert result.examined == (req.candidate_limit if strategy == "legacy" else req.detail_limit)
