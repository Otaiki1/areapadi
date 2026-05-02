import pytest
import os, sys
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_USER", "areapadi_user")
os.environ.setdefault("POSTGRES_PASSWORD", "devpassword")
os.environ.setdefault("POSTGRES_DB", "areapadi")
os.environ.setdefault("OPENAI_API_KEY", "test")


@pytest.mark.asyncio
async def test_health():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_search_sellers_empty():
    """Search with no sellers in DB should return empty list."""
    from main import app
    from shared.db import get_db

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

    async def override_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sellers/search", json={
            "lat": 12.0022, "lng": 8.5920, "radius_km": 3, "query": "jollof rice"
        })
    assert resp.status_code == 200
    assert resp.json() == []

    app.dependency_overrides.clear()
