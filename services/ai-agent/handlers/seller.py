"""
Seller conversation handler — onboarding wizard + active seller commands.
Stage 4 in the build plan. Onboarding stages are fully implemented;
active-seller command routing (add menu items, toggle availability) is wired.
"""
from __future__ import annotations
import os
import httpx
from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.logger import get_logger
from claude_client import call_claude_json, HAIKU
from prompts import SELLER_INTENT

logger = get_logger("seller-handler")
SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://localhost:8002")


async def handle_seller_message(
    state: ConversationState,
    message: MessagePayload,
) -> None:
    wa = get_whatsapp_client()
    phone = state.phone_number
    stage = state.stage

    # ── Onboarding wizard ─────────────────────────────────────────────────────

    if stage == "new_seller_name":
        name = (message.text or "").strip()
        if not name:
            await wa.send_text(phone, "What is your business name?")
            return
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["business_name"] = name
        state.stage = "new_seller_category"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            f"Nice! What type of food do you sell, {name}?\n\n"
            "Examples: jollof rice, shawarma, small chops, cakes, pizza\n"
            "You can list multiple types.",
        )
        return

    if stage == "new_seller_category":
        category_text = (message.text or "").strip()
        if not category_text:
            await wa.send_text(phone, "What type of food do you sell?")
            return
        categories = [c.strip() for c in category_text.replace(",", " ").split() if c.strip()]
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["food_categories"] = categories
        state.stage = "new_seller_location"
        await save_conversation_state(state)
        await wa.send_location_request(
            phone,
            "Please share your shop/kitchen location pin so buyers near you can find you.",
        )
        return

    if stage == "new_seller_location":
        if message.message_type != "location":
            await wa.send_location_request(phone, "Please share your location pin to continue.")
            return
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["lat"] = message.location_lat
        state.onboarding_data["lng"] = message.location_lng
        state.stage = "new_seller_menu"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Location saved! Now let's add your first menu item.\n\n"
            "Send the item name and price like this:\n"
            "Jollof Rice — 1500\n\n"
            "Send 'done' when you've added all your items.",
        )
        return

    if stage == "new_seller_menu":
        text = (message.text or "").strip()
        if not text:
            await wa.send_text(phone, "Send an item like: Jollof Rice — 1500\nOr send 'done' to finish.")
            return

        if text.lower() in ("done", "finish", "that's all", "thats all"):
            await _complete_seller_onboarding(state, phone, wa)
            return

        # Parse "Name — Price" or "Name: Price" or "Name 1500"
        item_name, item_price = _parse_menu_line(text)
        if not item_name or item_price is None:
            await wa.send_text(
                phone,
                "I didn't get that. Send it like:\nJollof Rice — 1500\n\nOr send 'done' to finish.",
            )
            return

        state.onboarding_data = state.onboarding_data or {}
        items = state.onboarding_data.get("menu_items", [])
        items.append({"name": item_name, "price": item_price})
        state.onboarding_data["menu_items"] = items
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            f"Added: {item_name} — ₦{item_price:,.0f}\n\n"
            f"Send another item or 'done' to finish. ({len(items)} item{'s' if len(items) != 1 else ''} so far)",
        )
        return

    # ── Active seller ─────────────────────────────────────────────────────────

    if stage == "seller_active":
        text = (message.text or "").strip().lower()

        # Quick-keyword shortcuts to avoid Claude on simple commands
        if text in ("open", "on", "available", "i'm open"):
            await _toggle_availability(phone, state, True, wa)
            return
        if text in ("close", "closed", "off", "not available"):
            await _toggle_availability(phone, state, False, wa)
            return

        parsed = await call_claude_json(
            SELLER_INTENT,
            f"Seller message: {message.text}",
            model=HAIKU,
        )
        intent = parsed.get("intent", "other")

        if intent == "toggle_open":
            await _toggle_availability(phone, state, True, wa)
        elif intent == "toggle_closed":
            await _toggle_availability(phone, state, False, wa)
        elif intent == "view_orders":
            await wa.send_text(phone, "Checking your recent orders... (not yet implemented in this stage)")
        else:
            reply = parsed.get("reply_text") or "How can I help? You can say 'open', 'close', or ask about your orders."
            await wa.send_text(phone, reply)
        return

    # Fallback
    await wa.send_text(phone, "How can I help you today?")


