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