"""
Rider Dispatch Service — finds the nearest available rider and offers them a job.
Port 8004.

Flow:
  1. Order Service → POST /dispatch  (food is marked ready)
  2. Query riders near seller location, ranked by distance × rating
  3. Send WhatsApp offer to top rider (accept_job / decline_job buttons)
  4. APScheduler timeout at 3 minutes → try next rider
  5. AI Agent rider handler → POST /accept  (rider tapped Accept)
  6. Assign rider to order, update status → rider_assigned, notify buyer
"""
from __future__ import annotations
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text as sa_text

from shared.db import AsyncSessionLocal
from shared.whatsapp_client import get_whatsapp_client
from shared.logger import get_logger

logger = get_logger("rider-dispatch")

ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003")
SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://localhost:8002")
AI_AGENT_URL = os.getenv("AI_AGENT_URL", "http://localhost:8001")

JOB_TIMEOUT_SECS = 180  # 3 minutes per rider before trying the next

scheduler = AsyncIOScheduler()

# In-memory dispatch state: order_id → {candidates, tried_phones, status}
_dispatch: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    logger.info("rider_dispatch_starting")
    yield
    scheduler.shutdown(wait=False)
    logger.info("rider_dispatch_stopped")


app = FastAPI(title="Areapadi Rider Dispatch", version="1.0.0", lifespan=lifespan)


class DispatchRequest(BaseModel):
    order_id: str


class AcceptRequest(BaseModel):
    order_id: str
    rider_phone: str


class DeclineRequest(BaseModel):
    order_id: str
    rider_phone: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/dispatch")
async def dispatch(req: DispatchRequest) -> JSONResponse:
    """
    Called by Order Service when food is marked ready.
    Finds the nearest available riders and offers the job to the best candidate.
    """
    order_id = req.order_id

    # Don't double-dispatch
    if order_id in _dispatch and _dispatch[order_id]["status"] == "pending":
        logger.info("dispatch_already_active", order_id=order_id)
        return JSONResponse({"status": "already_active"})

    order = await _get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    seller_id = order.get("seller_id")
    seller = await _get_seller(seller_id)
    if not seller:
        logger.error("dispatch_seller_not_found", order_id=order_id, seller_id=seller_id)
        await _notify_buyer_delay(order_id)
        raise HTTPException(status_code=404, detail="Seller not found")

    riders = await _find_nearby_riders(seller)
    if not riders:
        logger.warning("no_riders_available", order_id=order_id)
        await _notify_buyer_delay(order_id)
        return JSONResponse({"status": "no_riders"})

    _dispatch[order_id] = {
        "candidates": riders,
        "tried_phones": [],
        "status": "pending",
        "order": order,
        "seller": seller,
    }

    await _offer_to_next_rider(order_id)
    return JSONResponse({"status": "dispatching"})


@app.post("/accept")
async def accept(req: AcceptRequest) -> JSONResponse:
    """
    Called by AI Agent when a rider accepts the job.
    Assigns rider to order, transitions to rider_assigned, notifies buyer.
    """
    order_id = req.order_id
    rider_phone = req.rider_phone

    state = _dispatch.get(order_id)
    if not state or state["status"] != "pending":
        raise HTTPException(status_code=409, detail="Dispatch not active for this order")

    # Cancel the timeout job
    job_id = f"timeout_{order_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    rider = await _get_rider_by_phone(rider_phone)
    if not rider:
        logger.error("accept_rider_not_found", phone=rider_phone[-4:])
        raise HTTPException(status_code=404, detail="Rider not found")

    rider_id = rider["id"]
    rider_name = rider.get("full_name", "Your rider")

    success = await _assign_rider_to_order(order_id, rider_id)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to assign rider to order")

    _dispatch[order_id]["status"] = "assigned"

    # Mark rider as unavailable
    await _set_rider_availability(rider_id, False)

    # Notify buyer
    await _notify_buyer(order_id, "rider_assigned", rider_name=rider_name)

    logger.info("rider_assigned", order_id=order_id, rider_id=rider_id)
    return JSONResponse({"status": "assigned", "rider_id": rider_id})


