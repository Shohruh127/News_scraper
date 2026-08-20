from datetime import date
from types import SimpleNamespace

import pytest

from apps.digest import llm, publish, ranking, tasks, verification


@pytest.mark.django_db
def test_compose_and_publish_hands_off_selected_candidates(monkeypatch):
    target_date = date(2026, 8, 19)
    selected = [(SimpleNamespace(id=42), SimpleNamespace(), 0.9, [])]
    analysed_ids = []
    compose_calls = []

    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: selected)
    monkeypatch.setattr(
        llm,
        "analyse_for_digest_logic",
        lambda article_ids: analysed_ids.append(article_ids),
    )

    def compose(digest_date, edition=None, candidates=None):
        compose_calls.append((digest_date, edition, candidates))
        return object()

    monkeypatch.setattr(ranking, "compose_digest", compose)
    monkeypatch.setattr(publish, "publish_digest", lambda digest: {"status": "published"})

    result = tasks.compose_and_publish(target_date.isoformat(), edition="morning")

    assert result == {"status": "published"}
    assert analysed_ids == [[42]]
    assert compose_calls == [(target_date, "morning", selected)]


@pytest.mark.django_db
def test_compose_and_publish_does_not_verify_when_flag_is_off(monkeypatch, settings):
    settings.BENCHMARK_VERIFICATION_ENABLED = False
    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: [])
    monkeypatch.setattr(
        ranking, "compose_digest", lambda digest_date, edition=None, candidates=None: object()
    )
    monkeypatch.setattr(publish, "publish_digest", lambda digest: {"status": "published"})
    monkeypatch.setattr(
        verification,
        "apply_cluster_evidence",
        lambda digest: pytest.fail("disabled verifier was called"),
    )

    assert tasks.compose_and_publish("2026-08-19") == {"status": "published"}


@pytest.mark.django_db
def test_compose_and_publish_verifies_before_publish_when_flag_is_on(monkeypatch, settings):
    settings.BENCHMARK_VERIFICATION_ENABLED = True
    events = []
    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: [])
    monkeypatch.setattr(
        ranking, "compose_digest", lambda digest_date, edition=None, candidates=None: object()
    )
    monkeypatch.setattr(
        verification,
        "apply_cluster_evidence",
        lambda digest: events.append("verify"),
    )
    monkeypatch.setattr(
        publish,
        "publish_digest",
        lambda digest: events.append("publish") or {"status": "published"},
    )

    assert tasks.compose_and_publish("2026-08-19") == {"status": "published"}
    assert events == ["verify", "publish"]
