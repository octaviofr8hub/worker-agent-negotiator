"""
Main Worker - LiveKit Agents SDK entrypoint.
Escucha dispatches y maneja llamadas SIP salientes.
NO usa livekit-cli, solo SDK.
"""
from core.config import settings
from agents.voice_agent import VoiceAgent

import json
import asyncio
import logging
from livekit import api
from livekit.plugins import (
    silero,
    openai,
    elevenlabs,
    noise_cancellation,
    google,
)
from livekit.agents import (
    AgentSession,
    JobContext,
    cli,
    WorkerOptions,
    RoomInputOptions,
)


logger = logging.getLogger("outbound-dispatcher")
logger.setLevel(logging.INFO)


async def entrypoint(ctx: JobContext):
    """Entrypoint del worker: recibe un job con metadata y hace la llamada."""
    # Import config y voice_agent aquí (no disponibles durante download-files)

    logger.info(f"Conectando a room: {ctx.room.name}")
    await ctx.connect()
    # Parsear metadata del dispatch
    dial_info = json.loads(ctx.job.metadata)
    segment_id = dial_info.get("segment_id")
    phone_number = dial_info.get("phone_number")

    # Validar teléfono
    if not phone_number or not isinstance(phone_number, str) or not phone_number.startswith("+"):
        logger.error(f"Número inválido: {phone_number}")
        ctx.shutdown()
        return

    # Detectar idioma por prefijo
    if phone_number.startswith("+1"):
        language = "en"
    elif phone_number.startswith("+52") or phone_number.startswith("+34"):
        language = "es"
    else:
        language = "en"

    # Crear agente
    voice_agent = VoiceAgent(dial_info=dial_info, language=language)

    # Configurar sesión con credenciales de Google
    #logger.info(f"Usando credenciales de Google: {settings.GOOGLE_CREDENTIALS_DICT}")
    session = AgentSession(
        allow_interruptions=True,
        min_endpointing_delay=0.3,
        max_endpointing_delay=3.0,
        min_interruption_words=2,
        user_away_timeout=None,
        false_interruption_timeout=None,
        preemptive_generation=True,
        vad=silero.VAD.load(
            min_speech_duration=0.15,
            min_silence_duration=0.5,
            prefix_padding_duration=0.4,
            max_buffered_speech=300.0,
            activation_threshold=0.4,
        ),
        stt=google.STT(
            languages="es-MX" if language == "es" else "en-US",
            use_streaming=True,
            detect_language=True,
            model="latest_long",
            spoken_punctuation=False,
            credentials_info=settings.GOOGLE_CREDENTIALS_DICT,
        ),
        llm=openai.LLM(
            api_key=settings.OPENAI_API_KEY,
            model="gpt-4o",
            top_p=0.8,
            temperature=0.3,
            max_completion_tokens=150,
        ),
        tts=elevenlabs.TTS(
            api_key=settings.ELEVEN_API_KEY,
            voice_id="CaJslL1xziwefCeTNzHv" if language == "es" else "M7ya1YbaeFaPXljg9BpK",
            model="eleven_turbo_v2_5",
            enable_ssml_parsing=True,
        ),
    )

    # Marcar vía SIP PRIMERO
    participant_identity = "carrier"
    try:
        logger.info(f"Iniciando llamada SIP a {phone_number}")
        sip_participant = await asyncio.wait_for(
            ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=settings.SIP_OUTBOUND_TRUNK_ID,
                    sip_call_to=phone_number,
                    participant_identity=participant_identity,
                    wait_until_answered=True,
                )
            ),
            timeout=30
        )
        logger.info(f"Participante SIP creado: {sip_participant.sip_call_id}")
        
        # Esperar a que el participante entre al room
        participant = await ctx.wait_for_participant(identity=participant_identity)
        logger.info(f"Participante conectado: {participant.identity}")
        
        # Iniciar la sesión del agente
        logger.info("Iniciando sesión del agente...")
        await session.start(
            agent=voice_agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony(),
                close_on_disconnect=False,
            ),
        )
        

    except asyncio.TimeoutError:
        logger.error(f"Timeout - sin respuesta en {phone_number} después de 30 segundos")
        ctx.shutdown()
    except api.TwirpError as e:
        error_msg = e.message if hasattr(e, 'message') else str(e)
        logger.error(f"Error SIP al llamar a {phone_number}: {error_msg}")
        # Errores comunes:
        # 486 Busy Here = número ocupado
        # 487 Request Terminated = llamada cancelada
        # 603 Decline = llamada rechazada o creditos de Twilio insuficientes
        ctx.shutdown()
    except Exception as e:
        logger.error(f"Error inesperado al llamar a {phone_number}: {str(e)}")
        ctx.shutdown()


if __name__ == "__main__":
    logger.info("Starting worker with agent_name=outbound-dispatcher")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-dispatcher",
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
            ws_url=settings.LIVEKIT_URL,
            num_idle_processes=1,
        )
    )
