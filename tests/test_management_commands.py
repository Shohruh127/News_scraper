from datetime import date

import pytest
from django.core.management import call_command

from apps.digest import publish
from apps.digest.models import Article, Digest, DigestItem, Source


@pytest.fixture
def digest_with_item(db):
    source = Source.objects.create(
        name="command_source",
        connector=Source.Connector.RSS,
        url="https://command.example/rss",
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://command.example/item",
        content_hash="command-item",
        title="Command item",
        extracted_text="Body",
    )
    digest = Digest.objects.create(digest_date=date(2026, 8, 19))
    DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)
    return digest


def test_command_reports_stable_item_summary(monkeypatch, digest_with_item, capsys):
    monkeypatch.setattr(
        publish,
        "publish_digest",
        lambda _digest, **kw: {
            "digest_id": digest_with_item.id,
            "digest_date": "2026-08-19",
            "status": Digest.Status.PUBLISHED,
            "items_sent": 1,
            "items_skipped": 0,
            "items_failed": 0,
            "failed_items": [],
            "suppressed": False,
        },
    )

    call_command("publish_digest", "--digest-id", str(digest_with_item.id))

    output = capsys.readouterr().out
    assert "status=published" in output
    assert "items_sent=1" in output
    assert "items_skipped=0" in output
    assert "items_failed=0" in output
    assert "channel_msg" not in output
    assert "group_msg" not in output


def test_command_reports_suppressed_publish(monkeypatch, digest_with_item, capsys):
    monkeypatch.setattr(
        publish,
        "publish_digest",
        lambda _digest, **kw: {
            "digest_id": digest_with_item.id,
            "digest_date": "2026-08-19",
            "status": Digest.Status.COMPOSED,
            "items_sent": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "failed_items": [],
            "suppressed": True,
        },
    )

    call_command("publish_digest", "--digest-id", str(digest_with_item.id))

    output = capsys.readouterr().out
    assert "Publishing suppressed" in output
    assert "0 items not sent" in output


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
        paper_source, "4444", status=Article.Status.SKIPPED, verified=None,
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