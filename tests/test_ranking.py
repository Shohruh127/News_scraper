"""Tests for ranking, candidate selection, digest composition, and template rendering."""

from datetime import date, datetime, timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.digest import ranking
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source
from tests.helpers import make_editorial


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="test_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
        priority=80,
    )


@pytest.fixture
def classified_articles(db, source):
    """Create a set of classified articles with diverse topics and maturities."""
    articles = []

    # Art 1: frontier_models, reproducible_open_source (high score, +0.15 open bonus)
    a1 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art1",
        content_hash="h1",
        title="Open Model 30B Released",
        extracted_text="Text 1 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a1,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "reproducible_open_source",
            "novelty": 9,
            "evidence": 9,
            "production_readiness": 8,
            "reason": "Open model",
        },
        latency_ms=12000,
    )
    make_editorial(a1, summary_uz="30B ochiq model taqdim etildi.")
    articles.append(a1)

    # Art 2: ai_agents, live_product
    a2 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art2",
        content_hash="h2",
        title="Agent Framework V2",
        extracted_text="Text 2 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a2,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "ai_agents",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 9,
            "reason": "Agent update",
        },
        latency_ms=11000,
    )
    make_editorial(a2, summary_uz="MCP spetsifikatsiyasi yangilandi.")
    articles.append(a2)

    # Art 3: paper_only (hard excluded by ranking, NOT by triage)
    a3 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art3",
        content_hash="h3",
        title="Theoretical Paper on LLMs",
        extracted_text="Text 3 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a3,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "new_approaches",
            "maturity": "paper_only",
            "novelty": 10,
            "evidence": 10,
            "production_readiness": 1,
            "reason": "Just a paper",
        },
        latency_ms=10000,
    )
    articles.append(a3)

    # Art 4: announcement_only (hard excluded by ranking)
    a4 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art4",
        content_hash="h4",
        title="Company Announces Future Product",
        extracted_text="Text 4 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a4,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "announcement_only",
            "novelty": 7,
            "evidence": 2,
            "production_readiness": 1,
            "reason": "Future announcement",
        },
        latency_ms=9000,
    )
    articles.append(a4)

    return articles


def test_calculate_score_bonuses_and_penalties(db, source):
    art = Article.objects.create(
        source=source,
        canonical_url="https://github.com/test/repo",
        content_hash="h_score",
        title="Test Score Repo",
        extracted_text="Content " * 50,
    )
    analysis = Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "production_engineering",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
        latency_ms=5000,
    )

    score = ranking.calculate_score(art, analysis)
    assert score > 0.8


def test_low_evidence_penalty(db, source):
    """evidence <= 3 causes a -0.15 penalty."""
    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/vendor-claim",
        content_hash="h_low_ev",
        title="Vendor Claim Article",
        extracted_text="Content " * 50,
    )
    analysis_low = Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "startups",
            "maturity": "live_product",
            "novelty": 5,
            "evidence": 2,
            "production_readiness": 5,
        },
        latency_ms=5000,
    )
    score_low = ranking.calculate_score(art, analysis_low)

    art2 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/good-evidence",
        content_hash="h_hi_ev",
        title="Good Evidence Article",
        extracted_text="Content " * 50,
    )
    analysis_hi = Analysis.objects.create(
        article=art2,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "startups",
            "maturity": "live_product",
            "novelty": 5,
            "evidence": 8,
            "production_readiness": 5,
        },
        latency_ms=5000,
    )
    score_hi = ranking.calculate_score(art2, analysis_hi)
    assert score_hi > score_low


def test_hard_exclusion_and_candidate_selection(db, classified_articles):
    candidates = ranking.select_digest_candidates()
    assert len(candidates) == 2
    articles_in_digest = [c[0] for c in candidates]
    assert classified_articles[0] in articles_in_digest
    assert classified_articles[1] in articles_in_digest
    assert classified_articles[2] not in articles_in_digest
    assert classified_articles[3] not in articles_in_digest


def test_no_padding_rule(db, classified_articles):
    """If only 2 items qualify, the digest must have exactly 2 items (never pad to 7)."""
    digest = ranking.compose_digest()
    assert digest.items.count() == 2
    assert digest.status == Digest.Status.COMPOSED


def test_compose_digest_uses_supplied_candidates(db, classified_articles, monkeypatch):
    primary = classified_articles[0]
    secondary = classified_articles[1]
    analysis = primary.analyses.first()
    candidates = [(primary, analysis, 0.99, [secondary])]

    monkeypatch.setattr(
        ranking,
        "select_digest_candidates",
        lambda *_args, **_kwargs: pytest.fail("candidate selection must not run twice"),
    )

    digest = ranking.compose_digest(timezone.localdate(), candidates=candidates)
    item = digest.items.get()

    assert item.article_id == primary.id
    assert list(item.secondary_articles.values_list("id", flat=True)) == [secondary.id]


