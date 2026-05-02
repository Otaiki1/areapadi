"""
AI Agent Service — the conversational brain of Areapadi.
Receives parsed WhatsApp messages from the Gateway and drives the full
buyer / seller / rider state machines.
Port 8001.
"""
from __future__ import annotations
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.models import MessagePayload
from shared.logger import get_logger, hash_phone
from handlers.router import route_message
from handlers.buyer import send_rating_prompt

logger = get_logger("ai-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ai_agent_starting", port=os.getenv("AI_AGENT_PORT", "8001"))
    yield
    logger.info("ai_agent_stopped")


app = FastAPI(title="Areapadi AI Agent", version="1.0.0", lifespan=lifespan)


class RatingPromptRequest(BaseModel):
    order_id: str


@app.post("/handle")
async def handle(payload: MessagePayload, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Receive a parsed WhatsApp message from the Gateway and route it through
    the conversation state machine. Runs in the background so the gateway
    can ACK the webhook immediately.
    """
    logger.info(
        "handle_message",
        phone=hash_phone(payload.phone_number),
        type=payload.message_type,
    )
    background_tasks.add_task(_safe_route, payload)
    return JSONResponse({"status": "ok"})


async def _safe_route(payload: MessagePayload) -> None:
    """Wrap route_message so exceptions don't crash the background task silently."""
    try:
        await route_message(payload)
    except Exception as exc:
        logger.error(
            "route_message_unhandled_exception",
            phone=hash_phone(payload.phone_number),
            error=str(exc),
            exc_info=True,
        )


@app.post("/prompt-rating")
async def prompt_rating(req: RatingPromptRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Called by Order Service after a delivery is confirmed.
    Sends a rating prompt to the buyer via WhatsApp.
    """
    logger.info("prompt_rating_requested", order_id=req.order_id)
    background_tasks.add_task(send_rating_prompt, req.order_id)
    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health() -> JSONResponse:
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "ai-agent"})
