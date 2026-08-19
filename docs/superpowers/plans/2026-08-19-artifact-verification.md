# Artifact Verification Implementation Plan

> **Execution:** Work task-by-task and use the checkboxes as durable state. The implementation
> recipe below is historical for an implementation already present in the worktree; do not replay
> expected-failure steps unless the code has first been removed intentionally.

**Current status (2026-08-19):** Code, migration, admin fields, stale-comment cleanup, and tests
are present in the worktree. A read-only backfill re-examined 207 stored paper-domain articles and
admitted 6 with verified artifacts. The full worktree now passes the full suite, ruff, Django checks, and
migration drift checks. These changes are not committed or deployed.

**Goal:** Let a paper into the digest when it ships code that actually exists, instead of excluding every paper because most of them do not.

**Architecture:** Where the prefilter is about to drop a paper, it first looks for a repository link in the article text and asks GitHub whether that repository is real and non-empty. A paper that passes stops being `paper_only` and may claim `reproducible_open_source`. The answer is stored on the article, so the ceiling reads it later without a second request.

**Tech Stack:** Django, httpx, respx, pytest, the GitHub REST API unauthenticated.

**Spec:** none — bounded change agreed in chat on 2026-08-19. `maturity_ceiling`'s own docstring anticipates it: "highest maturity this item may claim **without checking an artifact**".

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- Record the suite count before you start and keep it green
- Tests run offline. The GitHub API is mocked with `respx`; no test may reach the network
- `SKIP_PAPER_DOMAINS` keeps its meaning and stays `True`. This plan adds an exception to it, not a replacement
- The maturity enum is unchanged. A verified paper claims `reproducible_open_source`, an existing value
- Unauthenticated GitHub allows 60 requests an hour. Measured need is far below that — see below — so no token is introduced
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

`SKIP_PAPER_DOMAINS` was added on 2026-08-18 after measuring that papers cannot reach a digest:
216 of 411 stored articles came from paper domains, consumed 169 triage and classification calls,
and produced zero digest items. Skipping them before the LLM was correct.

But the rule is blunt. Some papers ship working code, and those are exactly the ones this digest
exists for. `maturity_ceiling` caps a paper at `paper_only` because nothing has checked whether
its promised artifact is real — and its docstring says so.

The size of the opportunity was measured on 2026-08-19, and it is smaller than it first looked:

```
articles from paper domains                        227
with a repository link anywhere in the text         19   (8%)
```

So this admits at most 19 of 227, and fewer once the repositories that do not resolve are
removed. That is the honest expectation. It is still worth building, because those 19 are
self-selected: an author who publishes a working repository is making the claim this digest
tries to reward.

The request budget follows from the same number. Only a paper with a link is checked, only once,
and the result is stored. Nineteen checks across the measured stored corpus, and a handful a day
after that, against an unauthenticated limit of 60 an hour.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/artifacts.py` | find a repository link, ask whether it is real | create |
| `apps/digest/models.py` | remember the answer | add two fields |
| `apps/digest/migrations/0005_article_artifact.py` | the migration | create |
| `apps/digest/llm.py` | the prefilter exception and the raised ceiling | modify |
| `config/settings.py` | on/off, timeout | modify |
| `apps/digest/admin.py` | show the verdict where sources are reviewed | modify |
| `tests/test_maturity_ceiling.py` | prefilter and ceiling behaviour | modify |

### Context an engineer new to this repo needs

Two functions decide a paper's fate, and they run at different moments:

- `check_rule_prefilter(article)` in `llm.py` runs **before** any LLM call. Since 2026-08-18 it
  returns `False` for a paper domain, so a paper never reaches triage.
- `maturity_ceiling(article)` in `llm.py` runs **during** classification and caps what the model
  may claim. `apply_maturity_ceiling` then rewrites an over-claim in place.

Because they run at different times, verifying in one and reading in the other needs the answer
stored. That is what the two new fields are for — not caching for speed, but carrying an answer
across two stages.

`EXCLUDED_MATURITIES` in `models.py` holds `announcement_only` and `paper_only`. Ranking drops
both. Raising a verified paper to `reproducible_open_source` is what lets it compete; nothing in
ranking changes.

---

## Task 1: Find and verify an artifact

**Files:**
- Create: `apps/digest/artifacts.py`
- Modify: `config/settings.py`
- Test: `tests/test_maturity_ceiling.py`

**Interfaces:**
- Consumes: nothing from other tasks
- Produces: `find_repo_url(text: str) -> str` returning `""` when there is none, and
  `repo_is_real(url: str, client: httpx.Client | None = None) -> bool`. Task 2 calls both

- [ ] **Step 1: Add the settings**

Append to `config/settings.py`:

```python

