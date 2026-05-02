import pytest
import os, sys
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_USER", "areapadi_user")
os.environ.setdefault("POSTGRES_PASSWORD", "devpassword")
os.environ.setdefault("POSTGRES_DB", "areapadi")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")


@pytest.mark.asyncio
async def test_health():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


def test_valid_transitions():
    from shared.models import VALID_TRANSITIONS
    assert "confirmed" in VALID_TRANSITIONS["pending"]
    assert "cancelled" in VALID_TRANSITIONS["pending"]
    assert VALID_TRANSITIONS["delivered"] == []
    assert VALID_TRANSITIONS["cancelled"] == []


@pytest.mark.asyncio
async def test_invalid_transition_raises_409():
    from main import app, _validate_transition
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as exc_info:
        _validate_transition("delivered", "confirmed")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_get_order_not_found():
    from main import app
    from shared.db import get_db

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/orders/nonexistent-id")
    assert resp.status_code == 404

    app.dependency_overrides.clear()
