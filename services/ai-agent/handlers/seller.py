"""
Seller/vendor conversation handler.

Onboarding stages:
  new_seller_name      → business name
  new_seller_category  → food categories (comma-separated)
  new_seller_location  → GPS pin — stored in DB with reverse-geocoded address
  new_seller_menu      → add at least one menu item before going live

Active seller stages:
  seller_active        → receives commands (open/close/add/remove/edit/orders/menu)
  seller_order_pending → new order waiting for confirm/decline
  seller_decline_reason → capture reason before cancelling order
  seller_adding_item   → looping item entry
  seller_removing_item → waiting for item number to delete
  seller_editing_item  → waiting for item number then new price
"""
from __future__ import annotations
import re
import os
import httpx
from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.logger import get_logger

logger = get_logger("seller-handler")
SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://localhost:8002")
ORDER_SERVICE_URL  = os.getenv("ORDER_SERVICE_URL",  "http://localhost:8003")
GEO_SERVICE_URL    = os.getenv("GEO_SERVICE_URL",    "http://localhost:8006")

MAX_MENU_ITEMS = 50
MAX_PRICE      = 500_000   # ₦500,000 upper sanity limit
MIN_PRICE      = 50        # ₦50 lower sanity limit


# ── Entry point ───────────────────────────────────────────────────────────────

async def handle_seller_message(
    state: ConversationState,
    message: MessagePayload,
) -> None:
    wa    = get_whatsapp_client()
    phone = state.phone_number
    stage = state.stage

    # ── Onboarding ────────────────────────────────────────────────────────────

    if stage == "new_seller_name":
        await _step_name(state, message, phone, wa)
        return

    if stage == "new_seller_category":
        await _step_category(state, message, phone, wa)
        return

    if stage == "new_seller_location":
        await _step_location(state, message, phone, wa)
        return

    if stage == "new_seller_menu":
        await _step_menu(state, message, phone, wa)
        return

    # ── Active order flows ────────────────────────────────────────────────────

    if stage == "seller_order_pending":
        await _handle_order_pending(state, message, phone, wa)
        return

    if stage == "seller_decline_reason":
        await _handle_decline_reason(state, message, phone, wa)
        return

    if stage == "seller_adding_item":
        await _handle_adding_item(state, message, phone, wa)
        return

    if stage == "seller_removing_item":
        await _handle_removing_item(state, message, phone, wa)
        return

    if stage == "seller_editing_item":
        await _handle_editing_item(state, message, phone, wa)
        return

    # ── Active seller ─────────────────────────────────────────────────────────

    if stage == "seller_active":
        await _handle_active(state, message, phone, wa)
        return

    # Fallback
    await wa.send_text(phone, "Welcome back! Reply *help* to see what you can do.")


# ── Onboarding steps ──────────────────────────────────────────────────────────

async def _step_name(state, message, phone, wa):
    name = (message.text or "").strip()
    if len(name) < 2:
        await wa.send_text(
            phone,
            "What is your *business name*?\n\n"
            "Example: Mama Titi's Kitchen",
        )
        return

    if len(name) > 100:
        await wa.send_text(phone, "Business name is too long. Please keep it under 100 characters.")
        return

    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["business_name"] = name.strip()
    state.stage = "new_seller_category"
    await save_conversation_state(state)
    await wa.send_text(
        phone,
        f"Great name! *Step 2 of 4*\n\n"
        f"What types of food do you sell, {name}?\n\n"
        "List them separated by commas:\n"
        "Example: *Jollof Rice, Fried Rice, Grilled Chicken, Shawarma*",
    )