# --- Artifact verification ---------------------------------------------------
# SKIP_PAPER_DOMAINS drops every paper before triage, which is right on average and wrong for
# the papers that ship working code. This checks the exception.
#
# Measured 2026-08-19: of 227 stored articles from paper domains, 19 carry a repository link
# anywhere in their text. So this admits at most 19, and fewer once the links that do not
# resolve are removed. Nineteen checks across the measured corpus, a handful a day after that,
# against GitHub's unauthenticated limit of 60 an hour — no token is needed.
ARTIFACT_VERIFICATION_ENABLED = env.bool("ARTIFACT_VERIFICATION_ENABLED", default=True)
ARTIFACT_TIMEOUT = env.int("ARTIFACT_TIMEOUT", default=15)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_maturity_ceiling.py`:

```python
@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "We release our code at https://github.com/facebookresearch/segment-anything .",
            "https://github.com/facebookresearch/segment-anything",
        ),
        (
            "Code: github.com/openai/whisper and weights on the hub.",
            "https://github.com/openai/whisper",
        ),
        (
            "See https://gitlab.com/team/project for the implementation.",
            "https://gitlab.com/team/project",
        ),
        # A repository page, not a feature page. Two path segments are required.
        ("Hosted on https://github.com/features/actions today.", ""),
        ("Read more at https://github.com/about .", ""),
        # A paper with no artifact at all.
        ("We evaluate on three benchmarks and report gains.", ""),
        # Trailing punctuation and a .git suffix must not end up in the URL.
        (
            "Available at https://github.com/psf/requests.git, released today.",
            "https://github.com/psf/requests",
        ),
        (
            "Implementation: https://github.com/psf/requests.",
            "https://github.com/psf/requests",
        ),
    ],
)
def test_find_repo_url(text, expected):
    from apps.digest.artifacts import find_repo_url

    assert find_repo_url(text) == expected


def test_find_repo_url_takes_the_first_of_several():
    """A paper citing several repositories is judged on the one it leads with."""
    from apps.digest.artifacts import find_repo_url

    text = "Baselines at github.com/other/baseline; our code at github.com/authors/ours."
    assert find_repo_url(text) == "https://github.com/other/baseline"


@respx.mock
def test_repo_is_real_only_when_it_has_content():
    """200 is not enough. An empty repository is a promise, not an artifact."""
    from apps.digest.artifacts import repo_is_real

    respx.get("https://api.github.com/repos/authors/full").mock(
        return_value=httpx.Response(200, json={"size": 1240})
    )
    respx.get("https://api.github.com/repos/authors/empty").mock(
        return_value=httpx.Response(200, json={"size": 0})
    )
    respx.get("https://api.github.com/repos/authors/missing").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    assert repo_is_real("https://github.com/authors/full") is True
    assert repo_is_real("https://github.com/authors/empty") is False
    assert repo_is_real("https://github.com/authors/missing") is False


@respx.mock
def test_repo_is_real_is_false_when_github_is_unreachable():
    """An outage must not admit a paper. Unverified means not verified."""
    from apps.digest.artifacts import repo_is_real

    respx.get("https://api.github.com/repos/authors/timeout").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )

    assert repo_is_real("https://github.com/authors/timeout") is False


def test_repo_is_real_rejects_a_host_it_cannot_check():
    """Only GitHub is checkable today. A GitLab link is not treated as verified."""
    from apps.digest.artifacts import repo_is_real

    assert repo_is_real("https://gitlab.com/team/project") is False
