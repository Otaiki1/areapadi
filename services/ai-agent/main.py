"""
AI Agent Service — the conversational brain of Areapadi.
Receives parsed WhatsApp messages from the Gateway and drives the full
buyer / seller / rider state machines.
Port 8001.
"""
from __future__ import annotations
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text as sa_text

from shared.models import MessagePayload, ConversationState
from shared.logger import get_logger, hash_phone
from shared.whatsapp_client import get_whatsapp_client
from shared.redis_client import get_conversation_state, save_conversation_state
from shared.db import AsyncSessionLocal
from handlers.router import route_message
from handlers.buyer import send_rating_prompt
from db_models import Buyer

logger = get_logger("ai-agent")

ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ai_agent_starting", port=os.getenv("AI_AGENT_PORT", "8001"))
    yield
    logger.info("ai_agent_stopped")


app = FastAPI(title="Areapadi AI Agent", version="1.0.0", lifespan=lifespan)


class RatingPromptRequest(BaseModel):
    order_id: str


class NotifyBuyerRequest(BaseModel):
    order_id: str
    status: str
    rider_name: str | None = None
    eta_mins: int | None = None


class NotifySellerRequest(BaseModel):
    order_id: str
    seller_phone: str
    items: list[dict]
    total: float
    buyer_notes: str | None = None


# ── Inbound message handler ───────────────────────────────────────────────────

@app.post("/handle")
async def handle(payload: MessagePayload, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Receive a parsed WhatsApp message from the Gateway and route it through
    the conversation state machine.
    """
    logger.info(
        "handle_message",
        phone=hash_phone(payload.phone_number),
        type=payload.message_type,
    )
    background_tasks.add_task(_safe_route, payload)
    return JSONResponse({"status": "ok"})


async def _safe_route(payload: MessagePayload) -> None:
    try:
        await route_message(payload)
    except Exception as exc:
        logger.error(
            "route_message_unhandled_exception",
            phone=hash_phone(payload.phone_number),
            error=str(exc),
            exc_info=True,
        )


# ── Push notifications ────────────────────────────────────────────────────────

@app.post("/prompt-rating")
async def prompt_rating(req: RatingPromptRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Called by Order Service after delivery is confirmed."""
    logger.info("prompt_rating_requested", order_id=req.order_id)
    background_tasks.add_task(send_rating_prompt, req.order_id)
    return JSONResponse({"status": "ok"})


@app.post("/notify-buyer")
async def notify_buyer(req: NotifyBuyerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Called by Order Service or Rider Dispatch when order status changes.
    Proactively WhatsApp-messages the buyer and updates their conversation state.
    """
    logger.info("notify_buyer_requested", order_id=req.order_id, status=req.status)
    background_tasks.add_task(_push_buyer_status, req)
    return JSONResponse({"status": "ok"})


@app.post("/notify-seller")
async def notify_seller(req: NotifySellerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Called by Order Service when a new order is created.
    Sends the seller a WhatsApp message with order details + Confirm/Decline buttons.
    """
    logger.info("notify_seller_requested", order_id=req.order_id)
    background_tasks.add_task(_push_seller_order, req)
    return JSONResponse({"status": "ok"})


# ── Background task implementations ──────────────────────────────────────────

async def _push_buyer_status(req: NotifyBuyerRequest) -> None:
    """Look up buyer phone from order, send status update, update conversation state."""
    try:
        buyer_phone = await _buyer_phone_from_order(req.order_id)
        if not buyer_phone:
            logger.warning("notify_buyer_phone_not_found", order_id=req.order_id)
            return

        state = await get_conversation_state(buyer_phone)

        status_map: dict[str, tuple[str, str]] = {
            "confirmed":     ("order_confirmed",  "Your order is confirmed! The seller is preparing your food."),
            "food_ready":    ("awaiting_pickup",   "Your food is ready! A rider is on their way to pick it up."),
            "rider_assigned": ("awaiting_pickup",  f"Rider on the way: {req.rider_name or 'Your rider'} is heading to pick up your food."),
            "picked_up":     ("in_delivery",       "Your rider has your food and is heading to you!"),
            "cancelled":     ("idle",              "Your order has been cancelled. Sorry about that! Type what you'd like to order next."),
            "no_rider":      (state.stage,         "We're still finding a rider for your order. Please hang tight — you'll hear from us shortly."),
        }

        if req.status not in status_map:
            return

        new_stage, message = status_map[req.status]
        if new_stage != state.stage:
            state.stage = new_stage
            await save_conversation_state(state)

        wa = get_whatsapp_client()
        await wa.send_text(buyer_phone, message)

    except Exception as exc:
        logger.error("push_buyer_status_failed", error=str(exc), order_id=req.order_id)


async def _push_seller_order(req: NotifySellerRequest) -> None:
    """
    Send the seller a new-order notification with Confirm/Decline buttons.
    Updates their conversation state to seller_order_pending.
    """
    try:
        phone = req.seller_phone
        state = await get_conversation_state(phone)

        state.stage = "seller_order_pending"
        state.active_order_id = req.order_id
        await save_conversation_state(state)

        items_text = "\n".join(
            f"• {it.get('name', 'Item')} ×{it.get('quantity', 1)}" for it in req.items[:10]
        )
        notes = f"\nNote: {req.buyer_notes}" if req.buyer_notes else ""
        total = float(req.total)

        wa = get_whatsapp_client()
        await wa.send_interactive_buttons(
            phone,
            f"New order!\n\n{items_text}\n\nTotal: ₦{total:,.0f}{notes}\n\nConfirm or decline this order:",
            buttons=[
                {"id": f"confirm_order_{req.order_id}", "title": "Confirm"},
                {"id": f"decline_order_{req.order_id}", "title": "Decline"},
            ],
        )

    except Exception as exc:
        logger.error("push_seller_order_failed", error=str(exc), order_id=req.order_id)


async def _buyer_phone_from_order(order_id: str) -> str | None:
    """Fetch buyer_id from Order Service, then look up phone in DB."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
            if resp.status_code != 200:
                return None
            order = resp.json()
            buyer_id = order.get("buyer_id")
            if not buyer_id:
                return None

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Buyer).where(Buyer.id == buyer_id)
            )
            buyer = result.scalar_one_or_none()
            return buyer.phone_number if buyer else None

    except Exception as exc:
        logger.error("buyer_phone_lookup_failed", error=str(exc))
        return None


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "ai-agent"})
