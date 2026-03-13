"""
NegotiationAgent — Agente de negociación de tarifas de transporte.
Versión sin LiveKit: define tool schemas para OpenAI function calling.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("negotiation-agent")


# ──────────────────────────────────────────────────────────────
# OpenAI function-calling tool schemas
# ──────────────────────────────────────────────────────────────
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "accept_deal",
            "description": (
                "Llama esta función cuando el carrier ACEPTA la tarifa. "
                "Registra el precio final acordado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "final_price": {
                        "type": "number",
                        "description": "Precio final acordado en USD",
                    },
                    "farewell_message": {
                        "type": "string",
                        "description": "Mensaje de despedida confirmando todos los detalles",
                    },
                },
                "required": ["final_price", "farewell_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject_deal",
            "description": (
                "Llama esta función cuando NO se llega a un acuerdo y la negociación termina."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "farewell_message": {
                        "type": "string",
                        "description": "Mensaje de despedida amable",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Razón por la que no se llegó a acuerdo",
                    },
                },
                "required": ["farewell_message", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "user_cannot_talk",
            "description": (
                "Llama esta función cuando el usuario NO puede hablar en este momento."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "farewell_message": {
                        "type": "string",
                        "description": "Mensaje de despedida",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Razón por la que no puede hablar",
                    },
                },
                "required": ["farewell_message", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": (
                "Termina la llamada. Úsala cuando la conversación debe finalizar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "farewell_message": {
                        "type": "string",
                        "description": "Mensaje de despedida final",
                    },
                },
                "required": ["farewell_message"],
            },
        },
    },
]


def build_prompt(dial_info: dict, language: str) -> str:
    """Construye el system prompt dinámico con la info de la carga."""

    pickup = f"{dial_info.get('pickup_city', '')}, {dial_info.get('pickup_state', '')}, {dial_info.get('pickup_country', '')}"
    dropoff = f"{dial_info.get('dropoff_city', '')}, {dial_info.get('dropoff_state', '')}, {dial_info.get('dropoff_country', '')}"
    carrier_name = dial_info.get("carrier_name", "operador")
    trailer_type = dial_info.get("trailer_type", "N/A")
    date = dial_info.get("date", "N/A")
    distance = dial_info.get("distance", "N/A")
    ai_price = float(dial_info.get("ai_price", 0))
    max_price = round(ai_price * 1.15, 2)

    if language == "es":
        return f"""
Eres Cami, una asistente virtual de voz que representa a Fr8 Technologies, empresa especializada en logística y transporte de carga.
Tu personalidad es cercana, empática, profesional y con un toque humano: no suenas robótica ni repetitiva.

─── DATOS DE LA CARGA ───
- Origen: {pickup}
- Destino: {dropoff}
- Tipo de trailer: {trailer_type}
- Fecha de carga: {date}
- Distancia: {distance} millas
- Precio base (oferta inicial): ${ai_price} USD
- Precio tope (NUNCA superar): ${max_price} USD
- Carrier: {carrier_name}

─── OBJETIVO ───
Negociar la mejor tarifa posible sin exceder el precio tope (${max_price} USD).

─── ESTILO CONVERSACIONAL ───
- Cercana, natural y humana.
- Sé expresiva y variada: evita repetir frases exactas.
- Adapta tu tono al usuario (amable, directo, comprensivo).
- Usa lenguaje simple y claro, sin sonar automatizada.
- Habla en español.

─── FLUJO DE CONVERSACIÓN ───

1. Ya te presentaste en el mensaje de bienvenida. Espera a que el usuario responda.

2. PRESENTAR RUTA: Menciona origen ({pickup}), destino ({dropoff}), fecha ({date}),
   tipo de trailer ({trailer_type}) y distancia ({distance} millas).
   Pregunta si le interesa antes de dar precio.

3. OFERTA INICIAL: Presenta el precio base de ${ai_price} USD de forma natural.
   Pregunta si le parece bien.

4. NEGOCIACIÓN: Si rechaza o hace contraoferta:
   - Máximo 2 justificaciones antes de subir precio.
   - Escalera de concesión: +3%, +2%, +1.5%, +1% sobre el precio base.
   - NUNCA superar ${max_price} USD.
   - Si piden más del tope, explica amablemente que es el máximo autorizado.
   - Se puede mover la fecha máximo 3 días después.

