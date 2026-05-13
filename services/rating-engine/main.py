"""
Rating Engine — computes and maintains rider trust scores.
Port 8005.

Called after every delivery completion. Records raw metrics in rider_metrics,
then recomputes the rider's rolling rating_score and updates their tier.

Score formula (0–100):
  40% — buyer delivery rating (1–5 → 0–100)
  25% — response time (accepted within 60s = 100, 180s = 0, linear)
  20% — on-time delivery (within estimated ETA = 100, 2× ETA = 0)
  15% — experience bonus (capped at 15 for 50+ deliveries)

Running score: exponential moving average (α=0.3) so recent deliveries matter more.

Tiers:
  elite       ≥ 80
  reliable    ≥ 65
  developing  ≥ 50
  at_risk     ≥ 35
  suspended   < 35
"""
from __future__ import annotations
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from shared.db import AsyncSessionLocal
from shared.logger import get_logger

logger = get_logger("rating-engine")

ALPHA = 0.3  # EMA smoothing factor


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("rating_engine_starting")
    yield
    logger.info("rating_engine_stopped")


app = FastAPI(title="Areapadi Rating Engine", version="1.0.0", lifespan=lifespan)


class RecordDeliveryRequest(BaseModel):
    rider_id: str
    order_id: str
    buyer_rating: int = Field(ge=1, le=5)
    response_time_secs: int | None = None
    estimated_delivery_secs: int | None = None
    actual_delivery_secs: int | None = None
    had_integrity_issue: bool = False


class RiderScoreResponse(BaseModel):
    rider_id: str
    rating_score: float
    rating_tier: str
    total_deliveries: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/record")
async def record_delivery(req: RecordDeliveryRequest) -> JSONResponse:
    """
    Record a completed delivery's metrics and recompute the rider's score.
    Called by Order Service after delivery confirmation.
    """
    delivery_score = _compute_delivery_score(req)

    async with AsyncSessionLocal() as session:
        # Fetch current rider score
        row = (await session.execute(
            sa_text("SELECT rating_score, total_deliveries FROM riders WHERE id = :id"),
            {"id": req.rider_id},
        )).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Rider not found")

        current_score = float(row[0] or 50.0)
        total_deliveries = (row[1] or 0) + 1

        # Exponential moving average
        new_score = round(ALPHA * delivery_score + (1 - ALPHA) * current_score, 2)
        new_tier = _score_to_tier(new_score)

        # Record metrics
        await session.execute(
            sa_text("""
                INSERT INTO rider_metrics (
                    rider_id, order_id, response_time_secs,
                    estimated_delivery_secs, actual_delivery_secs,
                    buyer_rating, had_integrity_issue, computed_score_snapshot
                ) VALUES (
                    :rider_id, :order_id, :response_time,
                    :est_delivery, :actual_delivery,
                    :buyer_rating, :integrity_issue, :score
                )
            """),
            {
                "rider_id": req.rider_id,
                "order_id": req.order_id,
                "response_time": req.response_time_secs,
                "est_delivery": req.estimated_delivery_secs,
                "actual_delivery": req.actual_delivery_secs,
                "buyer_rating": req.buyer_rating,
                "integrity_issue": req.had_integrity_issue,
                "score": new_score,
            },
        )

        # Update rider record
        await session.execute(
            sa_text("""
                UPDATE riders
                SET rating_score = :score,
                    rating_tier = :tier,
                    total_deliveries = :total
                WHERE id = :id
            """),
            {
                "score": new_score,
                "tier": new_tier,
                "total": total_deliveries,
                "id": req.rider_id,
            },
        )

        # Suspend automatically if score drops below threshold
        if new_score < 35:
            await session.execute(
                sa_text("UPDATE riders SET is_suspended = TRUE WHERE id = :id"),
                {"id": req.rider_id},
            )
            logger.warning("rider_auto_suspended", rider_id=req.rider_id, score=new_score)

        await session.commit()

    logger.info("delivery_recorded", rider_id=req.rider_id, score=new_score, tier=new_tier)
    return JSONResponse({"rider_id": req.rider_id, "new_score": new_score, "tier": new_tier})


@app.get("/riders/{rider_id}/score")
async def get_rider_score(rider_id: str) -> JSONResponse:
    """Return a rider's current score and tier."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            sa_text("""
                SELECT rating_score, rating_tier, total_deliveries
                FROM riders WHERE id = :id
            """),
            {"id": rider_id},
        )).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Rider not found")

    return JSONResponse({
        "rider_id": rider_id,
        "rating_score": float(row[0] or 50),
        "rating_tier": row[1] or "developing",
        "total_deliveries": row[2] or 0,
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "rating-engine"})


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _compute_delivery_score(req: RecordDeliveryRequest) -> float:
    """Compute a 0–100 score for a single delivery."""
    # 40% — buyer rating
    buyer_component = ((req.buyer_rating - 1) / 4) * 100

    # 25% — response time (60s = full, 180s = 0, linear)
    if req.response_time_secs is not None:
        rt = max(0, min(req.response_time_secs, 180))
        response_component = max(0.0, (1 - rt / 180) * 100)
    else:
        response_component = 70.0  # neutral if not tracked

    # 20% — on-time delivery
    if req.estimated_delivery_secs and req.actual_delivery_secs:
        ratio = req.actual_delivery_secs / max(req.estimated_delivery_secs, 1)
        if ratio <= 1.0:
            time_component = 100.0
        elif ratio <= 2.0:
            time_component = max(0.0, (2.0 - ratio) * 100)
        else:
            time_component = 0.0
    else:
        time_component = 70.0  # neutral

    # 15% — integrity (0 if had issue)
    integrity_component = 0.0 if req.had_integrity_issue else 100.0

    score = (
        0.40 * buyer_component
        + 0.25 * response_component
        + 0.20 * time_component
        + 0.15 * integrity_component
    )
    return round(score, 2)


def _score_to_tier(score: float) -> str:
    if score >= 80:
        return "elite"
    if score >= 65:
        return "reliable"
    if score >= 50:
        return "developing"
    if score >= 35:
        return "at_risk"
    return "suspended"