async def _step_category(state, message, phone, wa):
    raw = (message.text or "").strip()
    if not raw:
        await wa.send_text(phone, "Please tell me what types of food you sell.")
        return

    # Split on commas only so multi-word names like "Jollof Rice" stay intact
    categories = [c.strip().title() for c in raw.split(",") if c.strip()]
    if not categories:
        await wa.send_text(phone, "I couldn't read that. Please list your food types separated by commas.")
        return

    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["food_categories"] = categories
    state.stage = "new_seller_location"
    await save_conversation_state(state)

    cats_display = ", ".join(categories)
    await wa.send_text(
        phone,
        f"Selling: *{cats_display}*\n\n*Step 3 of 4*\n\n"
        "Share your *shop or kitchen location* so buyers near you can find you.\n\n"
        "Tap the attachment icon → Location → Send your current location.",
    )
    await wa.send_location_request(
        phone,
        "Please share your shop/kitchen location pin.",
    )


async def _step_location(state, message, phone, wa):
    if message.message_type != "location":
        await wa.send_location_request(
            phone,
            "I need your shop location. Please tap 'Share Location' to continue.",
        )
        return

    lat, lng = message.location_lat, message.location_lng

    # Sanity check — reject (0,0) or implausible coordinates
    if not lat or not lng or (abs(lat) < 0.001 and abs(lng) < 0.001):
        await wa.send_location_request(
            phone,
            "That location didn't look right. Please share your actual shop location.",
        )
        return

    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["lat"] = lat
    state.onboarding_data["lng"] = lng
    state.stage = "new_seller_menu"
    await save_conversation_state(state)
    await wa.send_text(
        phone,
        "Location saved! *Step 4 of 4*\n\n"
        "Now add your menu items. Send each item on its own line like this:\n\n"
        "*Jollof Rice — 1500*\n"
        "*Grilled Chicken — 2500*\n"
        "*Shawarma — 2000*\n\n"
        "Send *done* when you've added all your items.\n"
        "You must add at least 1 item.",
    )


async def _step_menu(state, message, phone, wa):
    text = (message.text or "").strip()
    if not text:
        await wa.send_text(phone, "Send an item like: *Jollof Rice — 1500*\nOr send *done* to finish.")
        return

    if text.lower() in ("done", "finish", "that's all", "thats all"):
        await _complete_onboarding(state, phone, wa)
        return

    item_name, item_price, error = _parse_menu_line(text)
    if error:
        await wa.send_text(phone, f"{error}\n\nOr send *done* to finish.")
        return

    state.onboarding_data = state.onboarding_data or {}
    items = state.onboarding_data.get("menu_items", [])

    if len(items) >= MAX_MENU_ITEMS:
        await wa.send_text(phone, f"You've reached the {MAX_MENU_ITEMS}-item limit. Send *done* to finish.")
        return

    # Prevent duplicate names
    if any(i["name"].lower() == item_name.lower() for i in items):
        await wa.send_text(
            phone,
            f"*{item_name}* is already in your list. Send a different item or *done* to finish.",
        )
        return

    items.append({"name": item_name, "price": item_price})
    state.onboarding_data["menu_items"] = items
    await save_conversation_state(state)
    await wa.send_text(
        phone,
        f"✓ {item_name} — ₦{item_price:,.0f}\n\n"
        f"{len(items)} item{'s' if len(items) != 1 else ''} added. Send another or *done* to finish.",
    )


# ── Active seller command router ──────────────────────────────────────────────

async def _handle_active(state, message, phone, wa):
    text = (message.text or "").strip().lower()

    # open / close
    if text in ("open", "on", "available", "i'm open", "start"):
        await _toggle_availability(phone, state, True, wa)
        return
    if text in ("close", "closed", "off", "not available", "stop", "pause"):
        await _toggle_availability(phone, state, False, wa)
        return

    # food ready
    if any(p in text for p in ("food ready", "food is ready", "order ready", "ready for pickup")):
        await _trigger_food_ready(phone, state, wa)
        return

    # menu management
    if any(p in text for p in ("add item", "add food", "new item", "add product", "add menu")):
        state.stage = "seller_adding_item"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Send your item name and price:\n\n"
            "*Jollof Rice — 1500*\n\n"
            "Send *done* when finished.",
        )
        return

    if any(p in text for p in ("remove item", "delete item", "remove food", "delete food")):
        await _start_remove_item(state, phone, wa)
        return

    if any(p in text for p in ("edit item", "update item", "change price", "change item")):
        await _start_edit_item(state, phone, wa)
        return

    if any(p in text for p in ("my menu", "view menu", "show menu", "menu")):
        await _show_menu(state, phone, wa)
        return

    if any(p in text for p in ("my orders", "orders", "recent orders", "pending orders")):
        await _show_orders(state, phone, wa)
        return

    if any(p in text for p in ("my store", "store info", "profile", "update store", "edit store")):
        await _show_store_info(state, phone, wa)
        return

    if text in ("help", "?", "hi", "hello", "menu options"):
        await _send_help(state, phone, wa)
        return

    # Default: show help with current store status
    await _send_help(state, phone, wa)


