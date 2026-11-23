"""Новий алгоритм перевіряється локально: жодних запитів до Codex або сайтів."""

from copy import deepcopy
from dataclasses import replace, asdict
import hashlib
from io import BytesIO
import json
import random
from threading import Event

import pytest
from PIL import Image

from lazyref.models import Asset, Cancelled, SearchRequest
from lazyref.images import prepare, screening_preview
from lazyref.review_session import ReviewSession
from lazyref.retry_analysis import retry_review
from lazyref.search_cache import SearchCache, key_for, task_key, cached_rating
from lazyref.wide_agent import DISCOVER_SCHEMA, SCREEN_SCHEMA, SOURCE_SCHEMA, check_sources, discover_wide
from lazyref.wide_search import WideSearchPipeline
from lazyref.network import Network
from test_search_quality import PLAN, rating


def candidate_row(i):
    return {"title": f"Photo {i}", "image_url": f"https://img.example.org/{i}.jpg",
            "thumbnail_url": f"https://img.example.org/thumb/{i}.jpg", "page_url": f"https://example.org/photo/{i}"}


class Agent:
    def __init__(self, *, outcome="close", batch_size=40, bad_screen=False, fail_assess=0,
                 generated=(), blocked=False):
        self.calls, self.plan_calls, self.assess_calls, self.searches = [], 0, 0, 0
        self.outcome, self.batch_size, self.bad_screen, self.fail_assess = outcome, batch_size, bad_screen, fail_assess
        self.generated, self.blocked = generated, blocked
        self.guide_args = []

    def plan(self, description, cancel, **kwargs):
        self.plan_calls += 1
        self.guide_args.append(kwargs.get("reference_image"))
        return deepcopy(PLAN)

    def request(self, prompt, schema, cancel, **kwargs):
        data = json.loads(prompt.split("Task data: ", 1)[1])
        if schema is DISCOVER_SCHEMA:
            self.calls.append(("discover", data))
            assert not kwargs.get("images")
            self.searches += 1
            kwargs["trace"].append({"type": "web_search", "action": "search"})
            start = (self.searches-1) * self.batch_size
            return {"image_search_available": True, "queries": [f"query {self.searches}"], "strategy": "test",
                    "limitation": "", "blocked_sources": [],
                    "candidates": [candidate_row(i) for i in range(start, start + min(self.batch_size, data["candidate_limit"]))]}
        if schema is SCREEN_SCHEMA:
            self.calls.append(("screen", data))
            rows = [{"id": row["id"], "decision": "keep", "priority": 80, "reason": "pose"}
                    for row in data["attachments"]]
            if self.bad_screen:
                rows.pop()
            return {"images": rows}
        assert schema is SOURCE_SCHEMA
        self.calls.append(("source", data))
        kwargs["trace"].extend({"type": "web_search", "action": "open_page", "url": r["page_url"]} for r in data)
        return {"blocked_sources": [{"url": data[0]["page_url"], "reason": "captcha"}] if self.blocked else [],
            "images": [{"id": r["id"], "source_kind": "generated_label" if int(r["page_url"].rsplit("/",1)[-1]) in self.generated else "photo_credit",
                        "source_checked": True, "source_notes": "Source evidence", "creator": "Photographer"} for r in data]}

    def assess(self, description, batch, cancel, **kwargs):
        self.assess_calls += 1
        self.calls.append(("assess", [a.candidate.id for a in batch]))
        self.guide_args.append(kwargs.get("reference_image"))
        if self.assess_calls == self.fail_assess:
            raise TimeoutError("Test timeout")
        return {a.candidate.id: rating() if self.outcome == "close" else rating(25, failed=("c2", "c4")) for a in batch}


def photo_bytes(i, size=(240, 180)):
    image = Image.frombytes("RGB", size, random.Random(i).randbytes(size[0]*size[1]*3))
    out = BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


def preview_loader(c, directory, network):
    asset = prepare(photo_bytes(int(c.title.split()[-1])), c, directory, "download")
    return replace(asset, method="screening_thumbnail"), asset


def full_loader(*args):
    pytest.fail("Already downloaded full photo should be reused")


def request(tmp_path, **kwargs):
    return SearchRequest("Man seated at a bar seen from behind", 2, tmp_path,
        search_strategy="wide", discovery_limit=20, detail_limit=8, **kwargs)


def run(req, client=None, **kwargs):
    return WideSearchPipeline(client or Agent(), full_loader, preview_loader=preview_loader).run(req, Event(), **kwargs)


def manifest(result):
    return json.loads((result.folder / "manifest.json").read_text(encoding="utf-8"))


