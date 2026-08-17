"""Tests for T1.15 carried defects: lock handling, heartbeat, model digest cache."""

from io import StringIO
from unittest.mock import patch

from apps.digest import llm
from apps.digest.management.commands.run_pipeline import Command


def test_run_pipeline_handles_lock_held_without_keyerror():
    """T1.15 defect 1: a held lock produces a clean skip rather than KeyError."""
    cmd = Command(stdout=StringIO(), stderr=StringIO())

    # Mock triage_and_classify to return the lock_held shape
    with patch("apps.digest.tasks.triage_and_classify") as mock_tc:
        mock_tc.return_value = {"status": "skipped", "reason": "lock_held"}

        # This should NOT raise KeyError
        cmd.handle(date="today", skip_fetch=True)

        output = cmd.stdout.getvalue()
        assert "Skipped" in output or "skipped" in output.lower()


def test_model_digest_cache_issues_one_api_tags_call(settings):
    """T1.15 defect 3: fetch_model_digest calls /api/tags once per model, not N times."""
    settings.OLLAMA_BASE_URL = "http://localhost:11434"

    # Clear the cache before this test
    llm._model_digest_cache.clear()

    call_count = 0

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {
                        "name": "gemma4:31b",
                        "model": "gemma4:31b",
                        "digest": "sha256:abcdef1234567890",
                    }
                ]
            }

    class FakeClient:
        def get(self, url, timeout=None):
            nonlocal call_count
            call_count += 1
            return FakeResponse()

    client = FakeClient()

    # First call — should hit /api/tags
    d1 = llm.fetch_model_digest("gemma4:31b", client=client)
    assert d1 == "sha256:abcdef1234567890"
    assert call_count == 1

    # Second call — should use cache, no network call
    d2 = llm.fetch_model_digest("gemma4:31b", client=client)
    assert d2 == "sha256:abcdef1234567890"
    assert call_count == 1, "Expected cache hit, but /api/tags was called again"

    # Third call — still cached
    d3 = llm.fetch_model_digest("gemma4:31b", client=client)
    assert d3 == "sha256:abcdef1234567890"
    assert call_count == 1

    # Different model — should call /api/tags once more
    llm.fetch_model_digest("gemma4:latest", client=client)
    assert call_count == 2

    # Clean up
    llm._model_digest_cache.clear()
