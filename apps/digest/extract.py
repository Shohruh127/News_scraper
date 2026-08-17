"""Turn heterogeneous fetched items into rows Article can accept.

Deduplication is enforced by unique constraints on Article, not here. This module only
produces the two keys those constraints use: canonical_url and content_hash.
"""

import hashlib
import logging
import re
from urllib.parse import urlparse, urlunparse

import trafilatura
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

#: Stripped before hashing a URL, so the same article shared with different campaign
#: tags collapses to one row.
TRACKING_PARAM = re.compile(r"^(utm_[a-z_]*|fbclid|gclid|mc_cid|mc_eid|ref|source)$", re.I)

#: Text that means the page did not actually load for us.
BLOCKED_MARKERS = (
    "enable javascript",
    "access denied",
    "captcha",
    "are you a robot",
    "please verify you are human",
    "403 forbidden",
    "cloudflare",
)


class ExtractionFailed(Exception):
    """The page fetched but yielded nothing usable."""


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    kept = [kv for kv in p.query.split("&") if kv and not TRACKING_PARAM.match(kv.split("=")[0])]
    return urlunparse(
        (
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            "",
            "&".join(kept),
            "",
        )
    )


def content_hash(text: str) -> str:
    """Hash normalised text, so whitespace churn does not create a false new article."""
    normalised = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def looks_blocked(text: str) -> bool:
    low = text[:2000].lower()
    return any(m in low for m in BLOCKED_MARKERS)


def fetch_text(url: str) -> tuple[str, str]:
    """Return (text, method). Raises ExtractionFailed when the page is unusable."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ExtractionFailed(f"could not download {url}")

    text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
    if not text:
        raise ExtractionFailed(f"trafilatura extracted nothing from {url}")
    if looks_blocked(text):
        raise ExtractionFailed(f"page looks blocked (bot wall or paywall): {url}")
    if len(text) < settings.ARTICLE_MIN_CHARS:
        raise ExtractionFailed(f"only {len(text)} chars, below {settings.ARTICLE_MIN_CHARS}: {url}")
    return text, "trafilatura"


def page_title(url: str) -> str | None:
    """Real title for html-listing items, whose slug placeholder is not publishable."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    meta = trafilatura.extract_metadata(downloaded)
    return getattr(meta, "title", None) if meta else None


def normalize(item: dict, source) -> dict:
    """Build the kwargs for Article.objects.create.

    Raises ExtractionFailed if the item cannot become a usable article.
    """
    url = item["url"]
    text = (item.get("raw_text") or "").strip()
    method = "source"

    if len(text) < settings.ARTICLE_MIN_CHARS:
        text, method = fetch_text(url)

    title = (item.get("title") or "").strip()
    if item.get("meta", {}).get("needs_title"):
        title = page_title(url) or title
    if not title:
        raise ExtractionFailed(f"no title for {url}")

    published = item.get("published_at")
    meta = dict(item.get("meta") or {})
    meta["extraction_method"] = method
    if published is None:
        # Undated items are kept but flagged, so the 7-day rule filter in llm.py can
        # treat them differently from genuinely fresh ones.
        published = timezone.now()
        meta["date_missing"] = True

    return {
        "source": source,
        "canonical_url": canonical_url(url),
        "content_hash": content_hash(text),
        "title": title[:500],
        "published_at": published,
        "extracted_text": text,
        "meta": meta,
    }
