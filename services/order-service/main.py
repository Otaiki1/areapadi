"""
Order Service — manages the full order lifecycle from creation to delivery.
Enforces strict status transitions and auto-deactivates sellers who ignore 3+ orders.
Port 8003.
"""
from __future__ import annotations
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.db import get_db, AsyncSessionLocal
from shared.logger import get_logger, hash_phone
from shared.whatsapp_client import get_whatsapp_client
from shared.models import VALID_TRANSITIONS

from models import Order
from schemas import (
    CreateOrderRequest, UpdateStatusRequest, PaymentUpdateRequest,
    CancelOrderRequest, RateOrderRequest,
)

logger = get_logger("order-service")

RIDER_DISPATCH_URL = os.getenv("RIDER_DISPATCH_URL", "http://localhost:8004")
AI_AGENT_URL = os.getenv("AI_AGENT_URL", "http://localhost:8001")
SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://localhost:8002")

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    logger.info("order_service_starting")
    yield
    scheduler.shutdown(wait=False)
    logger.info("order_service_stopped")


app = FastAPI(title="Areapadi Order Service", version="1.0.0", lifespan=lifespan)


def order_to_dict(order: Order) -> dict:
    return {
        "id": str(order.id),
        "buyer_id": str(order.buyer_id) if order.buyer_id else None,
        "seller_id": str(order.seller_id) if order.seller_id else None,
        "rider_id": str(order.rider_id) if order.rider_id else None,
        "status": order.status,
        "items": order.items,
        "subtotal": float(order.subtotal),
        "delivery_fee": float(order.delivery_fee),
        "platform_commission": float(order.platform_commission or 0),
        "total_amount": float(order.total_amount),
        "payment_status": order.payment_status,
        "paystack_reference": order.paystack_reference,
        "delivery_address": order.delivery_address,
        "buyer_notes": order.buyer_notes,
        "buyer_food_rating": order.buyer_food_rating,
        "buyer_delivery_rating": order.buyer_delivery_rating,
        "ignored_by_seller_count": order.ignored_by_seller_count or 0,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


def _validate_transition(current: str, target: str) -> None:
    """Raise HTTP 409 if the status transition is not allowed."""
    allowed = VALID_TRANSITIONS.get(current, [])
    if target not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition from '{current}' to '{target}'. Allowed: {allowed}",
        )


@app.post("/orders", status_code=201)
async def create_order(req: CreateOrderRequest, db: AsyncSession = Depends(get_db)):
    """
    Create a new order in 'pending' status.
    Schedules a 10-minute check for seller no-show protection.
    """
    delivery_location = None
    if req.delivery_lat and req.delivery_lng:
        delivery_location = f"SRID=4326;POINT({req.delivery_lng} {req.delivery_lat})"

    order = Order(
        buyer_id=uuid.UUID(req.buyer_id),
        seller_id=uuid.UUID(req.seller_id),
        items=[item.model_dump() for item in req.items],
        subtotal=req.subtotal,
        delivery_fee=req.delivery_fee,
        platform_commission=req.platform_commission,
        platform_delivery_margin=req.platform_delivery_margin,
        total_amount=req.total_amount,
        delivery_address=req.delivery_address,
        delivery_location=delivery_location,
        buyer_notes=req.buyer_notes,
    )
    db.add(order)
    await db.flush()
    await db.refresh(order)

    order_id = str(order.id)
    seller_id = req.seller_id

    # Schedule seller no-show check in 10 minutes
    from datetime import timedelta
    run_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    scheduler.add_job(
        _check_seller_noshow,
        "date",
        run_date=run_time,
        args=[order_id, seller_id],
        id=f"noshow_{order_id}",
        replace_existing=True,
    )

    logger.info("order_created", order_id=order_id, seller_id=seller_id)
    return JSONResponse(order_to_dict(order), status_code=201)


async def _check_seller_noshow(order_id: str, seller_id: str) -> None:
    """
    10-minute post-creation check. If order is still 'pending', the seller has
    not confirmed. Increment their ignored count and auto-deactivate at 3.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order or order.status != "pending":
            return

        order.ignored_by_seller_count = (order.ignored_by_seller_count or 0) + 1
        await session.commit()

        ignored_count = order.ignored_by_seller_count
        logger.warning("seller_noshow", order_id=order_id, seller_id=seller_id, count=ignored_count)

        if ignored_count >= 3:
            await _auto_deactivate_seller(seller_id)


async def _auto_deactivate_seller(seller_id: str) -> None:
    """Deactivate a seller who has ignored 3+ orders and notify them."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}",
                json={"auto_deactivated": True},
            )
            await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/availability",
                json={"is_available": False},
            )

        seller_resp = await httpx.AsyncClient(timeout=10.0).get(
            f"{SELLER_SERVICE_URL}/sellers/{seller_id}"
        )
        seller = seller_resp.json() if seller_resp.status_code == 200 else {}
        phone = seller.get("phone_number", "")

        if phone:
            wa = get_whatsapp_client()
            await wa.send_text(
                phone,
                "Your store has been temporarily paused because 3 orders were not confirmed. "
                "Reply 'open' to reactivate when you are ready to take orders again.",
            )
    except Exception as exc:
        logger.error("auto_deactivate_seller_failed", seller_id=seller_id, error=str(exc))


@app.get("/orders/{order_id}")
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    """Get order details by ID."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(order_to_dict(order))


@app.patch("/orders/{order_id}/status")
async def update_status(order_id: str, req: UpdateStatusRequest, db: AsyncSession = Depends(get_db)):
    """Update order status with transition validation."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, req.status)
    order.status = req.status
    await db.flush()
    logger.info("order_status_updated", order_id=order_id, new_status=req.status)
    return JSONResponse(order_to_dict(order))


