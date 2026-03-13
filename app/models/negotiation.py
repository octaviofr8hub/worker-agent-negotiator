"""
Modelo Negotiation — registro de cada llamada de negociación.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Numeric, DateTime
from sqlalchemy.orm import relationship

from models.database import Base


class Negotiation(Base):
    __tablename__ = "negotiations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String(50), unique=True, nullable=False, index=True)
    call_sid = Column(String(50))

    # Carrier
    carrier_name = Column(String(200))
    carrier_phone = Column(String(30))

    # Route
    pickup_city = Column(String(100))
    pickup_state = Column(String(50))
    pickup_country = Column(String(10))
    dropoff_city = Column(String(100))
    dropoff_state = Column(String(50))
    dropoff_country = Column(String(10))
    trailer_type = Column(String(50))
    distance = Column(String(20))
    load_date = Column(String(30))

    # Pricing
    ai_price = Column(Numeric(10, 2), nullable=False, default=0)
    max_price = Column(Numeric(10, 2), nullable=False, default=0)
    final_price = Column(Numeric(10, 2))

    # Status: ringing | in_progress | accepted | rejected | unavailable | ended | error
    status = Column(String(20), nullable=False, default="ringing", index=True)
    reject_reason = Column(String(500))
    language = Column(String(5), default="es")

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    answered_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))

    # Relationship
    messages = relationship(
        "TranscriptMessage",
        back_populates="negotiation",
        order_by="TranscriptMessage.created_at",
    )
