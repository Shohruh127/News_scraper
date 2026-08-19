"""Does a paper's promised code actually exist?

maturity_ceiling caps a paper at paper_only because nothing has checked its artifact. This module
performs that check. Only GitHub is verifiable today; other recognised hosts are recorded but not
admitted because no verifier exists for them yet.
"""

import logging
import re

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

# The repository name ends where the URL does: at a slash, a query, a bracket, or whitespace.
# The previous pattern required the name to be the last thing in the URL, so every link with a
# /tree/ or /blob/ path was invisible, and a non-greedy name truncated next.js to next.
_REPO = re.compile(
    r"(?:https?://)?(?:www\.)?(github\.com|gitlab\.com)/"
    r"([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)",
    re.IGNORECASE,
)

#: First path segments that are site navigation, not an account.
_NOT_OWNERS = {
    "about", "apps", "collections", "enterprise", "explore", "features", "login",
    "marketplace", "notifications", "orgs", "pricing", "search", "settings", "signup",
    "sponsors", "topics", "trending",
}


def _clean_repo(raw: str) -> str:
    """Strip a sentence-final period and a .git suffix from a captured repository name."""
    repo = raw.rstrip(".")
    if repo.lower().endswith(".git"):
        repo = repo[:-4].rstrip(".")
    return repo


def _squash(value: str) -> str:
    """Lowercase alphanumerics only, so PACE-Bench and pacebench compare equal."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def find_repo_url(text: str, title: str = "") -> str:
    """Return the repository the article is about, normalised, or an empty string.

    When the title names one of the candidates, that one wins. Papers cite baseline
    repositories before releasing their own, so the first link in the text is often
    somebody else's code. With no title, or no match, the first link is used.
    """
    candidates: list[str] = []
    for match in _REPO.finditer(text or ""):
        host, owner = match.group(1).lower(), match.group(2)
        repo = _clean_repo(match.group(3))
        if owner.lower() in _NOT_OWNERS or not repo:
            continue
        url = f"https://{host}/{owner}/{repo}"
        if url not in candidates:
            candidates.append(url)

    if not candidates:
        return ""

    squashed_title = _squash(title)
    if squashed_title:
        for url in candidates:
            name = _squash(url.rsplit("/", 1)[-1])
            if len(name) >= 4 and name in squashed_title:
                return url

    return candidates[0]




def repo_is_real(url: str, client: httpx.Client | None = None) -> bool | None:
    """Whether a GitHub repository exists and has content.

    True  — it exists and is not empty.
    False — GitHub answered, and it is not a usable repository (404, or empty), or the host
            is one we have no verifier for.
    None  — GitHub did not answer: rate limit, 5xx, timeout, unparseable body. A caller must
            store True or False and must never store None. The unauthenticated API allows
            60 requests per hour per IP, so None is an ordinary outcome, not an alarm.
    """
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)", url or "")
    if not match:
        return False

    owner, repo = match.groups()
    close_client = False
    if client is None:
        client = httpx.Client(timeout=getattr(settings, "ARTIFACT_TIMEOUT", 15))
        close_client = True

    try:
        response = client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code == 404:
            log.info("Artifact %s not verified: repository does not exist", url)
            return False
        if response.status_code != 200:
            log.info("Artifact %s inconclusive: HTTP %s", url, response.status_code)
            return None
        size = response.json().get("size", 0)
    except Exception as exc:
        log.info("Artifact %s inconclusive: %s", url, exc)
        return None
    finally:
        if close_client:
            client.close()

    if not size:
        log.info("Artifact %s not verified: repository is empty", url)
        return False
    return True

