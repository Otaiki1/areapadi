"""
Message router — determines user role and stage, then delegates to the
appropriate handler. Also handles the new_user detection flow.
"""
from __future__ import annotations
from sqlalchemy import select, text as sa_text

from shared.models import ConversationState, MessagePayload
from shared.redis_client import get_conversation_state, save_conversation_state
from shared.db import AsyncSessionLocal
from shared.whatsapp_client import get_whatsapp_client
from shared.logger import get_logger

from handlers.buyer import handle_buyer_message
from handlers.seller import handle_seller_message
from handlers.rider import handle_rider_message

logger = get_logger("router")


async def route_message(message: MessagePayload) -> None:
    """Load state, hydrate if expired, delegate to role handler."""
    state = await get_conversation_state(message.phone_number)

    # Update message history (keep last 10 turns)
    if message.text:
        state.message_history.append({"role": "user", "content": message.text})
        if len(state.message_history) > 20:
            state.message_history = state.message_history[-20:]

    # Update whatsapp_name if provided
    if message.whatsapp_name and not state.user_role:
        pass  # will be persisted during buyer creation

    # If state has expired (no role) but user exists in DB — re-hydrate
    if state.stage == "new_user" or state.user_role is None:
        await _hydrate_from_db(state, message)

    if state.user_role == "buyer":
        await handle_buyer_message(state, message)
    elif state.user_role == "seller":
        await handle_seller_message(state, message)
    elif state.user_role == "rider":
        await handle_rider_message(state, message)
    else:
        await _handle_new_user(state, message)


async def _hydrate_from_db(state: ConversationState, message: MessagePayload) -> None:
    """Check DB for existing buyer/seller/rider and restore state if found.

    Sellers are checked before buyers so that a phone number registered as a
    seller always lands in the seller flow, even if it also has a stale buyer
    row from early testing.
    """
    phone = message.phone_number
    try:
        async with AsyncSessionLocal() as session:
            # Check sellers first — seller role takes priority
            seller_row = (await session.execute(
                sa_text("SELECT id, onboarding_complete, onboarding_step FROM sellers WHERE phone_number = :p LIMIT 1"),
                {"p": phone},
            )).fetchone()
            if seller_row:
                state.user_role = "seller"
                seller_id = str(seller_row[0])
                onboarding_complete = seller_row[1]
                state.stage = "seller_active" if onboarding_complete else (seller_row[2] or "new_seller_name")
                state.onboarding_data = {"seller_id": seller_id}
                await save_conversation_state(state)
                return

            # Check riders
            rider_row = (await session.execute(
                sa_text("SELECT id, onboarding_complete FROM riders WHERE phone_number = :p LIMIT 1"),
                {"p": phone},
            )).fetchone()
            if rider_row:
                state.user_role = "rider"
                state.stage = "rider_active" if rider_row[1] else "new_rider_name"
                await save_conversation_state(state)
                return

            # Check buyers last — also restore their saved location
            buyer_row = (await session.execute(
                sa_text("""
                    SELECT id,
                           ST_Y(location::geometry) AS lat,
                           ST_X(location::geometry) AS lng
                    FROM buyers WHERE phone_number = :p LIMIT 1
                """),
                {"p": phone},
            )).fetchone()
            if buyer_row:
                state.user_role = "buyer"
                if buyer_row[1] is not None:
                    state.location_lat = float(buyer_row[1])
                    state.location_lng = float(buyer_row[2])
                    state.stage = "idle"
                else:
                    state.stage = "awaiting_location"
                await save_conversation_state(state)
                return

    except Exception as exc:
        logger.error("db_hydration_failed", error=str(exc))


async def _handle_new_user(state: ConversationState, message: MessagePayload) -> None:
    """Detect role from first message and onboard accordingly."""
    wa = get_whatsapp_client()
    phone = state.phone_number

    # Handle role-selection button taps
    role_map = {"role_buyer": "buyer", "role_seller": "seller", "role_rider": "rider"}
    if message.interactive_id in role_map:
        message.text = {"role_buyer": "1", "role_seller": "2", "role_rider": "3"}[message.interactive_id]

    # Greet if no usable text (image, location, unrecognised button, etc.)
    if not message.text:
        await wa.send_interactive_buttons(
            phone,
            "Welcome to Areapadi!\n\nHow can I help you?",
            buttons=[
                {"id": "role_buyer",  "title": "Order food"},
                {"id": "role_seller", "title": "Sell food"},
                {"id": "role_rider",  "title": "Deliver food"},
            ],
        )
        return

    text = message.text.strip().lower()

    # Numeric shortcuts
    if text == "1":
        role = "buyer"
    elif text == "2":
        role = "seller"
    elif text == "3":
        role = "rider"
    # Keyword detection
    elif any(w in text for w in ("order", "food", "eat", "hungry", "buy", "customer")):
        role = "buyer"
    elif any(w in text for w in ("sell", "vendor", "kitchen", "cook", "store", "shop")):
        role = "seller"
    elif any(w in text for w in ("deliver", "rider", "dispatch", "driver", "bike")):
        role = "rider"
    else:
        # Can't determine — ask them to pick
        await wa.send_interactive_buttons(
            phone,
            "Welcome to Areapadi! 🍜\n\nHow can I help you?",
            buttons=[
                {"id": "role_buyer",  "title": "Order food"},
                {"id": "role_seller", "title": "Sell food"},
                {"id": "role_rider",  "title": "Deliver food"},
            ],
        )
        return

    reply = {
        "buyer":  "Welcome! Share your location so I can find food sellers near you.",
        "seller": "Welcome! Let's set up your store. What is your business name?",
        "rider":  "Welcome! Let's get you registered as a rider. What is your full name?",
    }[role]

    state.user_role = role
    if role == "buyer":
        state.stage = "awaiting_location"
        await save_conversation_state(state)
        await wa.send_text(phone, reply)
        await wa.send_location_request(phone, "Please share your location to find food near you.")
    elif role == "seller":
        state.stage = "new_seller_name"
        await save_conversation_state(state)
        await wa.send_text(phone, reply)
    elif role == "rider":
        state.stage = "new_rider_name"
        await save_conversation_state(state)
        await wa.send_text(phone, reply)
