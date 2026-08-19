# Artifact Verification Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make artifact verification answer the question it claims to answer. Today it stores "no artifact" when GitHub merely failed to reply, it cannot see the most common shape of a repository URL, it can verify a repository the paper only cited, and the six papers it did verify are still sitting in `skipped`.

**Architecture:** Four bounded changes inside `apps/digest/artifacts.py`, one guard in `llm._verify_artifact`, and one management command to return stranded articles to the pipeline. No new models, no new migration.

**Tech Stack:** Django, httpx, respx, pytest.

**Spec:** none — this closes defects found on 2026-08-19 while verifying `docs/superpowers/plans/2026-08-19-artifact-verification.md`, which is otherwise implemented and accepted.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite is green at **239 passed** before this plan and must stay green
- The worktree carries uncommitted work from plans A–E. Commit only the files this plan names
- `Article.artifact_url` and `Article.artifact_verified` already exist (migration `0005`). This plan adds no fields
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

Plan B shipped and works going forward. Four things it got wrong, each measured on 2026-08-19.

**1. A failure to ask is stored as an answer.**

```python
verified = artifacts.repo_is_real(url)
article.artifact_verified = verified  # apps/digest/llm.py
article.save(update_fields=["artifact_url", "artifact_verified"])
```

`repo_is_real` returns `False` for every non-200 — including 403, 429, 5xx and a bare
`except Exception` around the request. GitHub's unauthenticated API allows **60 requests per hour
per IP**. So a rate limit writes `artifact_verified=False`, and the very next line of
`_verify_artifact` guarantees the article is never asked about again:

```python
if article.artifact_verified is not None:
    return article.artifact_verified
```

This is the defect plan E was written to remove: recording an outcome that did not happen. E fixed
it for admin alerts by only writing `last_alerted_on` after a successful send. The same rule was
not applied here.

**2. The finder cannot see the common URL shapes.** Run against the shipped code:

```
"github.com/Tencent/AI-Infra-Guard/tree/main/ventor"   ->  ''                       (missed)
"github.com/vercel/next.js is popular"                 ->  'github.com/vercel/next' (wrong repo)
"github.com/socketio/socket.io/blob/main/README.md"    ->  'github.com/socketio/socket'
"github.com/foo/bar?tab=readme"                        ->  ''
"<https://github.com/foo/bar>"                         ->  ''
"https://github.com/foo/bar/"                          ->  ''
```

The trailing lookahead `(?=[\s,;.)\]]|$)` requires the repository name to be the end of the URL, so
any deeper path fails outright, and the non-greedy name stops at the first dot. The corpus already
has a victim: `github.com/Tencent/AI-Infra-Guard` in *Ventor-QTest*, found by a loose scan and
missed by the shipped finder. **19 links out of 227 papers is a floor, not a ceiling.**

**3. The first link wins, and papers cite baselines before they release code.** The shipped test
documents the behaviour rather than fixing it:

```python
text = "Baselines at github.com/other/baseline; our code at github.com/authors/ours."
assert find_repo_url(text) == "https://github.com/other/baseline"
```

A paper can be admitted because *somebody else's* repository resolves.

**4. Nothing brought the verified papers back.**

```
artifact_verified=True : 6      all six still status=skipped
artifact_verified=False: 1
never checked          : 12     of the 19 articles that carry a link
```