def test_cross_digest_exclusion(db, classified_articles):
    """Articles already published in a digest must never appear in subsequent digests."""
    today = timezone.localdate()
    d1 = ranking.compose_digest(today)
    assert d1.items.count() == 2

    # Attempt to compose for Day 2 — both qualifying articles are already in d1
    candidates_day2 = ranking.select_digest_candidates(today + timedelta(days=1))
    assert len(candidates_day2) == 0


def test_target_date_window_backfill(db, source):
    """Candidate selection respects target_date window rather than now()."""
    target_d = date(2026, 8, 1)
    target_dt = timezone.make_aware(datetime(2026, 8, 1, 12, 0, 0))

    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/old-backfill-art",
        content_hash="h_backfill",
        title="Backfill Old Article",
        extracted_text="Text " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Article.objects.filter(id=art.id).update(fetched_at=target_dt)

    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "ai_agents",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
        latency_ms=1000,
    )

    # Selecting for target_d finds it
    cands_at_target = ranking.select_digest_candidates(target_d)
    assert len(cands_at_target) == 1
    assert cands_at_target[0][0].id == art.id

    # Selecting for 15 days later (2026-08-16) does NOT find it because cutoff is 7 days
    cands_later = ranking.select_digest_candidates(date(2026, 8, 16))
    assert len(cands_later) == 0


def test_compose_digest_idempotency_constraint(db, classified_articles):
    today = timezone.localdate()
    ranking.compose_digest(today)

    with pytest.raises(IntegrityError):
        ranking.compose_digest(today)


def test_render_templates_snapshot(db, classified_articles):
    today = timezone.localdate()
    digest = ranking.compose_digest(today)

    post_html = ranking.render_channel_post(digest)
    assert str(today) in post_html
    assert "Open Model 30B Released" in post_html
    assert "30B ochiq model taqdim etildi." in post_html
    assert "<b>" in post_html and "</b>" in post_html

    comment_html = ranking.render_group_comment(digest)
    assert str(today) in comment_html
    assert "Open Model 30B Released" in comment_html


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/ollama/ollama/releases/tag/v0.32.10", "github.com/ollama"),
        ("https://github.com/k2-fsa/sherpa-onnx/releases/tag/v1.12.15", "github.com/k2-fsa"),
        ("https://www.anthropic.com/news/claude-opus-5", "anthropic.com"),
        ("https://anthropic.com/news/fable-5-safeguards", "anthropic.com"),
        ("https://api-docs.deepseek.com/guides/v4-pro", "api-docs.deepseek.com"),
        ("https://api-docs.deepseek.com/news/pricing", "api-docs.deepseek.com"),
        ("https://gds.blog.gov.uk/2026/08/06/a-post/", "gds.blog.gov.uk"),
        ("https://technology.blog.gov.uk/2026/07/07/another/", "technology.blog.gov.uk"),
        (
            "https://raw.githubusercontent.com/ollama/ollama/main/README.md",
            "raw.githubusercontent.com",
        ),
        # Exact equality, not a suffix test. These two are what a suffix match would
        # swallow -- both really do end in "github.com", and mygithub.com is an unrelated
        # domain that would get its first path segment glued onto the key.
        # raw.githubusercontent.com above does NOT test this: it ends in
        # "githubusercontent.com", so a suffix match leaves it alone either way.
        ("https://gist.github.com/someuser/abc123", "gist.github.com"),
        ("https://mygithub.com/owner/repo", "mygithub.com"),
        ("https://github.com", "github.com"),
        ("https://example.com:8443/post", "example.com"),
    ],
)
def test_subject_key(url, expected):
    assert ranking.subject_key(url) == expected


