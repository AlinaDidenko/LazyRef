"""A blocked page must not discard independent sources of the same photograph."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from random import Random
from threading import Event
from urllib.parse import urlsplit

import pytest
from PIL import Image

from lazyref.images import DuplicateIndex
from lazyref.models import Asset, Candidate, SearchRequest
from lazyref.search_cache import SearchCache, assessment_key, key_for, task_key
from lazyref.wide_agent import SCREEN_SCHEMA, SOURCE_SCHEMA
from lazyref.wide_search import WideSearchPipeline
from test_core import picture
from test_dedup_resolution import write_asset
from test_search_quality import PLAN, rating


class SourceClient:
    def __init__(self, blocked=(), *, generated=(), cancel_after_source=False, reject=False):
        self.blocked = set(blocked)
        self.generated = set(generated)
        self.cancel_after_source = cancel_after_source
        self.reject = reject
        self.source_batches, self.screened, self.assessed = [], [], []

    def plan(self, *args, **kwargs):
        return deepcopy(PLAN)

    def request(self, prompt, schema, cancel, **kwargs):
        data = json.loads(prompt.split("Task data: ", 1)[1])
        if schema is SCREEN_SCHEMA:
            self.screened.extend(row["id"] for row in data["attachments"])
            return {"images": [{"id": row["id"], "decision": "keep", "priority": 80,
                                "reason": "Visible pose"} for row in data["attachments"]]}
        assert schema is SOURCE_SCHEMA
        names = [urlsplit(row["page_url"]).hostname.split(".")[0] for row in data]
        self.source_batches.append(names)
        kwargs["trace"].extend({"action": "open_page", "url": row["page_url"]} for row in data)
        answer = {"blocked_sources": [{"url": row["page_url"], "reason": "captcha"}
                                      for row, name in zip(data, names) if name in self.blocked],
                  "images": [{"id": row["id"], **source_evidence(name, name not in self.blocked)}
                             for row, name in zip(data, names)]}
        for row, name in zip(answer["images"], names):
            if name in self.generated:
                row["source_kind"] = "generated_label"
        if self.cancel_after_source:
            cancel.set()
        return answer

    def assess(self, description, batch, cancel, **kwargs):
        self.assessed.extend(asdict(asset.candidate) for asset in batch)
        return {asset.candidate.id: rating(20, failed=("c1", "c2")) if self.reject else rating()
                for asset in batch}


def source_evidence(name, checked=True):
    return {"source_kind": "photo_credit" if checked else "unknown", "source_checked": checked,
            "source_notes": f"Evidence from {name}", "creator": f"Creator {name}" if checked else ""}


def run_sources(tmp_path, monkeypatch, *, names=("first", "second"), blocked=("first",),
                hq=False, enhanced=True, client=None, seed_cache=None, images=None, count=2, hosts=None):
    source = picture(size=(1200, 900))
    images = images or {name: source for name in names}
    hosts = hosts or {name: name for name in names}
    candidates = [Candidate(name, name, image_url=f"https://{hosts[name]}.example.org/{name}.jpg",
                            page_url=f"https://{hosts[name]}.example.org/photo") for name in names]
    request = SearchRequest("A person seated at a bar", count, tmp_path / "out", model="offline-test",
                            search_strategy="wide", search_rounds=1, candidate_limit=8,
                            discovery_limit=20, detail_limit=8, high_quality_only=hq,
                            enhanced_dedup=enhanced, reuse_cache=seed_cache is not None)
    client = client or SourceClient(blocked)
    loaded = []
    if seed_cache:
        fixture = tmp_path / "fixtures"
        fixture.mkdir(exist_ok=True)
        cache = SearchCache(request.output_dir / "_search_cache")
        try:
            seed_cache(cache, candidates, fixture, images, request)
        finally:
            cache.close()

    def discover(*args):
        seen = {candidate.image_url for candidate in args[3].values()}
        return [candidate for candidate in candidates if candidate.image_url not in seen], {
            "queries": ["synthetic query"], "strategy": "test", "limitation": ""}

    def loader(candidate, directory, network):
        loaded.append(candidate.title)
        return write_asset(candidate, directory, images[candidate.title])

    def preview(candidate, directory, network):
        path = directory / (candidate.id + "-screen.jpg")
        images[candidate.title].resize((240, 180)).save(path, quality=80)
        return Asset(candidate, path, path, 240, 180, "screening_thumbnail"), None

    monkeypatch.setattr("lazyref.wide_search.discover_wide", discover)
    result = WideSearchPipeline(client, loader, preview_loader=preview).run(request, Event())
    stats = json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))["statistics"]
    return result, client, loaded, stats


@pytest.mark.parametrize("hq", [False, True])
@pytest.mark.parametrize("enhanced", [False, True])
def test_blocked_first_copy_uses_independent_source_and_assessment(tmp_path, monkeypatch, hq, enhanced):
    result, client, loaded, stats = run_sources(tmp_path, monkeypatch, hq=hq, enhanced=enhanced)
    assert not result.error and not result.cancelled
    assert [asset.candidate.title for asset in result.assets] == ["second"]
    assert client.source_batches == [["first"], ["second"]]
    assert [row["title"] for row in client.assessed] == ["second"]
    candidate = result.assets[0].candidate
    assert candidate.page_url == "https://second.example.org/photo"
    assert candidate.source_checked and candidate.source_kind == "photo_credit"
    assert candidate.creator == "Creator second" and candidate.source_notes == "Evidence from second"
    assert client.assessed[0]["creator"] == candidate.creator
    assert client.assessed[0]["source_notes"] == candidate.source_notes
    assert result.examined == stats["metrics"]["newly_examined"] == 1
    assert stats["blocked_hosts"] == {"first.example.org": "CAPTCHA"}
    assert sorted(loaded) == ["first", "second"]  # Each full file is fetched at most once.


@pytest.mark.parametrize("hq", [False, True])
@pytest.mark.parametrize("enhanced", [False, True])
def test_valid_first_copy_does_not_trigger_extra_source_or_model_calls(tmp_path, monkeypatch, hq, enhanced):
    result, client, _, stats = run_sources(tmp_path, monkeypatch, blocked=(), hq=hq, enhanced=enhanced)
    assert not result.error
    assert [asset.candidate.title for asset in result.assets] == ["first"]
    assert client.source_batches == [["first"]]
    assert [row["title"] for row in client.assessed] == ["first"]
    assert len(client.screened) == 1
    assert result.duplicates == 1 and result.examined == 1
    assert stats["metrics"]["source_checks"] == 1


def test_every_unavailable_source_is_checked_without_assessing_or_accepting_it(tmp_path, monkeypatch):
    result, client, _, stats = run_sources(tmp_path, monkeypatch, blocked=("first", "second"))
    assert not result.error and not result.assets and result.examined == 0
    assert client.source_batches == [["first"], ["second"]]
    assert client.assessed == []
    assert stats["blocked_hosts"] == {"first.example.org": "CAPTCHA", "second.example.org": "CAPTCHA"}
    assert all(any(host in warning for warning in result.warnings) for host in stats["blocked_hosts"])


def test_three_sources_continue_until_one_is_available(tmp_path, monkeypatch):
    result, client, loaded, _ = run_sources(tmp_path, monkeypatch, names=("first", "second", "third"),
                                           blocked=("first", "second"))
    assert not result.error
    assert [asset.candidate.title for asset in result.assets] == ["third"]
    assert client.source_batches == [["first"], ["second"], ["third"]]
    assert [row["title"] for row in client.assessed] == ["third"]
    assert sorted(loaded) == ["first", "second", "third"]


def test_cached_full_files_and_source_evidence_keep_their_own_provenance(tmp_path, monkeypatch):
    def seed(cache, candidates, fixture, images, request):
        for candidate in candidates:
            cache.store_asset("full", write_asset(candidate, fixture, images[candidate.title]))
        second = candidates[1]
        cache.put("source", key_for([second.page_url, second.image_url]), source_evidence("second"))

    result, client, loaded, stats = run_sources(tmp_path, monkeypatch, seed_cache=seed)
    assert not result.error
    assert [asset.candidate.title for asset in result.assets] == ["second"]
    assert client.source_batches == [["first"]]  # The alternate has its own cached verification.
    assert [row["title"] for row in client.assessed] == ["second"]
    assert client.assessed[0]["creator"] == "Creator second"
    assert client.assessed[0]["source_notes"] == "Evidence from second"
    assert loaded == [] and stats["metrics"]["cached_sources"] == 1


def test_cancellation_after_blocked_source_does_not_try_an_alternate(tmp_path, monkeypatch):
    client = SourceClient(("first",), cancel_after_source=True)
    result, client, _, _ = run_sources(tmp_path, monkeypatch, client=client)
    assert result.cancelled and not result.assets and result.examined == 0
    assert client.source_batches == [["first"]] and client.assessed == []


def test_known_blocked_host_is_skipped_before_trying_independent_host(tmp_path, monkeypatch):
    result, client, _, stats = run_sources(tmp_path, monkeypatch, names=("first", "second", "third"),
        hosts={"first": "first", "second": "first", "third": "third"})
    assert not result.error
    assert [asset.candidate.title for asset in result.assets] == ["third"]
    assert client.source_batches == [["first"], ["third"]]
    assert [row["title"] for row in client.assessed] == ["third"]
    assert stats["blocked_hosts"] == {"first.example.org": "CAPTCHA"}


def test_generated_label_is_not_overridden_by_equal_copy_on_another_site(tmp_path, monkeypatch):
    client = SourceClient(generated=("first",))
    result, client, _, _ = run_sources(tmp_path, monkeypatch, client=client)
    assert not result.error and not result.assets
    assert result.rejected == 1 and result.duplicates == 1
    assert client.source_batches == [["first"]] and client.assessed == []


def test_cached_valid_first_copy_needs_no_new_verification_or_assessment(tmp_path, monkeypatch):
    def seed(cache, candidates, fixture, images, request):
        for candidate in candidates:
            asset = write_asset(candidate, fixture, images[candidate.title])
            cache.store_asset("full", asset)
            if candidate.title == "first":
                evidence = source_evidence("first")
                cache.put("source", key_for([candidate.page_url, candidate.image_url]), evidence)
                asset.candidate = replace(candidate, **evidence)
                cache.put("assessment", assessment_key(task_key(request), PLAN, asset), asdict(rating()))

    result, client, loaded, _ = run_sources(tmp_path, monkeypatch, blocked=(), seed_cache=seed)
    assert not result.error
    assert [asset.candidate.title for asset in result.assets] == ["first"]
    assert client.source_batches == [] and client.assessed == [] and loaded == []
    assert result.reused_assessments == 1 and result.examined == 1
    assert result.duplicates == 1


def test_unavailable_alternates_do_not_spend_or_expand_detail_budget(tmp_path, monkeypatch):
    names, images, blocked = [], {}, []
    for index in range(9):
        # Independent deterministic textures avoid accidental perceptual groups.
        image = Image.frombytes("RGB", (64, 64), Random(index).randbytes(64 * 64 * 3)).resize((1200, 900))
        first, second = f"blocked{index}", f"valid{index}"
        names.extend((first, second))
        images[first] = images[second] = image
        blocked.append(first)
    client = SourceClient(blocked, reject=True)
    result, client, loaded, stats = run_sources(tmp_path, monkeypatch, names=names,
                                               images=images, client=client, count=40)
    assert not result.error and not result.assets
    assert result.examined == stats["metrics"]["newly_examined"] == len(client.assessed) == 8
    assert all(row["title"].startswith("valid") for row in client.assessed)
    assert len({row["title"] for row in client.assessed}) == 8
    assert stats["stop_reason"] == "detail_limit"
    assert len(loaded) == len(set(loaded)) <= 18
    assert result.discovered == 18 and stats["metrics"]["source_checks"] <= 18


def test_repeat_search_does_not_reuse_blocked_evidence_as_available_source(tmp_path, monkeypatch):
    for _ in range(2):
        result, client, _, stats = run_sources(tmp_path, monkeypatch, blocked=("first", "second"),
                                               seed_cache=lambda *args: None)
        assert not result.error and not result.assets and result.examined == 0
        assert client.source_batches == [["first"], ["second"]]
        assert client.assessed == []
        assert stats["blocked_hosts"] == {"first.example.org": "CAPTCHA", "second.example.org": "CAPTCHA"}


def test_duplicate_index_prefers_settled_larger_group_to_earlier_exact_small_copy(tmp_path):
    source = picture(size=(1200, 900))
    small, large = tmp_path / "small.png", tmp_path / "large.png"
    source.resize((600, 450)).save(small)
    source.save(large)
    index = DuplicateIndex(allow_larger=True)
    assert index.add_with_index(small) == (0, True)
    assert index.add_with_index(large) == (1, True)
    assert index.add_with_index(small) == (0, False)
    assert index.add_with_index(small, preferred={1}) == (1, False)
    assert index.add(small) is False and index.add(large) is False
    assert len(index.records) == 2


@pytest.mark.parametrize("cached", [False, True])
def test_smaller_fallback_does_not_discard_available_larger_alternate(tmp_path, monkeypatch, cached):
    source = picture(size=(1200, 900))
    large = source.resize((2400, 1800), Image.Resampling.LANCZOS)
    images = {"first": large, "second": source, "third": large}

    def seed(cache, candidates, fixture, images, request):
        for candidate in candidates:
            cache.store_asset("full", write_asset(candidate, fixture, images[candidate.title]))
        second = candidates[1]
        cache.put("source", key_for([second.page_url, second.image_url]), source_evidence("second"))

    result, client, _, _ = run_sources(tmp_path, monkeypatch, names=tuple(images), images=images,
                                       seed_cache=seed if cached else None)
    assert not result.error
    assert [(asset.candidate.title, asset.width, asset.height) for asset in result.assets] == [
        ("third", 2400, 1800)]
    assert [row["title"] for row in client.assessed] in (["second", "third"], ["third"])
    third = next(row for row in client.assessed if row["title"] == "third")
    assert third["creator"] == "Creator third" and third["source_notes"] == "Evidence from third"
    assert third["source_checked"] and third["source_kind"] == "photo_credit"
    assert [name for batch in client.source_batches for name in batch].count("third") == 1


def test_smaller_fallback_does_not_hide_larger_copy_in_later_batch(tmp_path, monkeypatch):
    source = picture(size=(1200, 900))
    large = source.resize((2400, 1800), Image.Resampling.LANCZOS)
    images = {"first": large, "second": source}
    blocked = ["first"]
    # Seven unrelated blocked groups fill the first eight-group batch before
    # the final large alternative is loaded. The successful small fallback
    # must not leave the unavailable original's resolution in the settled index.
    for index in range(7):
        name = f"filler{index}"
        images[name] = Image.frombytes("RGB", (64, 64), Random(index).randbytes(64 * 64 * 3)).resize((1200, 900))
        blocked.append(name)
    images["third"] = large
    result, client, loaded, _ = run_sources(tmp_path, monkeypatch, names=tuple(images), images=images,
                                           blocked=blocked)
    assert not result.error
    assert [(asset.candidate.title, asset.width, asset.height) for asset in result.assets] == [
        ("third", 2400, 1800)]
    assert [row["title"] for row in client.assessed] == ["second", "third"]
    assert result.examined == 2 and loaded.count("third") == 1


def test_already_available_larger_copy_skips_smaller_queued_fallback(tmp_path, monkeypatch):
    source = picture(size=(1200, 900))
    images = {"first": source, "second": source,
              "third": source.resize((2400, 1800), Image.Resampling.LANCZOS)}
    result, client, _, _ = run_sources(tmp_path, monkeypatch, names=tuple(images), images=images)
    assert not result.error
    assert [(asset.candidate.title, asset.width, asset.height) for asset in result.assets] == [
        ("third", 2400, 1800)]
    assert client.source_batches == [["first", "third"]]
    assert [row["title"] for row in client.assessed] == ["third"]
    assert result.examined == 1 and result.duplicates == 1
