"""Перевірка ON/OFF без викликів хмари: походження, кеш, повтор і експорт."""

from dataclasses import asdict, replace
from io import BytesIO
import json
from threading import Event

import pytest
from PIL import Image, PngImagePlugin

from lazyref.authenticity import GeneratedImage
from lazyref.codex import CodexClient, POSE_ONLY_SCHEMA
from lazyref.exporter import export_results
from lazyref.images import download, screening_preview
from lazyref.models import Candidate, SearchRequest
from lazyref.network import Network
from lazyref.pipeline import SearchPipeline
from lazyref.review_session import ReviewSession
from lazyref.retry_analysis import retry_review
from lazyref.search_agent import discover
from lazyref.search_cache import SearchCache, cached_rating, key_for, task_key
from lazyref.wide_agent import candidate_from, discover_wide, DISCOVER_SCHEMA, SCREEN_SCHEMA, SOURCE_SCHEMA
from test_search_quality import PLAN, rating
from test_core import make_asset
from test_wide_search import Agent, request, run, manifest, candidate_row, preview_loader


class SwitchAgent(Agent):
    def __init__(self, *, page=True, **kwargs):
        super().__init__(**kwargs)
        self.page = page
        self.prompts = []

    def request(self, prompt, schema, cancel, **kwargs):
        self.prompts.append(prompt)
        data = super().request(prompt, schema, cancel, **kwargs)
        if schema is DISCOVER_SCHEMA and not self.page:
            for row in data["candidates"]:
                row["page_url"] = ""
        return data

    def assess(self, description, batch, cancel, **kwargs):
        ratings = super().assess(description, batch, cancel, **kwargs)
        if kwargs.get("verify_sources") is False:
            return {k: replace(v, media="unverified", confidence="not_checked", verify_sources=False) for k,v in ratings.items()}
        return ratings


@pytest.mark.parametrize("enabled", [True, False])
def test_source_stage_runs_only_on_and_result_never_mislabels_off(tmp_path, enabled):
    client = SwitchAgent()
    req = request(tmp_path, verify_sources=enabled)
    result = run(req, client)
    assert not result.error and len(result.assets) == 2 and result.verify_sources == enabled
    assert any(kind == "source" for kind,_ in client.calls) == enabled
    report = manifest(result)
    assert report["verify_sources"] == enabled
    for asset in result.assets:
        assert asset.assessment.verify_sources == enabled
        assert asset.assessment.media == ("photo" if enabled else "unverified")
        assert asset.assessment.confidence == ("high" if enabled else "not_checked")
    if not enabled:
        assert report["statistics"]["metrics"]["source_checks"] == 0
        assert report["statistics"]["source_verification"] == []
        assert report["provenance_status"] == "unverified"
        assert "Походження не перевірено" in (result.folder / "sources.csv").read_text(encoding="utf-8-sig")
        assert all(not r["source"]["source_checked"] for r in report["results"])
        assert "source page is OPTIONAL" in client.prompts[0]
        assert "Collect REAL" not in client.prompts[0]
        assert "subsequent stages will inspect its pixels and source" not in client.prompts[0]
        assert "or clear drawing/render" not in next(p for p in client.prompts if "Fast coarse screening" in p)


def test_missing_source_page_is_usable_off_but_not_on(tmp_path):
    off = run(request(tmp_path / "off", verify_sources=False), SwitchAgent(page=False))
    assert not off.error and off.assets and all(not a.candidate.page_url for a in off.assets)
    on = run(request(tmp_path / "on"), SwitchAgent(page=False))
    assert not on.error and on.discovered == 0 and not on.assets


def test_thumbnail_only_and_private_addresses(tmp_path):
    net = Network(Event())
    try:
        row = {**candidate_row(1), "image_url": "", "page_url": ""}
        c = candidate_from(row, net, verify_sources=False)
        assert c.image_url == row["thumbnail_url"] and not c.page_url
        with pytest.raises(ValueError): candidate_from(row, net, verify_sources=True)
        for field in ("thumbnail_url", "page_url"):
            with pytest.raises(ValueError): candidate_from({**row, field: "http://127.0.0.1/private"}, net, verify_sources=False)
    finally:
        net.close()


def test_model_off_omits_authenticity_fields_and_retains_pose_guide(tmp_path):
    asset = make_asset(tmp_path)
    guide = tmp_path / "guide.png"
    guide.write_bytes(asset.path.read_bytes())
    client = CodexClient.__new__(CodexClient)
    def response(prompt, schema, cancel, images):
        assert schema == POSE_ONLY_SCHEMA
        assert "media" not in schema["properties"]["images"]["items"]["properties"]
        assert "source_notes" not in prompt and "Source notes" not in prompt
        assert "Provenance verification is OFF" in prompt
        assert images == [guide, asset.preview]
        checks = [{k: v for k,v in c.items() if k in {"id", "status", "evidence"}} for c in rating().criterion_checks]
        return {"images": [{"id": asset.candidate.id, "score": 90, "reason": "Same pose", "matches": [], "misses": [],
                            "reference_type": "ordinary", "criterion_checks": checks}]}
    client.request = response
    assessed = client.assess("Man at bar", [asset], Event(), PLAN["criteria"], guide, verify_sources=False)[asset.candidate.id]
    assert assessed.is_close and assessed.media == "unverified" and not assessed.verify_sources


