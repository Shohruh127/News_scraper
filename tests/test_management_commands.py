import json
from datetime import date

import httpx
import pytest
import respx
from django.core.management import call_command

from apps.digest import publish
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source


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


def test_edit_digest_command_passes_sent_as_photo(monkeypatch, digest_with_item, capsys):
    """edit_digest command retrieves sent_as_photo from DigestItem and passes it to edit_message."""
    item = digest_with_item.items.first()
    item.channel_message_id = 12345
    item.sent_as_photo = True
    item.save()

    calls = []

    def mock_edit_message(chat_id, message_id, new_text, sent_as_photo=False, client=None):
        calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "photo": sent_as_photo,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(publish, "edit_message", mock_edit_message)

    call_command("edit_digest", "--item-id", str(item.id), "--text", "Updated caption")

    assert len(calls) == 1
    assert calls[0]["photo"] is True
    assert calls[0]["message_id"] == 12345
    assert calls[0]["text"] == "Updated caption"


def test_reconcile_delivery_command_sets_message_id_and_photo(digest_with_item):
    """reconcile_delivery updates message id and sets delivery state to SENT."""
    from apps.digest.models import DeliveryState

    item = digest_with_item.items.first()
    item.channel_delivery_state = DeliveryState.UNKNOWN
    item.save()

    call_command(
        "reconcile_delivery",
        str(item.id),
        "--message-id",
        "9999",
        "--sent-as-photo",
        "yes",
    )

    item.refresh_from_db()
    assert item.channel_message_id == 9999
    assert item.sent_as_photo is True
    assert item.channel_delivery_state == DeliveryState.SENT


def test_reconcile_delivery_command_reset_pending(digest_with_item):
    """reconcile_delivery with --reset-pending resets state to PENDING if ack given."""
    from django.core.management.base import CommandError

    from apps.digest.models import DeliveryState

    item = digest_with_item.items.first()
    item.channel_delivery_state = DeliveryState.UNKNOWN
    item.channel_message_id = 8888
    item.save()

    # Fails without confirmation
    with pytest.raises(CommandError, match="--i-checked-telegram"):
        call_command("reconcile_delivery", str(item.id), "--reset-pending")

    # Succeeds with confirmation
    call_command("reconcile_delivery", str(item.id), "--reset-pending", "--i-checked-telegram")
    item.refresh_from_db()
    assert item.channel_delivery_state == DeliveryState.PENDING
    assert item.channel_message_id is None


@pytest.mark.django_db
@respx.mock
def test_eval_post_shapes_generates_both_variants(settings):
    """The command must call the model twice per article — once per instruction — or it is
    comparing a post against itself."""
    from io import StringIO

    settings.LLM_PROVIDER = "gateway"
    settings.EDITORIAL_EN_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    source = Source.objects.create(
        name="shapes_source",
        connector=Source.Connector.RSS,
        url="https://shapes.example/rss",
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://shapes.example/item",
        content_hash="shapes-item",
        title="A vulnerability in a widely deployed tool",
        extracted_text="Body text about the vulnerability. " * 30,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="smart",
        payload={
            "primary_topic": "safety_security",
            "maturity": "live_product",
            "novelty": 7,
            "evidence": 7,
            "production_readiness": 7,
            "reason": "x",
        },
        latency_ms=1,
    )

    prompts = []
    payload = {
        "headline_en": "A risk was disclosed",
        "lead_en": "A vulnerability reaches every deployment of the tool.",
        "body_1_en": "It affects versions 1.0 through 2.4.",
        "kicker_en": "Upgrade before the weekend.",
        "evidence_level": "vendor_claim_only",
        "technical": {},
    }

    def capture(request):
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=capture)

    out = StringIO()
    call_command("eval_post_shapes", limit=1, stdout=out)

    assert len(prompts) == 2, "one call per instruction"
    assert "Pick the lead by this order" in prompts[0], "first is the general instruction"
    assert "what the risk is and who it reaches" in prompts[1], "second is the shape"
    text = out.getvalue()
    assert "GENERAL" in text and "risk" in text