```

`tests/test_maturity_ceiling.py` imports `pytest` already. Add `httpx` and `respx` to its imports
if they are not there:

```python
import httpx
import respx
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k "repo_url or repo_is_real"
```

Expected: FAIL, `ModuleNotFoundError: No module named 'apps.digest.artifacts'`

- [ ] **Step 4: Write the module**

Create `apps/digest/artifacts.py`:

```python
"""Does a paper's promised code actually exist?

`maturity_ceiling` caps a paper at `paper_only` because nothing has checked its artifact — its
docstring says exactly that. This is the check.

Only GitHub is verifiable today. GitLab and the HuggingFace hub both have APIs, but of the 19
repository links found across 227 stored papers on 2026-08-19, GitHub carried all of them.
A second host earns its code when a second host appears in the data.
"""

import logging
import re

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

#: Two path segments, because `github.com/features/actions` and `github.com/about` are pages,
#: not repositories. The trailing group stops before punctuation and drops a `.git` suffix.
_REPO = re.compile(
    r"(?:https?://)?(?:www\.)?(github\.com|gitlab\.com)/"
    r"([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*?)(?:\.git)?(?=[\s,;.)\]]|$)",
    re.IGNORECASE,
)

#: Paths that look like `owner/repo` but are GitHub's own pages.
_NOT_OWNERS = {"features", "about", "pricing", "enterprise", "orgs", "sponsors", "settings"}


def find_repo_url(text: str) -> str:
    """The first repository link in the text, normalised. Empty string when there is none.

    The first rather than the best: a paper citing several repositories is judged on the one it
    leads with, and choosing between them would need to understand the paper.
    """
    for match in _REPO.finditer(text or ""):
        host, owner, repo = match.group(1).lower(), match.group(2), match.group(3)
        if owner.lower() in _NOT_OWNERS:
            continue
        return f"https://{host}/{owner}/{repo}"
    return ""


def repo_is_real(url: str, client: httpx.Client | None = None) -> bool:
    """True only when the repository exists and holds something.

    A 200 alone is not enough: an empty repository is a promise, and this project has already
    learned that a promised artifact and a real one are different claims. GitHub reports `size`
    in kilobytes, and a repository with no commits reports zero.

    Any failure returns False. Unverified is not verified, and an API outage must never admit a
    paper the digest would otherwise have excluded.
    """
    match = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)$", url or "")
    if not match:
        return False

    owner, repo = match.group(1), match.group(2)
    close_client = False
    if client is None:
        client = httpx.Client(timeout=getattr(settings, "ARTIFACT_TIMEOUT", 15))
        close_client = True

    try:
        r = client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code != 200:
            log.info("Artifact %s not verified: HTTP %s", url, r.status_code)
            return False
        size = r.json().get("size", 0)
        if not size:
            log.info("Artifact %s not verified: repository is empty", url)
            return False
        return True
    except Exception as exc:
        log.info("Artifact %s not verified: %s", url, exc)
        return False
    finally:
        if close_client:
            client.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k "repo_url or repo_is_real"
```

Expected: PASS

- [ ] **Step 6: Check the finder against the real corpus**

This is a read-only measurement — no network, no writes:

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Article
from apps.digest.llm import PAPER_DOMAINS
from apps.digest.artifacts import find_repo_url
papers = [a for a in Article.objects.all() if any(d in (a.canonical_url or '').lower() for d in PAPER_DOMAINS)]
found = [(a.title[:52], find_repo_url(a.extracted_text or '')) for a in papers]
hits = [f for f in found if f[1]]
print(f'papers: {len(papers)}   with a repo link: {len(hits)}')
for t, u in hits[:15]:
    print(f'  {t}\n      {u}')
"
```

Paste the output. The count should land near the 19 measured on 2026-08-19; a much larger number
means the regex is matching pages rather than repositories, and that is worth reporting before
Task 2 stores anything.

- [ ] **Step 7: Commit**

```bash
git add apps/digest/artifacts.py config/settings.py tests/test_maturity_ceiling.py
git commit -m "Find a paper's repository link and ask whether it is real"
```

---

## Task 2: Let a verified paper through

**Files:**
- Modify: `apps/digest/models.py` — two fields on `Article`
- Create: `apps/digest/migrations/0005_article_artifact.py` — generated, not hand-written
- Modify: `apps/digest/llm.py` — `check_rule_prefilter` and `maturity_ceiling`
- Modify: `apps/digest/admin.py` — `ArticleAdmin`
- Modify: `config/settings.py` — retire the stale pre-M2 `SKIP_PAPER_DOMAINS` comment
- Test: `tests/test_maturity_ceiling.py`

