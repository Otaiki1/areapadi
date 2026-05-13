"""
Seller Service — manages seller profiles, menus, and geo+semantic search.
Port 8002.
"""
from __future__ import annotations
import os
import sys
import json
from contextlib import asynccontextmanager
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, func
from sqlalchemy.dialects.postgresql import insert

from shared.db import get_db
from shared.logger import get_logger

from models import Seller, MenuItem
from schemas import (
    CreateSellerRequest, UpdateSellerRequest, AvailabilityRequest,
    SellerResponse, CreateMenuItemRequest, UpdateMenuItemRequest,
    MenuItemResponse, SearchRequest, SearchResultSeller,
)
from embeddings import generate_embedding

logger = get_logger("seller-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("seller_service_starting")
    yield
    logger.info("seller_service_stopped")


app = FastAPI(title="Areapadi Seller Service", version="1.0.0", lifespan=lifespan)


def seller_to_dict(seller: Seller) -> dict:
    return {
        "id": str(seller.id),
        "phone_number": seller.phone_number,
        "business_name": seller.business_name,
        "owner_name": seller.owner_name,
        "food_categories": seller.food_categories or [],
        "address_text": seller.address_text,
        "is_available": seller.is_available,
        "rating": float(seller.rating or 0),
        "total_orders": seller.total_orders or 0,
        "total_reviews": seller.total_reviews or 0,
        "onboarding_complete": seller.onboarding_complete,
        "onboarding_step": seller.onboarding_step,
        "opening_time": seller.opening_time,
        "closing_time": seller.closing_time,
        "operating_days": seller.operating_days or [],
    }


def menu_item_to_dict(item: MenuItem) -> dict:
    return {
        "id": str(item.id),
        "seller_id": str(item.seller_id),
        "name": item.name,
        "description": item.description,
        "price": float(item.price),
        "is_available": item.is_available,
        "image_url": item.image_url,
    }


@app.post("/sellers", status_code=201)
async def create_seller(req: CreateSellerRequest, db: AsyncSession = Depends(get_db)):
    """Create a new seller record during onboarding."""
    existing = await db.execute(
        select(Seller).where(Seller.phone_number == req.phone_number)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Seller with this phone number already exists")

    point = f"SRID=4326;POINT({req.longitude} {req.latitude})"
    seller = Seller(
        phone_number=req.phone_number,
        business_name=req.business_name,
        owner_name=req.owner_name,
        food_categories=req.food_categories,
        location=point,
        address_text=req.address_text,
        opening_time=req.opening_time,
        closing_time=req.closing_time,
        operating_days=req.operating_days or [],
    )
    db.add(seller)
    await db.flush()
    await db.refresh(seller)
    logger.info("seller_created", seller_id=str(seller.id))
    return JSONResponse(seller_to_dict(seller), status_code=201)


@app.get("/sellers/by-phone/{phone_number}")
async def get_seller_by_phone(phone_number: str, db: AsyncSession = Depends(get_db)):
    """Get seller profile by phone number."""
    result = await db.execute(select(Seller).where(Seller.phone_number == phone_number))
    seller = result.scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
    return JSONResponse(seller_to_dict(seller))


@app.get("/sellers/{seller_id}")
async def get_seller(seller_id: str, db: AsyncSession = Depends(get_db)):
    """Get seller profile by ID."""
    result = await db.execute(select(Seller).where(Seller.id == seller_id))
    seller = result.scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
    return JSONResponse(seller_to_dict(seller))


@app.patch("/sellers/{seller_id}")
async def update_seller(seller_id: str, req: UpdateSellerRequest, db: AsyncSession = Depends(get_db)):
    """Update seller profile fields."""
    result = await db.execute(select(Seller).where(Seller.id == seller_id))
    seller = result.scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(seller, field, value)

    await db.flush()
    return JSONResponse(seller_to_dict(seller))


@app.patch("/sellers/{seller_id}/availability")
async def update_availability(seller_id: str, req: AvailabilityRequest, db: AsyncSession = Depends(get_db)):
    """Toggle seller open/closed status."""
    result = await db.execute(select(Seller).where(Seller.id == seller_id))
    seller = result.scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    seller.is_available = req.is_available
    if req.is_available:
        seller.auto_deactivated = False
    await db.flush()
    logger.info("seller_availability_updated", seller_id=seller_id, is_available=req.is_available)
    return JSONResponse({"seller_id": seller_id, "is_available": req.is_available})


@app.get("/sellers/{seller_id}/menu")
async def get_menu(seller_id: str, db: AsyncSession = Depends(get_db)):
    """Get all menu items for a seller."""
    result = await db.execute(
        select(MenuItem).where(
            MenuItem.seller_id == seller_id,
            MenuItem.is_available == True,
        )
    )
    items = result.scalars().all()
    return JSONResponse([menu_item_to_dict(i) for i in items])


@app.post("/sellers/{seller_id}/menu", status_code=201)
async def add_menu_item(
    seller_id: str,
    req: CreateMenuItemRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Add a menu item and generate its embedding in the background."""
    result = await db.execute(select(Seller).where(Seller.id == seller_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Seller not found")

    item = MenuItem(
        seller_id=seller_id,
        name=req.name,
        description=req.description,
        price=req.price,
        image_url=req.image_url,
    )
    db.add(item)
    await db.flush()
    await db.refresh(item)

    item_id = str(item.id)
    embed_text = f"{req.name} {req.description or ''}"
    background_tasks.add_task(_generate_and_store_embedding, item_id, embed_text)

    logger.info("menu_item_added", item_id=item_id, seller_id=seller_id)
    return JSONResponse(menu_item_to_dict(item), status_code=201)


async def _generate_and_store_embedding(item_id: str, text: str) -> None:
    """Generate embedding and update the menu_items row. Background task."""
    try:
        from shared.db import AsyncSessionLocal
        embedding = await generate_embedding(text)
        if embedding is None:
            return  # No API key or call failed — skip silently
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
            item = result.scalar_one_or_none()
            if item:
                item.embedding = embedding
                await session.commit()
    except Exception as exc:
        logger.error("embedding_generation_failed", item_id=item_id, error=str(exc))


@app.patch("/sellers/{seller_id}/menu/{item_id}")
async def update_menu_item(
    seller_id: str,
    item_id: str,
    req: UpdateMenuItemRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update a menu item."""
    result = await db.execute(
        select(MenuItem).where(MenuItem.id == item_id, MenuItem.seller_id == seller_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    await db.flush()
    return JSONResponse(menu_item_to_dict(item))


@app.delete("/sellers/{seller_id}/menu/{item_id}", status_code=204)
async def delete_menu_item(seller_id: str, item_id: str, db: AsyncSession = Depends(get_db)):
    """Remove a menu item."""
    result = await db.execute(
        select(MenuItem).where(MenuItem.id == item_id, MenuItem.seller_id == seller_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    await db.delete(item)


@app.post("/sellers/search")
async def search_sellers(req: SearchRequest, db: AsyncSession = Depends(get_db)):
    """
    Geo + semantic seller search.
    1. PostGIS: find available sellers within radius.
    2. Embed query, cosine-rank menu items per seller.
    3. Score = 0.4×proximity + 0.4×semantic + 0.2×rating. Return top 4.
    """
    radius_m = req.radius_km * 1000
    point_wkt = f"SRID=4326;POINT({req.lng} {req.lat})"

    # Step 1: geo filter
    geo_sql = text("""
        SELECT
            s.id::text,
            s.business_name,
            s.food_categories,
            s.rating,
            s.is_available,
            ST_Distance(s.location, ST_GeogFromText(:point)) AS distance_m
        FROM sellers s
        WHERE s.is_available = TRUE
          AND s.onboarding_complete = TRUE
          AND ST_DWithin(s.location, ST_GeogFromText(:point), :radius_m)
        ORDER BY distance_m ASC
        LIMIT 20
    """)
    geo_result = await db.execute(geo_sql, {"point": point_wkt, "radius_m": radius_m})
    nearby = geo_result.fetchall()

    if not nearby:
        return JSONResponse([])

    seller_ids = [row[0] for row in nearby]
    nearby_map = {row[0]: row for row in nearby}

    # Step 2: semantic scoring via embeddings
    try:
        query_embedding = await generate_embedding(req.query)
        embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        sem_sql = text("""
            SELECT
                mi.seller_id::text,
                MAX(1 - (mi.embedding <=> :embedding::vector)) AS semantic_score,
                array_agg(mi.name ORDER BY mi.price ASC) FILTER (WHERE mi.is_available) AS item_names
            FROM menu_items mi
            WHERE mi.seller_id::text = ANY(:seller_ids)
              AND mi.embedding IS NOT NULL
            GROUP BY mi.seller_id
        """)
        sem_result = await db.execute(
            sem_sql,
            {"embedding": embedding_str, "seller_ids": seller_ids},
        )
        semantic_map = {row[0]: {"score": float(row[1] or 0), "items": row[2] or []} for row in sem_result.fetchall()}
    except Exception as exc:
        logger.warning("semantic_search_failed", error=str(exc))
        semantic_map = {}

    # Step 3: also pull item names for sellers without embeddings
    items_sql = text("""
        SELECT seller_id::text, array_agg(name ORDER BY price ASC) AS item_names
        FROM menu_items
        WHERE seller_id::text = ANY(:seller_ids) AND is_available = TRUE
        GROUP BY seller_id
    """)
    items_result = await db.execute(items_sql, {"seller_ids": seller_ids})
    items_map = {row[0]: row[1] or [] for row in items_result.fetchall()}

    # Step 4: rank and build response
    max_dist = max((row[5] for row in nearby), default=1)

    scored = []
    for row in nearby:
        sid, biz_name, categories, rating, is_avail, dist_m = row
        proximity = 1.0 - (dist_m / max_dist)
        semantic = semantic_map.get(sid, {}).get("score", 0.0)
        rat = float(rating or 0) / 5.0
        composite = 0.4 * proximity + 0.4 * semantic + 0.2 * rat

        sample_items = (semantic_map.get(sid, {}).get("items") or items_map.get(sid, []))[:3]
        dist_text = f"{dist_m / 1000:.1f}km" if dist_m >= 1000 else f"{int(dist_m)}m"

        scored.append({
            "id": sid,
            "business_name": biz_name,
            "rating": float(rating or 0),
            "food_categories": categories or [],
            "distance_text": dist_text,
            "distance_m": dist_m,
            "sample_items": sample_items,
            "is_available": is_avail,
            "_score": composite,
        })

    scored.sort(key=lambda x: x["_score"], reverse=True)
    for s in scored:
        del s["_score"]

    return JSONResponse(scored[:4])


@app.get("/health")
async def health():
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "seller-service"})