def test_broad_pool_then_sources_only_for_shortlist_and_detailed_groups_of_three(tmp_path):
    client = Agent(outcome="reject")
    req = replace(request(tmp_path), discovery_limit=200)
    result = run(req, client)
    assert not result.error
    assert result.discovered == result.screened == 200
    assert result.examined == 8 and not result.assets and client.searches == 5
    source_rows = [d for kind, d in client.calls if kind == "source"]
    assert sum(map(len, source_rows)) == 8
    assert max(len(d) for kind, d in client.calls if kind == "assess") <= 3
    kinds = [kind for kind, _ in client.calls]
    assert kinds.index("source") > max(i for i, k in enumerate(kinds) if k == "screen")
    stats = manifest(result)["statistics"]
    assert stats["metrics"]["newly_examined"] == 8
    assert stats["stop_reason"] == "detail_limit" and len(stats["screening"]) == 200


def test_exact_repeat_reuses_plan_source_files_and_ratings_without_cloud_calls(tmp_path):
    req = request(tmp_path)
    first = run(req)
    assert not first.error and len(first.assets) == 2
    client = Agent()
    second = run(req, client)
    assert not second.error and len(second.assets) == 2
    assert not client.calls and client.plan_calls == 0
    assert second.reused_assessments >= 2 and second.usage["summary"]["calls"] == 0
    assert manifest(second)["statistics"]["stop_reason"] == "enough_cached_close_matches"
    assert first.folder != second.folder and (first.folder / "manifest.json").exists()


@pytest.mark.parametrize("change", ["description", "profile", "model", "guide", "nudity", "reasoning"])
def test_cache_identity_invalidates_changed_search(tmp_path, change):
    req = request(tmp_path)
    original = task_key(req)
    if change == "description": req = replace(req, description="Woman standing rear view")
    if change == "profile": req = replace(req, search_profile="pose")
    if change == "model": req = replace(req, model="another-model")
    if change == "reasoning": req = replace(req, reasoning_effort="high")
    if change == "nudity": req = replace(req, artistic_nudity=True)
    if change == "guide":
        guide = tmp_path / "guide.jpg"
        guide.write_bytes(photo_bytes(3))
        req = replace(req, reference_image=guide)
    assert task_key(req) != original


def test_changing_reasoning_does_not_reuse_other_levels_assessments(tmp_path):
    req = request(tmp_path)
    run(req)
    client = Agent()
    result = run(replace(req, reasoning_effort="high"), client)
    assert not result.error and result.reused_assessments == 0
    assert client.plan_calls == 1 and client.assess_calls > 0
    assert manifest(result)["requested_reasoning_effort"] == "high"


def test_cache_can_be_disabled_for_independent_comparison(tmp_path):
    req = request(tmp_path)
    run(req)
    client = Agent()
    result = run(replace(req, reuse_cache=False), client)
    assert not result.error and result.reused_assessments == 0
    assert client.plan_calls == client.searches == 1 and client.assess_calls == 3


def test_cached_rejections_leave_room_for_fresh_search(tmp_path):
    req = request(tmp_path)
    run(req, Agent(outcome="reject"))
    client = Agent(outcome="reject")
    result = run(req, client)
    assert not result.error and client.searches > 0
    assert result.discovered <= 20
    assert manifest(result)["statistics"]["metrics"]["cached_candidates"] <= 6


def test_timeout_keeps_all_shortlisted_files_and_retry_needs_no_source_or_search(tmp_path):
    client = Agent(fail_assess=2)
    result = run(request(tmp_path), client)
    assert result.error == "Test timeout" and result.examined == 3 and result.pending_review == 5
    session = ReviewSession.load(result.retry_path)
    assert session.request().search_strategy == "wide"
    assert len(session.assets()) == 8
    retry = Agent()
    retried = retry_review(result.retry_path, Event(), client=retry)
    assert not retried.error and not retried.pending_review and retried.examined == 8
    assert all(kind == "assess" for kind, _ in retry.calls) and retry.plan_calls == 0
    assert retried.search_strategy == "wide" and retried.screened == 20


def test_generated_source_and_captcha_never_reach_detailed_analysis(tmp_path):
    generated = run(request(tmp_path / "generated"), Agent(generated=range(40)))
    assert not generated.error and generated.examined == 0 and not generated.assets
    assert manifest(generated)["statistics"]["precheck_rejections"]
    client = Agent(blocked=True)
    blocked = run(request(tmp_path / "blocked"), client)
    assert not blocked.error and blocked.examined == 0 and not blocked.assets
    assert not any(kind == "assess" for kind, _ in client.calls)


def test_incomplete_screen_does_not_silently_drop_candidates(tmp_path):
    result = run(request(tmp_path), Agent(bad_screen=True))
    assert result.error and result.discovered == 20 and result.examined == 0
    assert not result.assets
    cache = SearchCache(tmp_path / "_search_cache")
    assert len(cache.get("task", task_key(request(tmp_path)))["candidates"]) == 20
    cache.close()


