import json
import hashlib
import hmac
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_WEBHOOK_SECRET", "test_secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from main import app


def make_signature(payload: bytes, secret: str = "test_secret") -> str:
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def text_payload(phone: str, text: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "type": "text",
                        "timestamp": "1700000000",
                        "text": {"body": text},
                    }],
                    "contacts": [{"profile": {"name": "Test User"}}],
                }
            }]
        }]
    }


def location_payload(phone: str, lat: float, lng: float) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "type": "location",
                        "timestamp": "1700000000",
                        "location": {"latitude": lat, "longitude": lng},
                    }],
                    "contacts": [{"profile": {"name": "Test User"}}],
                }
            }]
        }]
    }


@pytest.mark.asyncio
async def test_verify_webhook():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test_token",
            "hub.challenge": "abc123",
        })
    assert resp.status_code == 200
    assert resp.text == "abc123"


@pytest.mark.asyncio
async def test_verify_webhook_wrong_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "abc123",
        })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_receive_text_message():
    payload = text_payload("2348012345678", "abeg who dey sell jollof near me")
    body = json.dumps(payload).encode()
    sig = make_signature(body)

    with patch("main.forward_to_ai_agent", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_receive_location_message():
    payload = location_payload("2348012345678", 12.0022, 8.5920)
    body = json.dumps(payload).encode()
    sig = make_signature(body)

    with patch("main.forward_to_ai_agent", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
            )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_receive_invalid_signature():
    payload = text_payload("2348012345678", "hello")
    body = json.dumps(payload).encode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=invalidsig", "Content-Type": "application/json"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_status_update_ignored():
    """Status updates (no messages key) must return 200 silently."""
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    body = json.dumps(payload).encode()
    sig = make_signature(body)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
