"""
Rider conversation handler — onboarding and active rider job management.

Onboarding stages:
  new_rider_name     → full name
  new_rider_vehicle  → vehicle type (button)
  new_rider_zone     → service area text
  new_rider_bank     → bank name + account number
  new_rider_location → location pin  ← creates DB record and activates rider
  rider_active       → receives job offers, handles pickup/delivery confirmations
"""
from __future__ import annotations
import os
import httpx
from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.db import AsyncSessionLocal
from shared.logger import get_logger
from sqlalchemy import text as sa_text

logger = get_logger("rider-handler")
RIDER_DISPATCH_URL = os.getenv("RIDER_DISPATCH_URL", "http://localhost:8004")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003")


async def handle_rider_message(
    state: ConversationState,
    message: MessagePayload,
) -> None:
    wa = get_whatsapp_client()
    phone = state.phone_number
    stage = state.stage

    # ── Onboarding ────────────────────────────────────────────────────────────

    if stage == "new_rider_name":
        name = (message.text or "").strip()
        if not name:
            await wa.send_text(phone, "What is your full name?")
            return
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["full_name"] = name
        state.stage = "new_rider_vehicle"
        await save_conversation_state(state)
        await wa.send_interactive_buttons(
            phone,
            f"Welcome, {name}! What vehicle do you ride?",
            buttons=[
                {"id": "vehicle_bike",     "title": "Motorcycle"},
                {"id": "vehicle_tricycle", "title": "Tricycle (Keke)"},
                {"id": "vehicle_car",      "title": "Car"},
            ],
        )
        return

    if stage == "new_rider_vehicle":
        vehicle_map = {
            "vehicle_bike":     "bike",
            "vehicle_tricycle": "tricycle",
            "vehicle_car":      "car",
        }
        vehicle = vehicle_map.get(message.interactive_id or "")
        if not vehicle:
            text = (message.text or "").lower()
            if "keke" in text or "tricycle" in text:
                vehicle = "tricycle"
            elif "car" in text:
                vehicle = "car"
            else:
                vehicle = "bike"

        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["vehicle_type"] = vehicle
        state.stage = "new_rider_zone"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Which area do you mainly work in? E.g. *Kano Central*, *Sabon Gari*, *Wuse*",
        )
        return

    if stage == "new_rider_zone":
        zone = (message.text or "").strip()
        if not zone:
            await wa.send_text(phone, "Which area do you work in?")
            return
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["service_zone"] = zone
        state.stage = "new_rider_bank"
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Send your bank details to receive payments.\n\n"
            "Format: *BANK NAME, ACCOUNT NUMBER*\n"
            "Example: Access Bank, 0123456789",
        )
        return

    if stage == "new_rider_bank":
        text = (message.text or "").strip()
        parts = [p.strip() for p in text.split(",", 1)]
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await wa.send_text(
                phone,
                "Please send your bank details like this:\nAccess Bank, 0123456789",
            )
            return

        bank_name, account_number = parts[0], parts[1].strip()
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["bank_name"] = bank_name
        state.onboarding_data["account_number"] = account_number
        state.stage = "new_rider_location"
        await save_conversation_state(state)
        await wa.send_location_request(
            phone,
            "Last step! Share your current location so we can match you with nearby orders.",
        )
        return

    if stage == "new_rider_location":
        if message.message_type != "location":
            await wa.send_location_request(
                phone,
                "Please share your location pin to complete registration.",
            )
            return
        state.onboarding_data = state.onboarding_data or {}
        state.onboarding_data["lat"] = message.location_lat
        state.onboarding_data["lng"] = message.location_lng
        await _complete_rider_onboarding(state, phone, wa)
        return

    # ── Active rider ──────────────────────────────────────────────────────────

    if stage == "rider_active":
        # Location share → update current position
        if message.message_type == "location":
            await _update_rider_location(phone, message.location_lat, message.location_lng)
            await wa.send_text(phone, "Location updated! You'll receive delivery jobs near you.")
            return

        text = (message.text or "").strip().lower()
        interactive_id = message.interactive_id or ""

        if interactive_id.startswith("accept_job_") or text in ("accept", "yes", "ok", "i accept"):
            order_id = (
                interactive_id.replace("accept_job_", "")
                if interactive_id.startswith("accept_job_")
                else state.active_order_id
            )
            if order_id:
                await _accept_job(phone, order_id, state, wa)
            return

        if interactive_id.startswith("decline_job_") or text in ("decline", "no", "pass", "i pass"):
            order_id = (
                interactive_id.replace("decline_job_", "")
                if interactive_id.startswith("decline_job_")
                else state.active_order_id
            )
            if order_id:
                await _decline_job(phone, order_id, wa)
            else:
                await wa.send_text(phone, "OK, no problem.")
            return

        if text in ("pickup", "picked up", "i have the food", "i have it"):
            if state.active_order_id:
                await _confirm_pickup(phone, state.active_order_id, state, wa)
            else:
                await wa.send_text(phone, "No active order. You'll be notified when there's a job near you.")
            return

        if text in ("delivered", "done", "order delivered", "i delivered", "dropped off"):
            if state.active_order_id:
                await _confirm_delivery(phone, state.active_order_id, state, wa)
            else:
                await wa.send_text(phone, "No active order found.")
            return

        await wa.send_text(
            phone,
            "You're registered as a rider.\n\n"
            "You'll receive a WhatsApp message when a delivery job is near you.\n\n"
            "• Share your location at any time to update your position\n"
            "• Reply *accept* when offered a job\n"
            "• Reply *pickup* once you have the food\n"
            "• Reply *delivered* when the order is dropped off",
        )
        return

    # Fallback: prompt to start registration
    await wa.send_text(phone, "Welcome! Reply with your full name to start your rider registration.")


