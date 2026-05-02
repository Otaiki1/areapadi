"""
Buyer conversation state machine.

Stages:
  new_user         → role not set yet (handled by router, not here)
  awaiting_location → registered as buyer, no location yet
  idle             → has location, ready for food queries
  browsing         → showed seller results, waiting for selection
  viewing_menu     → buyer picked a seller, showing menu
  building_order   → parsed order, confirming details
  awaiting_payment → payment link sent, waiting for webhook
  order_confirmed  → payment done, waiting for seller to confirm
  awaiting_pickup  → seller confirmed, rider dispatched
  in_delivery      → rider picked up
  awaiting_rating  → delivered, prompting 1-5 rating
"""
from __future__ import annotations
import os
import math
import httpx
from datetime import datetime, timezone
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.db import AsyncSessionLocal
from shared.logger import get_logger

from claude_client import call_claude_json, HAIKU, SONNET
from prompts import BUYER_IDLE, ORDER_PARSER, CONFIRMATION_CHECK
from db_models import Buyer

logger = get_logger("buyer-handler")

SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://localhost:8002")
GEO_SERVICE_URL = os.getenv("GEO_SERVICE_URL", "http://localhost:8006")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://localhost:8007")


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_buyer_message(
    state: ConversationState,
    message: MessagePayload,
) -> None:
    wa = get_whatsapp_client()
    stage = state.stage

    if stage == "awaiting_location":
        await _handle_awaiting_location(state, message, wa)
    elif stage == "idle":
        await _handle_idle(state, message, wa)
    elif stage == "browsing":
        await _handle_browsing(state, message, wa)
    elif stage == "viewing_menu":
        await _handle_viewing_menu(state, message, wa)
    elif stage == "building_order":
        await _handle_building_order(state, message, wa)
    elif stage == "awaiting_payment":
        await _handle_awaiting_payment(state, message, wa)
    elif stage in ("order_confirmed", "awaiting_pickup", "in_delivery"):
        await _handle_order_in_progress(state, message, wa)
    elif stage == "awaiting_rating":
        await _handle_awaiting_rating(state, message, wa)
    else:
        # Catch-all: reset to idle if they have a location
        if state.location_lat:
            state.stage = "idle"
            await save_conversation_state(state)
            await wa.send_text(state.phone_number, "What food are you looking for today?")
        else:
            state.stage = "awaiting_location"
            await save_conversation_state(state)
            await wa.send_location_request(state.phone_number, "Please share your location so I can find sellers near you.")


# ── Stage handlers ────────────────────────────────────────────────────────────

