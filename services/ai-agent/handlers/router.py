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

from claude_client import call_claude_json, HAIKU
from prompts import ROLE_DETECTION
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
    """Check DB for existing buyer/seller/rider and restore state if found."""
    phone = message.phone_number
    try:
        async with AsyncSessionLocal() as session:
            # Check buyers
            buyer_row = (await session.execute(
                sa_text("SELECT id FROM buyers WHERE phone_number = :p LIMIT 1"),
                {"p": phone},
            )).fetchone()
            if buyer_row:
                state.user_role = "buyer"
                state.stage = "idle" if state.location_lat else "awaiting_location"
                await save_conversation_state(state)
                return

            # Check sellers
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

    except Exception as exc:
        logger.error("db_hydration_failed", error=str(exc))


async def _handle_new_user(state: ConversationState, message: MessagePayload) -> None:
    """Detect role from first message and onboard accordingly."""
    wa = get_whatsapp_client()
    phone = state.phone_number

    # Greet if they sent something unintelligible (image, location, etc.)
    if not message.text:
        await wa.send_text(
            phone,
            "Welcome to Areapadi! Are you here to:\n1. Order food\n2. Sell food\n3. Deliver food\n\nJust reply with 1, 2, or 3.",
        )
        return

    # Quick numeric shortcuts
    text = message.text.strip()
    if text == "1":
        role, reply = "buyer", "Welcome! Let's get you some food. Share your location so I can find sellers near you."
    elif text == "2":
        role, reply = "seller", "Welcome! Let's set up your store on Areapadi. What is your business name?"
    elif text == "3":
        role, reply = "rider", "Welcome! Let's get you registered as a rider. What is your full name?"
    else:
        parsed = await call_claude_json(
            ROLE_DETECTION,
            f"User's first message: {message.text}",
            model=HAIKU,
        )
        role = parsed.get("role", "buyer")
        reply = parsed.get("reply_text", "Welcome to Areapadi!")

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