**Interfaces:**
- Consumes: `find_repo_url` and `repo_is_real` from Task 1
- Produces: `Article.artifact_url: str` and `Article.artifact_verified: bool | None`, where
  `None` means never checked. No new callable

- [ ] **Step 0: Retire the stale paper-filter comment debt**

Replace the old `Set to False when M2 artifact verification lands` comment above
`SKIP_PAPER_DOMAINS`. The setting remains `True`: it now means paper-domain articles are skipped
unless the verified-artifact exception admits them. Pin this meaning in the comment so a future
operator does not disable the general filter after this feature lands.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_maturity_ceiling.py`:

```python
@pytest.mark.django_db
@respx.mock
def test_a_paper_with_a_real_repo_survives_the_prefilter():
    """The exception this plan exists for."""
    respx.get("https://api.github.com/repos/authors/code").mock(
        return_value=httpx.Response(200, json={"size": 900})
    )
    source = Source.objects.create(
        name="hn_artifact", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.01111",
        title="A Method With Code",
        extracted_text="We release our implementation at github.com/authors/code . " * 20,
    )

    passed, reason = check_rule_prefilter(paper)

    assert passed is True, reason
    paper.refresh_from_db()
    assert paper.artifact_url == "https://github.com/authors/code"
    assert paper.artifact_verified is True


@pytest.mark.django_db
@respx.mock
def test_a_paper_whose_repo_is_empty_is_still_skipped():
    """An empty repository is a promise, and the prefilter treats it as no artifact."""
    respx.get("https://api.github.com/repos/authors/empty").mock(
        return_value=httpx.Response(200, json={"size": 0})
    )
    source = Source.objects.create(
        name="hn_empty", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.02222",
        title="A Method With A Promise",
        extracted_text="Code will be available at github.com/authors/empty . " * 20,
    )

    passed, reason = check_rule_prefilter(paper)

    assert passed is False
    assert "Paper domain" in reason
    paper.refresh_from_db()
    assert paper.artifact_verified is False


@pytest.mark.django_db
def test_a_paper_with_no_link_is_skipped_without_a_request(monkeypatch):
    """91% of papers have no link. None of them may cost a request."""
    from apps.digest import artifacts

    monkeypatch.setattr(
        artifacts,
        "repo_is_real",
        lambda url: pytest.fail(f"repo_is_real must not run without a link: {url}"),
    )
    source = Source.objects.create(
        name="hn_nolink", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.03333",
        title="A Method Without Code",
        extracted_text="We evaluate on three benchmarks and report gains. " * 20,
    )

    passed, _ = check_rule_prefilter(paper)

    assert passed is False
    paper.refresh_from_db()
    assert paper.artifact_url == ""
    assert paper.artifact_verified is None, "never checked is not the same as checked and failed"


@pytest.mark.django_db
def test_a_verified_paper_may_claim_reproducible_open_source():
    """The ceiling reads the stored verdict rather than asking GitHub a second time."""
    source = Source.objects.create(
        name="hn_ceiling", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.04444",
        title="A Verified Method",
        extracted_text="x" * 2000,
        artifact_url="https://github.com/authors/code",
        artifact_verified=True,
    )

    assert maturity_ceiling(paper) == Maturity.REPRODUCIBLE_OPEN_SOURCE


@pytest.mark.django_db
def test_verified_nonpaper_keeps_no_ceiling():
    source = Source.objects.create(
        name="hn_verified_product", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://github.com/authors/code",
        title="A Verified Product",
        extracted_text="x" * 2000,
        artifact_url="https://github.com/authors/code",
        artifact_verified=True,
    )

    assert maturity_ceiling(article) is None


