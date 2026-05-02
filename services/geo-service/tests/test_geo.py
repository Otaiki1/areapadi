import pytest
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_USER", "areapadi_user")
os.environ.setdefault("POSTGRES_PASSWORD", "devpassword")
os.environ.setdefault("POSTGRES_DB", "areapadi")

from httpx import AsyncClient, ASGITransport
from main import app, calculate_delivery_fare


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


def test_delivery_fare_short():
    fare = calculate_delivery_fare(2.0)
    assert fare["total_fee"] == 550
    assert fare["rider_payout"] == 550 - round(550 * 0.18)


def test_delivery_fare_medium():
    fare = calculate_delivery_fare(5.0)
    assert fare["total_fee"] == 950


def test_delivery_fare_long():
    fare = calculate_delivery_fare(8.0)
    assert fare["total_fee"] == 950 + 200


def test_delivery_fare_margins():
    fare = calculate_delivery_fare(3.0)
    assert fare["platform_margin"] + fare["rider_payout"] == fare["total_fee"]


@pytest.mark.asyncio
async def test_parse_location_no_api_key():
    """Without an API key, returns coordinate fallback."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/parse-location", json={"lat": 12.0022, "lng": 8.5920})
    assert resp.status_code == 200
    data = resp.json()
    assert "address_text" in data


@pytest.mark.asyncio
async def test_delivery_fee_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/delivery-fee", json={"distance_km": 4.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_fee"] == 950
    assert "rider_payout" in data
    assert "platform_margin" in data


@pytest.mark.asyncio
async def test_calculate_eta_no_api_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/calculate-eta", json={
            "origin_lat": 12.0022, "origin_lng": 8.5920,
            "dest_lat": 12.010, "dest_lng": 8.600,
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "distance_km" in data
    assert "duration_mins" in data
