"""Connector tests. All network is mocked — no test touches the internet."""

import httpx
import pytest
import respx
from django.utils import timezone

from apps.digest import connectors
from apps.digest.models import Source

pytestmark = pytest.mark.django_db


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>First post</title><link>https://example.com/a</link>
      <pubDate>Wed, 13 Aug 2026 11:00:00 GMT</pubDate></item>
<item><title>Second post</title><link>https://example.com/b</link>
      <pubDate>Thu, 14 Aug 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def src(**kw):
    kw.setdefault("name", "t")
    kw.setdefault("connector", "rss")
    kw.setdefault("url", "https://example.com/feed.xml")
    kw.setdefault("config", {})
    return Source.objects.create(**kw)


@respx.mock
def test_rss_returns_entries_with_dates():
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=RSS))
    items = connectors.fetch(src())
    assert [i["title"] for i in items] == ["First post", "Second post"]
    assert items[0]["published_at"].year == 2026
    assert items[0]["published_at"].tzinfo is not None


@respx.mock
def test_github_skips_drafts_and_builds_text():
    s = src(connector="github", url="https://github.com/o/r", config={"repo": "o/r"})
    respx.get("https://api.github.com/repos/o/r/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "html_url": "https://github.com/o/r/releases/v2",
                    "name": "v2",
                    "tag_name": "v2",
                    "body": "changes here",
                    "published_at": "2026-08-13T10:00:00Z",
                    "draft": False,
                    "prerelease": False,
                },
                {
                    "html_url": "https://github.com/o/r/releases/v3",
                    "name": "v3",
                    "tag_name": "v3",
                    "body": "wip",
                    "published_at": "2026-08-14T10:00:00Z",
                    "draft": True,
                },
            ],
        )
    )
    items = connectors.fetch(s)
    assert len(items) == 1
    assert "changes here" in items[0]["raw_text"]


@respx.mock
def test_hn_drops_stories_without_a_url():
    s = src(connector="hn", url="https://hn.algolia.com/", config={"min_points": 50})
    respx.get(url__startswith="https://hn.algolia.com/api/").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "url": "https://blog.example/x",
                        "title": "Has url",
                        "points": 120,
                        "created_at": "2026-08-14T08:00:00Z",
                        "objectID": "1",
                    },
                    {
                        "url": None,
                        "title": "Ask HN, no url",
                        "points": 90,
                        "created_at": "2026-08-14T08:00:00Z",
                        "objectID": "2",
                    },
                ]
            },
        )
    )
    items = connectors.fetch(s)
    assert [i["title"] for i in items] == ["Has url"]


@respx.mock
def test_hf_uses_the_abstract_as_text():
    s = src(connector="hf", url="https://huggingface.co/papers", config={"limit": 10})
    respx.get(url__startswith="https://huggingface.co/api/daily_papers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "publishedAt": "2026-08-14T00:00:00Z",
                    "paper": {
                        "id": "2608.00001",
                        "title": "A Paper",
                        "summary": "abstract text",
                        "upvotes": 12,
                    },
                },
            ],
        )
    )
    items = connectors.fetch(s)
    assert items[0]["url"] == "https://huggingface.co/papers/2608.00001"
    assert "abstract text" in items[0]["raw_text"]


@respx.mock
def test_html_raises_when_the_layout_changes():
    """A source that quietly returns nothing is the failure that goes unnoticed.
    Too few matches must raise, not return an empty list."""
    s = src(
        connector="html",
        url="https://example.com/news",
        config={"link_pattern": "/news/", "min_items": 5},
    )
    respx.get("https://example.com/news").mock(
        return_value=httpx.Response(200, text='<a href="/news/only-one">x</a>')
    )
    with pytest.raises(connectors.StructureChanged, match="found 1 links"):
        connectors.fetch(s)


@respx.mock
def test_html_builds_absolute_urls():
    s = src(
        connector="html",
        url="https://example.com/news",
        config={"link_pattern": "/news/", "min_items": 1},
    )
    respx.get("https://example.com/news").mock(
        return_value=httpx.Response(
            200, text='<a href="/news/alpha">a</a><a href="/news/beta">b</a>'
        )
    )
    items = connectors.fetch(s)
    assert {i["url"] for i in items} == {
        "https://example.com/news/alpha",
        "https://example.com/news/beta",
    }
    assert all(i["meta"]["needs_title"] for i in items)


@respx.mock
def test_404_is_not_retried():
    """Three attempts with backoff on a permanently dead URL is wasted time."""
    route = respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        connectors.fetch(src())
    assert route.call_count == 1


