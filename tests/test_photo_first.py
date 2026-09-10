"""The photo is resolved before the editorial pays for a post (publish.keep_publishable).

2026-09-09 evening: DeepMind's AlphaGenome post was drafted and re-expressed on Gemini and
then failed at 18:10 for want of a photo, while Google's copy of the same story, with a
picture, sat one slot below as a separate item.
"""

import pytest

from apps.digest import llm, publish, ranking, tasks
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def src(db):
    return Source.objects.create(name="src", connector="rss", url="https://src.example")


def make(src, n, image=None, title=None):
    art = Article.objects.create(
        source=src,
        canonical_url=f"https://src.example/{n}",
        content_hash=f"photo{n:059d}",
        title=title or f"Story {n}",
        extracted_text=f"Text {n} " * 40,
        status=Article.Status.CLASSIFIED,
        meta={"image_url": image} if image else {},
    )
    an = Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 7,
            "evidence": 8,
            "production_readiness": 6,
            "audience": 6,
            "reason": "x",
        },
        latency_ms=1,
    )
    return art, an


def test_a_story_keeps_its_slot_only_with_a_photo(src):
    with_photo, an1 = make(src, 1, image="https://img.example/1.jpg")
    without, an2 = make(src, 2)
    kept = publish.keep_publishable([(with_photo, an1, 0.8, []), (without, an2, 0.7, [])])
    assert [(a.id, s) for a, _, s, _ in kept] == [(with_photo.id, 0.8)]


def test_the_copy_with_the_photo_becomes_the_primary(src):
    own, an_own = make(src, 3, title="DeepMind: AlphaGenome Atlas")
    blog, an_blog = make(src, 4, image="https://img.example/4.jpg", title="Google blog: Atlas")
    other, _ = make(src, 5)
    kept = publish.keep_publishable([(own, an_own, 0.82, [blog, other])])
    assert len(kept) == 1
    primary, analysis, score, secondary = kept[0]
    assert primary.id == blog.id
    assert analysis.id == an_blog.id, "the analysis follows the primary"
    assert score == 0.82, "the story keeps its score"
    assert [s.id for s in secondary] == [own.id, other.id]


def test_a_lookup_that_raises_counts_as_no_photo(src, monkeypatch):
    art, an = make(src, 6)

    def boom(article):
        raise RuntimeError("dns")

    monkeypatch.setattr(publish, "resolve_photo_url", boom)
    assert publish.keep_publishable([(art, an, 0.5, [])]) == []


def test_a_page_fetch_that_finds_a_photo_is_kept_and_stored(src, monkeypatch):
    art, an = make(src, 7)
    monkeypatch.setattr(publish.trafilatura, "fetch_url", lambda *a, **k: "<html></html>")
    monkeypatch.setattr(
        publish.media, "extract_image_url_from_html", lambda *a, **k: "https://img.example/7.jpg"
    )
    kept = publish.keep_publishable([(art, an, 0.5, [])])
    assert [a.id for a, _, _, _ in kept] == [art.id]
    art.refresh_from_db()
    assert art.meta["image_url"] == "https://img.example/7.jpg"


def test_compose_runs_the_editorial_only_on_publishable_stories(src, monkeypatch, settings):
    settings.PUBLISHING_ENABLED = False
    a, an_a = make(src, 8, image="https://img.example/8.jpg")
    b, an_b = make(src, 9)
    selected = [(a, an_a, 0.8, []), (b, an_b, 0.7, [])]
    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target_date: selected)
    asked = []

    def fake_editorial(ids):
        asked.append(list(ids))
        return []

    monkeypatch.setattr(llm, "analyse_for_digest_logic", fake_editorial)
    tasks.compose_and_publish(digest_date_str="2026-09-10", edition="evening")
    assert asked == [[a.id]], "the story without a photo never reaches the editorial"
