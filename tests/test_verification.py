"""Tests for deterministic cross-outlet benchmark corroboration."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.digest import verification
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source

pytestmark = pytest.mark.django_db


def test_shared_metric_numbers_normalize_units_and_separators():
    assert verification.shared_benchmark_numbers(
        "The model is 11x faster and the benchmark score reached 2.4 trillion.",
        "The model is 11x faster and the benchmark score reached 2.4 trillion.",
    ) == {"11", "2.4"}
    assert verification.shared_benchmark_numbers(
        "Benchmark score: 5,000 points.",
        "Benchmark score: 5000 points.",
    ) == {"5000"}


def test_different_metric_values_do_not_promote():
    assert (
        verification.shared_benchmark_numbers(
            "The benchmark is 11x faster.", "The benchmark is 7x faster."
        )
        == set()
    )


def test_years_and_versions_do_not_promote_even_with_benchmark_cue():
    assert (
        verification.shared_benchmark_numbers(
            "The 2026 benchmark reports a score of 88.",
            "The 2026 benchmark reports a score of 91.",
        )
        == set()
    )
    assert (
        verification.shared_benchmark_numbers(
            "GPT-4 and v2 are listed in the benchmark.",
            "GPT-4 and v2 are listed in the benchmark.",
        )
        == set()
    )


def test_model_identifier_numbers_do_not_ride_alongside_a_real_score():
    values = verification.shared_benchmark_numbers(
        "Qwen3.8-2.4T benchmark score 88.",
        "Qwen3.8-2.4T benchmark score 88.",
    )
    assert values == {"88"}


def test_year_is_rejected_when_unrelated_to_metric_value():
    assert (
        verification.shared_benchmark_numbers(
            "In 2026, the benchmark score is 88.",
            "In 2026, the benchmark score is 91.",
        )
        == set()
    )


@pytest.mark.parametrize(
    "text",
    [
        "accuracy reached 92%",
        "latency was 120 milliseconds",
        "throughput reached 500 tokens per second",
        "the test scored 87 points",
    ],
)
def test_direct_metric_units_are_accepted(text):
    assert verification.shared_benchmark_numbers(text, text) != set()


def test_ordinary_prose_and_empty_text_are_not_evidence():
    assert (
        verification.shared_benchmark_numbers("There are 11 examples.", "There are 11 examples.")
        == set()
    )
    assert verification.shared_benchmark_numbers("", "score 88") == set()


def _article(source, *, url, title, text):
    return Article.objects.create(
        source=source,
        canonical_url=url,
        content_hash=f"hash-{url}",
        title=title,
        extracted_text=text,
        published_at=timezone.now() - timedelta(minutes=1),
        status=Article.Status.CLASSIFIED,
    )


def _source(name, url="https://feed.example/rss"):
    return Source.objects.create(name=name, connector=Source.Connector.RSS, url=url)


def test_same_canonical_outlet_does_not_promote_across_source_rows():
    first = _article(
        _source("first"),
        url="https://www.example.com/one",
        title="One",
        text="Benchmark score: 88 points.",
    )
    second = _article(
        _source("second"),
        url="https://example.com/two",
        title="Two",
        text="Benchmark score: 88 points.",
    )
    assert verification.cluster_has_independent_benchmark(first, second) is False


def test_different_outlets_on_one_aggregator_source_can_promote():
    source = _source("aggregator")
    first = _article(
        source,
        url="https://first.example/news",
        title="One",
        text="Benchmark score: 88 points.",
    )
    second = _article(
        source,
        url="https://second.example/news",
        title="Two",
        text="Benchmark score: 88 points.",
    )
    assert verification.cluster_has_independent_benchmark(first, second) is True


def test_title_only_article_is_not_evidence():
    first = _article(
        _source("title-first"),
        url="https://first.example/news",
        title="Benchmark score: 88 points",
        text="",
    )
    second = _article(
        _source("title-second"),
        url="https://second.example/news",
        title="Benchmark score: 88 points",
        text="",
    )
    assert verification.cluster_has_independent_benchmark(first, second) is False


def _digest_with_cluster(primary, secondaries):
    digest = Digest.objects.create(digest_date=timezone.localdate())
    item = DigestItem.objects.create(digest=digest, article=primary, position=1, score=0.9)
    item.secondary_articles.set(secondaries)
    return digest, item


def _editorial(article, evidence_level="vendor_claim_only"):
    return Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.EDITORIAL_EN,
        model_tag="test",
        payload={"evidence_level": evidence_level},
        latency_ms=1,
    )


def test_apply_promotes_primary_only_and_is_idempotent():
    source = _source("cluster")
    primary = _article(
        source,
        url="https://primary.example/news",
        title="Primary",
        text="Benchmark score: 88 points.",
    )
    secondary = _article(
        source,
        url="https://secondary.example/news",
        title="Secondary",
        text="Benchmark score: 88 points.",
    )
    primary_en = _editorial(primary)
    secondary_en = _editorial(secondary)
    digest, _item = _digest_with_cluster(primary, [secondary])

    assert verification.apply_cluster_evidence(digest) == 1
    primary_en.refresh_from_db()
    secondary_en.refresh_from_db()
    assert primary_en.payload["evidence_level"] == "multiple_evidence"
    assert secondary_en.payload["evidence_level"] == "vendor_claim_only"
    assert verification.apply_cluster_evidence(digest) == 0


def test_apply_preserves_existing_multiple_and_accepts_any_qualifying_secondary():
    source = _source("cluster-two")
    primary = _article(
        source,
        url="https://primary-two.example/news",
        title="Primary",
        text="Benchmark score: 88 points.",
    )
    wrong = _article(
        source,
        url="https://wrong.example/news",
        title="Wrong",
        text="Benchmark score: 91 points.",
    )
    right = _article(
        source,
        url="https://right.example/news",
        title="Right",
        text="Benchmark score: 88 points.",
    )
    editorial = _editorial(primary)
    digest, _item = _digest_with_cluster(primary, [wrong, right])

    assert verification.apply_cluster_evidence(digest) == 1
    editorial.refresh_from_db()
    assert editorial.payload["evidence_level"] == "multiple_evidence"


def test_apply_skips_missing_editorial_and_nonqualifying_cluster():
    source = _source("cluster-three")
    primary = _article(
        source,
        url="https://primary-three.example/news",
        title="Primary",
        text="Benchmark score: 88 points.",
    )
    secondary = _article(
        source,
        url="https://secondary-three.example/news",
        title="Secondary",
        text="Benchmark score: 91 points.",
    )
    _editorial(primary)
    digest, _item = _digest_with_cluster(primary, [secondary])
    assert verification.apply_cluster_evidence(digest) == 0


def test_apply_preserves_existing_multiple_without_work():
    source = _source("cluster-existing")
    primary = _article(
        source,
        url="https://primary-existing.example/news",
        title="Primary",
        text="Benchmark score: 88 points.",
    )
    secondary = _article(
        source,
        url="https://secondary-existing.example/news",
        title="Secondary",
        text="Benchmark score: 88 points.",
    )
    editorial = _editorial(primary, evidence_level="multiple_evidence")
    digest, _item = _digest_with_cluster(primary, [secondary])

    assert verification.apply_cluster_evidence(digest) == 0
    editorial.refresh_from_db()
    assert editorial.payload["evidence_level"] == "multiple_evidence"
