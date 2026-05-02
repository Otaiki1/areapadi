from __future__ import annotations
import uuid
from sqlalchemy import Column, String, Boolean, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import UUID
from geoalchemy2 import Geography
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from shared.db import Base


class Buyer(Base):
    __tablename__ = "buyers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(20), unique=True, nullable=False)
    whatsapp_name = Column(String(200))
    location = Column(Geography(geometry_type="POINT", srid=4326))
    location_updated_at = Column(TIMESTAMP(timezone=True))
    preferred_language = Column(String(10), default="en")
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