def test_uncertain_thumbnails_get_detailed_slots(tmp_path):
    class UncertainAgent(Agent):
        def request(self, prompt, schema, cancel, **kwargs):
            data = super().request(prompt, schema, cancel, **kwargs)
            if schema is SCREEN_SCHEMA:
                for row in data["images"]:
                    row["decision"], row["priority"] = "uncertain", 10
            return data
    result = run(request(tmp_path), UncertainAgent())
    assert not result.error and result.examined == 8 and len(result.assets) == 2


def test_small_preview_downloads_thumbnail_first_and_uses_448px_limit(tmp_path):
    from lazyref.wide_agent import candidate_from
    net = Network(Event())
    c = candidate_from(candidate_row(1), net)
    calls = []
    net.get = lambda url, **kwargs: calls.append(url) or photo_bytes(1, (900, 600))
    try:
        preview, full = screening_preview(c, tmp_path, net)
        assert calls == [c.thumbnail_url] and full is None
        with Image.open(preview.preview) as image:
            assert max(image.size) == 448
    finally:
        net.close()


def test_corrupt_cache_image_is_a_miss_and_stale_source_is_not_used(tmp_path):
    from lazyref.wide_agent import candidate_from
    net = Network(Event())
    try:
        c = candidate_from(candidate_row(0), net)
        asset, _ = preview_loader(c, tmp_path, net)
        cache = SearchCache(tmp_path / "cache")
        cache.store_asset("preview", asset)
        loaded = cache.asset("preview", c)
        assert loaded
        loaded.path.write_bytes(b"corrupted")
        assert cache.asset("preview", c) is None
        cache.put("source", "key", {"source_checked": True})
        cache.db.execute("UPDATE entries SET updated=0")
        cache.db.commit()
        assert cache.get("source", "key") is None
        cache.close()
    finally:
        net.close()


def test_false_open_claim_cannot_establish_attribution(tmp_path):
    from lazyref.wide_agent import candidate_from
    net = Network(Event())
    net.source_trace = []
    try:
        c = candidate_from(candidate_row(1), net)
        asset, _ = preview_loader(c, tmp_path, net)
        class Liar(Agent):
            def request(self, *args, **kwargs):
                result = super().request(*args, **kwargs)
                kwargs["trace"].clear()
                return result
        evidence = check_sources(Liar(), [asset], net, Event(), lambda s: None)[c.id]
        assert not evidence["source_checked"] and evidence["source_kind"] == "unknown"
    finally:
        net.close()


def test_search_context_is_compact_and_has_no_guide_attachment_or_url_history(tmp_path):
    net = Network(Event())
    net.search_trace = []
    client = Agent()
    try:
        guide = tmp_path / "guide.jpg"
        guide.write_bytes(photo_bytes(2))
        req = replace(request(tmp_path), reference_image=guide)
        discover_wide(client, req, PLAN, {}, [f"q{i}" for i in range(100)], [], 20, net, Event(), lambda s: None)
        context = client.calls[0][1]
        assert len(context["recent_queries"]) == 16
        assert "excluded_image_urls" not in context and "recent_feedback" not in context
    finally:
        net.close()


def test_guide_stays_in_planning_and_detailed_comparison(tmp_path):
    guide = tmp_path / "guide.jpg"
    guide.write_bytes(photo_bytes(7))
    client = Agent()
    result = run(replace(request(tmp_path), reference_image=guide), client)
    assert not result.error and all(p == guide for p in client.guide_args)


def test_invalid_cached_assessment_is_not_accepted():
    data = asdict(rating())
    data["criterion_checks"].pop()
    assert cached_rating(data, PLAN["criteria"], "strict") is None


def test_cancel_before_planning_does_not_call_codex(tmp_path):
    client = Agent()
    cancel = Event()
    cancel.set()
    result = WideSearchPipeline(client).run(request(tmp_path), cancel)
    assert result.cancelled and not client.calls and client.plan_calls == 0




def test_wide_usage_stages_do_not_merge_with_retrieval(tmp_path, monkeypatch):
    from test_waiting import setup_client, FastEvent
    from lazyref.usage import STAGES
    client, clock, _ = setup_client(tmp_path, monkeypatch, finish_at=239)
    client.request("screen", SCREEN_SCHEMA, FastEvent(clock), usage_stage="screening")
    clock.now = 0
    client.request("source", SOURCE_SCHEMA, FastEvent(clock), web_search=True, usage_stage="source_check")
    usage = client.usage_ledger.snapshot()
    assert usage["by_stage"]["screening"]["calls"] == 1
    assert usage["by_stage"]["source_check"]["calls"] == 1
    assert usage["by_stage"]["search"]["calls"] == 0