@pytest.fixture
def repetition_articles(db, source):
    """Create three releases for Ollama, two for DeepSeek, and two for Anthropic."""
    spec = [
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.10",
            "production_engineering",
            9,
            "Ollama changes the default repeat penalty for local inference runs. ",
        ),
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.9",
            "production_engineering",
            8,
            "Nemotron Lightning arrives with fresh agent tooling and driver support. ",
        ),
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.8",
            "production_engineering",
            7,
            "Muse Glimmer joins the coding lineup for editor integrations everywhere. ",
        ),
        (
            "https://api-docs.deepseek.com/guides/v4-pro",
            "frontier_models",
            6,
            "A quiet publication describes the reasoning system behind version four. ",
        ),
        (
            "https://api-docs.deepseek.com/news/pricing",
            "frontier_models",
            5,
            "Peak and off peak tariffs now apply across every inference endpoint. ",
        ),
        (
            "https://www.anthropic.com/news/claude-opus-5",
            "frontier_models",
            4,
            "Introducing the most capable frontier assistant this laboratory has built. ",
        ),
        (
            "https://www.anthropic.com/news/fable-5-safeguards",
            "safety_security",
            3,
            "Biology risk evaluations were tightened substantially during this quarter. ",
        ),
    ]
    made = []
    for index, (url, topic, novelty, body) in enumerate(spec):
        article = Article.objects.create(
            source=source,
            canonical_url=url,
            content_hash=f"rep{index}",
            title=f"Repetition fixture {index}",
            extracted_text=body * 30,
            status=Article.Status.CLASSIFIED,
        )
        Analysis.objects.create(
            article=article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="gemma4:31b",
            payload={
                "primary_topic": topic,
                "maturity": "live_product",
                "novelty": novelty,
                "evidence": novelty,
                "production_readiness": novelty,
                "reason": "fixture",
            },
            latency_ms=1000,
        )
        make_editorial(article)
        made.append(article)
    return made


def _selected_urls(candidates):
    return [article.canonical_url for article, _, _, _ in candidates]


def test_repetitive_subjects_are_dropped(repetition_articles):
    urls = _selected_urls(ranking.select_digest_candidates())

    assert "https://github.com/ollama/ollama/releases/tag/v0.32.10" in urls
    assert "https://github.com/ollama/ollama/releases/tag/v0.32.9" not in urls
    assert "https://github.com/ollama/ollama/releases/tag/v0.32.8" not in urls
    assert "https://api-docs.deepseek.com/guides/v4-pro" in urls
    assert "https://api-docs.deepseek.com/news/pricing" not in urls


def test_same_subject_different_topic_both_survive(repetition_articles):
    urls = _selected_urls(ranking.select_digest_candidates())

    assert "https://www.anthropic.com/news/claude-opus-5" in urls
    assert "https://www.anthropic.com/news/fable-5-safeguards" in urls


def test_backfill_keeps_the_digest_at_its_cap(repetition_articles, settings):
    settings.DIGEST_MAX_ITEMS = 3

    assert len(ranking.select_digest_candidates()) == 3


def test_rule_is_silent_when_every_subject_is_distinct(db, source):
    bodies = [
        "Alpha describes a storage engine rewrite with measured throughput gains. ",
        "Bravo reports on a scheduler that reorders work across many machines. ",
        "Charlie documents a compiler pass that removes redundant memory loads. ",
    ]
    for index, body in enumerate(bodies):
        article = Article.objects.create(
            source=source,
            canonical_url=f"https://site{index}.example/post",
            content_hash=f"dist{index}",
            title=f"Distinct story {index}",
            extracted_text=body * 30,
            status=Article.Status.CLASSIFIED,
        )
        Analysis.objects.create(
            article=article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="gemma4:31b",
            payload={
                "primary_topic": "ai_agents",
                "maturity": "live_product",
                "novelty": 8,
                "evidence": 8,
                "production_readiness": 8,
                "reason": "fixture",
            },
            latency_ms=1000,
        )
        make_editorial(article)

    assert len(ranking.select_digest_candidates()) == 3


def test_render_item_post_respects_v2_flag(db, source, settings):
    """render_item_post switches between v1 HTML template and v2 post_format."""
    article = Article.objects.create(
        source=source,
        canonical_url="https://site.example/v2-flag-test",
        content_hash="hash-v2",
        title="V2 Flag item",
        extracted_text="Text " * 40,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "robotics",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
            "reason": "fixture",
        },
        latency_ms=1000,
    )
    make_editorial(
        article,
        lead_uz="EHang uchar taksi xizmatini yo'lga qo'ydi.",
        body_1_uz="Parvoz 20 daqiqa davom etadi.",
    )

    digest = Digest.objects.create(digest_date=date(2026, 8, 23))
    item = DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)

    # Flag off: renders v1 template (contains emoji header)
    settings.POST_FORMAT_V2_ENABLED = False
    v1_html = ranking.render_item_post(item)
    assert "<b>" in v1_html

    # Flag on: renders v2 prose (contains single inline link and final hashtag)
    settings.POST_FORMAT_V2_ENABLED = True
    v2_html = ranking.render_item_post(item)
    assert "#robototexnika" in v2_html
    assert "<b>" not in v2_html
