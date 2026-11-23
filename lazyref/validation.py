"""Перевірка збережених кандидатів і оцінок перед їх повторним використанням."""

from dataclasses import fields

from .models import Assessment, Candidate
from .priorities import PROFILES
from .urls import remote_url


def validated_candidate(data):
    try:
        candidate = Candidate(**data)
        for item in fields(candidate):
            value = getattr(candidate, item.name)
            if item.name == "search_page":
                if type(value) is not int or value < 1:
                    raise ValueError()
            elif item.name == "source_checked":
                if type(value) is not bool:
                    raise ValueError()
            elif not isinstance(value, str):
                raise ValueError()
        if not candidate.id or candidate.source_kind not in {"unknown", "photo_credit", "generated_label"}:
            raise ValueError()
        for key in ("page_url", "image_url", "thumbnail_url", "license_url"):
            value = getattr(candidate, key)
            if value:
                setattr(candidate, key, remote_url(value))
        return candidate
    except (TypeError, ValueError) as exc:
        raise ValueError("Некоректні дані або посилання збереженого кандидата.") from exc


def validated_criteria(criteria):
    if not isinstance(criteria, list) or len(criteria) > 10:
        raise ValueError("Некоректні критерії збереженого плану.")
    expected = {}
    for criterion in criteria:
        if (not isinstance(criterion, dict) or not isinstance(criterion.get("id"), str)
                or not criterion["id"] or criterion["id"] in expected
                or not isinstance(criterion.get("label"), str) or not criterion["label"].strip()
                or criterion.get("kind") not in ("subject", "pose", "view", "scene", "detail")):
            raise ValueError("Некоректні критерії збереженого плану.")
        expected[criterion["id"]] = criterion
    return expected


def validated_assessment(data, criteria, profile, verify_sources=True):
    """Ті самі межі для кешу та імпорту; жодної мовчазної підміни типів."""
    try:
        expected = validated_criteria(criteria)
        rating = Assessment(**data)
        if (profile not in PROFILES or rating.search_profile != profile
                or type(verify_sources) is not bool or type(rating.verify_sources) is not bool
                or rating.verify_sources != verify_sources
                or type(rating.score) is not int or not 0 <= rating.score <= 100
                or rating.media not in (("photo", "video_frame", "illustration", "render", "ai_generated", "uncertain")
                                        if verify_sources else ("unverified",))
                or rating.confidence not in (("high", "medium", "low") if verify_sources else ("not_checked",))
                or rating.reference_type not in ("ordinary", "adult_artistic_nude", "sexual", "unsafe_or_uncertain_nudity")
                or not isinstance(rating.reason, str)
                or any(not isinstance(value, list) or not all(isinstance(text, str) for text in value)
                       for value in (rating.matches, rating.misses))
                or not isinstance(rating.criterion_checks, list)
                or len(rating.criterion_checks) != len(expected)):
            raise ValueError()
        seen = set()
        for check in rating.criterion_checks:
            if (not isinstance(check, dict) or not isinstance(check.get("id"), str)
                    or check["id"] not in expected or check["id"] in seen
                    or check.get("status") not in ("met", "not_met", "uncertain")
                    or check.get("label") != expected[check["id"]]["label"]
                    or check.get("kind") != expected[check["id"]]["kind"]
                    or (profile == "custom" and (expected[check["id"]].get("priority") not in {"required", "preferred"}
                        or check.get("priority") != expected[check["id"]]["priority"]))
                    or not isinstance(check.get("evidence"), str)):
                raise ValueError()
            seen.add(check["id"])
        return rating
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise ValueError("Збережена оцінка неповна або не відповідає плану й режиму перевірки.") from exc