The backfill filled the fields and stopped. `triage_and_classify` only ever picks up
`status=FETCHED`, so those six cannot re-enter the pipeline on their own. **The measured yield of
plan B, in the database today, is zero articles.**

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/artifacts.py` | find and verify a repository | modify |
| `apps/digest/llm.py` | `_verify_artifact` prefilter hook | modify |
| `apps/digest/management/commands/recheck_artifacts.py` | return stranded papers to the pipeline | create |
| `tests/test_maturity_ceiling.py` | artifact tests live here already | modify |
| `docs/superpowers/plans/2026-08-19-artifact-verification.md` | the plan an executor will re-read | modify |
| `docs/REMAINING_WORK.md` | the project's map | modify |

---

## Task 1: Never store a verdict GitHub did not give

**Files:**
- Modify: `apps/digest/artifacts.py`
- Modify: `apps/digest/llm.py`
- Modify: `tests/test_maturity_ceiling.py`

**Interfaces:**
- Changes `repo_is_real(url, client=None) -> bool` to `-> bool | None`.
  `True` verified, `False` GitHub answered and it is not a usable repository,
  `None` GitHub did not answer — **never store `None`**

- [ ] **Step 1: Write the failing tests first**

Replace `test_repo_is_real_is_false_when_github_is_unreachable` in
`tests/test_maturity_ceiling.py` with:

```python
@respx.mock
def test_repo_is_inconclusive_when_github_does_not_answer():
    """A rate limit is not evidence of absence. It must not become a stored verdict."""
    from apps.digest.artifacts import repo_is_real

    respx.get("https://api.github.com/repos/authors/timeout").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )
    respx.get("https://api.github.com/repos/authors/throttled").mock(
        return_value=httpx.Response(403, json={"message": "API rate limit exceeded"})
    )
    respx.get("https://api.github.com/repos/authors/broken").mock(
        return_value=httpx.Response(500, text="upstream error")
    )

    assert repo_is_real("https://github.com/authors/timeout") is None
    assert repo_is_real("https://github.com/authors/throttled") is None
    assert repo_is_real("https://github.com/authors/broken") is None


@respx.mock
def test_repo_is_definitely_false_when_github_says_it_is_not_there():
    """404 is an answer. It is stored, and the article is not asked about again."""
    from apps.digest.artifacts import repo_is_real

    respx.get("https://api.github.com/repos/authors/missing").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    assert repo_is_real("https://github.com/authors/missing") is False


@pytest.mark.django_db
@respx.mock
def test_an_inconclusive_check_stores_nothing_and_stays_retryable():
    """The bug this closes: a 403 wrote artifact_verified=False, permanently."""
    respx.get("https://api.github.com/repos/authors/code").mock(
        return_value=httpx.Response(403, json={"message": "API rate limit exceeded"})
    )
    source = Source.objects.create(
        name="hn_throttled", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.06666",
        content_hash="g" * 64,
        title="A Method With Code",
        extracted_text="We release our implementation at github.com/authors/code . " * 20,
    )

    passed, _reason = check_rule_prefilter(paper)

    assert passed is False, "an unverified paper is still skipped this round"
    paper.refresh_from_db()
    assert paper.artifact_verified is None, "an unanswered check must remain unanswered"
    assert paper.artifact_url == "", "no URL is recorded against a verdict that was not reached"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k "inconclusive or definitely_false"
```

Expected: FAIL. The first two on `False is not None`; the third on
`assert paper.artifact_verified is None` because `False` was stored.

- [ ] **Step 3: Give `repo_is_real` a third answer**

In `apps/digest/artifacts.py`, replace the whole function:

```python
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
```

- [ ] **Step 4: Make the caller respect it**

In `apps/digest/llm.py`, replace the tail of `_verify_artifact`:

```python
verified = artifacts.repo_is_real(url)
if verified is None:
    log.warning(
        "Artifact check for article %s was inconclusive; storing nothing so a later run "
        "can ask again",
        article.id,
    )
    return False

article.artifact_url = url
article.artifact_verified = verified
article.save(update_fields=["artifact_url", "artifact_verified"])
log.info("Artifact for article %s: %s -> %s", article.id, url, verified)
return verified
```

- [ ] **Step 5: Run the tests, then prove the guard has teeth**

```bash
uv run pytest tests/test_maturity_ceiling.py -q
```

Expected: PASS.

Now change `if verified is None:` to `if False:` in `_verify_artifact` and run:

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k inconclusive_check_stores_nothing
```

Expected: FAIL. Restore the line and report both results.

---

## Task 2: Find the repository links that are actually there

**Files:**
- Modify: `apps/digest/artifacts.py`
- Modify: `tests/test_maturity_ceiling.py`

**Interfaces:**
- `find_repo_url` keeps its signature in this task. Task 3 extends it

- [ ] **Step 1: Extend the parametrized test with the shapes that fail today**

In `tests/test_maturity_ceiling.py`, add these cases to the existing
`@pytest.mark.parametrize` list on `test_find_repo_url`, keeping every existing case unchanged:

