"""Canonical story/outlet identity derived from article URLs."""

from urllib.parse import urlparse

from django.conf import settings


def subject_key(url: str) -> str:
    """Return the outlet/subject key used by diversification and cross-outlet checks."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    if host in getattr(settings, "SUBJECT_CODE_HOSTS", ()):
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments:
            return f"{host}/{segments[0]}"
    return host
