"""Tests for ranking, candidate selection, digest composition, and template rendering."""

from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone

from apps.digest import ranking
from apps.digest.models import Analysis, Article, Digest, DigestItem
from tests.helpers import make_editorial


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


def test_composing_twice_does_not_duplicate_a_block(db, classified_articles):
    """The invariant the old IntegrityError protected: never repost what already went out."""
    today = timezone.localdate()
    # compose_and_publish passes its candidates explicitly. Without them,
    # select_digest_candidates already excludes anything sitting in a digest, so a second
    # call adds nothing and the guard is never exercised — the first version of this test
    # passed with the guard deleted.
    candidates = ranking.select_digest_candidates(today)
    assert candidates, "fixture must produce candidates"

    first = ranking.compose_digest(today, candidates=candidates)
    positions = list(first.items.values_list("position", flat=True))
    assert positions, "fixture must produce a non-empty block"

    second = ranking.compose_digest(today, candidates=candidates)

    assert second.pk == first.pk
    assert list(second.items.values_list("position", flat=True)) == positions


def test_an_empty_unpublished_slot_is_filled_not_refused(db, classified_articles):
    """Measured 2026-08-26: this cost a whole edition.

    A stray compose_and_publish created an empty digest while triage was still running.
    The real cycle then paid for five English analyses and five Uzbek translations, hit
    the unique constraint here, and discarded every one of them. Nothing published.
    """
    today = timezone.localdate()
    empty = Digest.objects.create(
        digest_date=today, edition=Digest.Edition.EVENING, status=Digest.Status.COMPOSED
    )
    assert not empty.items.exists()

    filled = ranking.compose_digest(today)

    assert filled.pk == empty.pk, "must reuse the slot, not fail on the constraint"
    assert filled.items.exists(), "the candidates were dropped instead of composed"


def test_a_published_slot_is_left_alone(db, classified_articles):
    """Refilling a published digest would compose a second block over one already sent."""
    today = timezone.localdate()
    published = Digest.objects.create(
        digest_date=today, edition=Digest.Edition.EVENING, status=Digest.Status.PUBLISHED
    )

    result = ranking.compose_digest(today)

    assert result.pk == published.pk
    assert not result.items.exists()


def test_render_templates_snapshot(db, classified_articles):
    today = timezone.localdate()
    digest = ranking.compose_digest(today)
    for idx, item in enumerate(digest.items.all(), start=1):
        item.channel_delivery_state = "sent"
        item.channel_message_id = 100 + idx
        item.save()

    post_html = ranking.render_roundup_post(digest)
    assert "dayjest" in post_html.lower()
    assert "<b>" in post_html and "</b>" in post_html
    assert "#dayjest" in post_html

    appendix_html = ranking.render_item_appendix(digest.items.first())
    assert "Open Model 30B Released" in appendix_html


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
    settings.DIGEST_SELECT_MARGIN = 0

    assert len(ranking.select_digest_candidates()) == 3


def test_rule_is_silent_when_every_subject_is_distinct(db, source):
    topics = ["ai_agents", "robotics", "frontier_models"]
    bodies = [
        "Alpha describes a storage engine rewrite with measured throughput gains. ",
        "Bravo reports on a scheduler that reorders work across many machines. ",
        "Charlie documents a compiler pass that removes redundant memory loads. ",
    ]
    for index, (topic, body) in enumerate(zip(topics, bodies, strict=False)):
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
                "primary_topic": topic,
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


def test_render_item_post_renders_v2_format(db, source):
    """render_item_post renders v2 post_format with bold headline and hashtag."""
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
        headline_uz="EHang yangi uchar taksi chiqardi",
        lead_uz="EHang uchar taksi xizmatini yo'lga qo'ydi.",
        body_1_uz="Parvoz 20 daqiqa davom etadi.",
    )

    digest = Digest.objects.create(digest_date=date(2026, 8, 23))
    item = DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)

    html = ranking.render_item_post(item)
    assert "#robototexnika" in html
    assert "<b>" in html


@pytest.mark.django_db
def test_item_data_carries_the_kicker(digest_with_item):
    from apps.digest import ranking

    item = digest_with_item.items.first()
    data = ranking._item_data(item)
    assert data["kicker_uz"] == "Endi o'z serveringizda ishlatsa bo'ladi."
    assert data["headline_uz"] == "Yangi model chiqdi"


@pytest.mark.django_db
def test_rendered_item_post_carries_headline_and_kicker(digest_with_item):
    from apps.digest import ranking

    html = ranking.render_item_post(digest_with_item.items.first())
    assert html.splitlines()[0] == "<b>Yangi model chiqdi</b>"
    assert "serveringizda ishlatsa" in html


@pytest.mark.django_db
def test_roundup_post_lists_all_sent_items(digest_with_two_items):
    """The roundup carries one numbered line per item that landed,
    pointing back to the channel post.
    """
    from apps.digest import ranking
    from apps.digest.models import DeliveryState

    first, second = digest_with_two_items.items.order_by("position")
    first.channel_delivery_state = DeliveryState.SENT
    first.channel_message_id = 101
    first.save()
    second.channel_delivery_state = DeliveryState.SENT
    second.channel_message_id = 102
    second.save()

    html = ranking.render_roundup_post(digest_with_two_items)
    assert "<b>" in html.splitlines()[0]
    assert "1. " in html
    assert "2. " in html
    assert "t.me/" in html or f"/{101}" in html
    assert "#dayjest" in html


@pytest.mark.django_db
def test_roundup_post_skips_items_that_failed_to_post(digest_with_two_items):
    """A failed post must not be linked in the roundup: the link would 404 in Telegram."""
    from apps.digest import ranking
    from apps.digest.models import DeliveryState

    first, second = digest_with_two_items.items.order_by("position")
    first.channel_delivery_state = DeliveryState.SENT
    first.channel_message_id = 101
    first.save()
    second.channel_delivery_state = DeliveryState.FAILED
    second.channel_message_id = None
    second.save()

    html = ranking.render_roundup_post(digest_with_two_items)
    assert "1. " in html
    assert "2. " not in html


@pytest.mark.django_db
def test_the_appendix_reads_technical_from_the_uzbek_row(digest_with_item):
    """The single-stage row carries the technical block; the English row is not written."""
    from apps.digest import ranking
    from apps.digest.models import Analysis

    item = digest_with_item.items.first()
    item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN).delete()
    uz = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_UZ)
    uz.payload = dict(uz.payload, technical={"repo_url": "https://example.com/only-here"})
    uz.save(update_fields=["payload"])

    assert "only-here" in ranking.render_item_appendix(item)
