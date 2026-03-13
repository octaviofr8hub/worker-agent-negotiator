"""
Servicio de base de datos (sync SQLAlchemy) + bus de eventos SSE para transcripts en tiempo real.
Las operaciones DB se ejecutan en hilos via asyncio.to_thread para no bloquear el event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from models.database import SessionLocal
from models.negotiation import Negotiation
from models.transcript_message import TranscriptMessage

logger = logging.getLogger("db-service")


# ═════════════════════════════════════════════════════════════
#  SSE EVENT BUS — Broadcast de transcripts en tiempo real
# ═════════════════════════════════════════════════════════════

class TranscriptBus:
    """Pub/sub en memoria para enviar transcript events a clientes SSE."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, call_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(call_id, []).append(q)
        return q

    def unsubscribe(self, call_id: str, q: asyncio.Queue):
        if call_id in self._subscribers:
            self._subscribers[call_id] = [
                s for s in self._subscribers[call_id] if s is not q
            ]
            if not self._subscribers[call_id]:
                del self._subscribers[call_id]

    async def publish(self, call_id: str, event: dict):
        for q in self._subscribers.get(call_id, []):
            await q.put(event)

    async def publish_end(self, call_id: str):
        """Enviar señal de fin a todos los suscriptores."""
        for q in self._subscribers.get(call_id, []):
            await q.put(None)


# Singleton global
transcript_bus = TranscriptBus()


# ═════════════════════════════════════════════════════════════
#  SYNC DB HELPERS (se llaman dentro de asyncio.to_thread)
# ═════════════════════════════════════════════════════════════

def _sync_create_negotiation(call_id: str, dial_info: dict[str, Any], language: str) -> dict:
    ai_price = float(dial_info.get("ai_price", 0))
    db = SessionLocal()
    try:
        nego = Negotiation(
            call_id=call_id,
            carrier_name=dial_info.get("carrier_name", ""),
            carrier_phone=dial_info.get("carrier_main_phone", ""),
            pickup_city=dial_info.get("pickup_city", ""),
            pickup_state=dial_info.get("pickup_state", ""),
            pickup_country=dial_info.get("pickup_country", ""),
            dropoff_city=dial_info.get("dropoff_city", ""),
            dropoff_state=dial_info.get("dropoff_state", ""),
            dropoff_country=dial_info.get("dropoff_country", ""),
            trailer_type=dial_info.get("trailer_type", ""),
            distance=dial_info.get("distance", ""),
            load_date=dial_info.get("date", ""),
            ai_price=ai_price,
            max_price=round(ai_price * 1.15, 2),
            language=language,
            status="ringing",
        )
        db.add(nego)
        db.commit()
        db.refresh(nego)
        logger.info(f"[DB] Negotiation created: {call_id}")
        return _nego_to_dict(nego)
    finally:
        db.close()


def _sync_update_negotiation(call_id: str, **kwargs) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(Negotiation).where(Negotiation.call_id == call_id).values(**kwargs)
        )
        db.commit()
    finally:
        db.close()


def _sync_get_negotiation(call_id: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        nego = db.query(Negotiation).filter(Negotiation.call_id == call_id).first()
        if not nego:
            return None
        return _nego_to_dict(nego)
    finally:
        db.close()


def _sync_get_active_negotiation() -> Optional[dict]:
    db = SessionLocal()
    try:
        nego = (
            db.query(Negotiation)
            .filter(Negotiation.status.in_(["ringing", "in_progress"]))
            .order_by(Negotiation.created_at.desc())
            .first()
        )
        if not nego:
            return None
        return _nego_to_dict(nego)
    finally:
        db.close()


def _sync_get_all_negotiations(limit: int = 50) -> list[dict]:
    db = SessionLocal()
    try:
        results = (
            db.query(Negotiation)
            .order_by(Negotiation.created_at.desc())
            .limit(limit)
            .all()
        )
        return [_nego_to_dict(n) for n in results]
    finally:
        db.close()


def _sync_add_transcript_message(
    call_id: str, role: str, content: str, tool_name: str | None = None
) -> Optional[dict]:
    db = SessionLocal()
    try:
        nego = db.query(Negotiation).filter(Negotiation.call_id == call_id).first()
        if not nego:
            logger.warning(f"[DB] No negotiation found for {call_id}, skipping transcript")
            return None

        msg = TranscriptMessage(
            negotiation_id=nego.id,
            role=role,
            content=content,
            tool_name=tool_name,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return {
            "id": msg.id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "timestamp": msg.created_at.isoformat() if msg.created_at else None,
        }
    finally:
        db.close()


def _sync_get_transcript(call_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        nego = db.query(Negotiation).filter(Negotiation.call_id == call_id).first()
        if not nego:
            return []

        messages = (
            db.query(TranscriptMessage)
            .filter(TranscriptMessage.negotiation_id == nego.id)
            .order_by(TranscriptMessage.created_at)
            .all()
        )
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "timestamp": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ]
    finally:
        db.close()


def _nego_to_dict(nego: Negotiation) -> dict:
    return {
        "id": nego.id,
        "call_id": nego.call_id,
        "call_sid": nego.call_sid,
        "carrier_name": nego.carrier_name,
        "carrier_phone": nego.carrier_phone,
        "pickup_city": nego.pickup_city,
        "pickup_state": nego.pickup_state,
        "pickup_country": nego.pickup_country,
        "dropoff_city": nego.dropoff_city,
        "dropoff_state": nego.dropoff_state,
        "dropoff_country": nego.dropoff_country,
        "trailer_type": nego.trailer_type,
        "distance": nego.distance,
        "load_date": nego.load_date,
        "ai_price": float(nego.ai_price) if nego.ai_price else 0,
        "max_price": float(nego.max_price) if nego.max_price else 0,
        "final_price": float(nego.final_price) if nego.final_price else None,
        "status": nego.status,
        "reject_reason": nego.reject_reason,
        "language": nego.language,
        "created_at": nego.created_at.isoformat() if nego.created_at else None,
        "answered_at": nego.answered_at.isoformat() if nego.answered_at else None,
        "ended_at": nego.ended_at.isoformat() if nego.ended_at else None,
    }


# ═════════════════════════════════════════════════════════════
#  ASYNC WRAPPERS (para usar desde el event loop de FastAPI)
# ═════════════════════════════════════════════════════════════

async def create_negotiation(call_id: str, dial_info: dict[str, Any], language: str) -> dict:
    return await asyncio.to_thread(_sync_create_negotiation, call_id, dial_info, language)


async def update_negotiation(call_id: str, **kwargs) -> None:
    await asyncio.to_thread(_sync_update_negotiation, call_id, **kwargs)


async def get_negotiation(call_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_sync_get_negotiation, call_id)


async def get_active_negotiation() -> Optional[dict]:
    return await asyncio.to_thread(_sync_get_active_negotiation)


async def get_all_negotiations(limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_sync_get_all_negotiations, limit)


async def add_transcript_message(
    call_id: str, role: str, content: str, tool_name: str | None = None
) -> None:
    """Guarda el mensaje en DB y lo publica al bus SSE."""
    event = await asyncio.to_thread(_sync_add_transcript_message, call_id, role, content, tool_name)
    if event:
        await transcript_bus.publish(call_id, event)


async def get_transcript(call_id: str) -> list[dict]:
    return await asyncio.to_thread(_sync_get_transcript, call_id)
