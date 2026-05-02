import pytest
import json
import hmac
import hashlib
import os, sys
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("PAYSTACK_SECRET_KEY", "test_paystack_secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("ORDER_SERVICE_URL", "http://localhost:8003")

from main import app, verify_paystack_signature


def make_paystack_sig(payload: bytes, secret: str = "test_paystack_secret") -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()


def test_verify_paystack_signature_valid():
    payload = b'{"event":"charge.success"}'
    sig = make_paystack_sig(payload)
    assert verify_paystack_signature(payload, sig) is True


def test_verify_paystack_signature_invalid():
    payload = b'{"event":"charge.success"}'
    assert verify_paystack_signature(payload, "invalidsig") is False


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_invalid_signature():
    payload = json.dumps({"event": "charge.success", "data": {}}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            content=payload,
            headers={"x-paystack-signature": "bad", "Content-Type": "application/json"},
        )
    # Returns 200 even for bad sig (to stop Paystack retries) but does nothing
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_charge_success():
    data = {
        "event": "charge.success",
        "data": {
            "reference": "AREAPADI-ABC12345",
            "metadata": {"order_id": "order-uuid-1234", "buyer_phone": "2348012345678"},
        },
    }
    payload = json.dumps(data).encode()
    sig = make_paystack_sig(payload)

    with patch("main._handle_charge_success", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook",
                content=payload,
                headers={"x-paystack-signature": sig, "Content-Type": "application/json"},
            )
    assert resp.status_code == 200
