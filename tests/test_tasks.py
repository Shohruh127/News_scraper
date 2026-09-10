from datetime import date
from types import SimpleNamespace

import pytest

from apps.digest import llm, publish, ranking, tasks, verification


@pytest.mark.django_db
def test_compose_and_publish_hands_off_selected_candidates(monkeypatch, settings):
    settings.PUBLISHING_ENABLED = True
    target_date = date(2026, 8, 19)
    # A candidate reaches the editorial only with a photo (publish.keep_publishable).
    article = SimpleNamespace(id=42, title="t", meta={"image_url": "https://img.example/42.jpg"})
    selected = [(article, SimpleNamespace(), 0.9, [])]
    analysed_ids = []
    compose_calls = []

    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: selected)

    def fake_analyse(article_ids):
        analysed_ids.append(article_ids)
        return [SimpleNamespace(article_id=cid) for cid in article_ids]

    monkeypatch.setattr(llm, "analyse_for_digest_logic", fake_analyse)

    def compose(digest_date, edition=None, candidates=None):
        compose_calls.append((digest_date, edition, candidates))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(ranking, "compose_digest", compose)
    monkeypatch.setattr(tasks.publish_next_item, "delay", lambda digest_id: None)

    result = tasks.compose_and_publish(target_date.isoformat(), edition="morning")

    assert result == {"status": "drip_started", "digest_id": 1}
    assert analysed_ids == [[42]]
    assert compose_calls == [(target_date, "morning", selected)]


@pytest.mark.django_db
def test_compose_and_publish_does_not_verify_when_flag_is_off(monkeypatch, settings):
    settings.PUBLISHING_ENABLED = True
    settings.BENCHMARK_VERIFICATION_ENABLED = False
    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: [])
    monkeypatch.setattr(
        ranking,
        "compose_digest",
        lambda digest_date, edition=None, candidates=None: SimpleNamespace(id=1),
    )
    monkeypatch.setattr(tasks.publish_next_item, "delay", lambda digest_id: None)
    monkeypatch.setattr(
        verification,
        "apply_cluster_evidence",
        lambda digest: pytest.fail("disabled verifier was called"),
    )

    assert tasks.compose_and_publish("2026-08-19") == {"status": "drip_started", "digest_id": 1}


@pytest.mark.django_db
def test_compose_and_publish_verifies_before_publish_when_flag_is_on(monkeypatch, settings):
    settings.PUBLISHING_ENABLED = True
    settings.BENCHMARK_VERIFICATION_ENABLED = True
    events = []
    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: [])
    monkeypatch.setattr(
        ranking,
        "compose_digest",
        lambda digest_date, edition=None, candidates=None: SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        verification,
        "apply_cluster_evidence",
        lambda digest: events.append("verify"),
    )
    monkeypatch.setattr(
        tasks.publish_next_item,
        "delay",
        lambda digest_id: events.append("publish"),
    )

    assert tasks.compose_and_publish("2026-08-19") == {"status": "drip_started", "digest_id": 1}
    assert events == ["verify", "publish"]


@pytest.mark.django_db
def test_only_translated_candidates_are_composed(monkeypatch, settings, classified_articles):
    """analyse_for_digest_logic returns the items that got a usable translation.

    Discarding that return value is why an item whose translation failed still reached the
    renderer, raised ValueError and failed the whole digest.
    """
    from apps.digest import llm, tasks
    from apps.digest.models import Digest

    settings.DIGEST_MAX_ITEMS = 2
    settings.DIGEST_SELECT_MARGIN = 2
    settings.PUBLISHING_ENABLED = False

    translated = classified_articles[:2]

    def fake_analyse(article_ids, client=None):
        from tests.helpers import make_editorial

        return [make_editorial(a)[1] for a in translated]

    monkeypatch.setattr(llm, "analyse_for_digest_logic", fake_analyse)
    tasks.compose_and_publish(edition=Digest.Edition.MORNING)

    digest = Digest.objects.get(edition=Digest.Edition.MORNING)
    composed = {item.article_id for item in digest.items.all()}
    assert composed == {a.id for a in translated}


