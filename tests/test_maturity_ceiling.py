"""Source-based maturity ceiling (ADR-004 §3 follow-up).

Measured 2026-08-17: the classifier returned reproducible_open_source for 12 of 15
selected items, including seven arXiv abstracts scored evidence 9-10, and paper_only for
none of them. Since paper_only is the only hard-excluded value, the anti-vapourware
filter was excluding nothing and seven narrow CV papers reached the digest.

The prompt is not at fault. CONTENT_SCHEMA §3 requires "a link that resolves today", and
the model cannot open a link — it falls back to "we release our code", which appears in
essentially every abstract. The source is ground truth and needs no inference.
"""

import httpx
import pytest
import respx

from apps.digest.llm import apply_maturity_ceiling, check_rule_prefilter, maturity_ceiling
from apps.digest.models import Article, Maturity, Source

pytestmark = pytest.mark.django_db


def art(source, url):
    return Article.objects.create(
        source=source,
        canonical_url=url,
        content_hash=url[-40:].rjust(64, "0"),
        title="T",
        extracted_text="x" * 900,
    )


@pytest.fixture
def hn(db):
    return Source.objects.create(name="hn", connector="hn", url="https://hn.algolia.com/")


@pytest.fixture
def hf(db):
    return Source.objects.create(name="hf_papers", connector="hf", url="https://hf.co/papers")


@pytest.fixture
def gh(db):
    return Source.objects.create(name="gh", connector="github", url="https://github.com/o/r")


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
        ("Hosted on https://github.com/features/actions today.", ""),
        ("Read more at https://github.com/about .", ""),
        ("We evaluate on three benchmarks and report gains.", ""),
        (
            "Available at https://github.com/psf/requests.git, released today.",
            "https://github.com/psf/requests",
        ),
        (
            "Implementation: https://github.com/psf/requests.",
            "https://github.com/psf/requests",
        ),
        (
            "Code at https://github.com/Tencent/AI-Infra-Guard/tree/main/ventor today.",
            "https://github.com/Tencent/AI-Infra-Guard",
        ),
        (
            "Built on https://github.com/vercel/next.js in production.",
            "https://github.com/vercel/next.js",
        ),
        (
            "See https://github.com/socketio/socket.io/blob/main/README.md for usage.",
            "https://github.com/socketio/socket.io",
        ),
        ("Repo: github.com/foo/bar?tab=readme-ov-file", "https://github.com/foo/bar"),
        ("Repo: <https://github.com/foo/bar>", "https://github.com/foo/bar"),
        ("Repo: https://github.com/foo/bar/", "https://github.com/foo/bar"),
        ("Repo: https://github.com/foo/bar).", "https://github.com/foo/bar"),
        ("Only an owner: https://github.com/foo and nothing more.", ""),
    ],
)
def test_find_repo_url(text, expected):
    from apps.digest.artifacts import find_repo_url

    assert find_repo_url(text) == expected


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


@respx.mock
def test_repo_is_real_only_when_it_has_content():
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


def test_repo_is_real_rejects_a_host_it_cannot_check():
    from apps.digest.artifacts import repo_is_real

    assert repo_is_real("https://gitlab.com/team/project") is False


def test_arxiv_is_capped_at_paper_only_whatever_the_abstract_promises(hn):
    a = art(hn, "https://arxiv.org/abs/2608.12345")
    payload = apply_maturity_ceiling(a, {"maturity": Maturity.REPRODUCIBLE_OPEN_SOURCE})
    assert payload["maturity"] == Maturity.PAPER_ONLY
    assert payload["maturity_capped_from"] == Maturity.REPRODUCIBLE_OPEN_SOURCE


def test_hf_papers_connector_is_capped_regardless_of_url(hf):
    a = art(hf, "https://huggingface.co/papers/2608.99999")
    assert maturity_ceiling(a) == Maturity.PAPER_ONLY


def test_a_huggingface_model_card_is_not_a_paper(hn):
    """The Qwen model card scored reproducible_open_source and was right to: the weights
    are downloadable. Only huggingface.co/papers is a paper, not huggingface.co at large."""
    a = art(hn, "https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B-FP8")
    assert maturity_ceiling(a) is None
    payload = apply_maturity_ceiling(a, {"maturity": Maturity.REPRODUCIBLE_OPEN_SOURCE})
    assert payload["maturity"] == Maturity.REPRODUCIBLE_OPEN_SOURCE
    assert "maturity_capped_from" not in payload


def test_github_release_is_not_capped(gh):
    a = art(gh, "https://github.com/ollama/ollama/releases/tag/v1")
    assert maturity_ceiling(a) is None


