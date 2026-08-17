"""Tests to assert every registered Celery task routes to the expected queue."""

import pytest

from config.celery import app


@pytest.mark.parametrize(
    ("task_name", "expected_queue"),
    [
        ("digest.fetch_all_sources", "fetch"),
        ("digest.fetch_source", "fetch"),
        ("digest.triage_article", "llm"),
        ("digest.classify_article", "llm"),
        ("digest.triage_and_classify", "llm"),
        ("digest.analyse_for_digest", "llm"),
        ("digest.compose_and_publish", "publish"),
    ],
)
def test_celery_task_routes(task_name, expected_queue):
    route = app.amqp.router.route({}, task_name)
    queue = route.get("queue")
    queue_name = queue.name if hasattr(queue, "name") else str(queue)
    assert queue_name == expected_queue, (
        f"Task {task_name} routed to queue '{queue_name}', expected '{expected_queue}'"
    )
