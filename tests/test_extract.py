"""Extraction, normalisation and the pre-filter that protects sources from us."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.digest import extract, tasks
from apps.digest.models import Article, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def source():
    return Source.objects.create(name="s", connector="rss", url="https://e.com/f.xml")


# --------------------------------------------------------------- canonical_url


def test_tracking_params_collapse_to_one_url():
    a = extract.canonical_url("https://e.com/post?utm_source=x&utm_medium=y")
    b = extract.canonical_url("https://e.com/post")
    assert a == b


def test_meaningful_query_params_are_kept():
    assert "id=42" in extract.canonical_url("https://e.com/p?id=42&utm_source=x")


def test_host_case_and_trailing_slash_are_normalised():
    assert extract.canonical_url("https://Example.COM/post/") == extract.canonical_url(
        "https://example.com/post"
    )


# --------------------------------------------------------------- content_hash


def test_whitespace_churn_does_not_create_a_new_article():
    assert extract.content_hash("Hello   world\n\n") == extract.content_hash("hello world")


def test_different_text_hashes_differently():
    assert extract.content_hash("alpha") != extract.content_hash("beta")


# --------------------------------------------------------------- blocked pages


@pytest.mark.parametrize(
    "text",
    [
        "Please enable JavaScript to continue",
        "Access Denied — you do not have permission",
        "Are you a robot? Complete the captcha",
    ],
)
def test_bot_walls_are_detected(text):
    assert extract.looks_blocked(text)


def test_ordinary_article_text_is_not_flagged():
    assert not extract.looks_blocked("OpenAI released a new model today. " * 20)


# --------------------------------------------------------------- normalize


def test_source_supplied_text_avoids_a_page_fetch(source):
    """GitHub and HF give us the body already; fetching the page again is wasted."""
    item = {
        "url": "https://e.com/a",
        "title": "Release v2",
        "published_at": timezone.now(),
        "raw_text": "x" * 900,
        "meta": {},
    }
    fields = extract.normalize(item, source)
    assert fields["meta"]["extraction_method"] == "source"
    assert fields["title"] == "Release v2"


def test_missing_date_falls_back_to_now_and_is_flagged(source):
    item = {
        "url": "https://e.com/a",
        "title": "T",
        "published_at": None,
        "raw_text": "y" * 900,
        "meta": {},
    }
    fields = extract.normalize(item, source)
    assert fields["published_at"] is not None
    assert fields["meta"]["date_missing"] is True


def test_untitled_item_is_rejected(source):
    item = {
        "url": "https://e.com/a",
        "title": "",
        "published_at": timezone.now(),
        "raw_text": "z" * 900,
        "meta": {},
    }
    with pytest.raises(extract.ExtractionFailed, match="no title"):
        extract.normalize(item, source)


# --------------------------------------------------------------- prefilter


def make_item(url, days_old=0, text="q" * 900):
    return {
        "url": url,
        "title": "T",
        "raw_text": text,
        "meta": {},
        "published_at": timezone.now() - timedelta(days=days_old),
    }


def test_prefilter_drops_items_older_than_the_window(source):
    items = [make_item("https://e.com/new", 1), make_item("https://e.com/old", 30)]
    todo, stale, already = tasks._prefilter(source, items)
    assert stale == 1
    assert [i["url"] for i in todo] == ["https://e.com/new"]


def test_prefilter_drops_urls_already_stored(source):
    Article.objects.create(
        source=source,
        canonical_url="https://e.com/known",
        content_hash="a" * 64,
        title="known",
        published_at=timezone.now(),
        extracted_text="k" * 900,
    )
    items = [make_item("https://e.com/known"), make_item("https://e.com/fresh")]
    todo, stale, already = tasks._prefilter(source, items)
    assert already == 1
    assert [i["url"] for i in todo] == ["https://e.com/fresh"]


def test_prefilter_collapses_duplicates_inside_one_batch(source):
    items = [make_item("https://e.com/p"), make_item("https://e.com/p?utm_source=x")]
    todo, _, _ = tasks._prefilter(source, items)
    assert len(todo) == 1


def test_undated_items_survive_the_window_check(source):
    """html listings have no date until the page is fetched, so they must not be
    dropped as stale before extraction."""
    item = make_item("https://e.com/a")
    item["published_at"] = None
    todo, stale, _ = tasks._prefilter(source, [item])
    assert stale == 0 and len(todo) == 1