def test_ceiling_never_raises_a_claim(hn):
    """A paper already labelled paper_only or announcement_only stays where it is."""
    a = art(hn, "https://arxiv.org/abs/2608.1")
    for claimed in (Maturity.PAPER_ONLY, Maturity.ANNOUNCEMENT_ONLY):
        payload = apply_maturity_ceiling(a, {"maturity": claimed})
        assert payload["maturity"] == claimed
        assert "maturity_capped_from" not in payload


def test_unknown_maturity_is_left_alone(hn):
    a = art(hn, "https://arxiv.org/abs/2608.2")
    payload = apply_maturity_ceiling(a, {"maturity": "something_else"})
    assert payload["maturity"] == "something_else"


@pytest.mark.django_db
@pytest.mark.django_db
@respx.mock
def test_a_paper_with_a_real_repo_survives_the_prefilter():
    respx.get("https://api.github.com/repos/authors/code").mock(
        return_value=httpx.Response(200, json={"size": 900})
    )
    source = Source.objects.create(
        name="hn_artifact", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.01111",
        content_hash="a" * 64,
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
    respx.get("https://api.github.com/repos/authors/empty").mock(
        return_value=httpx.Response(200, json={"size": 0})
    )
    source = Source.objects.create(
        name="hn_empty", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.02222",
        content_hash="b" * 64,
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
        content_hash="c" * 64,
        title="A Method Without Code",
        extracted_text="We evaluate on three benchmarks and report gains. " * 20,
    )

    passed, _ = check_rule_prefilter(paper)

    assert passed is False
    paper.refresh_from_db()
    assert paper.artifact_url == ""
    assert paper.artifact_verified is None


@pytest.mark.django_db
def test_a_verified_paper_may_claim_reproducible_open_source():
    source = Source.objects.create(
        name="hn_ceiling", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.04444",
        content_hash="d" * 64,
        title="A Verified Method",
        extracted_text="x" * 2000,
        artifact_url="https://github.com/authors/code",
        artifact_verified=True,
    )

    assert maturity_ceiling(paper) == Maturity.REPRODUCIBLE_OPEN_SOURCE


@pytest.mark.django_db
def test_an_unverified_paper_keeps_its_paper_only_ceiling():
    source = Source.objects.create(
        name="hn_unverified", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.05555",
        content_hash="e" * 64,
        title="An Unverified Method",
        extracted_text="x" * 2000,
    )

    assert maturity_ceiling(paper) == Maturity.PAPER_ONLY


@pytest.mark.django_db
def test_verified_nonpaper_keeps_no_ceiling():
    source = Source.objects.create(
        name="hn_verified_product", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://github.com/authors/code",
        content_hash="f" * 64,
        title="A Verified Product",
        extracted_text="x" * 2000,
        artifact_url="https://github.com/authors/code",
        artifact_verified=True,
    )

    assert maturity_ceiling(article) is None


def test_prefilter_skips_paper_domains_before_any_llm_call():
    """A paper is rejected before triage, because it can never reach a digest.

    Measured 2026-08-18: paper domains were 216 of 411 stored articles and had consumed
    169 triage and classification calls without ever producing a digest item.
    """
    source = Source.objects.create(
        name="hn", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.01234",
        title="A Paper About Agents",
        extracted_text="x" * 2000,
    )
    passed, reason = check_rule_prefilter(paper)
    assert passed is False
    assert "Paper domain" in reason


@pytest.mark.django_db
def test_prefilter_keeps_non_paper_articles_from_the_same_source():
    """The rule is keyed on the URL, so `hn` still delivers everything else it finds."""
    source = Source.objects.create(
        name="hn2", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    product = Article.objects.create(
        source=source,
        canonical_url="https://github.com/ollama/ollama/releases/tag/v0.32.10",
        title="Ollama v0.32.10",
        extracted_text="x" * 2000,
    )
    passed, _ = check_rule_prefilter(product)
    assert passed is True


@pytest.mark.django_db
def test_paper_prefilter_can_be_switched_off_for_m2(settings):
    """M2 artifact verification needs papers triaged again; one setting restores them."""
    settings.SKIP_PAPER_DOMAINS = False
    source = Source.objects.create(
        name="hn3", url="https://hn.algolia.com/", connector="hn", enabled=True
    )
    paper = Article.objects.create(
        source=source,
        canonical_url="https://arxiv.org/abs/2508.09999",
        title="Another Paper",
        extracted_text="x" * 2000,
    )
    passed, _ = check_rule_prefilter(paper)
    assert passed is True
