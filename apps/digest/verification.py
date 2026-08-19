"""Deterministic cross-outlet corroboration for benchmark claims."""

import logging
import re

from django.db.models import Prefetch

from .models import Analysis, Article, Digest
from .story_identity import subject_key
from .translation_gates import extract_numbers

log = logging.getLogger(__name__)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMBER = re.compile(r"\d+(?:[,.\u00a0 ]\d{3})*(?:\.\d+)?")
_METRIC_CUE = re.compile(
    r"\b(?:accuracy|benchmark|bleu|elo|latency|performance|precision|recall|rouge|"
    r"score|scored|scores|throughput)\b",
    re.IGNORECASE,
)
_DIRECT_UNIT = re.compile(
    r"(?:%|percent(?:age)?|points?|milliseconds?|msecs?|ms|seconds?|secs?|s|"
    r"tokens?\s*(?:/|per)\s*second|x(?:\s+(?:faster|slower))?|times?\s+(?:faster|slower))"
    r"(?=\W|$)",
    re.IGNORECASE,
)
_VERSION_PREFIX = re.compile(
    r"(?:\b(?:v|gpt|qwen|llama|gemma|mistral)[\w.-]*[-_]*$|"
    r"\b(?:version|release|model)[\s_-]*$)",
    re.IGNORECASE,
)


def _has_direct_unit(sentence: str, end: int) -> bool:
    """Return whether a metric unit is attached immediately after a number."""
    suffix = sentence[end : end + 32]
    return bool(_DIRECT_UNIT.match(suffix.lstrip()))


def _is_version_or_year(sentence: str, start: int, end: int, number: str) -> bool:
    """Reject years and model/version numbers unless the number itself has a unit."""
    if _has_direct_unit(sentence, end):
        return False
    if number.isdigit() and 1900 <= int(number) <= 2100:
        return True
    prefix = sentence[max(0, start - 18) : start]
    return bool(_VERSION_PREFIX.search(prefix))


def _metric_numbers(text: str) -> set[str]:
    """Return numeric tokens that have a local benchmark meaning in ``text``."""
    accepted: set[str] = set()
    for sentence in _SENTENCE_BREAK.split(text or ""):
        for match in _NUMBER.finditer(sentence):
            values = extract_numbers(match.group())
            if not values:
                continue
            number = next(iter(values))
            local_start = max(0, match.start() - 48)
            local_end = min(len(sentence), match.end() + 48)
            local = sentence[local_start:local_end]
            direct_unit = _has_direct_unit(sentence, match.end())
            if not direct_unit and not _METRIC_CUE.search(local):
                continue
            if _is_version_or_year(sentence, match.start(), match.end(), number):
                continue
            accepted.add(number)
    return accepted


def shared_benchmark_numbers(primary_text: str, secondary_text: str) -> set[str]:
    """Return normalized metric tokens reported by both article texts."""
    return _metric_numbers(primary_text) & _metric_numbers(secondary_text)


def _article_text(article: Article) -> str:
    return f"{article.title}\n{article.extracted_text or ''}"


def cluster_has_independent_benchmark(primary: Article, secondary: Article) -> bool:
    """Whether two articles independently report at least one shared metric token."""
    if not primary or not secondary:
        return False
    if subject_key(primary.canonical_url) == subject_key(secondary.canonical_url):
        return False
    if not primary.extracted_text or not secondary.extracted_text:
        return False
    return bool(shared_benchmark_numbers(_article_text(primary), _article_text(secondary)))


def _latest_editorial(article: Article) -> Analysis | None:
    prefetched = getattr(article, "_verification_editorials", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return (
        article.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN).order_by("-created_at").first()
    )


def apply_cluster_evidence(digest: Digest) -> int:
    """Promote primary editorial evidence when a cluster has independent corroboration."""
    editorial_qs = Analysis.objects.filter(stage=Analysis.Stage.EDITORIAL_EN).order_by(
        "-created_at"
    )
    secondary_qs = Article.objects.prefetch_related(
        Prefetch("analyses", queryset=editorial_qs, to_attr="_verification_editorials")
    )
    items = digest.items.select_related("article").prefetch_related(
        Prefetch(
            "article__analyses",
            queryset=editorial_qs,
            to_attr="_verification_editorials",
        ),
        Prefetch("secondary_articles", queryset=secondary_qs, to_attr="_verification_secondaries"),
    )

    upgraded = 0
    for item in items:
        editorial = _latest_editorial(item.article)
        if editorial is None:
            log.warning("Skipping evidence check: item %s has no editorial_en", item.id)
            continue
        payload = editorial.payload or {}
        if payload.get("evidence_level") == "multiple_evidence":
            continue

        secondaries = getattr(item, "_verification_secondaries", None)
        if secondaries is None:
            secondaries = list(item.secondary_articles.all())
        for secondary in secondaries:
            shared = shared_benchmark_numbers(_article_text(item.article), _article_text(secondary))
            if not shared or subject_key(item.article.canonical_url) == subject_key(
                secondary.canonical_url
            ):
                continue
            payload["evidence_level"] = "multiple_evidence"
            editorial.payload = payload
            editorial.save(update_fields=["payload"])
            upgraded += 1
            log.info(
                "Promoted evidence for item %s using %s cross-outlet metric tokens",
                item.id,
                len(shared),
            )
            break
    return upgraded
