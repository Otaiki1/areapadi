"""
Payment Service — Paystack integration for payment link generation,
webhook handling, and seller disbursement.
Port 8007.
"""
from __future__ import annotations
import os
import sys
import hmac
import hashlib
import json
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.logger import get_logger, hash_phone
from shared.whatsapp_client import get_whatsapp_client

logger = get_logger("payment-service")

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_BASE = "https://api.paystack.co"
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("payment_service_starting")
    yield


app = FastAPI(title="Areapadi Payment Service", version="1.0.0", lifespan=lifespan)


def verify_paystack_signature(payload: bytes, signature: str) -> bool:
    """Verify Paystack HMAC-SHA512 webhook signature."""
    if not signature or not PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode(),
        payload,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def paystack_headers() -> dict:
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


class InitializeRequest(BaseModel):
    order_id: str
    amount_kobo: int
    buyer_email: str
    buyer_phone: str
    metadata: dict = {}


class DisburseRequest(BaseModel):
    order_id: str
    seller_recipient_code: str
    amount_kobo: int
    reason: str = "Areapadi order payout"


@app.post("/initialize")
async def initialize_payment(req: InitializeRequest):
    """
    Create a Paystack payment transaction and return the authorization URL.
    amount_kobo is in kobo (N1 = 100 kobo).
    """
    payload = {
        "email": req.buyer_email,
        "amount": req.amount_kobo,
        "reference": f"AREAPADI-{req.order_id[:8].upper()}",
        "metadata": {
            "order_id": req.order_id,
            "buyer_phone": req.buyer_phone,
            **req.metadata,
        },
        "channels": ["card", "bank", "ussd", "mobile_money"],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PAYSTACK_BASE}/transaction/initialize",
                json=payload,
                headers=paystack_headers(),
            )
            data = resp.json()

        if not data.get("status"):
            logger.error("paystack_init_failed", message=data.get("message", ""))
            raise HTTPException(status_code=502, detail="Payment initialization failed")

        tx = data["data"]
        logger.info("payment_initialized", order_id=req.order_id, reference=tx["reference"])
        return JSONResponse({
            "reference": tx["reference"],
            "authorization_url": tx["authorization_url"],
            "access_code": tx["access_code"],
        })

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("paystack_init_exception", error=str(exc), order_id=req.order_id)
        raise HTTPException(status_code=500, detail="Payment service error")


@app.post("/webhook")
async def paystack_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Paystack payment events.
    Verifies HMAC-SHA512 signature. On charge.success, updates order payment status.
    Always returns 200 (Paystack retries on non-200).
    """
    raw_body = await request.body()
    sig = request.headers.get("x-paystack-signature", "")

    if not verify_paystack_signature(raw_body, sig):
        logger.warning("invalid_paystack_signature")
        return JSONResponse({"status": "ok"})  # Return 200 to stop retries

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "ok"})

    event = body.get("event", "")
    data = body.get("data", {})

    logger.info("paystack_webhook_received", event=event)

    if event == "charge.success":
        background_tasks.add_task(_handle_charge_success, data)
    elif event == "charge.failed":
        background_tasks.add_task(_handle_charge_failed, data)

    return JSONResponse({"status": "ok"})


async def _handle_charge_success(data: dict) -> None:
    """Update order payment status and trigger order confirmation flow."""
    reference = data.get("reference", "")
    metadata = data.get("metadata", {})
    order_id = metadata.get("order_id", "")
    buyer_phone = metadata.get("buyer_phone", "")

    if not order_id:
        logger.warning("charge_success_no_order_id", reference=reference)
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{ORDER_SERVICE_URL}/orders/{order_id}/payment",
                json={"payment_status": "paid", "paystack_reference": reference},
            )
            if resp.status_code != 200:
                logger.error("order_payment_update_failed", order_id=order_id, status=resp.status_code)
    except Exception as exc:
        logger.error("order_payment_update_exception", error=str(exc), order_id=order_id)

    if buyer_phone:
        wa = get_whatsapp_client()
        await wa.send_text(
            buyer_phone,
            "Payment received! Your order has been confirmed. We are notifying the seller now.",
        )


async def _handle_charge_failed(data: dict) -> None:
    """Notify buyer when payment fails."""
    metadata = data.get("metadata", {})
    buyer_phone = metadata.get("buyer_phone", "")
    order_id = metadata.get("order_id", "")

    logger.warning("charge_failed", order_id=order_id)

    if buyer_phone:
        wa = get_whatsapp_client()
        await wa.send_text(
            buyer_phone,
            "Your payment did not go through. Please try again or use a different card. Reply 'pay' to get a new payment link.",
        )


@app.post("/disburse")
async def disburse_to_seller(req: DisburseRequest):
    """
    Initiate Paystack transfer to seller after delivery confirmation.
    Uses seller's stored paystack_recipient_code.
    """
    payload = {
        "source": "balance",
        "amount": req.amount_kobo,
        "recipient": req.seller_recipient_code,
        "reason": req.reason,
        "reference": f"PAYOUT-{req.order_id[:8].upper()}",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PAYSTACK_BASE}/transfer",
                json=payload,
                headers=paystack_headers(),
            )
            data = resp.json()

        if not data.get("status"):
            logger.error("disbursement_failed", order_id=req.order_id, message=data.get("message", ""))
            raise HTTPException(status_code=502, detail="Disbursement failed")

        logger.info("disbursement_initiated", order_id=req.order_id, amount_kobo=req.amount_kobo)
        return JSONResponse({"status": "initiated", "transfer_code": data["data"].get("transfer_code", "")})

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("disbursement_exception", error=str(exc), order_id=req.order_id)
        raise HTTPException(status_code=500, detail="Disbursement error")


@app.get("/status/{reference}")
async def payment_status(reference: str):
    """Check the current status of a Paystack transaction by reference."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{PAYSTACK_BASE}/transaction/verify/{reference}",
                headers=paystack_headers(),
            )
            data = resp.json()

        if not data.get("status"):
            raise HTTPException(status_code=404, detail="Transaction not found")

        tx = data["data"]
        return JSONResponse({
            "reference": reference,
            "status": tx.get("status"),
            "amount_kobo": tx.get("amount"),
            "paid_at": tx.get("paid_at"),
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("status_check_exception", error=str(exc), reference=reference)
        raise HTTPException(status_code=500, detail="Status check failed")


@app.get("/health")
async def health():
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "payment-service"})