@pytest.mark.parametrize("ref_type", ["sexual", "unsafe_or_uncertain_nudity"])
def test_off_does_not_disable_content_or_pose_checks(tmp_path, ref_type):
    a = replace(rating(), media="unverified", confidence="not_checked", verify_sources=False)
    req = request(tmp_path, verify_sources=False)
    assert a.eligible_for(req)
    assert not replace(a, reference_type=ref_type).eligible_for(req)
    assert not replace(a, score=20).eligible_for(req)
    assert not a.eligible_for(replace(req, verify_sources=True))
    assert not replace(a, reference_type="adult_artistic_nude").eligible_for(req)
    assert replace(a, reference_type="adult_artistic_nude").eligible_for(replace(req, artistic_nudity=True))


def test_metadata_filter_respects_mode_and_unchecked_files_cannot_leak_into_on_cache(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "Stable Diffusion")
    buf = BytesIO()
    Image.new("RGB", (300, 300), "green").save(buf, "PNG", pnginfo=info)
    net = Network(Event())
    net.get = lambda *a, **kw: buf.getvalue()
    c = Candidate("picture", "Candidate", image_url="https://example.org/a.png")
    try:
        net.verify_sources = False
        asset = download(c, tmp_path, net)
        small, full = screening_preview(c, tmp_path, net)
        assert small and full
        cache = SearchCache(tmp_path / "cache")
        cache.store_asset("full_unverified", asset)
        assert cache.asset("full", c) is None
        cache.close()
        net.verify_sources = True
        with pytest.raises(GeneratedImage): download(c, tmp_path, net)
        with pytest.raises(GeneratedImage): screening_preview(c, tmp_path, net)
    finally:
        net.close()


def test_off_cache_reuses_only_off_and_on_requires_new_analysis(tmp_path):
    off_request = request(tmp_path, verify_sources=False)
    first = run(off_request, SwitchAgent())
    assert not first.error
    client = SwitchAgent()
    second = run(off_request, client)
    assert not second.error and second.reused_assessments and not client.calls
    on_request = replace(off_request, verify_sources=True)
    assert task_key(off_request) != task_key(on_request)
    on_client = SwitchAgent()
    on = run(on_request, on_client)
    assert not on.error and on.reused_assessments == 0 and on_client.plan_calls == 1
    assert any(kind == "source" for kind,_ in on_client.calls)
    a = asdict(first.assets[0].assessment)
    assert cached_rating(a, PLAN["criteria"], "strict", False)
    assert cached_rating(a, PLAN["criteria"], "strict", True) is None


def test_off_timeout_retry_retains_mode_and_no_source_check(tmp_path):
    first = run(request(tmp_path, verify_sources=False), SwitchAgent(fail_assess=2))
    assert first.error and first.pending_review
    session = ReviewSession.load(first.retry_path)
    assert session.request().verify_sources is False
    client = SwitchAgent()
    result = retry_review(first.retry_path, Event(), client=client)
    assert not result.error and result.assets and result.verify_sources is False
    assert all(kind == "assess" for kind,_ in client.calls)
    assert all(not a.assessment.verify_sources for a in result.assets)


def test_legacy_algorithm_supports_off_without_source_and_with_thumbnail(tmp_path):
    from lazyref.search_agent import SEARCH_SCHEMA
    class Legacy(SwitchAgent):
        def request(self, prompt, schema, cancel, **kwargs):
            assert schema == SEARCH_SCHEMA
            assert "Do NOT open source pages" in prompt
            assert "Inspect source pages" not in prompt
            kwargs["trace"].append({"action": "search", "type": "web_search"})
            return {"image_search_available": True, "queries": ["crouch"], "strategy": "test", "limitation": "",
                    "candidates": [{**candidate_row(0), "page_url": "", "image_url": "", "source_kind": "generated_label", "source_checked": True}]}
    def loader(c,d,n):
        assert not n.verify_sources and not c.source_checked and c.source_kind == "unknown"
        return preview_loader(c,d,n)[1]
    req = replace(request(tmp_path, verify_sources=False), search_strategy="legacy", search_rounds=1)
    result = SearchPipeline(Legacy(), loader=loader).run(req, Event())
    assert not result.error and len(result.assets) == 1
    assert not result.assets[0].candidate.page_url and not result.assets[0].assessment.verify_sources


def test_old_session_defaults_to_on_and_removed_library_cannot_start_new_search(tmp_path):
    req = request(tmp_path)
    session = ReviewSession.create(req, PLAN)
    session.data["request"].pop("verify_sources")
    session.data["request"]["provider"] = "commons"
    session.save()
    restored = ReviewSession.load(session.path).request()
    assert restored.verify_sources and restored.provider == "agent"
    with pytest.raises(ValueError, match="Codex"):
        replace(req, provider="commons").validate()


