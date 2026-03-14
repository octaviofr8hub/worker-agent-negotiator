"""
CallSession – Orquesta una llamada completa sobre Twilio Media Streams.

Flujo:
  Twilio WS (mulaw 8kHz) ─► Google STT (streaming) ─► OpenAI GPT-4o
                                                              │
  Twilio WS ◄── mulaw 8kHz ◄── ElevenLabs TTS (ulaw_8000) ◄─┘
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import re
import threading
import time
from typing import Any, Optional

import httpx
import openai
from google.cloud import speech_v1 as speech
from google.oauth2 import service_account
from starlette.websockets import WebSocket, WebSocketDisconnect

from core.config import settings
from agents.voice_agent import NegotiationAgent, TOOLS_SCHEMA
from services.db_service import add_transcript_message, update_negotiation

logger = logging.getLogger("call-session")

AUDIO_CHUNK_SIZE = 160


# ═════════════════════════════════════════════════════════════
#  HELPERS TTS — normalización de texto para pronunciación
# ═════════════════════════════════════════════════════════════

def _num_es(n: int) -> str:
    """Convierte entero (0–999 999) a palabras en español."""
    if n == 0:
        return "cero"
    _units = ["", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
              "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete",
              "dieciocho", "diecinueve"]
    _tens = ["", "", "veinte", "treinta", "cuarenta", "cincuenta",
             "sesenta", "setenta", "ochenta", "noventa"]
    _hunds = ["", "cien", "doscientos", "trescientos", "cuatrocientos", "quinientos",
              "seiscientos", "setecientos", "ochocientos", "novecientos"]
    parts = []
    if n >= 1000:
        t = n // 1000
        n %= 1000
        parts.append("mil" if t == 1 else f"{_num_es(t)} mil")
    if n >= 100:
        h, n = divmod(n, 100)
        if h == 1 and n == 0:
            parts.append("cien")
        elif h == 1:
            parts.append("ciento")
        else:
            parts.append(_hunds[h])
    if n > 0:
        if n < 20:
            parts.append(_units[n])
        elif n % 10 == 0:
            parts.append(_tens[n // 10])
        else:
            t, u = divmod(n, 10)
            parts.append(f"veinti{_units[u]}" if t == 2 else f"{_tens[t]} y {_units[u]}")
    return " ".join(parts)


def _normalize_for_tts(text: str, language: str) -> str:
    """Convierte cifras y monedas a palabras para que ElevenLabs las pronuncie bien."""
    if language != "es":
        return text

    def _usd(m: re.Match) -> str:
        raw = m.group(1).replace(",", "")
        parts = raw.split(".")
        integer = int(parts[0])
        cents = int(parts[1].ljust(2, "0")[:2]) if len(parts) > 1 and parts[1] else 0
        result = f"{_num_es(integer)} dólares"
        if cents:
            result += f" con {_num_es(cents)} centavos"
        return result

    # $4427.38  o  $4,427.38
    text = re.sub(r'\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)', _usd, text)
    # $4427  sin decimales
    text = re.sub(r'\$([0-9]+)', _usd, text)

    def _big_num(m: re.Match) -> str:
        return _num_es(int(m.group(0).replace(",", "")))

    # Números >= 1000 que no fueron capturados (ej: 1445 millas)
    text = re.sub(r'\b[0-9]{4,}(?:,[0-9]{3})*\b', _big_num, text)
    return text


class CallSession:
    """Maneja una sesión de llamada individual sobre Twilio Media Streams."""

    def __init__(self, ws: WebSocket, dial_info: dict[str, Any], call_id: str):
        self.ws = ws
        self.dial_info = dial_info
        self.call_id = call_id

        # Twilio stream metadata
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None

        # Detectar idioma por país
        pickup_country = dial_info.get("pickup_country", "").upper()
        dropoff_country = dial_info.get("dropoff_country", "").upper()
        spanish_countries = {"MX", "ES", "CO", "AR", "CL", "PE", "GT", "HN", "SV", "NI", "CR", "PA"}
        #if pickup_country in spanish_countries or dropoff_country in spanish_countries:
        #    self.language = "es"
        #else:
        #    self.language = "en"
        self.language = "es"
        # Voice Agent (business logic)
        self.agent = NegotiationAgent(dial_info, self.language, call_id)
        self.agent._say_callback = self.say
        self.agent._hangup_callback = self.hangup

        # Conversación para OpenAI
        self.messages: list[dict] = [
            {"role": "system", "content": self.agent.instructions}
        ]

        # ── STT ──────────────────────────────────────────────
        self._stt_audio_q: queue.Queue[Optional[bytes]] = queue.Queue()
        self._stt_running = False
        self._stt_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Playback state ───────────────────────────────────
        self._is_playing = False
        self._interrupted = False
        self._playback_done = asyncio.Event()

        # ── Concurrency ──────────────────────────────────────
        self._processing_lock = asyncio.Lock()
        self._ended = False

    # ═════════════════════════════════════════════════════════
    #  MAIN LOOP
    # ═════════════════════════════════════════════════════════

    async def run(self):
        self._loop = asyncio.get_event_loop()
        try:
            while True:
                raw = await self.ws.receive_text()
                data = json.loads(raw)
                await self._handle_twilio_message(data)
        except WebSocketDisconnect:
            logger.info(f"[{self.call_id}] WebSocket disconnected")
        except Exception as e:
            logger.error(f"[{self.call_id}] WS error: {e}")
        finally:
            self._cleanup()

    async def _handle_twilio_message(self, data: dict):
        event = data.get("event")

        if event == "connected":
            logger.info(f"[{self.call_id}] Twilio stream connected")

        elif event == "start":
            self.stream_sid = data["start"]["streamSid"]
            self.call_sid = data["start"]["callSid"]
            logger.info(
                f"[{self.call_id}] Stream started – sid={self.stream_sid} call={self.call_sid}"
            )
            self._start_stt()
            await update_negotiation(self.call_id, status="in_progress", answered_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
            asyncio.create_task(self._send_welcome())

        elif event == "media":
            payload = data["media"]["payload"]
            audio_bytes = base64.b64decode(payload)
            self._stt_audio_q.put(audio_bytes)

        elif event == "mark":
            self._playback_done.set()

        elif event == "stop":
            logger.info(f"[{self.call_id}] Twilio stream stopped")
            self._ended = True

    # ═════════════════════════════════════════════════════════
    #  GOOGLE STT (streaming, hilo background)
    # ═════════════════════════════════════════════════════════

    def _start_stt(self):
        self._stt_running = True
        self._stt_thread = threading.Thread(target=self._stt_worker, daemon=True)
        self._stt_thread.start()

    def _stt_worker(self):
        creds = service_account.Credentials.from_service_account_info(
            settings.GOOGLE_CREDENTIALS_DICT
        )
        client = speech.SpeechClient(credentials=creds)

        lang_code = "es-MX" if self.language == "es" else "en-US"
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code=lang_code,
            model="telephony",
            enable_automatic_punctuation=True,
            use_enhanced=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
        )

        while self._stt_running:
            try:
                requests = self._audio_request_generator()
                responses = client.streaming_recognize(streaming_config, requests)

                for response in responses:
                    if not self._stt_running:
                        break
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        transcript = result.alternatives[0].transcript

                        if result.is_final:
                            if transcript.strip():
                                logger.info(f"[STT final] {transcript.strip()}")
                                asyncio.run_coroutine_threadsafe(
                                    self._on_final_transcript(transcript.strip()),
                                    self._loop,
                                )
                        else:
                            if transcript.strip() and self._is_playing:
                                logger.info("[STT] Interruption detected")
                                self._interrupted = True
                                asyncio.run_coroutine_threadsafe(
                                    self._clear_audio(), self._loop
                                )
            except Exception as e:
                if self._stt_running:
                    logger.warning(f"[STT] Stream ended, restarting: {e}")
                    time.sleep(0.2)
                else:
                    break

    def _audio_request_generator(self):
        while self._stt_running:
            try:
                chunk = self._stt_audio_q.get(timeout=1.0)
                if chunk is None:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)
            except queue.Empty:
                if not self._stt_running:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=b"\x7f" * 160)

    # ═════════════════════════════════════════════════════════
    #  LLM (OpenAI GPT-4o con function calling)
    # ═════════════════════════════════════════════════════════

    async def _on_final_transcript(self, transcript: str):
        if self._ended:
            return
        start_time = time.time()
        async with self._processing_lock:
            if self._ended:  # re-check tras adquirir el lock
                return
            await self._process_user_speech(transcript)
        elapsed = time.time() - start_time
        logger.info(f"[TIMING] Process speech took {elapsed:.2f}s")

    async def _process_user_speech(self, transcript: str):
        if self._is_playing:
            await self._clear_audio()
            self._is_playing = False

        self.messages.append({"role": "user", "content": transcript})
        await add_transcript_message(self.call_id, "user", transcript)
        await self._run_llm_loop()

    def _sanitize_messages(self):
        """Elimina assistant tool_call messages sin tool responses correspondientes."""
        clean = []
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tc_ids = {tc["id"] for tc in msg["tool_calls"]}
                j = i + 1
                found_ids = set()
                while j < len(self.messages) and self.messages[j].get("role") == "tool":
                    found_ids.add(self.messages[j].get("tool_call_id", ""))
                    j += 1
                if not tc_ids.issubset(found_ids):
                    logger.warning(f"[LLM] Sanitizando tool_calls huérfanos: {tc_ids - found_ids}")
                    i = j  # saltar el bloque incompleto
                    continue
            clean.append(msg)
            i += 1
        self.messages = clean

    async def _run_llm_loop(self):
        while True:
            if self._ended:
                return

            self._sanitize_messages()

            try:
                llm_start = time.time()
                client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                stream = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=self.messages,
                    tools=TOOLS_SCHEMA,
                    temperature=0.3,
                    top_p=0.8,
                    max_completion_tokens=120,
                    timeout=10.0,
                    stream=True,
                )
            except Exception as e:
                logger.error(f"[LLM] Error: {e}")
                return

            full_content = ""
            pending_text = ""
            tool_calls_acc: dict[int, dict] = {}
            sentence_queue: asyncio.Queue = asyncio.Queue()

            # TTS consumer: reproduce oraciones conforme llegan
            tts_task = asyncio.create_task(
                self._tts_sentence_player(sentence_queue)
            )

            async for chunk in stream:
                if self._ended:
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Acumular tool call deltas
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_acc[idx]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_acc[idx]["arguments"] += tc.function.arguments

                # Acumular contenido y emitir oraciones completas al TTS
                if delta.content:
                    full_content += delta.content
                    pending_text += delta.content

                    while True:
                        m = re.search(r'[.!?]+(?:\s+|$)', pending_text)
                        if not m:
                            break
                        sentence = pending_text[:m.end()].strip()
                        pending_text = pending_text[m.end():]
                        if sentence:
                            await sentence_queue.put(sentence)

            llm_elapsed = time.time() - llm_start
            logger.info(f"[TIMING] LLM stream took {llm_elapsed:.2f}s")

            # Vaciar texto restante (sin puntuación final)
            if pending_text.strip() and not tool_calls_acc:
                await sentence_queue.put(pending_text.strip())

            # Señal de fin al TTS consumer
            await sentence_queue.put(None)

            if tool_calls_acc:
                await tts_task

                tool_calls_list = [
                    {
                        "id": tc_data["id"],
                        "type": "function",
                        "function": {
                            "name": tc_data["name"],
                            "arguments": tc_data["arguments"],
                        },
                    }
                    for tc_data in tool_calls_acc.values()
                ]
                self.messages.append({
                    "role": "assistant",
                    "content": full_content or "",
                    "tool_calls": tool_calls_list,
                })

                end_after = False
                for tc_data in tool_calls_acc.values():
                    try:
                        args = json.loads(tc_data["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    try:
                        result_text, should_end = await self.agent.execute_tool(
                            tc_data["name"], args
                        )
                    except Exception as e:
                        logger.error(f"[LLM] execute_tool '{tc_data['name']}' error: {e}")
                        result_text = f"error: {e}"
                        should_end = False

                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "content": result_text,
                    })
                    if should_end:
                        end_after = True

                if end_after:
                    return
                continue

            # Esperar a que termine el audio antes de regresar
            await tts_task

            if full_content:
                self.messages.append({"role": "assistant", "content": full_content})
                await add_transcript_message(self.call_id, "assistant", full_content)

            return

    async def _tts_sentence_player(self, queue: asyncio.Queue):
        """Consume oraciones del queue y las reproduce secuencialmente via TTS."""
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            if self._ended:
                continue  # drenar el queue sin reproducir
            await self.say(sentence, allow_interruptions=True)

    # ═════════════════════════════════════════════════════════
    #  TTS (ElevenLabs streaming → mulaw 8 kHz)
    # ═════════════════════════════════════════════════════════

    async def say(self, text: str, allow_interruptions: bool = True):
        if self._ended or not text.strip():
            return

        text = _normalize_for_tts(text, self.language)
        tts_start = time.time()
        self._is_playing = True
        self._interrupted = False
        self._playback_done.clear()

        voice_id = (
            "CaJslL1xziwefCeTNzHv" if self.language == "es" else "M7ya1YbaeFaPXljg9BpK"
        )
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            f"/stream?output_format=ulaw_8000"
        )
        headers = {
            "xi-api-key": settings.ELEVEN_API_KEY,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.7,
            },
        }

        buffer = b""
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                async with http.stream("POST", url, json=body, headers=headers) as resp:
                    resp.raise_for_status()
                    async for data in resp.aiter_bytes(640):
                        if self._interrupted and allow_interruptions:
                            logger.info("[TTS] Interrupted, stopping stream")
                            break
                        buffer += data
                        while len(buffer) >= AUDIO_CHUNK_SIZE:
                            chunk = buffer[:AUDIO_CHUNK_SIZE]
                            buffer = buffer[AUDIO_CHUNK_SIZE:]
                            await self._send_audio_chunk(chunk)

            if buffer and not (self._interrupted and allow_interruptions):
                await self._send_audio_chunk(buffer)

            # Solo esperar confirmación de playback en mensajes de despedida (before hangup).
            # En conversación normal no esperamos el mark: Twilio bufferiza el audio en orden
            # y next sentence empieza TTS inmediatamente sin bloquear.
            if not allow_interruptions and not self._interrupted:
                self._playback_done.clear()
                await self._send_mark("tts_done")
                try:
                    await asyncio.wait_for(self._playback_done.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning("[TTS] Timeout esperando confirmación de playback farewell")

        except Exception as e:
            logger.error(f"[TTS] Error: {e}")

        self._is_playing = False
        tts_elapsed = time.time() - tts_start
        logger.info(f"[TIMING] TTS took {tts_elapsed:.2f}s")

    # ═════════════════════════════════════════════════════════
    #  TWILIO WEBSOCKET I/O
    # ═════════════════════════════════════════════════════════

    async def _send_audio_chunk(self, chunk: bytes):
        if self._ended:
            return
        payload = base64.b64encode(chunk).decode("ascii")
        msg = json.dumps(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload},
            }
        )
        try:
            await self.ws.send_text(msg)
        except Exception:
            pass

    async def _send_mark(self, name: str):
        if self._ended:
            return
        msg = json.dumps(
            {
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": name},
            }
        )
        try:
            await self.ws.send_text(msg)
        except Exception:
            pass

    async def _clear_audio(self):
        if self._ended:
            return
        msg = json.dumps({"event": "clear", "streamSid": self.stream_sid})
        try:
            await self.ws.send_text(msg)
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════
    #  LIFECYCLE
    # ═════════════════════════════════════════════════════════

    async def _send_welcome(self):
        await asyncio.sleep(0.5)
        welcome = self.agent.get_welcome_message()
        self.messages.append({"role": "assistant", "content": welcome})
        await add_transcript_message(self.call_id, "assistant", welcome)
        await self.say(welcome, allow_interruptions=True)

    async def hangup(self):
        logger.info(f"[{self.call_id}] Hanging up")
        self._ended = True
        self._stt_running = False
        self._stt_audio_q.put(None)

        try:
            await self.ws.close()
        except Exception:
            pass

        if self.call_sid:
            try:
                from twilio.rest import Client

                def _end_call():
                    c = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
                    c.calls(self.call_sid).update(status="completed")

                await asyncio.to_thread(_end_call)
            except Exception as e:
                logger.warning(f"[HANGUP] REST API fallback: {e}")

    def _cleanup(self):
        self._stt_running = False
        self._stt_audio_q.put(None)
        self._ended = True
        # Update DB status if still in_progress
        from datetime import datetime, timezone
        asyncio.run_coroutine_threadsafe(
            update_negotiation(self.call_id, ended_at=datetime.now(timezone.utc)),
            self._loop,
        )
        logger.info(f"[{self.call_id}] Session cleaned up")