# ── Pending order handling ────────────────────────────────────────────────────

async def _handle_order_pending(state, message, phone, wa):
    order_id = state.active_order_id
    if not order_id:
        state.stage = "seller_active"
        await save_conversation_state(state)
        await wa.send_text(phone, "No pending order found.")
        return

    interactive_id = message.interactive_id or ""
    text = (message.text or "").strip().lower()

    confirmed = (
        interactive_id == f"confirm_order_{order_id}"
        or text in ("confirm", "yes", "accept", "ok", "okay", "oya", "sure")
    )
    declined = (
        interactive_id == f"decline_order_{order_id}"
        or text in ("decline", "no", "reject", "cancel")
    )

    if confirmed:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/confirm-seller")
            if resp.status_code == 200:
                state.stage = "seller_active"
                await save_conversation_state(state)
                await wa.send_text(
                    phone,
                    "Order confirmed! Start preparing.\n\n"
                    "When the food is ready, reply *food ready* to call a rider.",
                )
            else:
                await wa.send_text(phone, "Could not confirm the order. Please try again.")
        except Exception as exc:
            logger.error("seller_confirm_order_failed", error=str(exc), order_id=order_id)
            await wa.send_text(phone, "Something went wrong. Please try again.")
        return

    if declined:
        state.stage = "seller_decline_reason"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Why are you declining this order?\n\n"
            "Examples: *out of stock*, *kitchen closed*, *too busy*",
        )
        return

    # Unrecognised reply — re-send the buttons
    await wa.send_interactive_buttons(
        phone,
        "You have a pending order. Please confirm or decline:",
        buttons=[
            {"id": f"confirm_order_{order_id}", "title": "Confirm"},
            {"id": f"decline_order_{order_id}", "title": "Decline"},
        ],
    )


async def _handle_decline_reason(state, message, phone, wa):
    order_id = state.active_order_id
    reason = (message.text or "").strip() or "Seller declined"

    if order_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{ORDER_SERVICE_URL}/orders/{order_id}/cancel",
                    json={"reason": reason},
                )
        except Exception as exc:
            logger.error("seller_decline_cancel_failed", error=str(exc), order_id=order_id)

    state.stage = "seller_active"
    state.active_order_id = None
    await save_conversation_state(state)
    await wa.send_text(phone, "Order declined. You're back to receiving new orders.")


# ── Menu management ───────────────────────────────────────────────────────────

async def _handle_adding_item(state, message, phone, wa):
    text = (message.text or "").strip()
    if not text:
        await wa.send_text(phone, "Send an item like: *Jollof Rice — 1500*\nOr send *done* to finish.")
        return

    if text.lower() in ("done", "finish", "stop", "back", "cancel"):
        state.stage = "seller_active"
        await save_conversation_state(state)
        await wa.send_text(phone, "Done adding items. Reply *my menu* to see your updated menu.")
        return

    item_name, item_price, error = _parse_menu_line(text)
    if error:
        await wa.send_text(phone, f"{error}\n\nOr send *done* to finish.")
        return

    seller_id = (state.onboarding_data or {}).get("seller_id")
    if not seller_id:
        await wa.send_text(phone, "Could not find your seller profile. Please contact support.")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu",
                json={"name": item_name, "price": item_price},
            )
        if resp.status_code in (200, 201):
            await wa.send_text(
                phone,
                f"✓ Added *{item_name}* — ₦{item_price:,.0f}\n\nSend another item or *done* to finish.",
            )
        else:
            await wa.send_text(phone, "Could not add that item. Please try again.")
    except Exception as exc:
        logger.error("add_menu_item_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong. Please try again.")