# ── Onboarding completion ─────────────────────────────────────────────────────

async def _complete_rider_onboarding(state: ConversationState, phone: str, wa) -> None:
    """Persist rider to DB with all onboarding data including location."""
    data = state.onboarding_data or {}
    lat = data.get("lat")
    lng = data.get("lng")

    location_sql = (
        "ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography"
        if lat is not None and lng is not None
        else "NULL"
    )
    params: dict = {
        "phone":   phone,
        "name":    data.get("full_name"),
        "vehicle": data.get("vehicle_type", "bike"),
        "zone":    data.get("service_zone"),
        "account": data.get("account_number"),
    }
    if lat is not None and lng is not None:
        params["lat"] = float(lat)
        params["lng"] = float(lng)

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text(f"""
                    INSERT INTO riders (
                        phone_number, full_name, vehicle_type, service_zone,
                        bank_account_number, onboarding_complete,
                        current_location, is_available
                    )
                    VALUES (
                        :phone, :name, :vehicle, :zone,
                        :account, TRUE,
                        {location_sql}, TRUE
                    )
                    ON CONFLICT (phone_number) DO UPDATE
                      SET full_name            = EXCLUDED.full_name,
                          vehicle_type         = EXCLUDED.vehicle_type,
                          service_zone         = EXCLUDED.service_zone,
                          bank_account_number  = EXCLUDED.bank_account_number,
                          onboarding_complete  = TRUE,
                          current_location     = EXCLUDED.current_location,
                          is_available         = TRUE
                """),
                params,
            )
            await session.commit()

        state.stage = "rider_active"
        state.onboarding_data = {}
        await save_conversation_state(state)

        await wa.send_text(
            phone,
            f"You're all set, {data.get('full_name', '')}!\n\n"
            "You will receive WhatsApp messages when delivery jobs are near you.\n"
            "Reply *accept* or *decline* when offered a job.\n\n"
            "Good luck on the road!",
        )
    except Exception as exc:
        logger.error("rider_onboarding_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong with your registration. Please try again.")


# ── Job flow ──────────────────────────────────────────────────────────────────

async def _accept_job(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{RIDER_DISPATCH_URL}/accept",
                json={"order_id": order_id, "rider_phone": phone},
            )
        state.active_order_id = order_id
        await save_conversation_state(state)
        await wa.send_text(
            phone,
            "Job accepted! Head to the seller to pick up the order.\nReply *pickup* when you have it.",
        )
    except Exception as exc:
        logger.error("accept_job_failed", error=str(exc), order_id=order_id)
        await wa.send_text(phone, "Could not accept job right now. Please try again.")


async def _decline_job(phone: str, order_id: str, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{RIDER_DISPATCH_URL}/decline",
                json={"order_id": order_id, "rider_phone": phone},
            )
    except Exception as exc:
        logger.error("decline_job_failed", error=str(exc))
    await wa.send_text(phone, "OK, no problem. The job has been passed on.")


async def _confirm_pickup(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/rider-pickup")
        await wa.send_text(
            phone,
            "Pickup confirmed! Head to the delivery address.\nReply *delivered* when you've dropped it off.",
        )
    except Exception as exc:
        logger.error("confirm_pickup_failed", error=str(exc))
        await wa.send_text(phone, "Could not confirm pickup. Try again.")


async def _confirm_delivery(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/delivery-confirm")
        state.active_order_id = None
        await save_conversation_state(state)
        await wa.send_text(phone, "Delivery confirmed! Great work. Payout will be processed shortly.")
    except Exception as exc:
        logger.error("confirm_delivery_failed", error=str(exc))
        await wa.send_text(phone, "Could not confirm delivery. Try again.")


async def _update_rider_location(phone: str, lat: float, lng: float) -> None:
    """Update rider's current_location in DB so dispatch can find them."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    UPDATE riders
                    SET current_location = ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                        is_available     = TRUE
                    WHERE phone_number = :phone
                """),
                {"lng": lng, "lat": lat, "phone": phone},
            )
            await session.commit()
    except Exception as exc:
        logger.error("update_rider_location_failed", error=str(exc))
