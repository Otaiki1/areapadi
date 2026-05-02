from __future__ import annotations
from decimal import Decimal
from typing import Any
from pydantic import BaseModel, Field


class OrderItemSchema(BaseModel):
    name: str
    quantity: int = Field(ge=1)
    unit_price: float
    subtotal: float


class CreateOrderRequest(BaseModel):
    buyer_id: str
    seller_id: str
    items: list[OrderItemSchema]
    subtotal: float
    delivery_fee: float
    platform_commission: float = 0.0
    platform_delivery_margin: float = 0.0
    total_amount: float
    delivery_address: str | None = None
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    buyer_notes: str | None = None
    buyer_phone: str | None = None


class UpdateStatusRequest(BaseModel):
    status: str


class PaymentUpdateRequest(BaseModel):
    payment_status: str
    paystack_reference: str | None = None


class CancelOrderRequest(BaseModel):
    reason: str


class RateOrderRequest(BaseModel):
    food_rating: int = Field(ge=1, le=5)
    delivery_rating: int = Field(ge=1, le=5)


class OrderResponse(BaseModel):
    id: str
    buyer_id: str | None
    seller_id: str | None
    rider_id: str | None
    status: str
    items: list[dict]
    subtotal: float
    delivery_fee: float
    total_amount: float
    payment_status: str
    delivery_address: str | None
    buyer_notes: str | None
    created_at: str

    model_config = {"from_attributes": True}
