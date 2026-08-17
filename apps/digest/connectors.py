"""Four fetchers, one dict. No base class — there is no second implementation to share.

Each fetcher returns a list of dicts:
    {"url": str, "title": str, "published_at": datetime|None, "raw_text": str, "meta": dict}

`raw_text` may be empty; extract.py fills it from the page when the source does not
provide text of its own.
"""

import logging
import re
from datetime import UTC, datetime

import feedparser
import httpx
from dateutil import parser as dateparser
from django.conf import settings
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


class StructureChanged(Exception):
    """A source returned a well-formed response with too few items.

    Raised instead of returning an empty list, because a silently empty source is the
    failure mode that goes unnoticed for months.
    """


class RetryableHTTPError(Exception):
    """A 5xx or 429. Worth another attempt; a 404 or 403 is not."""


#: Retry only what can succeed on a second attempt.
RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, RetryableHTTPError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(RETRYABLE),
    reraise=True,
)
def _get(client: httpx.Client, url: str) -> httpx.Response:
    r = client.get(url)
    if r.status_code >= 500 or r.status_code == 429:
        raise RetryableHTTPError(f"{r.status_code} from {url}")
    # Anything else in the 4xx range is permanent — fail now rather than
    # spending three attempts and ~20s of backoff on a dead URL.
    r.raise_for_status()
    return r


def http_client() -> httpx.Client:
    return httpx.Client(
        timeout=45,
        follow_redirects=True,
        headers={"User-Agent": settings.USER_AGENT},
    )


def parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        d = dateparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


# --------------------------------------------------------------------- fetchers


def fetch_rss(source, client) -> list[dict]:
    feed = feedparser.parse(_get(client, source.url).text)
    return [
        {
            "url": e.link,
            "title": e.get("title", ""),
            "published_at": parse_date(e.get("published") or e.get("updated")),
            "raw_text": "",
            "meta": {},
        }
        for e in feed.entries
        if e.get("link")
    ]


def fetch_github(source, client) -> list[dict]:
    repo = source.config.get("repo") or source.url.rstrip("/").split("github.com/")[-1]
    url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    out = []
    for rel in _get(client, url).json():
        if rel.get("draft"):
            continue
        out.append(
            {
                "url": rel["html_url"],
                "title": f"{repo} {rel.get('name') or rel.get('tag_name', '')}".strip(),
                "published_at": parse_date(rel.get("published_at")),
                "raw_text": f"{rel.get('name') or ''}\n\n{rel.get('body') or ''}".strip(),
                "meta": {
                    "repo": repo,
                    "tag": rel.get("tag_name"),
                    "prerelease": rel.get("prerelease", False),
                },
            }
        )
    return out


def fetch_hn(source, client) -> list[dict]:
    min_points = source.config.get("min_points", 50)
    url = (
        "https://hn.algolia.com/api/v1/search_by_date?tags=story"
        f"&numericFilters=points%3E{min_points}&hitsPerPage=100"
    )
    out = []
    for h in _get(client, url).json().get("hits", []):
        if not h.get("url"):
            continue
        out.append(
            {
                "url": h["url"],
                "title": h.get("title", ""),
                "published_at": parse_date(h.get("created_at")),
                "raw_text": "",
                "meta": {"points": h.get("points"), "hn_id": h.get("objectID")},
            }
        )
    return out


def fetch_hf(source, client) -> list[dict]:
    """Hugging Face daily papers. Abstracts arrive with the listing, so no page fetch.

    Most of these classify as `paper_only` and are then excluded from publication by
    CONTENT_SCHEMA.md §3. That is intended: this source supplies candidates, and the
    maturity filter keeps only the papers that shipped artifacts.
    """
    limit = source.config.get("limit", 100)
    url = f"https://huggingface.co/api/daily_papers?limit={limit}"
    out = []
    for entry in _get(client, url).json():
        paper = entry.get("paper", entry)
        paper_id = paper.get("id", "")
        title = paper.get("title", "").strip()
        summary = paper.get("summary", "").strip()
        if not paper_id or not title:
            continue
        out.append(
            {
                "url": f"https://huggingface.co/papers/{paper_id}",
                "title": title,
                "published_at": parse_date(entry.get("publishedAt") or paper.get("publishedAt")),
                "raw_text": f"{title}\n\n{summary}",
                "meta": {"arxiv_id": paper_id, "upvotes": paper.get("upvotes")},
            }
        )
    return out


def fetch_html(source, client) -> list[dict]:
    """Listing pages with no feed. Anthropic is the reason this exists.

    `source.config` drives it entirely, so adding a site is an admin edit:
        {"link_pattern": "/news/", "title_selector": "h1", "min_items": 5}
    """
    cfg = source.config
    pattern = cfg.get("link_pattern", "/")
    min_items = cfg.get("min_items", 1)

    html = _get(client, source.url).text
    hrefs = sorted(set(re.findall(rf'href="({re.escape(pattern)}[^"#?]+)"', html)))

    if len(hrefs) < min_items:
        raise StructureChanged(
            f"{source.name}: found {len(hrefs)} links matching {pattern!r}, "
            f"expected at least {min_items}. The page layout probably changed."
        )

    base = source.url.split("/", 3)
    origin = f"{base[0]}//{base[2]}"
    return [
        {
            "url": h if h.startswith("http") else origin + h,
            # A real title needs the article page; extract.py supplies it. The slug is
            # only a placeholder so the row is identifiable before extraction.
            "title": h.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(),
            "published_at": None,
            "raw_text": "",
            "meta": {"from_listing": True, "needs_title": True},
        }
        for h in hrefs
    ]


FETCHERS = {
    "rss": fetch_rss,
    "github": fetch_github,
    "hn": fetch_hn,
    "hf": fetch_hf,
    "html": fetch_html,
}


def fetch(source, client=None) -> list[dict]:
    """Dispatch to the right fetcher. Raises; the caller records the failure."""
    fetcher = FETCHERS.get(source.connector)
    if fetcher is None:
        raise ValueError(f"unknown connector {source.connector!r} on source {source.name}")
    own_client = client is None
    client = client or http_client()
    try:
        items = fetcher(source, client)
    finally:
        if own_client:
            client.close()
    log.info("fetched %s items from %s", len(items), source.name)
    return items