def _parse_menu_line(text: str) -> tuple[str | None, float | None]:
    """Extract (name, price) from lines like 'Jollof Rice — 1500' or 'Jollof Rice 1500'."""
    import re
    # Try separator patterns: —, -, :, comma
    match = re.split(r"\s*[—\-:,]\s*", text, maxsplit=1)
    if len(match) == 2:
        name = match[0].strip().title()
        price_str = re.sub(r"[^\d.]", "", match[1])
        try:
            return name, float(price_str)
        except ValueError:
            pass

    # Try: last token is a number
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2:
        price_str = re.sub(r"[^\d.]", "", parts[1])
        try:
            return parts[0].strip().title(), float(price_str)
        except ValueError:
            pass

    return None, None


async def _toggle_availability(phone: str, state: ConversationState, available: bool, wa) -> None:
    seller_id = state.onboarding_data.get("seller_id") if state.onboarding_data else None
    if not seller_id:
        await wa.send_text(phone, "Could not find your seller profile. Please contact support.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/availability",
                json={"is_available": available},
            )
        status_word = "open" if available else "closed"
        await wa.send_text(phone, f"Your store is now {status_word}. {'Buyers can find you.' if available else 'You will not receive orders until you reopen.'}")
    except Exception as exc:
        logger.error("toggle_availability_failed", error=str(exc))
        await wa.send_text(phone, "Could not update your availability right now. Try again.")


async def _complete_seller_onboarding(state: ConversationState, phone: str, wa) -> None:
    """Create seller record + menu items via seller-service. Mark onboarding complete."""
    data = state.onboarding_data or {}
    menu_items = data.get("menu_items", [])

    if not menu_items:
        await wa.send_text(phone, "Please add at least one menu item before finishing.")
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Create seller
            seller_resp = await client.post(
                f"{SELLER_SERVICE_URL}/sellers",
                json={
                    "phone_number": phone,
                    "business_name": data.get("business_name", "My Store"),
                    "food_categories": data.get("food_categories", []),
                    "latitude": data.get("lat", 0),
                    "longitude": data.get("lng", 0),
                },
            )
            if seller_resp.status_code not in (200, 201, 409):
                raise Exception(f"Seller creation failed: {seller_resp.status_code}")

            seller = seller_resp.json()
            seller_id = seller.get("id") or seller.get("seller_id")

            # If 409, seller already exists — fetch by phone
            if seller_resp.status_code == 409:
                existing = await client.get(f"{SELLER_SERVICE_URL}/sellers/by-phone/{phone}")
                seller_id = existing.json().get("id")

            # Add menu items
            for item in menu_items:
                await client.post(
                    f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu",
                    json={"name": item["name"], "price": item["price"]},
                )

            # Mark onboarding complete
            await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}",
                json={"onboarding_complete": True, "onboarding_step": "complete"},
            )

        state.stage = "seller_active"
        state.onboarding_data = {"seller_id": seller_id}
        await save_conversation_state(state)

        item_list = "\n".join(f"• {i['name']} — ₦{i['price']:,.0f}" for i in menu_items)
        await wa.send_text(
            phone,
            f"Your store is set up! Here's what you added:\n\n{item_list}\n\n"
            "Reply 'open' to start receiving orders, or 'close' to pause.\n"
            "You'll get a WhatsApp message when a new order comes in.",
        )
    except Exception as exc:
        logger.error("seller_onboarding_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong setting up your store. Please try again.")