@respx.mock
def test_500_is_retried():
    route = respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(503))
    with pytest.raises(connectors.RetryableHTTPError):
        connectors.fetch(src())
    assert route.call_count == 3


def test_unknown_connector_is_rejected():
    s = src()
    s.connector = "carrier-pigeon"
    with pytest.raises(ValueError, match="unknown connector"):
        connectors.fetch(s)


def test_parse_date_always_returns_aware_datetimes():
    assert connectors.parse_date(None) is None
    assert connectors.parse_date("not a date") is None
    d = connectors.parse_date("2026-08-14T10:00:00")
    assert d is not None and d.tzinfo is not None
    assert timezone.is_aware(d)


@pytest.mark.django_db
def test_source_yield_counts_the_funnel():
    """Each stage is counted separately, because they answer different questions."""
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Article, Digest, DigestItem, Source

    src = Source.objects.create(
        name="yield_src", connector=Source.Connector.RSS, url="https://example.com/rss"
    )
    made = []
    for i, status in enumerate(
        [
            Article.Status.CLASSIFIED,
            Article.Status.CLASSIFIED,
            Article.Status.SKIPPED,
            Article.Status.TRIAGED,
        ]
    ):
        made.append(
            Article.objects.create(
                source=src,
                canonical_url=f"https://example.com/y{i}",
                content_hash=f"y{i}",
                title=f"Article {i}",
                extracted_text="Body " * 60,
                status=status,
            )
        )
    digest = Digest.objects.create(digest_date=timezone.localdate())
    DigestItem.objects.create(
        digest=digest, article=made[0], position=1, score=0.9, channel_message_id=42
    )
    DigestItem.objects.create(digest=digest, article=made[1], position=2, score=0.8)

    out = StringIO()
    call_command("source_yield", stdout=out)
    line = next(li for li in out.getvalue().splitlines() if "yield_src" in li)

    # Positional, not "is this digit somewhere in the line". The previous assertions passed
    # while a whole column was missing, because the digits they looked for appeared anyway.
    cols = line.split()
    assert cols[0] == "yield_src"
    assert cols[1] == "4", "ARTS counts every article the source produced"
    assert cols[2] == "1", "TRIAGED counts articles still moving, not rejected ones"
    assert cols[3] == "2", "CLASSIF counts articles that finished classification"
    assert cols[4] == "2", "DIGEST counts articles ranking selected"
    assert cols[5] == "1", "PUBLISHED counts articles that reached a reader"


@pytest.mark.django_db
def test_source_yield_lists_a_source_that_produced_nothing():
    """A zero is the measurement. A source missing from the report cannot be judged."""
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Source

    Source.objects.create(
        name="silent_src", connector=Source.Connector.RSS, url="https://example.com/quiet"
    )

    out = StringIO()
    call_command("source_yield", stdout=out)

    assert "silent_src" in out.getvalue()


@pytest.mark.django_db
def test_source_yield_days_window_excludes_older_articles():
    """`--days` answers "what has this source done lately", not "ever"."""
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Article, Source

    src = Source.objects.create(
        name="window_src", connector=Source.Connector.RSS, url="https://example.com/w"
    )
    old = Article.objects.create(
        source=src,
        canonical_url="https://example.com/old",
        content_hash="old",
        title="Old",
        extracted_text="Body " * 60,
        status=Article.Status.CLASSIFIED,
    )
    Article.objects.filter(pk=old.pk).update(fetched_at=timezone.now() - timedelta(days=30))

    out = StringIO()
    call_command("source_yield", "--days", "7", stdout=out)
    line = next(li for li in out.getvalue().splitlines() if "window_src" in li)

    assert line.split()[1] == "0"


def test_seed_covers_every_source_added_since_the_file_was_written():
    """A source added by a one-off script is invisible to a fresh environment.

    Measured 2026-08-18: the seed held 12 sources and the database held 23. Eleven had been
    inserted directly and never reached the file.
    """
    from apps.digest.management.commands.seed_sources import SOURCES

    names = {entry["name"] for entry in SOURCES}
    added_2026_08_18 = {
        "nextgov",
        "fedscoop",
        "statescoop",
        "ec_digital",
        "gds_uk",
        "gh_sherpa_onnx",
        "gh_pyannote",
        "gh_whisperx",
        "techcrunch_ai",
        "crunchbase_news",
        "sifted",
    }

    assert added_2026_08_18 <= names, f"missing from the seed: {added_2026_08_18 - names}"