async def _start_remove_item(state, phone, wa):
    items = await _fetch_menu_items(state, phone, wa)
    if items is None:
        return
    if not items:
        await wa.send_text(phone, "Your menu is empty. Nothing to remove.")
        return

    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["menu_cache"] = [{"id": it["id"], "name": it["name"]} for it in items]
    state.stage = "seller_removing_item"
    await save_conversation_state(state)

    lines = "\n".join(f"{i+1}. {it['name']} — ₦{it['price']:,.0f}" for i, it in enumerate(items))
    await wa.send_text(
        phone,
        f"Which item do you want to remove?\n\n{lines}\n\nReply with the number or *cancel* to go back.",
    )


async def _handle_removing_item(state, message, phone, wa):
    text = (message.text or "").strip().lower()

    if text in ("cancel", "back", "stop"):
        state.stage = "seller_active"
        await save_conversation_state(state)
        await wa.send_text(phone, "Cancelled.")
        return

    menu_cache = (state.onboarding_data or {}).get("menu_cache", [])
    seller_id  = (state.onboarding_data or {}).get("seller_id")

    try:
        index = int(text) - 1
        if index < 0 or index >= len(menu_cache):
            raise ValueError
    except (ValueError, TypeError):
        await wa.send_text(
            phone,
            f"Please reply with a number between 1 and {len(menu_cache)}, or *cancel* to go back.",
        )
        return

    item    = menu_cache[index]
    item_id = item["id"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu/{item_id}")
        if resp.status_code in (200, 204):
            state.stage = "seller_active"
            state.onboarding_data.pop("menu_cache", None)
            await save_conversation_state(state)
            await wa.send_text(phone, f"Removed *{item['name']}* from your menu.")
        else:
            await wa.send_text(phone, "Could not remove that item. Please try again.")
    except Exception as exc:
        logger.error("remove_menu_item_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong. Please try again.")


async def _start_edit_item(state, phone, wa):
    items = await _fetch_menu_items(state, phone, wa)
    if items is None:
        return
    if not items:
        await wa.send_text(phone, "Your menu is empty. Nothing to edit.")
        return

    state.onboarding_data = state.onboarding_data or {}
    state.onboarding_data["menu_cache"] = [
        {"id": it["id"], "name": it["name"], "price": it["price"]} for it in items
    ]
    state.stage = "seller_editing_item"
    await save_conversation_state(state)

    lines = "\n".join(f"{i+1}. {it['name']} — ₦{it['price']:,.0f}" for i, it in enumerate(items))
    await wa.send_text(
        phone,
        f"Which item do you want to edit?\n\n{lines}\n\n"
        "Reply with the number or *cancel* to go back.",
    )


async def _handle_editing_item(state, message, phone, wa):
    text      = (message.text or "").strip()
    text_low  = text.lower()
    data      = state.onboarding_data or {}
    menu_cache = data.get("menu_cache", [])
    seller_id  = data.get("seller_id")

    if text_low in ("cancel", "back", "stop"):
        state.stage = "seller_active"
        await save_conversation_state(state)
        await wa.send_text(phone, "Cancelled.")
        return

    # Step 1 — seller sent a number to pick item
    if "edit_target" not in data:
        try:
            index = int(text) - 1
            if index < 0 or index >= len(menu_cache):
                raise ValueError
        except (ValueError, TypeError):
            await wa.send_text(
                phone,
                f"Please reply with a number between 1 and {len(menu_cache)}, or *cancel* to go back.",
            )
            return

        item = menu_cache[index]
        data["edit_target"] = item
        state.onboarding_data = data
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            f"Editing: *{item['name']}* — ₦{float(item['price']):,.0f}\n\n"
            "What do you want to change?\n\n"
            "• To change the *name*: send  *name: New Name*\n"
            "• To change the *price*: send  *price: 2500*\n"
            "• To change *both*: send  *New Name — 2500*\n\n"
            "Or send *cancel* to go back.",
        )
        return

    # Step 2 — seller sent the new value(s)
    target   = data["edit_target"]
    item_id  = target["id"]
    update   = {}

    # "price: 2500"
    price_match = re.match(r"^price[:\s]+(.+)$", text_low)
    if price_match:
        price_str = re.sub(r"[^\d.]", "", price_match.group(1))
        try:
            price = float(price_str)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                await wa.send_text(phone, f"Price must be between ₦{MIN_PRICE:,} and ₦{MAX_PRICE:,}.")
                return
            update["price"] = price
        except ValueError:
            await wa.send_text(phone, "Could not read that price. Try: *price: 2500*")
            return

    # "name: New Name"
    name_match = re.match(r"^name[:\s]+(.+)$", text, re.IGNORECASE)
    if name_match:
        new_name = name_match.group(1).strip().title()
        if len(new_name) < 2:
            await wa.send_text(phone, "Name is too short.")
            return
        update["name"] = new_name

    # "New Name — 2500" (both at once)
    if not update:
        item_name, item_price, error = _parse_menu_line(text)
        if not error and item_name and item_price:
            update["name"]  = item_name
            update["price"] = item_price

    if not update:
        await wa.send_text(
            phone,
            "Couldn't read that. Try:\n"
            "• *price: 2500*\n"
            "• *name: Spicy Rice*\n"
            "• *Spicy Rice — 2500*\n\n"
            "Or *cancel* to go back.",
        )
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu/{item_id}",
                json=update,
            )
        if resp.status_code == 200:
            updated = resp.json()
            state.stage = "seller_active"
            data.pop("edit_target", None)
            data.pop("menu_cache", None)
            state.onboarding_data = data
            await save_conversation_state(state)
            await wa.send_text(
                phone,
                f"Updated! *{updated['name']}* — ₦{float(updated['price']):,.0f}",
            )
        else:
            await wa.send_text(phone, "Could not update the item. Please try again.")
    except Exception as exc:
        logger.error("edit_menu_item_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong. Please try again.")


