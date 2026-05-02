from __future__ import annotations
import uuid
from sqlalchemy import Column, String, Boolean, Numeric, Integer, Text, TIMESTAMP, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from geoalchemy2 import Geography
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from shared.db import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("buyers.id"))
    seller_id = Column(UUID(as_uuid=True), ForeignKey("sellers.id"))
    rider_id = Column(UUID(as_uuid=True), ForeignKey("riders.id"), nullable=True)
    status = Column(String(50), default="pending", nullable=False)
    items = Column(JSONB, nullable=False)
    subtotal = Column(Numeric(10, 2), nullable=False)
    delivery_fee = Column(Numeric(10, 2), nullable=False)
    platform_commission = Column(Numeric(10, 2))
    platform_delivery_margin = Column(Numeric(10, 2))
    total_amount = Column(Numeric(10, 2), nullable=False)
    payment_reference = Column(String(200))
    paystack_reference = Column(String(200))
    payment_status = Column(String(50), default="unpaid")
    delivery_address = Column(Text)
    delivery_location = Column(Geography(geometry_type="POINT", srid=4326))
    buyer_notes = Column(Text)
    buyer_food_rating = Column(Integer)
    buyer_delivery_rating = Column(Integer)
    ignored_by_seller_count = Column(Integer, default=0)
    cancelled_reason = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