```python
(
    (
        "Code at https://github.com/Tencent/AI-Infra-Guard/tree/main/ventor today.",
        "https://github.com/Tencent/AI-Infra-Guard",
    ),
)
(
    (
        "Built on https://github.com/vercel/next.js in production.",
        "https://github.com/vercel/next.js",
    ),
)
(
    (
        "See https://github.com/socketio/socket.io/blob/main/README.md for usage.",
        "https://github.com/socketio/socket.io",
    ),
)
(("Repo: github.com/foo/bar?tab=readme-ov-file", "https://github.com/foo/bar"),)
(("Repo: <https://github.com/foo/bar>", "https://github.com/foo/bar"),)
(("Repo: https://github.com/foo/bar/", "https://github.com/foo/bar"),)
(("Repo: https://github.com/foo/bar).", "https://github.com/foo/bar"),)
(("Only an owner: https://github.com/foo and nothing more.", ""),)
```

The dotted names are the point of the pair: `next.js` must survive, and `requests.` at the end of
a sentence must lose its period. Both already appear in the list; do not remove them.

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k find_repo_url
```

Expected: FAIL on the first six new cases. `Only an owner` should already pass.

- [ ] **Step 3: Let the path end the match, not a lookahead**

In `apps/digest/artifacts.py`, replace the pattern and the not-owners set:

```python
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
    "about",
    "apps",
    "collections",
    "enterprise",
    "explore",
    "features",
    "login",
    "marketplace",
    "notifications",
    "orgs",
    "pricing",
    "search",
    "settings",
    "signup",
    "sponsors",
    "topics",
    "trending",
}


def _clean_repo(raw: str) -> str:
    """Strip a sentence-final period and a .git suffix from a captured repository name."""
    repo = raw.rstrip(".")
    if repo.lower().endswith(".git"):
        repo = repo[:-4].rstrip(".")
    return repo
```

Then rewrite the finder body:

```python
def find_repo_url(text: str) -> str:
    """Return the first repository link, normalised, or an empty string."""
    for match in _REPO.finditer(text or ""):
        host, owner = match.group(1).lower(), match.group(2)
        repo = _clean_repo(match.group(3))
        if owner.lower() in _NOT_OWNERS or not repo:
            continue
        return f"https://{host}/{owner}/{repo}"
    return ""
```

- [ ] **Step 4: Run it to verify it passes**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k find_repo_url
```

Expected: PASS, every case.

- [ ] **Step 5: Re-measure the corpus**

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Article
from apps.digest.llm import PAPER_DOMAINS
from apps.digest.artifacts import find_repo_url
papers = [a for a in Article.objects.all()
          if any(d in (a.canonical_url or '').lower() for d in PAPER_DOMAINS)
          or (a.source and a.source.connector == 'hf')]
found = [a for a in papers if find_repo_url(a.extracted_text or '')]
print(f'paper-domain articles: {len(papers)}')
print(f'with a repository link: {len(found)}')
"
```

The shipped finder reported **19 of 227**. The pattern above was prototyped against this corpus
on 2026-08-19 and reported **20**: the one link it recovers is
`github.com/Tencent/AI-Infra-Guard` in *Ventor-QTest*, and nothing that used to be found is lost.

**Expect 20.** A lower number means the pattern is rejecting something it used to accept; a higher
number means it is matching something the prototype did not — either way, stop and report rather
than accepting the count.

The honest size of this task is one article. It is still worth doing, because the shapes it fixes
(`/tree/`, `/blob/`, `next.js`, `?tab=`) are ordinary and this corpus is only 227 papers deep.

---

## Task 3: Prefer the paper's own repository over one it merely cites

**Files:**
- Modify: `apps/digest/artifacts.py`
- Modify: `apps/digest/llm.py`
- Modify: `tests/test_maturity_ceiling.py`

**Interfaces:**
- Changes `find_repo_url(text)` to `find_repo_url(text, title="")`. With no title, behaviour is
  unchanged: the first link wins

- [ ] **Step 1: Write the failing tests first**

Replace `test_find_repo_url_takes_the_first_of_several` with:

```python
def test_the_repository_named_by_the_title_beats_an_earlier_one():
    """A paper cites baselines before it releases its own code, so first-wins picks wrong."""
    from apps.digest.artifacts import find_repo_url

    text = "Baselines at github.com/other/baseline; our code at github.com/thunlp/PACE-Bench."
    title = "PACE-Bench: Benchmarking Physics Adaptation"

    assert find_repo_url(text, title) == "https://github.com/thunlp/PACE-Bench"
    assert find_repo_url(text) == "https://github.com/other/baseline"


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Dion3: Full-Stack Orthogonal Updates", "https://github.com/microsoft/dion"),
        ("ConceptFormer: Learning Adaptive Latents", "https://github.com/Neuir/ConceptFormer"),
        ("A Method With No Named Artifact", "https://github.com/other/baseline"),
    ],
)
def test_title_matching_is_substring_based_and_falls_back(title, expected):
    """Real titles from the corpus. dion vs Dion3 must still match; no match falls back."""
    from apps.digest.artifacts import find_repo_url

    text = (
        "Baselines at github.com/other/baseline. "
        "Code at github.com/microsoft/dion and github.com/Neuir/ConceptFormer."
    )
    assert find_repo_url(text, title) == expected
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/test_maturity_ceiling.py -q -k "named_by_the_title or substring_based"
```

Expected: FAIL — `find_repo_url()` takes one argument.

- [ ] **Step 3: Collect the candidates, then choose**

In `apps/digest/artifacts.py`, add the helper and rewrite the finder:

```python
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
```

- [ ] **Step 4: Pass the title at the call site**

In `apps/digest/llm.py`, inside `_verify_artifact`:

```python
    url = artifacts.find_repo_url(article.extracted_text or "", article.title or "")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_maturity_ceiling.py -q
