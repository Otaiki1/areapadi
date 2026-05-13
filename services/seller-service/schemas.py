from __future__ import annotations
from decimal import Decimal
from pydantic import BaseModel, Field
from typing import Any
import uuid


class CreateSellerRequest(BaseModel):
    phone_number: str
    business_name: str
    owner_name: str | None = None
    food_categories: list[str] = []
    latitude: float
    longitude: float
    address_text: str | None = None
    opening_time: str | None = None
    closing_time: str | None = None
    operating_days: list[str] = []


class UpdateSellerRequest(BaseModel):
    business_name: str | None = None
    owner_name: str | None = None
    food_categories: list[str] | None = None
    address_text: str | None = None
    onboarding_step: str | None = None
    onboarding_complete: bool | None = None
    opening_time: str | None = None
    closing_time: str | None = None
    operating_days: list[str] | None = None


class AvailabilityRequest(BaseModel):
    is_available: bool


class SellerResponse(BaseModel):
    id: str
    phone_number: str
    business_name: str
    owner_name: str | None
    food_categories: list[str]
    address_text: str | None
    is_available: bool
    rating: float
    total_orders: int
    total_reviews: int
    onboarding_complete: bool
    onboarding_step: str | None

    model_config = {"from_attributes": True}


class CreateMenuItemRequest(BaseModel):
    name: str
    description: str | None = None
    price: Decimal = Field(gt=0)
    image_url: str | None = None


class UpdateMenuItemRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    is_available: bool | None = None


class MenuItemResponse(BaseModel):
    id: str
    seller_id: str
    name: str
    description: str | None
    price: float
    is_available: bool
    image_url: str | None

    model_config = {"from_attributes": True}


class SearchRequest(BaseModel):
    lat: float
    lng: float
    radius_km: float = Field(default=3.0, ge=0.5, le=20.0)
    query: str


class SearchResultSeller(BaseModel):
    id: str
    business_name: str
    rating: float
    food_categories: list[str]
    distance_text: str
    distance_m: float
    sample_items: list[str]
    is_available: bool