5. CIERRE: Si acepta, confirma TODOS los detalles:
   origen, destino, fecha, tipo de trailer, precio final.
   Llama accept_deal() con el precio final.

6. SIN ACUERDO: Si no hay acuerdo, agradece su tiempo y llama reject_deal().

─── REGLAS CRÍTICAS ───
- NUNCA superes ${max_price} USD.
- NUNCA menciones que el tope es 1.15x, solo di que es el máximo autorizado.
- Confirma todos los términos antes de cerrar.
- No prometas nada fuera de lo autorizado.
- Solo pide datos necesarios: nombre, tipo de unidad y disponibilidad.
- Usa preguntas calibradas: "¿Qué número te haría moverla hoy?", "Si mejoro a $X, ¿la tomas ya?"

─── DETECCIÓN DE TONO ───
- Usuario molesto: responde con calma y pregunta directa.
- Usuario neutro: sé clara y directa.
- Usuario positivo: avanza rápido y cierra.

─── USO DE HERRAMIENTAS ───
- accept_deal(final_price, farewell_message): Cuando el carrier ACEPTA la tarifa
- reject_deal(farewell_message, reason): Cuando NO se llega a acuerdo
- user_cannot_talk(farewell_message, reason): Cuando el usuario no puede hablar ahora
- end_call(farewell_message): Para terminar la llamada en cualquier otro caso

─── BUZÓN DE VOZ ───
Si detectas que es buzón o no hay respuesta, deja un mensaje breve y llama end_call():
"Hola, soy Cami de Fr8 Technologies. Tenemos una ruta de {pickup} a {dropoff}, oferta de ${ai_price} USD para {date}. Si te interesa, responde a este número. ¡Gracias!"
""".strip()

    else:
        return f"""
You are Cami, a virtual voice assistant representing Fr8 Technologies, a company specializing in freight logistics and transportation.
Your personality is friendly, empathetic, professional, and human-like.

─── LOAD DETAILS ───
- Origin: {pickup}
- Destination: {dropoff}
- Trailer type: {trailer_type}
- Pickup date: {date}
- Distance: {distance} miles
- Base price (initial offer): ${ai_price} USD
- Maximum price (NEVER exceed): ${max_price} USD
- Carrier: {carrier_name}

─── OBJECTIVE ───
Negotiate the best possible rate without exceeding ${max_price} USD.

─── CONVERSATION STYLE ───
- Friendly, natural, and human-like.
- Be expressive and varied: avoid repeating exact phrases.
- Adapt your tone to the user.
- Use simple, clear language. Speak in English.

─── CONVERSATION FLOW ───

1. You already introduced yourself in the welcome message. Wait for the user to respond.

2. PRESENT ROUTE: Mention origin ({pickup}), destination ({dropoff}), date ({date}),
   trailer type ({trailer_type}), and distance ({distance} miles).
   Ask if they're interested before giving the price.

3. INITIAL OFFER: Present the base price of ${ai_price} USD naturally.

4. NEGOTIATION: If they reject or counter-offer:
   - Max 2 justifications before raising the price.
   - Concession ladder: +3%, +2%, +1.5%, +1% over base price.
   - NEVER exceed ${max_price} USD.
   - Date can be moved up to 3 days later.

5. CLOSE: If accepted, confirm ALL details and call accept_deal() with the final price.

6. NO DEAL: Thank them and call reject_deal().

─── CRITICAL RULES ───
- NEVER exceed ${max_price} USD.
- NEVER mention the cap is 1.15x, just say it's the maximum authorized.
- Confirm all terms before closing.
- Use calibrated questions: "What number would make you move it today?"

─── TOOL USAGE ───
- accept_deal(final_price, farewell_message): When carrier ACCEPTS the rate
- reject_deal(farewell_message, reason): When NO deal is reached
- user_cannot_talk(farewell_message, reason): When user can't talk now
- end_call(farewell_message): To end the call in any other case