@pytest.mark.django_db
def test_an_unverified_paper_keeps_its_paper_only_ceiling():
    source = Source.objects.create(
        name="hn_unverified", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.05555",
        title="An Unverified Method",
        extracted_text="x" * 2000,
    )

    assert maturity_ceiling(paper) == Maturity.PAPER_ONLY
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k "artifact or repo or verified or paper_with"
```

Expected: FAIL — `Article` has no field `artifact_url`.

- [ ] **Step 3: Add the two fields**

In `apps/digest/models.py`, inside `class Article`, add after `meta` (or after the last field
before `class Meta`):

```python
    #: The repository this article promises, if it names one. Written once by the prefilter.
    artifact_url = models.URLField(max_length=1000, blank=True, default="")
    #: True when that repository exists and holds something; False when it does not;
    #: NULL when nothing has looked. The three states are different and the ceiling reads all
    #: three — "never checked" must not be mistaken for "checked and failed".
    artifact_verified = models.BooleanField(null=True, blank=True, default=None)
```

- [ ] **Step 4: Generate the migration**

```bash
uv run python manage.py makemigrations digest --name article_artifact
```

Expected: `Migrations for 'digest': apps/digest/migrations/0005_article_artifact.py`

Do not hand-write it. Then apply it:

```bash
uv run python manage.py migrate
```

- [ ] **Step 5: Verify in the prefilter**

In `apps/digest/llm.py`, `check_rule_prefilter` currently ends:

```python
    skip_papers = getattr(settings, "SKIP_PAPER_DOMAINS", True)
    if skip_papers and maturity_ceiling(article) == Maturity.PAPER_ONLY:
        return False, "Paper domain: excluded from ranking by maturity, so never triaged"

    return True, ""
```

Replace that block with:

```python
    skip_papers = getattr(settings, "SKIP_PAPER_DOMAINS", True)
    if skip_papers and maturity_ceiling(article) == Maturity.PAPER_ONLY:
        if _verify_artifact(article):
            return True, ""
        return False, "Paper domain: excluded from ranking by maturity, so never triaged"

    return True, ""
```

And add this helper directly above `check_rule_prefilter`:

```python
def _verify_artifact(article: Article) -> bool:
    """Look for a repository link and check it, once, storing all three possible answers.

    Called only where a paper is about to be dropped, so the 91% of papers with no link cost
    nothing: `find_repo_url` is pure string work and returns before any request.

    `artifact_verified` stays NULL when there was nothing to check. That is not the same as
    False, and the ceiling in `maturity_ceiling` depends on the difference.
    """
    if not getattr(settings, "ARTIFACT_VERIFICATION_ENABLED", True):
        return False
    if article.artifact_verified is not None:
        return article.artifact_verified

    url = artifacts.find_repo_url(article.extracted_text or "")
    if not url:
        return False

    verified = artifacts.repo_is_real(url)
    article.artifact_url = url
    article.artifact_verified = verified
    article.save(update_fields=["artifact_url", "artifact_verified"])
    log.info("Artifact for article %s: %s -> %s", article.id, url, verified)
    return verified
```

Add the import beside the other local imports at the top of `llm.py`:

```python
from . import artifacts
```

- [ ] **Step 6: Raise the ceiling for a verified paper**

In `apps/digest/llm.py`, `maturity_ceiling` currently begins its checks with:

```python
    url = (article.canonical_url or "").lower()
    if any(d in url for d in PAPER_DOMAINS):
        return Maturity.PAPER_ONLY
```

Insert the exception above them, right after the docstring:

```python
    # A paper whose promised repository exists and holds code is no longer just a paper.
    # This is the exception the docstring above anticipates: the cap applies "without checking
    # an artifact", and here one has been checked.
    url = (article.canonical_url or "").lower()
    is_paper = any(d in url for d in PAPER_DOMAINS) or (
        article.source and article.source.connector == "hf"
    )
    if article.artifact_verified and is_paper:
        return Maturity.REPRODUCIBLE_OPEN_SOURCE
    if is_paper:
        return Maturity.PAPER_ONLY

```

Note this returns a ceiling rather than `None`: a verified paper may claim
`reproducible_open_source` but not `live_product`, because a repository is not a running service.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/test_maturity_ceiling.py -q
```

Expected: PASS

- [ ] **Step 8: Show the verdict in the admin**

In `apps/digest/admin.py`, `ArticleAdmin` currently reads:

```python
    list_display = ("title", "source", "status", "published_at", "fetched_at")
    list_filter = ("status", "source", "language")
```

Change those two lines to:

```python
    list_display = ("title", "source", "status", "artifact_verified", "published_at", "fetched_at")
    list_filter = ("status", "artifact_verified", "source", "language")
```

