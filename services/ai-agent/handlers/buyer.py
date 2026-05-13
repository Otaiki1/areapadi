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
import math
import os
import httpx
from sqlalchemy import select
from sqlalchemy import text as sa_text

from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.db import AsyncSessionLocal
from shared.logger import get_logger

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
        if state.location_lat:
            state.stage = "idle"
            await save_conversation_state(state)
            await wa.send_text(state.phone_number, "What food are you looking for today?")
        else:
            state.stage = "awaiting_location"
            await save_conversation_state(state)
            await wa.send_location_request(
                state.phone_number,
                "Please share your location so I can find sellers near you.",
            )


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

    # Persist buyer to DB immediately — create if new, update location if returning
    await _upsert_buyer(state.phone_number, message.whatsapp_name, lat, lng)

    area_name = await _get_area_name(lat, lng)
    await save_conversation_state(state)

    await wa.send_text(
        state.phone_number,
        f"Location saved — {area_name}!\n\nWhat food are you looking for? "
        "You can say things like *jollof rice*, *shawarma*, or *small chops*.",
    )


async def _handle_idle(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    if message.message_type == "location":
        lat, lng = message.location_lat, message.location_lng
        state.location_lat = lat
        state.location_lng = lng
        await _upsert_buyer(phone, None, lat, lng)
        area_name = await _get_area_name(lat, lng)
        await save_conversation_state(state)
        await wa.send_text(phone, f"Location updated to {area_name}. What food are you looking for?")
        return

    if not message.text:
        await wa.send_text(phone, "What food are you looking for? Just type it.")
        return

    text = message.text.strip().lower()

    if any(w in text for w in ("status", "my order", "where is", "order status")):
        await _send_order_status(state, phone, wa)
        return

    non_food = (
        "hi", "hello", "hey", "i want to order", "order food", "i want food",
        "want to order", "i want to buy", "help", "menu", "what can you do",
    )
    if text in non_food or text.startswith("i want to order") or text.startswith("i want food"):
        await wa.send_text(
            phone,
            "What food are you looking for? E.g. *shawarma*, *jollof rice*, *small chops*",
        )
        return

    await _search_and_present_sellers(state, phone, message.text.strip(), wa)


async def _handle_browsing(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    seller_id = message.interactive_id
    if not seller_id and message.text:
        text = message.text.strip()
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
    """
    Numbered menu selection.
    • "1"     → add 1 of item 1
    • "2 x3"  → add 3 of item 2
    • "done"  → proceed to checkout
    • "cart"  → show current cart
    • "clear" → empty cart, back to idle
    """
    import re
    phone = state.phone_number
    text = (message.text or "").strip().lower()

    seller_id = state.active_seller_id
    if not seller_id:
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "Something went wrong. What food are you looking for?")
        return

    menu_items = await _fetch_menu(seller_id)
    if not menu_items:
        await wa.send_text(phone, "Could not load the menu. Please try selecting the seller again.")
        return

    cart: list[dict] = state.pending_items or []

    if text in ("done", "order", "checkout", "proceed", "confirm"):
        if not cart:
            await wa.send_text(phone, "Your cart is empty. Send a number from the menu to add an item.")
            return
        await _show_order_summary(state, phone, cart, wa)
        return

    if text in ("cart", "my cart", "show cart"):
        if not cart:
            await wa.send_text(phone, "Your cart is empty.")
        else:
            lines = "\n".join(f"• {it['name']} x{it['quantity']} — ₦{it['subtotal']:,.0f}" for it in cart)
            await wa.send_text(phone, f"Your cart:\n{lines}\n\nSend more numbers to add items, or *done* to order.")
        return

    if text in ("clear", "cancel", "restart", "reset", "back"):
        state.pending_items = None
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "Cart cleared. What food are you looking for?")
        return

    match = re.match(r"^(\d+)(?:\s*[xX\*]\s*(\d+))?$", text)
    if not match:
        await wa.send_text(
            phone,
            "Send the item number to add it to your cart.\n"
            "Example: *1* to add item 1, or *2 x3* for 3 of item 2.\n\n"
            "Send *cart* to see your order, *done* to checkout, *clear* to start over.",
        )
        return

    item_num = int(match.group(1))
    quantity = int(match.group(2)) if match.group(2) else 1

    if item_num < 1 or item_num > len(menu_items):
        await wa.send_text(phone, f"Please send a number between 1 and {len(menu_items)}.")
        return

    selected = menu_items[item_num - 1]
    unit_price = float(selected["price"])

    existing = next((it for it in cart if it["name"] == selected["name"]), None)
    if existing:
        existing["quantity"] += quantity
        existing["subtotal"] = round(existing["quantity"] * unit_price, 2)
    else:
        cart.append({
            "name": selected["name"],
            "quantity": quantity,
            "unit_price": unit_price,
            "subtotal": round(quantity * unit_price, 2),
        })

    state.pending_items = cart
    await save_conversation_state(state)

    total_so_far = sum(it["subtotal"] for it in cart)
    await wa.send_text(
        phone,
        f"Added: {selected['name']} x{quantity} — ₦{quantity * unit_price:,.0f}\n"
        f"Cart total: ₦{total_so_far:,.0f} ({len(cart)} item{'s' if len(cart) != 1 else ''})\n\n"
        "Add more items or send *done* to checkout.",
    )


