from __future__ import annotations
import os
import json
from typing import Any
import redis.asyncio as aioredis
from shared.models import ConversationState


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STATE_TTL = 86400  # 24 hours — WhatsApp session window

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the shared async Redis connection pool."""
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _pool


async def get_conversation_state(phone_number: str) -> ConversationState:
    """Load conversation state from Redis, returning a new state if not found."""
    r = get_redis()
    key = f"conv:{phone_number}"
    data = await r.get(key)
    if data:
        return ConversationState(**json.loads(data))
    return ConversationState(phone_number=phone_number)


async def save_conversation_state(state: ConversationState) -> None:
    """Persist conversation state to Redis with 24h TTL."""
    r = get_redis()
    key = f"conv:{state.phone_number}"
    await r.setex(key, STATE_TTL, state.model_dump_json())


async def delete_conversation_state(phone_number: str) -> None:
    """Remove conversation state (e.g. on explicit reset)."""
    r = get_redis()
    await r.delete(f"conv:{phone_number}")
