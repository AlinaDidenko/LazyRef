"""Прості структури даних, спільні для інтерфейсу й пошуку."""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from .priorities import PROFILES, required
from .reasoning import DEFAULT_REASONING_EFFORT, validate_reasoning


class Cancelled(Exception):
    """Користувач зупинив роботу. Уже оцінені результати можна зберегти."""


def check_cancel(event: Event) -> None:
    if event.is_set():
        raise Cancelled()


@dataclass
class SearchRequest:
    description: str
    count: int
    output_dir: Path
    provider: str = "agent"
    candidate_limit: int = 72
    screenshots: bool = True
    codex_path: str = ""
    model: str = ""
    artistic_nudity: bool = False
    search_rounds: int = 6
    reference_image: Path | None = None
    search_profile: str = "strict"
    # Старі збережені запити залишають попередній алгоритм Codex.
    search_strategy: str = "legacy"
    discovery_limit: int = 200
    detail_limit: int = 40
    reuse_cache: bool = True
    verify_sources: bool = True
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    reference_mode: str = "pose"  # visual: фото або фрагмент з галереї, не обов’язково ціла людина
    custom_criteria: list[dict] | None = None
    enhanced_dedup: bool = False
    high_quality_only: bool = False

    def validate(self) -> None:
        validate_reasoning(self.reasoning_effort)
        if self.reference_mode not in {"pose", "visual"}:
            raise ValueError("Невідомий режим зразка.")
        if self.reference_mode == "visual" and self.reference_image is None:
            raise ValueError("Обери зображення для пошуку з галереї або комп’ютера.")
        if not 5 <= len(self.description.strip()) <= 2000:
            raise ValueError("Опиши референс: від 5 до 2000 символів.")
        if not 1 <= self.count <= 40:
            raise ValueError("Кількість має бути від 1 до 40.")
        if not 8 <= self.candidate_limit <= 120:
            raise ValueError("Ліміт кандидатів має бути від 8 до 120.")
        if not 1 <= self.search_rounds <= 8:
            raise ValueError("Кількість циклів агента має бути від 1 до 8.")
        if self.search_profile not in PROFILES:
            raise ValueError("Обери відомий варіант пріоритетів пошуку.")
        if self.custom_criteria is not None:
            if self.search_profile != "custom":
                raise ValueError("Ручні умови потребують режиму «Власні пріоритети».")
            from .custom_priorities import validate_choices
            validate_choices(self.custom_criteria)
        if self.search_strategy not in {"legacy", "wide"}:
            raise ValueError("Невідомий алгоритм пошуку.")
        if type(self.verify_sources) is not bool:
            raise ValueError("Перевірка джерела має бути ON або OFF.")
        if type(self.enhanced_dedup) is not bool or type(self.high_quality_only) is not bool:
            raise ValueError("Фільтри повторів і якості мають бути ON або OFF.")
        if not 20 <= self.discovery_limit <= 300 or not 8 <= self.detail_limit <= 120:
            raise ValueError("Новий пошук: 20–300 кандидатів і 8–120 детальних оцінок.")
        if self.reference_image is not None:
            if self.provider != "agent":
                raise ValueError("Пошук за зображенням працює в режимі агента Codex.")
            from .generation import validate_reference
            validate_reference(self.reference_image, min_side=32 if self.reference_mode == "visual" else 180)
        if self.provider != "agent":
            raise ValueError("Пошук працює лише через агент Codex.")

    @property
    def allows_artistic_nudity(self):
        return self.artistic_nudity

    @property
    def search_description(self):
        if self.search_profile == "custom" and self.custom_criteria is not None:
            from .custom_priorities import validate_choices
            labels = {"required": "Обов’язково", "preferred": "Бажано"}
            return "\n".join(f"{labels[c['priority']]}: {c['label']}"
                             for c in validate_choices(self.custom_criteria) if c["priority"] != "ignored")
        return self.description

    @property
    def reference_args(self):
        if self.reference_image is None:
            return {}
        args = {"reference_image": self.reference_image}
        if self.reference_mode == "visual":
            args["reference_mode"] = "visual"
        return args


