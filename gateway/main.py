"""
Gateway service — receives all WhatsApp webhook events from Meta Cloud API.
Verifies signatures, parses messages, forwards to AI Agent service.
Returns 200 immediately to satisfy WhatsApp's 20-second response requirement.
"""
from __future__ import annotations
import os
import hashlib
import hmac
import json
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

from shared.logger import get_logger, hash_phone
from shared.whatsapp_client import get_whatsapp_client
from shared.models import MessagePayload

logger = get_logger("gateway")

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_WEBHOOK_SECRET = os.getenv("WHATSAPP_WEBHOOK_SECRET", "")
AI_AGENT_URL = os.getenv("AI_AGENT_URL", "http://localhost:8001")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("gateway_starting", port=os.getenv("GATEWAY_PORT", "8000"))
    yield
    logger.info("gateway_stopped")


app = FastAPI(title="Areapadi Gateway", version="1.0.0", lifespan=lifespan)


def verify_whatsapp_signature(payload: bytes, signature_header: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta Cloud API."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        WHATSAPP_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)


def parse_whatsapp_message(body: dict) -> MessagePayload | None:
    """
    Extract a structured MessagePayload from the Meta webhook body.
    Returns None for status updates and non-message events.
    """
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        messages = value.get("messages")
        if not messages:
            return None

        msg = messages[0]
        contacts = value.get("contacts", [{}])
        contact = contacts[0] if contacts else {}

        phone_number = msg.get("from", "")
        msg_type = msg.get("type", "")
        whatsapp_name = contact.get("profile", {}).get("name")
        timestamp = int(msg.get("timestamp", 0))

        payload = MessagePayload(
            phone_number=phone_number,
            message_type=msg_type,
            whatsapp_name=whatsapp_name,
            timestamp=timestamp,
        )

        if msg_type == "text":
            payload.text = msg.get("text", {}).get("body", "").strip()

        elif msg_type == "location":
            loc = msg.get("location", {})
            payload.location_lat = loc.get("latitude")
            payload.location_lng = loc.get("longitude")

        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            itype = interactive.get("type")
            if itype == "button_reply":
                reply = interactive.get("button_reply", {})
                payload.interactive_id = reply.get("id")
                payload.interactive_title = reply.get("title")
            elif itype == "list_reply":
                reply = interactive.get("list_reply", {})
                payload.interactive_id = reply.get("id")
                payload.interactive_title = reply.get("title")

        elif msg_type == "image":
            payload.text = "__image__"

        return payload

    except Exception as exc:
        logger.error("parse_message_failed", error=str(exc))
        return None


async def forward_to_ai_agent(payload: MessagePayload) -> None:
    """Send parsed message to AI Agent service. Called as a background task."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{AI_AGENT_URL}/handle",
                json=payload.model_dump(),
            )
            if resp.status_code != 200:
                logger.error(
                    "ai_agent_forward_failed",
                    status=resp.status_code,
                    phone=hash_phone(payload.phone_number),
                )
    except Exception as exc:
        logger.error(
            "ai_agent_forward_exception",
            error=str(exc),
            phone=hash_phone(payload.phone_number),
        )


@app.get("/webhook")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Meta webhook verification handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        logger.info("webhook_verified")
        return PlainTextResponse(challenge)

    logger.warning("webhook_verification_failed", mode=mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Receive inbound WhatsApp messages from Meta Cloud API.
    Verifies HMAC signature, parses payload, forwards to AI Agent in background.
    Always returns 200 immediately — WhatsApp requires response within 20s.
    """
    raw_body = await request.body()

    sig = request.headers.get("X-Hub-Signature-256", "")
    if WHATSAPP_WEBHOOK_SECRET and not verify_whatsapp_signature(raw_body, sig):
        logger.warning("invalid_webhook_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "ok"})

    payload = parse_whatsapp_message(body)
    if payload is None:
        # Status update or unsupported event — silently acknowledge
        return JSONResponse({"status": "ok"})

    logger.info(
        "message_received",
        phone=hash_phone(payload.phone_number),
        type=payload.message_type,
    )

    if payload.message_type == "image":
        wa = get_whatsapp_client()
        await wa.send_text(
            payload.phone_number,
            "Hi! I can only read text messages for now. Please type what you'd like to order.",
        )
        return JSONResponse({"status": "ok"})

    background_tasks.add_task(forward_to_ai_agent, payload)
    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "healthy", "service": "gateway"})
