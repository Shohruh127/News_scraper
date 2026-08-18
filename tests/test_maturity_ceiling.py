"""Source-based maturity ceiling (ADR-004 §3 follow-up).

Measured 2026-08-17: the classifier returned reproducible_open_source for 12 of 15
selected items, including seven arXiv abstracts scored evidence 9-10, and paper_only for
none of them. Since paper_only is the only hard-excluded value, the anti-vapourware
filter was excluding nothing and seven narrow CV papers reached the digest.

The prompt is not at fault. CONTENT_SCHEMA §3 requires "a link that resolves today", and
the model cannot open a link — it falls back to "we release our code", which appears in
essentially every abstract. The source is ground truth and needs no inference.
"""

import pytest

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