```

Expected: PASS. Every existing single-argument case must still pass unchanged — that is the
compatibility this signature preserves.

- [ ] **Step 6: Check the choice against the six the corpus already verified**

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Article
from apps.digest.artifacts import find_repo_url
for a in Article.objects.filter(artifact_verified=True):
    now = find_repo_url(a.extracted_text or '', a.title or '')
    flag = 'same' if now == a.artifact_url else 'CHANGED'
    print(f'{flag:8} stored={a.artifact_url}')
    print(f'         now   ={now}   <- {a.title[:50]}')
"
```

Paste the output. Prototyped on 2026-08-19, all six came back `same` — title preference changes
which candidate wins only where there is more than one, and these six have one each.

**Expect six `same` lines.** A `CHANGED` line is not automatically wrong — it may be the fix
working on a paper that cites a baseline — but every one must be inspected and reported with a
judgement, not just listed.

---

## Task 4: Return the stranded papers to the pipeline

**Files:**
- Create: `apps/digest/management/commands/recheck_artifacts.py`
- Modify: `tests/test_management_commands.py`

**Interfaces:**
- Produces: `manage.py recheck_artifacts [--dry-run]`

- [ ] **Step 1: Write the failing tests first**

Add to `tests/test_management_commands.py`:

```python
@pytest.fixture
def paper_source(db):
    return Source.objects.create(
        name="paper_src",
        connector=Source.Connector.RSS,
        url="https://arxiv.example/rss",
    )


def _paper(source, slug, *, status, verified, text="See github.com/authors/code for the code."):
    return Article.objects.create(
        source=source,
        canonical_url=f"https://arxiv.org/abs/{slug}",
        content_hash=f"hash-{slug}",
        title=f"Paper {slug}",
        extracted_text=text,
        status=status,
        artifact_verified=verified,
    )


def test_recheck_returns_verified_and_unanswered_papers(paper_source, capsys):
    verified = _paper(paper_source, "1111", status=Article.Status.SKIPPED, verified=True)
    unanswered = _paper(paper_source, "2222", status=Article.Status.SKIPPED, verified=None)

    call_command("recheck_artifacts")

    verified.refresh_from_db()
    unanswered.refresh_from_db()
    assert verified.status == Article.Status.FETCHED
    assert unanswered.status == Article.Status.FETCHED
    assert "2 article" in capsys.readouterr().out


def test_recheck_leaves_settled_and_unrelated_articles_alone(paper_source):
    rejected = _paper(paper_source, "3333", status=Article.Status.SKIPPED, verified=False)
    no_link = _paper(
        paper_source,
        "4444",
        status=Article.Status.SKIPPED,
        verified=None,
        text="We evaluate on three benchmarks and report gains.",
    )
    already_moving = _paper(paper_source, "5555", status=Article.Status.CLASSIFIED, verified=True)
    not_a_paper = Article.objects.create(
        source=paper_source,
        canonical_url="https://github.com/ollama/ollama/releases/tag/v1",
        content_hash="hash-release",
        title="A release",
        extracted_text="See github.com/authors/code for the code.",
        status=Article.Status.SKIPPED,
    )

    call_command("recheck_artifacts")

    for article in (rejected, no_link, already_moving, not_a_paper):
        before = article.status
        article.refresh_from_db()
        assert article.status == before, f"{article.canonical_url} must not be touched"


def test_recheck_dry_run_changes_nothing(paper_source, capsys):
    verified = _paper(paper_source, "6666", status=Article.Status.SKIPPED, verified=True)

    call_command("recheck_artifacts", "--dry-run")

    verified.refresh_from_db()
    assert verified.status == Article.Status.SKIPPED
    assert "would return" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/test_management_commands.py -q -k recheck
```

