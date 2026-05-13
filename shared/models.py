from __future__ import annotations
from enum import Enum
from decimal import Decimal
from typing import Any
from pydantic import BaseModel, Field
import uuid
from datetime import datetime


class UserRole(str, Enum):
    buyer = "buyer"
    seller = "seller"
    rider = "rider"


class OrderStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    food_ready = "food_ready"
    rider_assigned = "rider_assigned"
    picked_up = "picked_up"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentStatus(str, Enum):
    unpaid = "unpaid"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


class RiderTier(str, Enum):
    elite = "elite"
    reliable = "reliable"
    developing = "developing"
    at_risk = "at_risk"
    suspended = "suspended"


class VehicleType(str, Enum):
    bike = "bike"
    tricycle = "tricycle"
    car = "car"


class PartnerTier(str, Enum):
    anchor = "anchor"
    standard = "standard"
    on_demand = "on_demand"


class Language(str, Enum):
    en = "en"
    pidgin = "pidgin"


class OrderItem(BaseModel):
    name: str
    quantity: int = Field(ge=1)
    unit_price: Decimal
    subtotal: Decimal


class DeliveryFare(BaseModel):
    total_fee: float
    rider_payout: float
    platform_margin: float
    distance_km: float


class SellerSummary(BaseModel):
    id: str
    business_name: str
    rating: float
    food_categories: list[str]
    distance_text: str
    eta_mins: int
    sample_items: list[str]
    is_available: bool


class MessagePayload(BaseModel):
    phone_number: str
    message_type: str
    text: str | None = None
    location_lat: float | None = None
    location_lng: float | None = None
    interactive_id: str | None = None
    interactive_title: str | None = None
    whatsapp_name: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None
    timestamp: int | None = None


class ConversationState(BaseModel):
    phone_number: str
    user_role: str | None = None
    stage: str = "new_user"
    active_order_id: str | None = None
    active_seller_id: str | None = None
    pending_items: list[Any] | None = None
    location_lat: float | None = None
    location_lng: float | None = None
    onboarding_data: dict[str, Any] | None = None
    last_message_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    message_history: list[dict[str, str]] = Field(default_factory=list)


VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["food_ready", "cancelled"],
    "food_ready": ["rider_assigned", "cancelled"],
    "rider_assigned": ["picked_up", "cancelled"],
    "picked_up": ["delivered"],
    "delivered": [],
    "cancelled": [],
}
