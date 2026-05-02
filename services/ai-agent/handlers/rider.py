"""
Rider conversation handler — onboarding and active rider job management.
Stage 3 in the build plan. Onboarding is fully implemented here.
Active job handling (accept/decline) is wired.
"""
from __future__ import annotations
import os
import uuid
import httpx
from datetime import datetime, timezone
from shared.models import ConversationState, MessagePayload
from shared.redis_client import save_conversation_state
from shared.whatsapp_client import get_whatsapp_client
from shared.logger import get_logger

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
                {"id": "vehicle_bike", "title": "Motorcycle"},
                {"id": "vehicle_tricycle", "title": "Tricycle (Keke)"},
                {"id": "vehicle_car", "title": "Car"},
            ],
        )
        return

    if stage == "new_rider_vehicle":
        vehicle_map = {
            "vehicle_bike": "bike",
            "vehicle_tricycle": "tricycle",
            "vehicle_car": "car",
        }
        vehicle = vehicle_map.get(message.interactive_id or "")
        # Fallback: parse text
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
            "Which area do you work in? E.g. 'Kano Central', 'Sabon Gari', 'Nassarawa'",
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
            "Last step — send your bank details to receive payments.\n\n"
            "Format: BANK NAME, ACCOUNT NUMBER\n"
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
        await _complete_rider_onboarding(state, phone, wa)
        return

    if stage == "rider_active":
        text = (message.text or "").strip().lower()
        interactive_id = message.interactive_id or ""

        if interactive_id.startswith("accept_job_") or text in ("accept", "yes", "ok", "i accept"):
            order_id = interactive_id.replace("accept_job_", "") if interactive_id.startswith("accept_job_") else state.active_order_id
            if order_id:
                await _accept_job(phone, order_id, state, wa)
            return

        if interactive_id.startswith("decline_job_") or text in ("decline", "no", "pass", "i pass"):
            await wa.send_text(phone, "OK, no problem. I'll offer this job to another rider.")
            return

        if text in ("pickup", "picked up", "i have the food"):
            if state.active_order_id:
                await _confirm_pickup(phone, state.active_order_id, state, wa)
            return

        if text in ("delivered", "done", "order delivered"):
            if state.active_order_id:
                await _confirm_delivery(phone, state.active_order_id, state, wa)
            return

        await wa.send_text(
            phone,
            "You're registered as a rider. You'll get a message when a delivery job is near you.\n\n"
            "Reply 'pickup' when you've collected the food, 'delivered' when it's dropped off.",
        )
        return

    await wa.send_text(phone, "Welcome! Reply with your full name to start your rider registration.")


async def _complete_rider_onboarding(state: ConversationState, phone: str, wa) -> None:
    data = state.onboarding_data or {}
    try:
        from shared.db import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        import uuid as _uuid

        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    INSERT INTO riders (phone_number, full_name, vehicle_type, service_zone,
                                       bank_account_number, onboarding_complete)
                    VALUES (:phone, :name, :vehicle, :zone, :account, TRUE)
                    ON CONFLICT (phone_number) DO UPDATE
                      SET full_name = EXCLUDED.full_name,
                          vehicle_type = EXCLUDED.vehicle_type,
                          service_zone = EXCLUDED.service_zone,
                          bank_account_number = EXCLUDED.bank_account_number,
                          onboarding_complete = TRUE
                """),
                {
                    "phone": phone,
                    "name": data.get("full_name"),
                    "vehicle": data.get("vehicle_type", "bike"),
                    "zone": data.get("service_zone"),
                    "account": data.get("account_number"),
                },
            )
            await session.commit()

        state.stage = "rider_active"
        state.onboarding_data = {}
        await save_conversation_state(state)

        await wa.send_text(
            phone,
            f"You're all set, {data.get('full_name', '')}!\n\n"
            "You will receive WhatsApp messages when delivery jobs are near you.\n"
            "Reply 'accept' or 'decline' when offered a job.\n\n"
            "Good luck on the road!",
        )
    except Exception as exc:
        logger.error("rider_onboarding_failed", error=str(exc))
        await wa.send_text(phone, "Something went wrong with your registration. Please try again.")


async def _accept_job(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{RIDER_DISPATCH_URL}/accept", json={"order_id": order_id, "rider_phone": phone})
        state.active_order_id = order_id
        await save_conversation_state(state)
        await wa.send_text(phone, "Job accepted! Head to the seller to pick up the order. Reply 'pickup' when you have it.")
    except Exception as exc:
        logger.error("accept_job_failed", error=str(exc), order_id=order_id)
        await wa.send_text(phone, "Could not accept job right now. Please try again.")


async def _confirm_pickup(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/rider-pickup")
        await wa.send_text(phone, "Pickup confirmed! Now head to the delivery address. Reply 'delivered' when done.")
    except Exception as exc:
        logger.error("confirm_pickup_failed", error=str(exc))
        await wa.send_text(phone, "Could not confirm pickup. Try again.")


async def _confirm_delivery(phone: str, order_id: str, state: ConversationState, wa) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{ORDER_SERVICE_URL}/orders/{order_id}/delivery-confirm")
        state.active_order_id = None
        await save_conversation_state(state)
        await wa.send_text(phone, "Delivery confirmed! Great work. You'll receive your payout shortly.")
    except Exception as exc:
        logger.error("confirm_delivery_failed", error=str(exc))
        await wa.send_text(phone, "Could not confirm delivery. Try again.")