Expected: FAIL — `Unknown command: 'recheck_artifacts'`.

- [ ] **Step 3: Write the command**

Create `apps/digest/management/commands/recheck_artifacts.py`:

```python
"""Return paper articles that artifact verification stranded in `skipped`.

Two groups qualify, and only two:

* `artifact_verified=True` — the repository was verified and the article was skipped anyway,
  because the backfill filled the fields without moving the status. Measured 2026-08-19: six
  articles, none of which had ever re-entered the pipeline.
* `artifact_verified IS NULL` with a repository link in the text — GitHub never answered, so
  nothing was stored. These retry.

`artifact_verified=False` is a settled answer and is never reset, which is what stops this
command from looping. Everything moves to `fetched`, and the next evening run triages it.
"""

from django.core.management.base import BaseCommand

from apps.digest import artifacts
from apps.digest.llm import PAPER_DOMAINS
from apps.digest.models import Article


def _is_paper(article: Article) -> bool:
    url = (article.canonical_url or "").lower()
    return any(domain in url for domain in PAPER_DOMAINS) or (
        article.source is not None and article.source.connector == "hf"
    )


class Command(BaseCommand):
    help = "Move stranded, artifact-bearing paper articles back to fetched for re-triage"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would move without changing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skipped = (
            Article.objects.filter(status=Article.Status.SKIPPED)
            .exclude(artifact_verified=False)
            .select_related("source")
        )

        selected = []
        for article in skipped:
            if not _is_paper(article):
                continue
            if article.artifact_verified is True:
                selected.append((article, "verified"))
            elif artifacts.find_repo_url(article.extracted_text or "", article.title or ""):
                selected.append((article, "unanswered"))

        for article, reason in selected:
            self.stdout.write(f"  [{reason}] {article.canonical_url}  {article.title[:60]}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"\n  Would return {len(selected)} article(s) to fetched.")
            )
            return

        for article, _reason in selected:
            article.status = Article.Status.FETCHED
            article.save(update_fields=["status"])

        self.stdout.write(
            self.style.SUCCESS(
                f"\n  Returned {len(selected)} article(s) to fetched. "
                "The next evening run will triage them."
            )
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_management_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Run it against the real database, dry first**

```bash
uv run python manage.py recheck_artifacts --dry-run
```

Paste the whole output. Six `[verified]` lines are expected from the 2026-08-19 measurement, plus
however many `[unanswered]` lines Task 2's wider finder now sees. Then, only if the list looks
right:

```bash
uv run python manage.py recheck_artifacts
```

- [ ] **Step 6: Report what actually happens to them**

After the next evening run, or after running triage by hand, report this:

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from collections import Counter
from apps.digest.models import Article, Analysis
arts = Article.objects.filter(artifact_verified=True)
print('status:', dict(Counter(a.status for a in arts)))
for a in arts:
    cls = a.analyses.filter(stage=Analysis.Stage.CLASSIFICATION).order_by('-created_at').first()
    print(f'  {a.status:11} maturity={getattr(cls, \"maturity\", None)}  {a.title[:44]}')
"
```

**Read this honestly.** `EXCLUDED_MATURITIES` still drops `paper_only` and `announcement_only`
from ranking, and the verified ceiling only *permits* `reproducible_open_source` — it does not
grant it. If the classifier still calls these papers `paper_only`, they will be classified and
still never reach a digest. That is a real possible outcome and it must be reported as the yield,
not hidden. The number to report is how many reach a digest, not how many were admitted.

---

## Task 5: Suite, docs, and commit

- [ ] **Step 1: Correct the parent plan**