@dataclass
class Candidate:
    id: str
    title: str
    image_url: str = ""
    page_url: str = ""
    thumbnail_url: str = ""
    source: str = ""
    creator: str = ""
    license: str = ""
    license_url: str = ""
    local_path: str = ""
    description: str = ""
    search_query: str = ""
    search_page: int = 1
    provider: str = ""
    source_notes: str = ""
    source_kind: str = "unknown"
    source_checked: bool = False


@dataclass
class Assessment:
    media: str
    confidence: str
    score: int
    reason: str
    matches: list[str]
    misses: list[str]
    reference_type: str = "ordinary"
    criterion_checks: list[dict] = field(default_factory=list)
    search_profile: str = "strict"
    verify_sources: bool = True

    @property
    def required_checks(self) -> list[dict]:
        return [c for c in self.criterion_checks if required(c, self.search_profile)]

    @property
    def coverage(self) -> float:
        checks = self.required_checks
        return (sum(c.get("status") == "met" for c in checks) / len(checks) if checks else 1.0)

    @property
    def is_close(self) -> bool:
        return self.eligible and self.score >= 75 and self.coverage == 1

    @property
    def unmet_criteria(self) -> list[str]:
        return [c["label"] for c in self.required_checks if c.get("status") != "met"]

    @property
    def unmet_preferences(self) -> list[str]:
        return [c["label"] for c in self.criterion_checks
                if not required(c, self.search_profile) and c.get("status") != "met"]

    @property
    def preference_coverage(self):
        preferred = [c for c in self.criterion_checks if c.get("priority") == "preferred"]
        return sum(c.get("status") == "met" for c in preferred) / len(preferred) if preferred else 0

    @property
    def pose_confirmed(self) -> bool:
        if self.search_profile != "pose":
            return True
        # Одяг/бар не можуть приховати непідтверджений ракурс або повну відсутність збігу пози.
        return (any(c.get("kind") == "subject" and c.get("status") == "met" for c in self.criterion_checks)
                and any(c.get("kind") == "pose" and c.get("status") == "met" for c in self.criterion_checks)
                and all(c.get("status") == "met" for c in self.criterion_checks if c.get("kind") == "view"))

    @property
    def eligible(self) -> bool:
        # OFF не стверджує, що зображення є справжнім фото.
        medium_ok = (self.media in {"photo", "video_frame"} and self.confidence == "high" if self.verify_sources
                     else self.media == "unverified" and self.confidence == "not_checked")
        return (medium_ok and self.score >= 60
                and self.coverage >= 2 / 3
                and self.pose_confirmed
                and (self.search_profile == "custom" or all(c.get("status") == "met" for c in self.criterion_checks
                        if c.get("kind") == "subject" and required(c, self.search_profile)))
                and self.reference_type in {"ordinary", "adult_artistic_nude"})

    def eligible_for(self, request: SearchRequest):
        return (self.verify_sources == request.verify_sources and self.eligible
                and (self.reference_type == "ordinary" or request.allows_artistic_nudity))


@dataclass
class Asset:
    candidate: Candidate
    path: Path
    preview: Path
    width: int
    height: int
    method: str = "download"
    assessment: Assessment | None = None
    review_owned: bool = False  # Internal session copy required for recovery.


@dataclass
class SearchResult:
    folder: Path | None
    assets: list[Asset]
    requested: int
    examined: int = 0
    duplicates: int = 0
    rejected: int = 0
    cancelled: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    access_links: list[dict[str, str]] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    usage_path: Path | None = None
    search_profile: str = "strict"
    retry_path: Path | None = None
    pending_review: int = 0
    export_pending: bool = False
    search_strategy: str = "legacy"
    discovered: int = 0
    screened: int = 0
    reused_assessments: int = 0
    verify_sources: bool = True
    enhanced_dedup: bool = False
    high_quality_only: bool = False
    low_resolution_rejected: int = 0