# ── View helpers ──────────────────────────────────────────────────────────────

async def _show_menu(state, phone, wa):
    items = await _fetch_menu_items(state, phone, wa)
    if items is None:
        return
    if not items:
        await wa.send_text(
            phone,
            "Your menu is empty.\n\nReply *add item* to add your first item.",
        )
        return

    lines = "\n".join(
        f"{i+1}. {it['name']} — ₦{float(it['price']):,.0f}"
        for i, it in enumerate(items)
    )
    await wa.send_text(phone, f"*Your Menu ({len(items)} items)*\n\n{lines}")


async def _show_orders(state, phone, wa):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders/seller/{phone}")
        if resp.status_code != 200:
            raise Exception(f"Status {resp.status_code}")
        orders = resp.json()
    except Exception as exc:
        logger.error("fetch_seller_orders_failed", error=str(exc))
        await wa.send_text(phone, "Could not load your orders right now. Try again.")
        return

    if not orders:
        await wa.send_text(phone, "No orders yet. Reply *open* to start receiving orders.")
        return

    status_labels = {
        "pending":        "⏳ Waiting",
        "confirmed":      "✅ Confirmed",
        "food_ready":     "🍽 Food Ready",
        "rider_assigned": "🚴 Rider Coming",
        "picked_up":      "📦 Picked Up",
        "delivered":      "✓ Delivered",
        "cancelled":      "✗ Cancelled",
    }

    lines = []
    for o in orders[:10]:
        label    = status_labels.get(o["status"], o["status"])
        total    = float(o["total_amount"])
        items    = o.get("items", [])
        summary  = ", ".join(f"{it.get('name','?')} x{it.get('quantity',1)}" for it in items[:3])
        lines.append(f"{label} — ₦{total:,.0f}\n   {summary}")

    await wa.send_text(phone, "*Recent Orders*\n\n" + "\n\n".join(lines))