@pytest.mark.django_db
def test_publish_next_item_picks_the_lowest_unposted_position(digest_with_two_items, monkeypatch):
    from apps.digest import tasks

    called = []
    monkeypatch.setattr(
        publish,
        "publish_digest_item",
        lambda item, **kw: called.append(item.position) or {"status": "sent"},
    )
    res = tasks.publish_next_item()
    assert res["status"] == "sent"
    assert res["position"] == 1
    assert called == [1]


@pytest.mark.django_db
def test_publish_next_item_is_a_noop_when_all_items_are_done(digest_with_two_items, monkeypatch):
    from apps.digest import tasks
    from apps.digest.models import DeliveryState

    digest_with_two_items.items.update(channel_delivery_state=DeliveryState.SENT)
    res = tasks.publish_next_item()
    assert res["status"] == "idle"


@pytest.mark.django_db
def test_a_failed_item_is_never_re_sent(digest_with_two_items, monkeypatch):
    """FAILED is terminal, and treating it as retryable reposts it every tick forever.

    publish.TERMINAL_DELIVERY_STATES already counts FAILED as done. Listing it among the
    states the drip retries made the two disagree, so the item had its attempt, failed, and
    was picked up again two hours later — for as long as the digest existed.
    """
    from apps.digest import tasks
    from apps.digest.models import DeliveryState

    first, second = digest_with_two_items.items.order_by("position")
    first.channel_delivery_state = DeliveryState.FAILED
    first.save(update_fields=["channel_delivery_state"])

    attempted = []
    monkeypatch.setattr(
        publish,
        "publish_digest_item",
        lambda item, **kw: attempted.append(item.position) or {"status": "sent"},
    )
    tasks.publish_next_item()
    assert attempted == [2], "the drip must step past the failed item, not retry it"


@pytest.mark.django_db
def test_a_failed_item_does_not_block_the_roundup(digest_with_two_items, monkeypatch):
    """A block that lost one post still gets its index.

    While FAILED counted as unfinished, `has_remaining` was permanently true and the roundup
    for that block never fired.
    """
    from apps.digest import tasks
    from apps.digest.models import DeliveryState

    first, second = digest_with_two_items.items.order_by("position")
    first.channel_delivery_state = DeliveryState.FAILED
    first.save(update_fields=["channel_delivery_state"])

    roundups = []
    monkeypatch.setattr(
        tasks.publish_roundup, "delay", lambda digest_id: roundups.append(digest_id)
    )

    def _send(item, **kw):
        item.channel_delivery_state = DeliveryState.SENT
        item.channel_message_id = 900 + item.position
        item.save(update_fields=["channel_delivery_state", "channel_message_id"])
        return {"status": "sent"}

    monkeypatch.setattr(publish, "publish_digest_item", _send)
    tasks.publish_next_item()
    assert roundups == [digest_with_two_items.id]


@pytest.mark.django_db
def test_the_drip_drains_the_oldest_block_first(digest_with_two_items, monkeypatch):
    """FIFO is by composition time, not by edition.

    TextChoices sort alphabetically, so ordering on `edition` puts "evening" before
    "morning" and drains the newer block first.
    """
    from datetime import date

    from apps.digest import tasks
    from apps.digest.models import Digest, DigestItem

    older = digest_with_two_items
    newer = Digest.objects.create(digest_date=date(2026, 8, 25), edition=Digest.Edition.EVENING)
    DigestItem.objects.create(
        digest=newer, article=older.items.first().article, position=1, score=0.5
    )

    drained = []
    monkeypatch.setattr(
        publish,
        "publish_digest_item",
        lambda item, **kw: drained.append(item.digest_id) or {"status": "sent"},
    )
    tasks.publish_next_item()
    assert drained == [older.id]
