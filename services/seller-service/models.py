from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, Numeric, Integer, Text, ARRAY, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import text
from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from shared.db import Base


class Seller(Base):
    __tablename__ = "sellers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(20), unique=True, nullable=False)
    business_name = Column(String(200), nullable=False)
    owner_name = Column(String(200))
    food_categories = Column(ARRAY(Text))
    location = Column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    address_text = Column(Text)
    is_available = Column(Boolean, default=False)
    auto_deactivated = Column(Boolean, default=False)
    rating = Column(Numeric(3, 2), default=0.0)
    total_orders = Column(Integer, default=0)
    total_reviews = Column(Integer, default=0)
    is_pro = Column(Boolean, default=False)
    pro_expires_at = Column(TIMESTAMP(timezone=True))
    onboarding_complete = Column(Boolean, default=False)
    onboarding_step = Column(String(100))
    opening_time = Column(String(20))
    closing_time = Column(String(20))
    operating_days = Column(ARRAY(Text))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class MenuItem(Base):
    __tablename__ = "menu_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    price = Column(Numeric(10, 2), nullable=False)
    is_available = Column(Boolean, default=True)
    image_url = Column(Text)
    embedding = Column(Vector(1536))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
