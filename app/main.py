"""
Main - FastAPI server con WebSocket para Twilio Media Streams.

Endpoints:
  POST /dispatch                → Inicia llamada saliente vía Twilio REST API
  WS   /ws/{call_id}            → WebSocket bidireccional de Twilio Media Streams
  GET  /health                  → Health check
  GET  /negotiations            → Lista todas las negociaciones
  GET  /negotiations/active     → Negociación activa (si existe)
  GET  /negotiations/{call_id}  → Detalle de una negociación
  GET  /negotiations/{call_id}/transcript → Transcript completo
  GET  /negotiations/{call_id}/stream     → SSE transcript en tiempo real
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from core.config import settings
from services.call_session import CallSession
from services.db_service import (
    create_negotiation,
    update_negotiation,
    get_negotiation,
    get_active_negotiation,
    get_all_negotiations,
    get_transcript,
    transcript_bus,
)

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("main")

# ── App ──────────────────────────────────────────────────────
app = FastAPI(title="Worker Agent Negotiator (Twilio WS)", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Almacén en memoria de llamadas pendientes: call_id → dial_info
pending_calls: dict[str, dict[str, Any]] = {}

# Sesión activa (solo una a la vez)
active_session: dict[str, Any] = {"call_id": None}


# ═════════════════════════════════════════════════════════════
#  HTTP ENDPOINTS
# ═════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.1.0-twilio-ws"}


@app.post("/dispatch")
async def dispatch(dial_info: dict[str, Any]):
    """
    Crea una llamada saliente via Twilio y conecta el audio
    a nuestro WebSocket a través de <Connect><Stream>.
    Solo permite UNA llamada activa a la vez.
    """
    # ── Validar que no haya llamada activa ────────────────────
    if active_session["call_id"]:
        existing = await get_active_negotiation()
        if existing:
            return JSONResponse(
                {
                    "error": "Ya hay una negociación activa",
                    "active_call_id": existing["call_id"],
                    "status": existing["status"],
                },
                status_code=409,
            )
        else:
            active_session["call_id"] = None

    phone_number = dial_info.get("carrier_main_phone", "")
    if not phone_number:
        return JSONResponse({"error": "carrier_main_phone es requerido"}, status_code=400)
    if not phone_number.startswith("+"):
        return JSONResponse(
            {"error": f"carrier_main_phone debe ser E.164 (ej: +525512345678), recibido: {phone_number}"},
            status_code=400,
        )

    call_id = f"nego-{uuid.uuid4().hex[:12]}"
    pending_calls[call_id] = dial_info
    active_session["call_id"] = call_id

    # Crear registro en DB
    await create_negotiation(call_id, dial_info, "es")

    # Limpiar entry después de 120s si Twilio nunca se conecta
    async def _cleanup():
        await asyncio.sleep(120)
        pending_calls.pop(call_id, None)
        if active_session["call_id"] == call_id:
            active_session["call_id"] = None
            await update_negotiation(call_id, status="error")
    asyncio.create_task(_cleanup())

    # ── Generar TwiML con <Connect><Stream> ──────────────────
    from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
    from twilio.rest import Client

    ws_url = f"wss://{settings.SERVER_HOST}/ws/{call_id}"

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="call_id", value=call_id)
    connect.append(stream)
    response.append(connect)

    twiml_str = str(response)
    logger.info(f"[DISPATCH] call_id={call_id} phone={phone_number}")

    # ── Crear llamada vía Twilio REST API ─────────────────────
    try:
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        def _create_call():
            return client.calls.create(
                twiml=twiml_str,
                to=phone_number,
                from_=settings.TWILIO_PHONE_NUMBER,
            )

        call = await asyncio.to_thread(_create_call)

        await update_negotiation(call_id, call_sid=call.sid)

        logger.info(f"[DISPATCH] Twilio call created: sid={call.sid} status={call.status}")
        return {
            "call_id": call_id,
            "call_sid": call.sid,
            "status": call.status,
            "phone_number": phone_number,
        }

    except Exception as e:
        pending_calls.pop(call_id, None)
        active_session["call_id"] = None
        await update_negotiation(call_id, status="error")
        logger.error(f"[DISPATCH] Error creating Twilio call: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ═════════════════════════════════════════════════════════════
#  NEGOTIATION API ENDPOINTS
# ═════════════════════════════════════════════════════════════

@app.get("/negotiations")
async def list_negotiations(limit: int = Query(50, ge=1, le=200)):
    """Lista todas las negociaciones, más recientes primero."""
    return await get_all_negotiations(limit)


@app.get("/negotiations/active")
async def get_active():
    """Retorna la negociación activa, si existe."""
    nego = await get_active_negotiation()
    if not nego:
        return JSONResponse({"active": False}, status_code=200)
    return {"active": True, **nego}


@app.get("/negotiations/{call_id}")
async def get_negotiation_detail(call_id: str):
    """Detalle de una negociación específica."""
    nego = await get_negotiation(call_id)
    if not nego:
        return JSONResponse({"error": "Negotiation not found"}, status_code=404)
    return nego


@app.get("/negotiations/{call_id}/transcript")
async def get_negotiation_transcript(call_id: str):
    """Transcript completo (histórico) de una negociación."""
    nego = await get_negotiation(call_id)
    if not nego:
        return JSONResponse({"error": "Negotiation not found"}, status_code=404)
    messages = await get_transcript(call_id)
    return {"call_id": call_id, "status": nego["status"], "messages": messages}


@app.get("/negotiations/{call_id}/stream")
async def stream_transcript(call_id: str):
    """
    SSE (Server-Sent Events) — Transmite transcript en tiempo real.
    El frontend se conecta aquí y recibe cada mensaje conforme llega.
    """
    nego = await get_negotiation(call_id)
    if not nego:
        return JSONResponse({"error": "Negotiation not found"}, status_code=404)

    async def event_generator():
        # Primero enviar mensajes existentes
        existing = await get_transcript(call_id)
        for msg in existing:
            import json
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"

        # Si ya terminó, no suscribirse
        nego_now = await get_negotiation(call_id)
        if nego_now and nego_now["status"] in ("accepted", "rejected", "unavailable", "ended", "error"):
            yield f"data: {json.dumps({'type': 'end', 'status': nego_now['status']}, ensure_ascii=False)}\n\n"
            return

        # Suscribirse al bus para nuevos mensajes
        q = transcript_bus.subscribe(call_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    if event is None:  # Señal de fin
                        nego_final = await get_negotiation(call_id)
                        final_status = nego_final["status"] if nego_final else "ended"
                        yield f"data: {json.dumps({'type': 'end', 'status': final_status}, ensure_ascii=False)}\n\n"
                        return
                    import json
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        finally:
            transcript_bus.unsubscribe(call_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═════════════════════════════════════════════════════════════
#  WEBSOCKET – Twilio Media Streams
# ═════════════════════════════════════════════════════════════

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(ws: WebSocket, call_id: str):
    await ws.accept()
    logger.info(f"[WS] Accepted connection for call_id={call_id}")

    dial_info = pending_calls.pop(call_id, None)
    if dial_info is None:
        logger.warning(f"[WS] No pending call found for call_id={call_id}")
        dial_info = {}

    session = CallSession(ws, dial_info, call_id)
    try:
        await session.run()
    finally:
        if active_session["call_id"] == call_id:
            active_session["call_id"] = None
        await transcript_bus.publish_end(call_id)


# ═════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn

    port = 8000
    logger.info(f"Starting server on port {port}")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        timeout_keep_alive=3600,
    )