async def _show_order_summary(
    state: ConversationState, phone: str, cart: list[dict], wa
) -> None:
    subtotal = sum(it["subtotal"] for it in cart)
    delivery_fee = await _get_delivery_fee(state, state.active_seller_id)
    total = round(subtotal + delivery_fee, 2)

    state.pending_items = cart
    state.stage = "building_order"
    await save_conversation_state(state)

    lines = "\n".join(f"{it['name']} x{it['quantity']} — ₦{it['subtotal']:,.0f}" for it in cart)
    await wa.send_interactive_buttons(
        phone,
        f"Your order:\n{lines}\n\n"
        f"Subtotal: ₦{subtotal:,.0f}\n"
        f"Delivery: ₦{delivery_fee:,.0f}\n"
        f"Total: ₦{total:,.0f}\n\n"
        "Confirm to place this order?",
        buttons=[
            {"id": "confirm_order", "title": "Confirm"},
            {"id": "cancel_order",  "title": "Cancel"},
        ],
    )


async def _handle_building_order(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number

    confirmed = message.interactive_id == "confirm_order" or any(
        w in (message.text or "").lower()
        for w in ("confirm", "yes", "ok", "okay", "oya", "go ahead")
    )
    cancelled = message.interactive_id == "cancel_order" or any(
        w in (message.text or "").lower()
        for w in ("cancel", "no", "stop")
    )

    if confirmed:
        await _place_order(state, phone, wa)
        return

    if cancelled:
        state.stage = "idle"
        state.pending_items = None
        await save_conversation_state(state)
        await wa.send_text(phone, "Order cancelled. What else can I help you find?")
        return

    await wa.send_interactive_buttons(
        phone,
        "Please confirm or cancel your order:",
        buttons=[
            {"id": "confirm_order", "title": "Confirm"},
            {"id": "cancel_order",  "title": "Cancel"},
        ],
    )


async def _handle_awaiting_payment(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number
    text = (message.text or "").lower().strip()

    if text in ("pay", "link", "payment", "send link", "resend"):
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
            "Your payment link was sent above. Complete the payment to confirm your order.\n\n"
            "Reply *pay* if you need a fresh link.",
        )


async def _handle_order_in_progress(
    state: ConversationState, message: MessagePayload, wa
) -> None:
    phone = state.phone_number
    if not state.active_order_id:
        state.stage = "idle"
        await save_conversation_state(state)
        await wa.send_text(phone, "What food are you looking for?")
        return

    stage_messages = {
        "order_confirmed": "Your order is confirmed and being prepared. We'll notify you when the rider picks it up.",
        "awaiting_pickup": "Your food is ready and a rider has been arranged. Hang tight!",
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
    pending_food = (state.onboarding_data or {}).get("pending_food_rating")

    if pending_food is None:
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["pending_food_rating"] = rating
        await save_conversation_state(state)
        await wa.send_text(phone, f"Food rating: {rating}/5. Now rate your delivery (1–5):")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{ORDER_SERVICE_URL}/orders/{order_id}/rate",
                json={"food_rating": pending_food, "delivery_rating": rating},
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
            result = await session.execute(
                sa_text("SELECT phone_number FROM buyers WHERE id = :id::uuid"),
                {"id": buyer_id},
            )
            row = result.fetchone()
            if not row:
                return
            phone = row[0]

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

    count = len(sellers[:4])
    header = f"Found {count} seller{'s' if count != 1 else ''} for '{food_query}':"
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
    return " · ".join(parts)[:72]


async def _show_menu(
    state: ConversationState, phone: str, seller_id: str, wa
) -> None:
    menu_items = await _fetch_menu(seller_id)
    if not menu_items:
        await wa.send_text(phone, "This seller has no items available right now. Try another one.")
        return

    state.active_seller_id = seller_id
    state.stage = "viewing_menu"
    state.pending_items = []
    await save_conversation_state(state)

    lines = []
    for i, item in enumerate(menu_items[:15], 1):
        price = f"₦{item['price']:,.0f}"
        desc = f" — {item['description']}" if item.get("description") else ""
        lines.append(f"{i}. {item['name']} — {price}{desc}")

    await wa.send_text(
        phone,
        "Here's the menu:\n\n" + "\n".join(lines) + "\n\n"
        "Reply with an item number to add it to your cart. E.g. *1* or *2 x3* for 3 of item 2.",
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
    commission = round(subtotal * 0.05, 2)
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
                    "buyer_phone": phone,
                },
            )
            if order_resp.status_code != 201:
                raise Exception(f"Order creation failed: {order_resp.status_code}")

            order = order_resp.json()
            order_id = order["id"]

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

            pay_url = pay_resp.json()["authorization_url"]

        state.active_order_id = order_id
        state.stage = "awaiting_payment"
        state.pending_items = None
        await save_conversation_state(state)

        await wa.send_text(
            phone,
            f"Order placed! Pay ₦{total:,.0f} to confirm:\n\n{pay_url}\n\n"
            "The link is valid for 30 minutes. Reply *pay* if you need a new one.",
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
            total = order_resp.json()["total_amount"]

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
        await wa.send_text(phone, "You don't have an active order. What food are you looking for?")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
        if resp.status_code != 200:
            raise Exception("Order not found")
        status = resp.json()["status"]
        status_messages = {
            "pending":         "Your order is waiting for the seller to confirm.",
            "confirmed":       "The seller confirmed your order and is preparing it.",
            "food_ready":      "Your food is ready! A rider is on the way to pick it up.",
            "rider_assigned":  "A rider has been assigned and is heading to pick up your food.",
            "picked_up":       "Your rider has your food and is heading to you!",
            "delivered":       "Your order was delivered. Enjoy!",
            "cancelled":       "Your order was cancelled.",
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


async def _get_seller_coords(seller_id: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a seller by querying PostGIS directly."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""
                    SELECT ST_Y(location::geometry), ST_X(location::geometry)
                    FROM sellers WHERE id = :id::uuid
                """),
                {"id": seller_id},
            )
            row = result.fetchone()
            if row and row[0] is not None:
                return float(row[0]), float(row[1])
    except Exception as exc:
        logger.error("get_seller_coords_failed", error=str(exc))
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance between two GPS coordinates in km."""
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


async def _get_delivery_fee(state: ConversationState, seller_id: str | None) -> float:
    """Calculate delivery fee using real buyer-to-seller distance."""
    if not state.location_lat or not seller_id:
        return 550.0

    seller_coords = await _get_seller_coords(seller_id)
    if not seller_coords:
        return 550.0

    seller_lat, seller_lng = seller_coords
    dist_km = _haversine_km(
        state.location_lat, state.location_lng,
        seller_lat, seller_lng,
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{GEO_SERVICE_URL}/delivery-fee",
                json={"distance_km": dist_km},
            )
            if resp.status_code == 200:
                return float(resp.json().get("total_fee", 550))
    except Exception:
        pass

    # Inline fallback matching geo service fare schedule
    if dist_km <= 3:
        return 550.0
    elif dist_km <= 6:
        return 950.0
    else:
        return round(950 + (dist_km - 6) * 100)


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


async def _upsert_buyer(phone: str, name: str | None, lat: float, lng: float) -> None:
    """Create buyer if new, update location always. Saves whatsapp_name if provided."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    INSERT INTO buyers (phone_number, whatsapp_name, location, location_updated_at)
                    VALUES (
                        :phone, :name,
                        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                        NOW()
                    )
                    ON CONFLICT (phone_number) DO UPDATE
                      SET location             = EXCLUDED.location,
                          location_updated_at  = NOW(),
                          whatsapp_name        = COALESCE(EXCLUDED.whatsapp_name, buyers.whatsapp_name)
                """),
                {"phone": phone, "name": name, "lat": lat, "lng": lng},
            )
            await session.commit()
    except Exception as exc:
        logger.error("upsert_buyer_failed", error=str(exc))


async def _get_or_create_buyer_id(phone: str, state: ConversationState) -> str | None:
    """Return buyer's UUID string, inserting a record if none exists yet."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""
                    INSERT INTO buyers (
                        phone_number, location, location_updated_at
                    )
                    VALUES (
                        :phone,
                        CASE WHEN :lat IS NOT NULL
                             THEN ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
                             ELSE NULL END,
                        CASE WHEN :lat IS NOT NULL THEN NOW() ELSE NULL END
                    )
                    ON CONFLICT (phone_number) DO UPDATE
                      SET location = CASE WHEN :lat IS NOT NULL
                                          THEN EXCLUDED.location
                                          ELSE buyers.location END,
                          location_updated_at = CASE WHEN :lat IS NOT NULL
                                                     THEN NOW()
                                                     ELSE buyers.location_updated_at END
                    RETURNING id::text
                """),
                {
                    "phone": phone,
                    "lat": state.location_lat,
                    "lng": state.location_lng,
                },
            )
            row = result.fetchone()
            await session.commit()
            return row[0] if row else None
    except Exception as exc:
        logger.error("get_or_create_buyer_failed", error=str(exc))
        return None
