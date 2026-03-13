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
            model="latest_long",
            enable_automatic_punctuation=True,
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

    async def _run_llm_loop(self):
        while True:
            if self._ended:
                return

            try:
                llm_start = time.time()
                client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                response = await client.chat.completions.create(
                    model="gpt-4o",
                    messages=self.messages,
                    tools=TOOLS_SCHEMA,
                    temperature=0.3,
                    top_p=0.8,
                    max_completion_tokens=150,
                    timeout=10.0,
                )
                llm_elapsed = time.time() - llm_start
                logger.info(f"[TIMING] LLM took {llm_elapsed:.2f}s")
            except Exception as e:
                logger.error(f"[LLM] Error: {e}")
                return

            choice = response.choices[0]
            msg = choice.message

            if msg.tool_calls:
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )

                end_after = False
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    result_text, should_end = await self.agent.execute_tool(
                        tc.function.name, args
                    )

                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        }
                    )

                    if should_end:
                        end_after = True

                if end_after:
                    return
                continue

            if msg.content:
                self.messages.append({"role": "assistant", "content": msg.content})
                await add_transcript_message(self.call_id, "assistant", msg.content)
                await self.say(msg.content)

            return

    # ═════════════════════════════════════════════════════════
    #  TTS (ElevenLabs streaming → mulaw 8 kHz)
    # ═════════════════════════════════════════════════════════

    async def say(self, text: str, allow_interruptions: bool = True):
        if self._ended or not text.strip():
            return

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
