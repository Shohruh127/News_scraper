"""Redis handoff for Telegram channel auto-forward correlation."""

import time

import redis
from django.conf import settings


def forward_key(channel_id: str | int, channel_message_id: int) -> str:
    """Return the namespaced key for one channel post's linked-group forward."""
    return f"news_radar:telegram_forward:{channel_id}:{channel_message_id}"


def _client(client=None):
    if client is not None:
        return client, False
    return redis.Redis.from_url(settings.CELERY_BROKER_URL), True


def remember_group_forward(
    channel_id: str | int,
    channel_message_id: int,
    forward_message_id: int,
    *,
    client=None,
    ttl: int | None = None,
) -> None:
    """Store a validated automatic-forward ID for the publisher to consume."""
    redis_client, owns_client = _client(client)
    try:
        redis_client.set(
            forward_key(channel_id, channel_message_id),
            str(forward_message_id),
            ex=ttl if ttl is not None else settings.TELEGRAM_FORWARD_TTL,
        )
    finally:
        if owns_client:
            redis_client.close()


def wait_for_group_forward(
    channel_id: str | int,
    channel_message_id: int,
    *,
    client=None,
    max_retries: int = 4,
    retry_delay: float = 1.5,
) -> int | None:
    """Poll Redis briefly for the bot's automatic-forward handoff."""
    redis_client, owns_client = _client(client)
    try:
        key = forward_key(channel_id, channel_message_id)
        for attempt in range(max_retries):
            value = redis_client.get(key)
            if value is not None:
                if isinstance(value, bytes):
                    value = value.decode()
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
        return None
    finally:
        if owns_client:
            redis_client.close()