@app.patch("/orders/{order_id}/payment")
async def update_payment(order_id: str, req: PaymentUpdateRequest, db: AsyncSession = Depends(get_db)):
    """Update payment status after Paystack webhook confirmation."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.payment_status = req.payment_status
    if req.paystack_reference:
        order.paystack_reference = req.paystack_reference
    await db.flush()
    logger.info("payment_status_updated", order_id=order_id, status=req.payment_status)
    return JSONResponse(order_to_dict(order))


@app.post("/orders/{order_id}/confirm-seller")
async def seller_confirm(order_id: str, db: AsyncSession = Depends(get_db)):
    """Seller confirmed the order. Transitions pending -> confirmed."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, "confirmed")
    order.status = "confirmed"
    await db.flush()
    logger.info("order_confirmed_by_seller", order_id=order_id)
    return JSONResponse(order_to_dict(order))


@app.post("/orders/{order_id}/food-ready")
async def food_ready(order_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """
    Seller marks food as ready. Transitions confirmed -> food_ready.
    Triggers rider dispatch in background.
    """
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, "food_ready")
    order.status = "food_ready"
    await db.flush()

    background_tasks.add_task(_trigger_rider_dispatch, order_id)
    logger.info("food_ready", order_id=order_id)
    return JSONResponse(order_to_dict(order))


async def _trigger_rider_dispatch(order_id: str) -> None:
    """Call rider dispatch service to find and assign a rider."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{RIDER_DISPATCH_URL}/dispatch",
                json={"order_id": order_id},
            )
            if resp.status_code != 200:
                logger.error("rider_dispatch_failed", order_id=order_id, status=resp.status_code)
    except Exception as exc:
        logger.error("rider_dispatch_exception", error=str(exc), order_id=order_id)


@app.post("/orders/{order_id}/rider-pickup")
async def rider_pickup(order_id: str, db: AsyncSession = Depends(get_db)):
    """Rider confirmed pickup. Transitions rider_assigned -> picked_up."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, "picked_up")
    order.status = "picked_up"
    await db.flush()
    logger.info("rider_pickup_confirmed", order_id=order_id)
    return JSONResponse(order_to_dict(order))


@app.post("/orders/{order_id}/delivery-confirm")
async def delivery_confirm(order_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """
    Rider confirmed delivery. Transitions picked_up -> delivered.
    Triggers rating prompt to buyer.
    """
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, "delivered")
    order.status = "delivered"
    await db.flush()

    background_tasks.add_task(_send_rating_prompt, order_id)
    logger.info("delivery_confirmed", order_id=order_id)
    return JSONResponse(order_to_dict(order))


async def _send_rating_prompt(order_id: str) -> None:
    """Notify AI Agent to send rating prompt to buyer."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{AI_AGENT_URL}/prompt-rating",
                json={"order_id": order_id},
            )
    except Exception as exc:
        logger.error("rating_prompt_failed", error=str(exc), order_id=order_id)


@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, req: CancelOrderRequest, db: AsyncSession = Depends(get_db)):
    """Cancel an order with a reason."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _validate_transition(order.status, "cancelled")
    order.status = "cancelled"
    order.cancelled_reason = req.reason
    await db.flush()
    logger.info("order_cancelled", order_id=order_id, reason=req.reason)
    return JSONResponse(order_to_dict(order))


@app.post("/orders/{order_id}/rate")
async def rate_order(order_id: str, req: RateOrderRequest, db: AsyncSession = Depends(get_db)):
    """Buyer submits food and delivery rating after delivery."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != "delivered":
        raise HTTPException(status_code=409, detail="Can only rate delivered orders")

    order.buyer_food_rating = req.food_rating
    order.buyer_delivery_rating = req.delivery_rating
    await db.flush()
    logger.info("order_rated", order_id=order_id, food=req.food_rating, delivery=req.delivery_rating)
    return JSONResponse(order_to_dict(order))


@app.get("/orders/buyer/{phone_number}")
async def buyer_orders(phone_number: str, db: AsyncSession = Depends(get_db)):
    """Get order history for a buyer by phone number."""
    buyer_sql = text("SELECT id FROM buyers WHERE phone_number = :phone LIMIT 1")
    buyer_result = await db.execute(buyer_sql, {"phone": phone_number})
    buyer_row = buyer_result.fetchone()
    if not buyer_row:
        return JSONResponse([])

    result = await db.execute(
        select(Order).where(Order.buyer_id == buyer_row[0]).order_by(Order.created_at.desc()).limit(20)
    )
    return JSONResponse([order_to_dict(o) for o in result.scalars().all()])


@app.get("/orders/seller/{phone_number}")
async def seller_orders(phone_number: str, db: AsyncSession = Depends(get_db)):
    """Get recent orders for a seller by phone number."""
    seller_sql = text("SELECT id FROM sellers WHERE phone_number = :phone LIMIT 1")
    seller_result = await db.execute(seller_sql, {"phone": phone_number})
    seller_row = seller_result.fetchone()
    if not seller_row:
        return JSONResponse([])

    result = await db.execute(
        select(Order).where(Order.seller_id == seller_row[0]).order_by(Order.created_at.desc()).limit(20)
    )
    return JSONResponse([order_to_dict(o) for o in result.scalars().all()])


@app.get("/health")
async def health():
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "order-service"})