─── VOICEMAIL ───
If voicemail: leave a brief message and call end_call():
"Hi, this is Cami from Fr8 Technologies. We have a route from {pickup} to {dropoff}, offering ${ai_price} USD for {date}. If interested, call us back. Thanks!"
""".strip()


class NegotiationAgent:
    """Agente de voz para negociación de tarifas de transporte."""

    def __init__(self, dial_info: dict[str, Any], language: str = "es", call_id: str = ""):
        self.dial_info = dial_info
        self.language = language
        self.call_id = call_id
        self.instructions = build_prompt(dial_info, language)

        # Callbacks – inyectados por CallSession
        self._say_callback: Optional[Callable[..., Coroutine]] = None
        self._hangup_callback: Optional[Callable[..., Coroutine]] = None

        logger.info(
            f"NegotiationAgent creado — carrier={dial_info.get('carrier_name')}, "
            f"ruta={dial_info.get('pickup_city')} → {dial_info.get('dropoff_city')}, "
            f"precio_base=${dial_info.get('ai_price')}"
        )

    def get_welcome_message(self) -> str:
        carrier_name = self.dial_info.get("carrier_name", "")
        if self.language == "es":
            return (
                f"¡Hola, buenas! Soy Cami de Fr8 Technologies. "
                f"¿Hablo con alguien de {carrier_name}?"
            )
        return (
            f"Hi there! This is Cami from Fr8 Technologies. "
            f"Am I speaking with someone from {carrier_name}?"
        )

    async def execute_tool(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        if tool_name == "accept_deal":
            await self._accept_deal(**arguments)
            return "Deal accepted, call ending", True

        if tool_name == "reject_deal":
            await self._reject_deal(**arguments)
            return "Deal rejected, call ending", True

        if tool_name == "user_cannot_talk":
            await self._user_cannot_talk(**arguments)
            return "User cannot talk, call ending", True

        if tool_name == "end_call":
            await self._end_call(**arguments)
            return "Call ended", True

        return f"Unknown tool: {tool_name}", False

    # ═════════════════════════════════════════════════════════
    #  TOOL IMPLEMENTATIONS
    # ═════════════════════════════════════════════════════════

    async def _accept_deal(self, final_price: float, farewell_message: str):
        logger.info(f"[DEAL ACCEPTED] price=${final_price} call={self.call_id}")
        from services.db_service import update_negotiation, add_transcript_message
        await update_negotiation(self.call_id, status="accepted", final_price=final_price)
        await add_transcript_message(self.call_id, "tool", f"Deal accepted at ${final_price}", tool_name="accept_deal")
        if self._say_callback:
            await self._say_callback(farewell_message, allow_interruptions=False)
        if self._hangup_callback:
            await self._hangup_callback()

    async def _reject_deal(self, farewell_message: str, reason: str):
        logger.info(f"[DEAL REJECTED] reason={reason} call={self.call_id}")
        from services.db_service import update_negotiation, add_transcript_message
        await update_negotiation(self.call_id, status="rejected", reject_reason=reason)
        await add_transcript_message(self.call_id, "tool", f"Deal rejected: {reason}", tool_name="reject_deal")
        if self._say_callback:
            await self._say_callback(farewell_message, allow_interruptions=False)
        if self._hangup_callback:
            await self._hangup_callback()

    async def _user_cannot_talk(self, farewell_message: str, reason: str):
        logger.info(f"[USER UNAVAILABLE] reason={reason} call={self.call_id}")
        from services.db_service import update_negotiation, add_transcript_message
        await update_negotiation(self.call_id, status="unavailable", reject_reason=reason)
        await add_transcript_message(self.call_id, "tool", f"User unavailable: {reason}", tool_name="user_cannot_talk")
        if self._say_callback:
            await self._say_callback(farewell_message, allow_interruptions=False)
        if self._hangup_callback:
            await self._hangup_callback()

    async def _end_call(self, farewell_message: str):
        logger.info(f"[CALL ENDED] call={self.call_id}")
        from services.db_service import update_negotiation, add_transcript_message
        await update_negotiation(self.call_id, status="ended")
        await add_transcript_message(self.call_id, "tool", "Call ended", tool_name="end_call")
        if self._say_callback:
            await self._say_callback(farewell_message, allow_interruptions=False)
        if self._hangup_callback:
            await self._hangup_callback()