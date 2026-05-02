"""
Geo Service — location parsing, seller radius search, ETA, delivery fee.
Port 8006.
"""
from __future__ import annotations
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.logger import get_logger

logger = get_logger("geo-service")

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("geo_service_starting")
    yield


app = FastAPI(title="Areapadi Geo Service", version="1.0.0", lifespan=lifespan)


class ParseLocationRequest(BaseModel):
    lat: float
    lng: float


class SearchSellersRequest(BaseModel):
    lat: float
    lng: float
    radius_km: float = Field(default=3.0, ge=0.5, le=20.0)
    limit: int = Field(default=10, ge=1, le=50)


class EtaRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    mode: str = "driving"


class DeliveryFeeRequest(BaseModel):
    distance_km: float


def calculate_delivery_fare(distance_km: float) -> dict:
    """
    Areapadi delivery fare schedule.
    N400-700 (midpoint N550) for <=3km, N700-1200 (midpoint N950) for <=6km,
    N950 + N100/km beyond 6km. Platform takes 18%.
    """
    if distance_km <= 3:
        total_fee = 550
    elif distance_km <= 6:
        total_fee = 950
    else:
        total_fee = 950 + ((distance_km - 6) * 100)

    total_fee = round(total_fee)
    platform_margin = round(total_fee * 0.18)
    rider_payout = total_fee - platform_margin
    return {
        "total_fee": total_fee,
        "rider_payout": rider_payout,
        "platform_margin": platform_margin,
        "distance_km": round(distance_km, 2),
    }


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance between two points in km."""
    import math
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def format_distance(distance_m: float) -> str:
    if distance_m < 1000:
        return f"{int(distance_m)}m"
    return f"{distance_m / 1000:.1f}km"


@app.post("/parse-location")
async def parse_location(req: ParseLocationRequest):
    """
    Reverse-geocode lat/lng to a human-readable address using Google Maps.
    Falls back to coordinate string if Google Maps key is not set.
    """
    if not GOOGLE_MAPS_API_KEY:
        return JSONResponse({
            "address_text": f"{req.lat:.4f}, {req.lng:.4f}",
            "area_name": "Unknown area",
            "city": "Unknown city",
        })

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={
                    "latlng": f"{req.lat},{req.lng}",
                    "key": GOOGLE_MAPS_API_KEY,
                    "result_type": "sublocality|locality",
                },
            )
            data = resp.json()

        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            address_text = result.get("formatted_address", "")
            area_name = ""
            city = ""
            for comp in result.get("address_components", []):
                types = comp.get("types", [])
                if "sublocality" in types or "neighborhood" in types:
                    area_name = comp["long_name"]
                if "locality" in types:
                    city = comp["long_name"]
            return JSONResponse({
                "address_text": address_text,
                "area_name": area_name or address_text.split(",")[0],
                "city": city,
            })
    except Exception as exc:
        logger.error("geocode_failed", error=str(exc))

    return JSONResponse({
        "address_text": f"{req.lat:.4f}, {req.lng:.4f}",
        "area_name": "Nearby area",
        "city": "Nigeria",
    })


@app.post("/search-sellers")
async def search_sellers(req: SearchSellersRequest):
    """
    Find seller IDs within radius using PostGIS ST_DWithin.
    Returns [{seller_id, distance_m, distance_text}] sorted by distance.
    NOTE: actual geo+semantic ranking is done in seller-service /sellers/search.
    This endpoint returns raw proximity data for internal use.
    """
    from shared.db import AsyncSessionLocal
    from sqlalchemy import text

    radius_m = req.radius_km * 1000
    point_wkt = f"SRID=4326;POINT({req.lng} {req.lat})"

    sql = text("""
        SELECT
            id::text,
            ST_Distance(location, ST_GeogFromText(:point)) AS distance_m
        FROM sellers
        WHERE is_available = TRUE
          AND onboarding_complete = TRUE
          AND ST_DWithin(location, ST_GeogFromText(:point), :radius_m)
        ORDER BY distance_m ASC
        LIMIT :limit
    """)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sql, {"point": point_wkt, "radius_m": radius_m, "limit": req.limit}
            )
            rows = result.fetchall()

        return JSONResponse([
            {
                "seller_id": row[0],
                "distance_m": round(row[1], 1),
                "distance_text": format_distance(row[1]),
            }
            for row in rows
        ])
    except Exception as exc:
        logger.error("search_sellers_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Geo search failed")


@app.post("/calculate-eta")
async def calculate_eta(req: EtaRequest):
    """
    Get ETA and distance between two points using Google Maps Distance Matrix.
    Falls back to haversine estimate if API key not set.
    """
    if not GOOGLE_MAPS_API_KEY:
        dist_km = haversine_km(req.origin_lat, req.origin_lng, req.dest_lat, req.dest_lng)
        avg_speed_kmh = 25
        duration_mins = int((dist_km / avg_speed_kmh) * 60)
        return JSONResponse({
            "distance_km": round(dist_km, 2),
            "duration_mins": max(duration_mins, 5),
            "duration_text": f"{max(duration_mins, 5)} mins",
        })

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={
                    "origins": f"{req.origin_lat},{req.origin_lng}",
                    "destinations": f"{req.dest_lat},{req.dest_lng}",
                    "mode": req.mode,
                    "key": GOOGLE_MAPS_API_KEY,
                },
            )
            data = resp.json()

        element = data["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist_m = element["distance"]["value"]
            dur_s = element["duration"]["value"]
            return JSONResponse({
                "distance_km": round(dist_m / 1000, 2),
                "duration_mins": max(int(dur_s / 60), 1),
                "duration_text": element["duration"]["text"],
            })
    except Exception as exc:
        logger.error("eta_calculation_failed", error=str(exc))

    dist_km = haversine_km(req.origin_lat, req.origin_lng, req.dest_lat, req.dest_lng)
    duration_mins = int((dist_km / 25) * 60)
    return JSONResponse({
        "distance_km": round(dist_km, 2),
        "duration_mins": max(duration_mins, 5),
        "duration_text": f"{max(duration_mins, 5)} mins",
    })


@app.post("/delivery-fee")
async def delivery_fee(req: DeliveryFeeRequest):
    """Calculate delivery fare breakdown for a given distance."""
    return JSONResponse(calculate_delivery_fare(req.distance_km))


@app.get("/health")
async def health():
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "geo-service"})