and add both "artifact_url" and "artifact_verified" to the existing readonly_fields tuple.

- [ ] **Step 9: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: green, with 17 new test cases from this plan — 11 in Task 1 and 6 in Task 2.

- [ ] **Step 10: Measure the real effect**

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Article
from apps.digest.llm import PAPER_DOMAINS, check_rule_prefilter
papers = [a for a in Article.objects.filter(status=Article.Status.SKIPPED)
          if any(d in (a.canonical_url or '').lower() for d in PAPER_DOMAINS)]
admitted = [a for a in papers if check_rule_prefilter(a)[0]]
print(f'skipped papers re-examined: {len(papers)}')
print(f'admitted by artifact verification: {len(admitted)}')
for a in admitted[:15]:
    print(f'  {a.title[:56]}\n      {a.artifact_url}')
"
```

This makes real GitHub requests — one per paper that names a repository, roughly twenty in total,
and stores the resulting artifact verdict fields. It does not change article status. Paste the
output. Expect a single-digit or low-double-digit number; anything near 227 means the prefilter
exception is firing where it should not.

Measured 2026-08-19 on the local skipped-paper corpus:

    skipped papers re-examined: 207
    admitted by artifact verification: 6

- [ ] **Step 11: Commit**

```bash
git add apps/digest/models.py apps/digest/migrations/0005_article_artifact.py \
        apps/digest/llm.py apps/digest/admin.py tests/test_maturity_ceiling.py
git commit -m "Admit a paper whose promised repository actually exists"
```

- [ ] **Step 12: Rebuild, because this is code and a migration**

```bash
docker compose build
docker compose up -d
docker compose exec -T worker-llm uv run python manage.py migrate --check
```

Expected: `migrate --check` exits quietly, meaning the container's database is up to date.

---

## Not in this plan

**No status change for articles already skipped.** Step 10 may fill the two artifact verdict
fields while measuring the effect, but it does not change an article status. Whether to re-run
the pipeline over historic papers is the owner's call, and it is one command once this lands.

**GitLab and HuggingFace are not checked.** `find_repo_url` recognises GitLab so the link is
recorded, but `repo_is_real` returns False for it. Of the 19 links measured across 227 papers,
GitHub carried all of them; a second host earns its code when a second host appears in the data.

**No GitHub token.** The measured need is roughly twenty checks over the whole stored corpus and
a handful a day after that, against an unauthenticated limit of 60 an hour.

---

## Self-review

**Coverage**

| Requirement | Step |
|---|---|
| "Resolves" means 200 **and** non-empty | Task 1 Step 4, pinned by the `size: 0` case |
| Only papers about to be dropped are checked | Task 2 Step 5, inside the skip branch |
| A paper with no link costs no request | Task 2 Step 1, `test_a_paper_with_no_link_is_skipped_without_a_request` |
| The answer crosses two pipeline stages | Task 2 Steps 3 and 6, via the stored fields |
| An outage does not admit a paper | Task 1 Step 4 returns False, pinned by the timeout test |
| Never-checked is distinct from checked-and-failed | Task 2 Step 3, `artifact_verified` is nullable, pinned by the no-link test |

**Placeholder scan:** none. Every code step carries its code and every command its expected
output.

**Type consistency:** `find_repo_url(text) -> str` and `repo_is_real(url, client=None) -> bool`
are defined in Task 1 and called in Task 2's `_verify_artifact` with those types.
`maturity_ceiling` keeps returning `str | None`, and `REPRODUCIBLE_OPEN_SOURCE` is an existing
member of `Maturity`.

**One note for the reviewer.** The honest expected yield is at most 19 articles out of 227, and
probably fewer. An earlier estimate in conversation said this would "bring 225 back", which was
wrong: it assumed every paper names a repository, and 9% of them do. The feature is still worth
building, but not for the volume — for the self-selection.

Four defects were found in this implementation on 2026-08-19 and are closed by
`docs/superpowers/plans/2026-08-19-artifact-verification-followup.md`: an unanswered GitHub
request was stored as a negative verdict; the finder could not see a URL with a path, a query,
or a dotted repository name; the first link won even when the paper only cited it; and the six
verified papers were never returned to the pipeline.