async def _handle_awaiting_location(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    if message.message_type != "location":
        await wa.send_location_request(
            state.phone_number,
            "I need your location to find food near you. Please tap 'Share Location'.",
        )
        return

    lat, lng = message.location_lat, message.location_lng
    state.location_lat = lat
    state.location_lng = lng
    state.stage = "idle"

    # Save location to buyers table in the background (best effort)
    await _update_buyer_location(state.phone_number, lat, lng)

    # Reverse geocode for a friendly area name
    area_name = await _get_area_name(lat, lng)
    await save_conversation_state(state)

    await wa.send_text(
        state.phone_number,
        f"Location saved — {area_name}!\n\nWhat food are you looking for? "
        "You can say things like 'jollof rice', 'shawarma near me', or 'small chops'.",
    )


async def _handle_idle(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    if message.message_type == "location":
        # Buyer re-shared location — update it
        state.location_lat = message.location_lat
        state.location_lng = message.location_lng
        await _update_buyer_location(phone, message.location_lat, message.location_lng)
        area_name = await _get_area_name(message.location_lat, message.location_lng)
        await save_conversation_state(state)
        await wa.send_text(phone, f"Location updated to {area_name}. What food are you looking for?")
        return

    if not message.text:
        await wa.send_text(phone, "What food are you looking for? Just type it.")
        return

    parsed = await call_claude_json(
        BUYER_IDLE,
        message.text,
        model=HAIKU,
        history=state.message_history[-6:],
    )
    if not parsed:
        await wa.send_text(phone, "I didn't catch that. What food would you like?")
        return

    intent = parsed.get("intent", "off_topic")
    reply_text = parsed.get("reply_text", "")

    if intent == "order_status":
        await _send_order_status(state, phone, wa)
        return

    if intent in ("help", "off_topic"):
        if reply_text:
            await wa.send_text(phone, reply_text)
        else:
            await wa.send_text(phone, "I can help you find and order food nearby. Just tell me what you want to eat!")
        return

    # food_search
    food_query = parsed.get("food_query", message.text)
    if reply_text:
        await wa.send_text(phone, reply_text)

    await _search_and_present_sellers(state, phone, food_query, wa)


async def _handle_browsing(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    # User may reply with text if they didn't use the list
    seller_id = message.interactive_id
    if not seller_id and message.text:
        text = message.text.strip()
        # Allow them to re-search
        if len(text) > 3:
            await _search_and_present_sellers(state, phone, text, wa)
            return
        await wa.send_text(phone, "Please select a seller from the list above, or type a new food search.")
        return

    if not seller_id:
        await wa.send_text(phone, "Please select a seller from the list above.")
        return

    await _show_menu(state, phone, seller_id, wa)


async def _handle_viewing_menu(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    if not message.text:
        await wa.send_text(phone, "Tell me what you want to order. E.g. '2 jollof rice and 1 chicken'")
        return

    seller_id = state.active_seller_id
    if not seller_id:
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "Something went wrong. What food are you looking for?")
        return

    # Fetch menu items
    menu_items = await _fetch_menu(seller_id)
    if not menu_items:
        await wa.send_text(phone, "Sorry, could not load the menu. Please try selecting the seller again.")
        return

    menu_text = "\n".join(
        f"• {item['name']} — ₦{item['price']:,.0f}" + (f" ({item['description']})" if item.get("description") else "")
        for item in menu_items
    )
    user_prompt = f"Menu:\n{menu_text}\n\nBuyer says: {message.text}"

    parsed = await call_claude_json(
        ORDER_PARSER,
        user_prompt,
        model=SONNET,
        max_tokens=1024,
    )
    if not parsed or not parsed.get("items"):
        reply = parsed.get("reply_text") if parsed else None
        await wa.send_text(phone, reply or "I didn't understand your order. Try: '2 jollof rice and 1 chicken'")
        return

    items = parsed["items"]
    if parsed.get("confidence") == "low":
        await wa.send_text(phone, parsed.get("reply_text", "Can you clarify your order?"))
        return

    # Validate items against actual menu prices
    menu_price_map = {item["name"].lower(): item for item in menu_items}
    validated = []
    for it in items:
        menu_item = menu_price_map.get(it["name"].lower())
        if menu_item:
            qty = max(1, int(it.get("quantity", 1)))
            unit_price = float(menu_item["price"])
            validated.append({
                "name": menu_item["name"],
                "quantity": qty,
                "unit_price": unit_price,
                "subtotal": round(qty * unit_price, 2),
            })

    if not validated:
        unmatched = parsed.get("unmatched", [])
        await wa.send_text(
            phone,
            f"I couldn't find those items on the menu.\n"
            f"Not found: {', '.join(unmatched) if unmatched else 'unknown items'}\n\n"
            "Please check the menu and try again.",
        )
        return

    subtotal = sum(it["subtotal"] for it in validated)

    # Get delivery fee from geo-service
    delivery_fee = await _get_delivery_fee(state, seller_id)

    total = subtotal + delivery_fee
    state.pending_items = validated
    state.stage = "building_order"
    await save_conversation_state(state)

    lines = "\n".join(
        f"{it['name']} x{it['quantity']} — ₦{it['subtotal']:,.0f}" for it in validated
    )
    await wa.send_interactive_buttons(
        phone,
        f"Your order:\n{lines}\n\n"
        f"Subtotal: ₦{subtotal:,.0f}\n"
        f"Delivery: ₦{delivery_fee:,.0f}\n"
        f"Total: ₦{total:,.0f}\n\n"
        "Confirm to place this order?",
        buttons=[
            {"id": "confirm_order", "title": "Confirm"},
            {"id": "cancel_order", "title": "Cancel"},
        ],
    )


async def _handle_building_order(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    # Handle interactive button
    if message.interactive_id == "confirm_order" or (
        message.message_type == "text" and
        any(w in (message.text or "").lower() for w in ("confirm", "yes", "ok", "okay", "oya", "go ahead"))
    ):
        await _place_order(state, phone, wa)
        return

    if message.interactive_id == "cancel_order" or (
        message.message_type == "text" and
        any(w in (message.text or "").lower() for w in ("cancel", "no", "stop"))
    ):
        state.stage = "idle"
        state.pending_items = None
        await save_conversation_state(state)
        await wa.send_text(phone, "Order cancelled. What else can I help you find?")
        return

    # Let Claude decide
    parsed = await call_claude_json(CONFIRMATION_CHECK, message.text or "", model=HAIKU)
    if parsed.get("confirmed"):
        await _place_order(state, phone, wa)
    elif parsed.get("wants_cancel"):
        state.stage = "idle"
        state.pending_items = None
        await save_conversation_state(state)
        await wa.send_text(phone, "Order cancelled. What else can I get you?")
    else:
        # They want to change something — go back to menu view
        state.stage = "viewing_menu"
        state.pending_items = None
        await save_conversation_state(state)
        reply = parsed.get("reply_text", "")
        await wa.send_text(
            phone,
            (reply + "\n\n" if reply else "") + "Tell me your updated order.",
        )


async def _handle_awaiting_payment(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number
    text = (message.text or "").lower().strip()

    if text in ("pay", "link", "payment", "send link", "resend"):
        # Re-fetch and resend payment link
        order_id = state.active_order_id
        if not order_id:
            state.stage = "idle"
            await save_conversation_state(state)
            await wa.send_text(phone, "I couldn't find your order. What food are you looking for?")
            return
        await _resend_payment_link(phone, order_id, state, wa)
    else:
        await wa.send_text(
            phone,
            "Your payment link was sent above. Reply 'pay' if you need a new link.",
        )


async def _handle_order_in_progress(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number
    order_id = state.active_order_id

    if not order_id:
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "What food are you looking for?")
        return

    stage_messages = {
        "order_confirmed": "Your order is confirmed and being prepared. We'll notify you when the rider picks it up.",
        "awaiting_pickup": "Your food is ready and we're arranging pickup. Hang tight!",
        "in_delivery": "Your rider is on the way. You'll get a message when it arrives.",
    }
    await wa.send_text(phone, stage_messages.get(state.stage, "Your order is being processed. Please wait."))


async def _handle_awaiting_rating(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number
    order_id = state.active_order_id

    text = (message.text or "").strip()
    if not text.isdigit() or int(text) not in range(1, 6):
        await wa.send_text(phone, "Please rate your experience 1–5 (1 = poor, 5 = excellent).")
        return

    rating = int(text)
    delivery_rating = state.onboarding_data.get("pending_food_rating") if state.onboarding_data else None

    if delivery_rating is None:
        # First rating received = food rating
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["pending_food_rating"] = rating
        await save_conversation_state(state)
        await wa.send_text(phone, f"Food rating: {rating}/5. Now rate your delivery (1–5):")
        return

    # Second rating = delivery rating, submit both
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{ORDER_SERVICE_URL}/orders/{order_id}/rate",
                json={"food_rating": delivery_rating, "delivery_rating": rating},
            )
    except Exception as exc:
        logger.error("submit_rating_failed", error=str(exc), order_id=order_id)

    state.stage = "idle"
    state.active_order_id = None
    state.onboarding_data = None
    await save_conversation_state(state)
    await wa.send_text(
        phone,
        "Thanks for your feedback! Enjoy your meal.\n\nType what you want to eat anytime for another order.",
    )


# ── Rating prompt (called from /prompt-rating endpoint) ──────────────────────

async def send_rating_prompt(order_id: str) -> None:
    """Look up order, find buyer phone, send rating prompt."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
        if resp.status_code != 200:
            return
        order = resp.json()
        buyer_id = order.get("buyer_id")
        if not buyer_id:
            return

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Buyer).where(Buyer.id == buyer_id))
            buyer = result.scalar_one_or_none()
            if not buyer:
                return
            phone = buyer.phone_number

        from shared.redis_client import get_conversation_state
        state = await get_conversation_state(phone)
        state.stage = "awaiting_rating"
        state.active_order_id = order_id
        await save_conversation_state(state)

        wa = get_whatsapp_client()
        await wa.send_text(
            phone,
            "Your order has been delivered! How was your food? Rate 1–5 (1 = poor, 5 = excellent):",
        )
    except Exception as exc:
        logger.error("send_rating_prompt_failed", error=str(exc), order_id=order_id)


# ── Helper functions ──────────────────────────────────────────────────────────

async def _search_and_present_sellers(
    state: ConversationState, phone: str, food_query: str, wa
) -> None:
    if not state.location_lat:
        state.stage = "awaiting_location"
        await save_conversation_state(state)
        await wa.send_location_request(phone, "I need your location first. Please share it.")
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SELLER_SERVICE_URL}/sellers/search",
                json={
                    "lat": state.location_lat,
                    "lng": state.location_lng,
                    "radius_km": 5.0,
                    "query": food_query,
                },
            )
        sellers = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        logger.error("seller_search_failed", error=str(exc))
        sellers = []

    if not sellers:
        await wa.send_text(
            phone,
            f"No sellers near you right now for '{food_query}'.\n\n"
            "Try a different food or check back later.",
        )
        return

    # Store seller IDs in state for validation
    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["last_search_seller_ids"] = [s["id"] for s in sellers]
    state.stage = "browsing"
    await save_conversation_state(state)

    sections = [
        {
            "title": "Sellers near you",
            "rows": [
                {
                    "id": s["id"],
                    "title": s["business_name"][:24],
                    "description": _seller_row_desc(s),
                }
                for s in sellers[:4]
            ],
        }
    ]

    header = f"Found {len(sellers[:4])} seller{'s' if len(sellers[:4]) != 1 else ''} for '{food_query}':"
    await wa.send_interactive_list(phone, header, sections)


def _seller_row_desc(seller: dict) -> str:
    parts = []
    rating = seller.get("rating", 0)
    if rating:
        parts.append(f"★{rating:.1f}")
    dist = seller.get("distance_text", "")
    if dist:
        parts.append(dist)
    samples = seller.get("sample_items", [])[:2]
    if samples:
        parts.append(", ".join(samples))
    desc = " · ".join(parts)
    return desc[:72]  # WhatsApp row description limit


async def _show_menu(
    state: ConversationState, phone: str, seller_id: str, wa
) -> None:
    menu_items = await _fetch_menu(seller_id)
    if not menu_items:
        await wa.send_text(phone, "This seller has no items available right now. Try another one.")
        return

    state.active_seller_id = seller_id
    state.stage = "viewing_menu"
    await save_conversation_state(state)

    lines = []
    for i, item in enumerate(menu_items[:15], 1):
        price = f"₦{item['price']:,.0f}"
        desc = f" — {item['description']}" if item.get("description") else ""
        lines.append(f"{i}. {item['name']} — {price}{desc}")

    await wa.send_text(
        phone,
        "Here's the menu:\n\n" + "\n".join(lines) + "\n\nWhat would you like to order?",
    )


async def _place_order(state: ConversationState, phone: str, wa) -> None:
    items = state.pending_items
    if not items:
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "Something went wrong. What food are you looking for?")
        return

    buyer_id = await _get_or_create_buyer_id(phone, state)
    if not buyer_id:
        await wa.send_text(phone, "Could not find your account. Please try again.")
        return

    subtotal = sum(it["subtotal"] for it in items)
    delivery_fee = await _get_delivery_fee(state, state.active_seller_id)
    commission = round(subtotal * 0.05, 2)  # 5% platform commission on food
    total = round(subtotal + delivery_fee, 2)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            order_resp = await client.post(
                f"{ORDER_SERVICE_URL}/orders",
                json={
                    "buyer_id": buyer_id,
                    "seller_id": state.active_seller_id,
                    "items": items,
                    "subtotal": subtotal,
                    "delivery_fee": delivery_fee,
                    "platform_commission": commission,
                    "platform_delivery_margin": round(delivery_fee * 0.18, 2),
                    "total_amount": total,
                    "delivery_lat": state.location_lat,
                    "delivery_lng": state.location_lng,
                },
            )
            if order_resp.status_code != 201:
                raise Exception(f"Order creation failed: {order_resp.status_code}")

            order = order_resp.json()
            order_id = order["id"]

            # Generate Paystack link
            pay_resp = await client.post(
                f"{PAYMENT_SERVICE_URL}/initialize",
                json={
                    "order_id": order_id,
                    "amount_kobo": int(total * 100),
                    "buyer_email": f"{phone}@areapadi.ng",
                    "buyer_phone": phone,
                    "metadata": {"order_id": order_id},
                },
            )
            if pay_resp.status_code != 200:
                raise Exception(f"Payment init failed: {pay_resp.status_code}")

            payment_data = pay_resp.json()
            pay_url = payment_data["authorization_url"]

        state.active_order_id = order_id
        state.stage = "awaiting_payment"
        state.pending_items = None
        await save_conversation_state(state)

        await wa.send_text(
            phone,
            f"Order placed! Pay ₦{total:,.0f} to confirm:\n\n{pay_url}\n\n"
            "The link is valid for 30 minutes. Reply 'pay' if you need a new one.",
        )

    except Exception as exc:
        logger.error("place_order_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong placing your order. Please try again.")


async def _resend_payment_link(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            order_resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
            if order_resp.status_code != 200:
                raise Exception("Order not found")
            order = order_resp.json()
            total = order["total_amount"]

            pay_resp = await client.post(
                f"{PAYMENT_SERVICE_URL}/initialize",
                json={
                    "order_id": order_id,
                    "amount_kobo": int(float(total) * 100),
                    "buyer_email": f"{phone}@areapadi.ng",
                    "buyer_phone": phone,
                    "metadata": {"order_id": order_id},
                },
            )
            pay_url = pay_resp.json()["authorization_url"]
        await wa.send_text(phone, f"New payment link:\n{pay_url}")
    except Exception as exc:
        logger.error("resend_payment_link_failed", error=str(exc))
        await wa.send_text(phone, "Could not generate a new link. Please try again.")


async def _send_order_status(state: ConversationState, phone: str, wa) -> None:
    order_id = state.active_order_id
    if not order_id:
        await wa.send_text(phone, "You don't have an active order right now. What food are you looking for?")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
        if resp.status_code != 200:
            raise Exception("Order not found")
        order = resp.json()
        status = order["status"]
        status_messages = {
            "pending": "Your order is waiting for the seller to confirm.",
            "confirmed": "The seller confirmed your order and is preparing it.",
            "food_ready": "Your food is ready! A rider is on the way to pick it up.",
            "rider_assigned": "A rider has been assigned and is heading to pick up your food.",
            "picked_up": "Your rider has picked up your food and is on the way to you!",
            "delivered": "Your order was delivered.",
            "cancelled": f"Your order was cancelled. Reason: {order.get('cancelled_reason', 'unknown')}",
        }
        await wa.send_text(phone, status_messages.get(status, f"Order status: {status}"))
    except Exception as exc:
        logger.error("order_status_check_failed", error=str(exc))
        await wa.send_text(phone, "Could not check your order status right now. Please try again.")


async def _fetch_menu(seller_id: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu")
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.error("fetch_menu_failed", error=str(exc), seller_id=seller_id)
    return []


async def _get_delivery_fee(state: ConversationState, seller_id: str | None) -> float:
    """Estimate delivery fee. Falls back to flat ₦550 if geo is unavailable."""
    if not state.location_lat or not seller_id:
        return 550.0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            seller_resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}")
            if seller_resp.status_code != 200:
                return 550.0
    except Exception:
        return 550.0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            fee_resp = await client.post(
                f"{GEO_SERVICE_URL}/delivery-fee",
                json={"distance_km": 2.0},  # default estimate; real distance needs seller coords
            )
            if fee_resp.status_code == 200:
                return float(fee_resp.json().get("total_fee", 550))
    except Exception:
        pass
    return 550.0


async def _get_area_name(lat: float, lng: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{GEO_SERVICE_URL}/parse-location", json={"lat": lat, "lng": lng}
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("area_name") or data.get("city") or "your area"
    except Exception:
        pass
    return "your area"


async def _update_buyer_location(phone: str, lat: float, lng: float) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    UPDATE buyers
                    SET location = ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                        location_updated_at = NOW()
                    WHERE phone_number = :phone
                """),
                {"lng": lng, "lat": lat, "phone": phone},
            )
            await session.commit()
    except Exception as exc:
        logger.error("update_buyer_location_failed", error=str(exc))


async def _get_or_create_buyer_id(phone: str, state: ConversationState) -> str | None:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Buyer).where(Buyer.phone_number == phone))
            buyer = result.scalar_one_or_none()
            if not buyer:
                buyer = Buyer(
                    phone_number=phone,
                    whatsapp_name=None,
                    location=(
                        f"SRID=4326;POINT({state.location_lng} {state.location_lat})"
                        if state.location_lat else None
                    ),
                )
                session.add(buyer)
                await session.flush()
                await session.refresh(buyer)
            return str(buyer.id)
    except Exception as exc:
        logger.error("get_or_create_buyer_failed", error=str(exc))
        return None