In `docs/superpowers/plans/2026-08-19-artifact-verification.md`:

- the status line says "passes 238 tests"; the suite was **239** at that point. Replace the
  absolute number with "the full suite", which does not go stale
- after the measured-yield block near the end, add:

```
Four defects were found in this implementation on 2026-08-19 and are closed by
`docs/superpowers/plans/2026-08-19-artifact-verification-followup.md`: an unanswered GitHub
request was stored as a negative verdict; the finder could not see a URL with a path, a query,
or a dotted repository name; the first link won even when the paper only cited it; and the six
verified papers were never returned to the pipeline.
```

- [ ] **Step 2: Record the rule in the map**

In `docs/REMAINING_WORK.md`, under **### Measured facts an executor will need**:

```
| Artifact verification | `repo_is_real` returns `None` when GitHub did not answer, and `None` is never stored. The unauthenticated API allows 60 requests/hour/IP, so a rate limit must not become a permanent "no artifact" |
```

- [ ] **Step 3: Run everything**

```bash
uv run pytest -q
uv run ruff check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
```

Expected: green, `All checks passed!`, `No changes detected`. Report the test count; it was
**239** before this plan.

- [ ] **Step 4: Rebuild, because this is a code change**

```bash
docker compose build
docker compose up -d
```

`docker compose restart` does not pick up a code change.

- [ ] **Step 5: Commit only this plan's files**

```bash
git add apps/digest/artifacts.py \
        apps/digest/llm.py \
        apps/digest/management/commands/recheck_artifacts.py \
        tests/test_maturity_ceiling.py \
        tests/test_management_commands.py \
        docs/REMAINING_WORK.md \
        docs/superpowers/plans/2026-08-19-artifact-verification.md \
        docs/superpowers/plans/2026-08-19-artifact-verification-followup.md
git commit -m "Store an artifact verdict only when GitHub gave one, and find the links that are there"
```

- [ ] **Step 6: STOP and report**

Report: the two mutation results from Task 1 Step 5, the corpus count from Task 2 Step 5, the
`CHANGED`/`same` list from Task 3 Step 6, the dry-run list from Task 4 Step 5, and the honest
yield from Task 4 Step 6.

---

## Not in this plan, and why

**No authenticated GitHub client.** A token would raise the rate limit from 60/hour to 5000/hour
and make `None` rare. It also adds a second secret to a project that has already leaked one to a
terminal twice. The correct order is: stop storing bad verdicts first, measure how often `None`
actually happens, and only then decide whether a token is worth its handling cost.

**No GitLab or HuggingFace verifier.** Of the 19 repository links measured across 227 papers,
GitHub carried all of them. `repo_is_real` returns `False` for the other hosts, which is honest:
we did not verify it. A second verifier is warranted when a second host appears in the corpus.

**`SKIP_PAPER_DOMAINS` is not touched.** The general filter stays on; artifact verification remains
the narrow exception before triage.

---

## Self-review

**Coverage**

| Defect found 2026-08-19 | Task |
|---|---|
| A 403 or timeout is stored as `artifact_verified=False`, permanently | 1 |
| No test distinguished "GitHub said no" from "GitHub said nothing" | 1, Step 1 |
| A URL with `/tree/` or `/blob/` is invisible to the finder | 2 |
| A dotted repository name is truncated to the wrong repository | 2 |
| A query string, angle bracket, or trailing slash defeats the match | 2 |
| The first link wins, so a cited baseline can admit a paper | 3 |
| Six verified papers are stranded in `skipped` | 4 |
| Twelve linked papers were never checked at all | 4, the `unanswered` branch |
| The parent plan carries a stale absolute test count | 5, Step 1 |

**Placeholder scan:** none. Every step carries its code or command and its expected output.

**Type consistency:** `repo_is_real` returns `bool | None`; the single caller `_verify_artifact`
handles `None` before any `save()`, and still returns a plain `bool` to `check_rule_prefilter`,
whose `tuple[bool, str]` contract is unchanged. `find_repo_url`'s new `title` parameter has a
default, so every existing single-argument call still type-checks and still behaves identically.

**One note for the reviewer.** Task 3 changes an assertion that currently passes —
`find_repo_url` returning the baseline repository. That test documented the defect instead of
failing on it. A test that records wrong behaviour is worse than no test: it makes the behaviour
look deliberate.