async def _show_store_info(state, phone, wa):
    seller_id = (state.onboarding_data or {}).get("seller_id")
    if not seller_id:
        await wa.send_text(phone, "Could not find your profile. Please contact support.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}")
        if resp.status_code != 200:
            raise Exception(f"Status {resp.status_code}")
        s = resp.json()
    except Exception as exc:
        logger.error("fetch_store_info_failed", error=str(exc))
        await wa.send_text(phone, "Could not load your store info right now.")
        return

    status = "🟢 Open" if s.get("is_available") else "🔴 Closed"
    cats   = ", ".join(s.get("food_categories") or []) or "—"
    addr   = s.get("address_text") or "—"
    rating = s.get("rating", 0)
    orders = s.get("total_orders", 0)

    await wa.send_text(
        phone,
        f"*{s['business_name']}*\n"
        f"Status: {status}\n"
        f"Food types: {cats}\n"
        f"Address: {addr}\n"
        f"Rating: ★{float(rating):.1f}\n"
        f"Total orders: {orders}\n\n"
        "Reply *open* or *close* to change status.\n"
        "Reply *add item* / *remove item* / *edit item* to manage your menu.",
    )


async def _send_help(state, phone, wa):
    seller_id = (state.onboarding_data or {}).get("seller_id")
    status_line = ""
    if seller_id:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}")
            if resp.status_code == 200:
                s = resp.json()
                if s.get("is_available"):
                    status_line = "Your store is currently *open* 🟢\n\n"
                else:
                    status_line = "Your store is currently *closed* 🔴\n\n"
        except Exception:
            pass

    await wa.send_text(
        phone,
        f"{status_line}"
        "Here's what you can do:\n\n"
        "*Store*\n"
        "• *open* — start receiving orders\n"
        "• *close* — pause your store\n"
        "• *my store* — view store info\n\n"
        "*Menu*\n"
        "• *my menu* — view all items\n"
        "• *add item* — add a new item\n"
        "• *remove item* — delete an item\n"
        "• *edit item* — change a name or price\n\n"
        "*Orders*\n"
        "• *my orders* — see recent orders\n"
        "• *food ready* — call a rider for active order",
    )


# ── Food ready ────────────────────────────────────────────────────────────────

async def _trigger_food_ready(phone, state, wa):
    order_id = state.active_order_id
    if not order_id:
        await wa.send_text(phone, "No active order found. Is there a specific order that's ready?")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/food-ready")
        if resp.status_code == 200:
            await wa.send_text(phone, "Food marked as ready! We're finding a rider now.")
        else:
            await wa.send_text(phone, "Could not update the order. Please try again.")
    except Exception as exc:
        logger.error("food_ready_trigger_failed", error=str(exc), order_id=order_id)
        await wa.send_text(phone, "Something went wrong. Please try again.")


async def _toggle_availability(phone, state, available, wa):
    seller_id = (state.onboarding_data or {}).get("seller_id")
    if not seller_id:
        await wa.send_text(phone, "Could not find your seller profile. Please contact support.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/availability",
                json={"is_available": available},
            )
        if available:
            await wa.send_text(
                phone,
                "Your store is now *open* 🟢\n\nBuyers near you can find and order from you.",
            )
        else:
            await wa.send_text(
                phone,
                "Your store is now *closed* 🔴\n\nYou won't receive new orders until you reopen.",
            )
    except Exception as exc:
        logger.error("toggle_availability_failed", error=str(exc))
        await wa.send_text(phone, "Could not update your availability right now. Try again.")


# ── Onboarding completion ─────────────────────────────────────────────────────

