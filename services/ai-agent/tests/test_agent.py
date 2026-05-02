"""
AI Agent tests — covers routing logic, stage transitions, and Claude JSON helpers
without hitting real APIs (Anthropic, WhatsApp, Paystack, DB are all mocked).
"""
from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app
from shared.models import ConversationState, MessagePayload


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def buyer_state() -> ConversationState:
    return ConversationState(
        phone_number="2348012345678",
        user_role="buyer",
        stage="idle",
        location_lat=12.0022,
        location_lng=8.5920,
    )


@pytest.fixture
def new_user_message() -> MessagePayload:
    return MessagePayload(
        phone_number="2348099999999",
        message_type="text",
        text="Hello I want to order food",
        timestamp=1700000000,
    )


@pytest.fixture
def location_message() -> MessagePayload:
    return MessagePayload(
        phone_number="2348012345678",
        message_type="location",
        location_lat=12.0022,
        location_lng=8.5920,
        timestamp=1700000000,
    )


# ── Health check ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ── /handle endpoint ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_returns_200_immediately():
    """Gateway must get 200 immediately — actual processing runs in background."""
    with patch("main.route_message", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/handle",
                json={
                    "phone_number": "2348012345678",
                    "message_type": "text",
                    "text": "I want jollof rice",
                },
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_location_message():
    with patch("main.route_message", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/handle",
                json={
                    "phone_number": "2348012345678",
                    "message_type": "location",
                    "location_lat": 12.0022,
                    "location_lng": 8.5920,
                },
            )
    assert resp.status_code == 200


# ── /prompt-rating endpoint ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prompt_rating_queues_task():
    with patch("main.send_rating_prompt", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/prompt-rating", json={"order_id": "abc-123"})
    assert resp.status_code == 200


# ── claude_client helpers ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_claude_json_strips_fence():
    from claude_client import _strip_fence
    raw = "```json\n{\"role\": \"buyer\"}\n```"
    assert _strip_fence(raw) == '{"role": "buyer"}'


def test_strip_fence_plain_json():
    from claude_client import _strip_fence
    raw = '{"intent": "food_search"}'
    assert _strip_fence(raw) == '{"intent": "food_search"}'


# ── Buyer stage: awaiting_location ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_buyer_awaiting_location_non_location_message():
    """Non-location messages at awaiting_location stage should re-request location."""
    state = ConversationState(
        phone_number="2348011111111",
        user_role="buyer",
        stage="awaiting_location",
    )
    message = MessagePayload(
        phone_number="2348011111111",
        message_type="text",
        text="hello",
    )
    wa_mock = AsyncMock()
    wa_mock.send_location_request = AsyncMock(return_value=True)

    with (
        patch("handlers.buyer.get_whatsapp_client", return_value=wa_mock),
        patch("handlers.buyer.save_conversation_state", new_callable=AsyncMock),
    ):
        from handlers.buyer import handle_buyer_message
        await handle_buyer_message(state, message)

    wa_mock.send_location_request.assert_awaited_once()


# ── Buyer stage: idle ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buyer_idle_food_search_calls_seller_service():
    """At idle stage, a food query should trigger seller search."""
    state = ConversationState(
        phone_number="2348011111111",
        user_role="buyer",
        stage="idle",
        location_lat=12.0022,
        location_lng=8.5920,
    )
    message = MessagePayload(
        phone_number="2348011111111",
        message_type="text",
        text="I want jollof rice",
    )
    wa_mock = AsyncMock()
    wa_mock.send_text = AsyncMock(return_value=True)
    wa_mock.send_interactive_list = AsyncMock(return_value=True)

    claude_result = {"intent": "food_search", "food_query": "jollof rice", "reply_text": "Searching..."}
    sellers_result = [
        {
            "id": "seller-uuid-1",
            "business_name": "Mama Nkechi",
            "rating": 4.5,
            "food_categories": ["rice"],
            "distance_text": "1.2km",
            "distance_m": 1200,
            "sample_items": ["Jollof Rice", "Fried Rice"],
            "is_available": True,
        }
    ]

    with (
        patch("handlers.buyer.get_whatsapp_client", return_value=wa_mock),
        patch("handlers.buyer.save_conversation_state", new_callable=AsyncMock),
        patch("handlers.buyer.call_claude_json", new_callable=AsyncMock, return_value=claude_result),
        patch("handlers.buyer.httpx.AsyncClient") as mock_http,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sellers_result
        mock_http.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        from handlers.buyer import handle_buyer_message
        await handle_buyer_message(state, message)

    wa_mock.send_interactive_list.assert_awaited_once()
    assert state.stage == "browsing"


# ── ConversationState serialisation ──────────────────────────────────────────

def test_conversation_state_roundtrip():
    state = ConversationState(
        phone_number="2348099999999",
        user_role="buyer",
        stage="idle",
        location_lat=12.0,
        location_lng=8.5,
        pending_items=[{"name": "Jollof Rice", "quantity": 2, "unit_price": 1500, "subtotal": 3000}],
    )
    raw = state.model_dump_json()
    restored = ConversationState(**json.loads(raw))
    assert restored.phone_number == state.phone_number
    assert restored.pending_items[0]["name"] == "Jollof Rice"


def test_valid_transitions_enforced():
    from shared.models import VALID_TRANSITIONS
    assert "confirmed" in VALID_TRANSITIONS["pending"]
    assert "delivered" not in VALID_TRANSITIONS["pending"]
    assert VALID_TRANSITIONS["delivered"] == []