@app.post("/decline")
async def decline(req: DeclineRequest) -> JSONResponse:
    """
    Called by AI Agent when a rider declines. Tries the next candidate.
    """
    order_id = req.order_id
    state = _dispatch.get(order_id)
    if not state or state["status"] != "pending":
        return JSONResponse({"status": "not_active"})

    # Cancel current timeout and try next
    job_id = f"timeout_{order_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await _offer_to_next_rider(order_id)
    return JSONResponse({"status": "trying_next"})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "rider-dispatch"})


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _offer_to_next_rider(order_id: str) -> None:
    """Send a job offer to the next untried candidate. If none left, notify buyer of delay."""
    state = _dispatch.get(order_id)
    if not state:
        return

    tried = set(state["tried_phones"])
    remaining = [r for r in state["candidates"] if r["phone_number"] not in tried]

    if not remaining:
        logger.warning("all_riders_exhausted", order_id=order_id)
        _dispatch[order_id]["status"] = "failed"
        await _notify_buyer_delay(order_id)
        return

    rider = remaining[0]
    phone = rider["phone_number"]
    state["tried_phones"].append(phone)
    state["current_rider_phone"] = phone

    wa = get_whatsapp_client()
    order = state["order"]
    seller = state["seller"]
    total = float(order.get("total_amount", 0))
    items = order.get("items", [])
    summary = _format_items(items)

    await wa.send_interactive_buttons(
        phone,
        f"New delivery job!\n\n"
        f"From: {seller.get('business_name', 'Seller')}\n"
        f"{summary}\n"
        f"Order value: ₦{total:,.0f}\n\n"
        "Do you accept this delivery?",
        buttons=[
            {"id": f"accept_job_{order_id}", "title": "Accept"},
            {"id": f"decline_job_{order_id}", "title": "Decline"},
        ],
    )
    logger.info("job_offered", order_id=order_id, rider_phone=phone[-4:])

    # Schedule timeout
    run_at = datetime.now(timezone.utc) + timedelta(seconds=JOB_TIMEOUT_SECS)
    scheduler.add_job(
        _rider_timeout,
        "date",
        run_date=run_at,
        args=[order_id, phone],
        id=f"timeout_{order_id}",
        replace_existing=True,
    )


async def _rider_timeout(order_id: str, rider_phone: str) -> None:
    """Called when a rider didn't respond in time. Offers to the next candidate."""
    logger.warning("rider_timeout", order_id=order_id, rider_phone=rider_phone[-4:])
    wa = get_whatsapp_client()
    await wa.send_text(rider_phone, "Job offer expired — it has been assigned to another rider.")
    await _offer_to_next_rider(order_id)


async def _assign_rider_to_order(order_id: str, rider_id: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{ORDER_SERVICE_URL}/orders/{order_id}/assign-rider",
                json={"rider_id": rider_id},
            )
            return resp.status_code == 200
    except Exception as exc:
        logger.error("assign_rider_failed", error=str(exc), order_id=order_id)
        return False


async def _notify_buyer(order_id: str, status: str, rider_name: str | None = None) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{AI_AGENT_URL}/notify-buyer",
                json={"order_id": order_id, "status": status, "rider_name": rider_name},
            )
    except Exception as exc:
        logger.error("notify_buyer_failed", error=str(exc))


async def _notify_buyer_delay(order_id: str) -> None:
    await _notify_buyer(order_id, "no_rider")


async def _get_order(order_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.error("get_order_failed", error=str(exc))
    return None


async def _get_seller(seller_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.error("get_seller_failed", error=str(exc))
    return None


async def _find_nearby_riders(seller: dict) -> list[dict]:
    """
    Query DB for available riders near the seller location.
    Ranks by distance ASC, rating_score DESC. Returns up to 5 candidates.
    """
    # Seller location comes back as address_text; we need the coordinates.
    # The seller's location is stored in PostGIS — we query it from the DB.
    seller_id = seller.get("id")
    try:
        async with AsyncSessionLocal() as session:
            sql = sa_text("""
                SELECT
                    r.id::text,
                    r.phone_number,
                    r.full_name,
                    r.vehicle_type,
                    r.rating_score,
                    ST_Distance(
                        r.current_location,
                        s.location
                    ) AS distance_m
                FROM riders r
                CROSS JOIN sellers s
                WHERE s.id = :seller_id
                  AND r.is_available = TRUE
                  AND r.is_suspended = FALSE
                  AND r.onboarding_complete = TRUE
                  AND r.current_location IS NOT NULL
                  AND ST_DWithin(r.current_location, s.location, 10000)
                ORDER BY distance_m ASC, r.rating_score DESC
                LIMIT 5
            """)
            result = await session.execute(sql, {"seller_id": seller_id})
            rows = result.fetchall()
            return [
                {
                    "id": row[0],
                    "phone_number": row[1],
                    "full_name": row[2],
                    "vehicle_type": row[3],
                    "rating_score": float(row[4] or 50),
                    "distance_m": float(row[5] or 0),
                }
                for row in rows
            ]
    except Exception as exc:
        logger.error("find_riders_failed", error=str(exc))
        return []


async def _get_rider_by_phone(phone: str) -> dict | None:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("SELECT id::text, full_name FROM riders WHERE phone_number = :phone LIMIT 1"),
                {"phone": phone},
            )
            row = result.fetchone()
            if row:
                return {"id": row[0], "full_name": row[1]}
    except Exception as exc:
        logger.error("get_rider_by_phone_failed", error=str(exc))
    return None


async def _set_rider_availability(rider_id: str, available: bool) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("UPDATE riders SET is_available = :avail WHERE id = :id"),
                {"avail": available, "id": rider_id},
            )
            await session.commit()
    except Exception as exc:
        logger.error("set_rider_availability_failed", error=str(exc))


def _format_items(items: list[dict]) -> str:
    lines = []
    for it in items[:5]:
        qty = it.get("quantity", 1)
        name = it.get("name", "Item")
        lines.append(f"• {name} ×{qty}")
    return "\n".join(lines) if lines else "• Items"