async def _complete_onboarding(state, phone, wa):
    data       = state.onboarding_data or {}
    menu_items = data.get("menu_items", [])

    if not menu_items:
        await wa.send_text(phone, "Please add at least one menu item before finishing.")
        return

    lat = data.get("lat", 0)
    lng = data.get("lng", 0)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Reverse-geocode to get a human-readable address
            address_text = ""
            try:
                geo_resp = await client.post(
                    f"{GEO_SERVICE_URL}/parse-location",
                    json={"lat": lat, "lng": lng},
                )
                if geo_resp.status_code == 200:
                    address_text = geo_resp.json().get("address_text", "")
            except Exception:
                pass

            # Create seller record
            seller_resp = await client.post(
                f"{SELLER_SERVICE_URL}/sellers",
                json={
                    "phone_number":   phone,
                    "business_name":  data.get("business_name", "My Store"),
                    "food_categories": data.get("food_categories", []),
                    "latitude":        lat,
                    "longitude":       lng,
                    "address_text":    address_text,
                },
            )

            if seller_resp.status_code == 409:
                # Already exists — fetch the existing seller
                existing = await client.get(f"{SELLER_SERVICE_URL}/sellers/by-phone/{phone}")
                seller_id = existing.json().get("id")
            elif seller_resp.status_code in (200, 201):
                seller_id = seller_resp.json().get("id")
            else:
                raise Exception(f"Seller creation failed: {seller_resp.status_code} — {seller_resp.text}")

            if not seller_id:
                raise Exception("Seller ID missing from response")

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
        biz_name  = data.get("business_name", "Your store")
        addr_line = f"\nAddress: {address_text}" if address_text else ""

        await wa.send_text(
            phone,
            f"🎉 *{biz_name}* is ready!\n"
            f"{addr_line}\n\n"
            f"*Your menu ({len(menu_items)} items):*\n{item_list}\n\n"
            "Reply *open* to start receiving orders.\n"
            "Reply *help* anytime to see all commands.",
        )

    except Exception as exc:
        logger.error("seller_onboarding_failed", error=str(exc))
        await wa.send_text(
            phone,
            "Something went wrong setting up your store. Please try again.\n"
            "Your progress is saved — just send *done* to retry.",
        )


# ── Shared helpers ────────────────────────────────────────────────────────────

async def _fetch_menu_items(state, phone, wa) -> list | None:
    """Fetch menu from seller service. Returns None on error (error already sent to user)."""
    seller_id = (state.onboarding_data or {}).get("seller_id")
    if not seller_id:
        await wa.send_text(phone, "Could not find your seller profile. Please contact support.")
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SELLER_SERVICE_URL}/sellers/{seller_id}/menu")
        if resp.status_code == 200:
            return resp.json()
        raise Exception(f"Status {resp.status_code}")
    except Exception as exc:
        logger.error("fetch_menu_failed", error=str(exc))
        await wa.send_text(phone, "Could not load your menu right now. Try again.")
        return None


def _parse_menu_line(text: str) -> tuple[str | None, float | None, str | None]:
    """
    Parse 'Name — price' variants.
    Returns (name, price, error_message). Error is None on success.
    Accepts: em dash, hyphen, colon, equals  as separators.
    Also accepts plain 'Name 1500' (last token is number).
    """
    # Try named separator: — - : =
    match = re.split(r"\s*[—\-:=]\s*", text, maxsplit=1)
    if len(match) == 2:
        name      = match[0].strip().title()
        price_str = re.sub(r"[^\d.]", "", match[1])
        if name and price_str:
            try:
                price = float(price_str)
                err   = _validate_item(name, price)
                return (name, price, err) if not err else (None, None, err)
            except ValueError:
                pass

    # Try: last space-separated token is a number
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2:
        price_str = re.sub(r"[^\d.]", "", parts[1])
        if price_str:
            try:
                name  = parts[0].strip().title()
                price = float(price_str)
                err   = _validate_item(name, price)
                return (name, price, err) if not err else (None, None, err)
            except ValueError:
                pass

    return (
        None, None,
        "I couldn't read that. Please send it like:\n*Jollof Rice — 1500*",
    )


def _validate_item(name: str, price: float) -> str | None:
    """Return an error string, or None if valid."""
    if len(name) < 2:
        return "Item name is too short."
    if len(name) > 100:
        return "Item name is too long (max 100 characters)."
    if price < MIN_PRICE:
        return f"Price must be at least ₦{MIN_PRICE:,}."
    if price > MAX_PRICE:
        return f"Price must be ₦{MAX_PRICE:,} or less."
    return None
