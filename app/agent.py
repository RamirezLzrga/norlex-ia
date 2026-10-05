"""Loop de agente con function calling sobre Groq.

Flujo de una peticion:
    mensaje del usuario
      -> se resuelve el Contexto (rol) desde la BD
      -> el modelo ve SOLO las herramientas de su rol
      -> Groq decide si responde o pide una herramienta
      -> se ejecuta la herramienta (con control de rol en el servidor)
      -> se devuelve el resultado al modelo
      -> repite hasta que responda (tope de iteraciones)
      -> se audita en la tabla Auditoria

El modelo nunca recibe datos crudos: o pasa por una herramienta, o no los tiene.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from groq import Groq, RateLimitError

from . import db, tools
from .config import Config, get_config
from .prompts import construir_prompt
from .roles import Contexto, Rol

log = logging.getLogger(__name__)

_cliente: Groq | None = None


def _obtener_cliente(cfg: Config) -> Groq:
    global _cliente
    if _cliente is None:
        _cliente = Groq(api_key=cfg.api_key, timeout=30.0, max_retries=1)
    return _cliente


@dataclass(slots=True)
class Respuesta:
    mensaje: str
    rol: str
    herramientas_usadas: list[str]
    iteraciones: int
    sin_ia: bool = False
    #: Nombre de la herramienta -> lo que devolvio. Lo consume la pagina de
    #: pruebas para transparentar que hay detras de cada respuesta.
    resultados: list[dict[str, Any]] | None = None


class ErrorAgente(RuntimeError):
    pass


async def responder(
    mensaje: str,
    ctx: Contexto,
    historial: list[dict[str, str]] | None = None,
) -> Respuesta:
    cfg = get_config()
    mensaje = (mensaje or "").strip()
    if not mensaje:
        raise ErrorAgente("El mensaje va vacio.")
    if len(mensaje) > cfg.max_mensaje:
        raise ErrorAgente(f"Mensaje demasiado largo (max {cfg.max_mensaje} caracteres).")

    if not cfg.ia_habilitada:
        return Respuesta(
            mensaje=(
                "El asistente inteligente esta temporalmente deshabilitado por falta de "
                "configuracion. Contacta a soporte para reportarlo."
            ),
            rol=ctx.rol.value,
            herramientas_usadas=[],
            iteraciones=0,
            sin_ia=True,
        )

    cliente = _obtener_cliente(cfg)
    catalogo = tools.herramientas_para(ctx)

    mensajes: list[dict[str, Any]] = [{"role": "system", "content": construir_prompt(ctx)}]
    for turno in (historial or [])[-cfg.max_historial :]:
        if turno.get("rol") in {"user", "assistant"} and turno.get("contenido"):
            mensajes.append({"role": turno["rol"], "content": turno["contenido"]})
    mensajes.append({"role": "user", "content": mensaje})

    usadas: list[str] = []
    resultados: list[dict[str, Any]] = []

    for iteracion in range(1, cfg.max_iteraciones + 1):
        try:
            respuesta = cliente.chat.completions.create(
                model=cfg.modelo,
                messages=mensajes,
                tools=catalogo or None,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=900,
            )
        except RateLimitError:
            # Groq 429 (p. ej. limite diario de tokens): mensaje claro en vez
            # de 502 generico, para que la web no caiga al respaldo de reglas.
            raise ErrorAgente(
                "El asistente alcanzó temporalmente su límite de consultas. "
                "Intenta de nuevo en unos minutos; tus búsquedas siguen funcionando normalmente."
            ) from None
        choice = respuesta.choices[0]
        mensaje_modelo = choice.message

        # El modelo ya respondio: no pidio herramientas.
        if not mensaje_modelo.tool_calls:
            return Respuesta(
                mensaje=(mensaje_modelo.content or "").strip(),
                rol=ctx.rol.value,
                herramientas_usadas=usadas,
                iteraciones=iteracion,
                resultados=resultados,
            )

        mensajes.append(
            {
                "role": "assistant",
                "content": mensaje_modelo.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in mensaje_modelo.tool_calls
                ],
            }
        )

        for call in mensaje_modelo.tool_calls:
            usadas.append(call.function.name)
            argumentos = _parsear_argumentos(call.function.arguments)
            log.info("Herramienta %s rol=%s args=%s", call.function.name, ctx.rol.value, argumentos)
            resultado = tools.ejecutar(call.function.name, argumentos, ctx)
            resultados.append({"herramienta": call.function.name, "resultado": resultado})
            mensajes.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(resultado, ensure_ascii=False, default=str),
                }
            )

    # Se agotaron las iteraciones: se fuerza una respuesta final sin herramientas.
    final = cliente.chat.completions.create(
        model=cfg.modelo,
        messages=mensajes,
        temperature=0.3,
        max_tokens=600,
    )
    return Respuesta(
        mensaje=(final.choices[0].message.content or "").strip()
        or "No pude completar la consulta. Intenta con una pregunta mas especifica.",
        rol=ctx.rol.value,
        herramientas_usadas=usadas,
        iteraciones=cfg.max_iteraciones,
    )


def _parsear_argumentos(bruto: str) -> dict[str, Any]:
    if not bruto:
        return {}
    try:
        datos = json.loads(bruto)
    except json.JSONDecodeError:
        log.warning("Argumentos no parseables del modelo: %r", bruto)
        return {}
    return datos if isinstance(datos, dict) else {}


# ── Sugerencias de arranque, por rol ────────────────────────────────────

SUGERENCIAS: dict[Rol, list[str]] = {
    Rol.ADMIN: [
        "¿Como va el negocio este mes?",
        "¿Que solicitudes estan pendientes?",
        "¿Que empresas no estan usando la plataforma?",
        "¿Cuantos usuarios registrados hay?",
    ],
    Rol.TITULAR: [
        "¿Como va mi suscripcion?",
        "¿Que colecciones tengo contratadas?",
        "¿Cuantos usuarios tiene mi empresa?",
    ],
    Rol.USUARIO_EMPRESA: [
        "¿Que puedo ver en la plataforma?",
        "¿Como va la suscripcion de mi empresa?",
        "¿Busca leyes de seguridad e higiene",
        "¿Cuales son mis alertas?",
    ],
    Rol.VISITANTE: [
        "¿Que es Norlex?",
        "¿Cuanto cuestan los planes?",
        "¿Que colecciones manejan?",
        "¿Como se contrata?",
    ],
}


def sugerencias(ctx: Contexto) -> list[dict[str, str]]:
    preguntas = SUGERENCIAS.get(ctx.rol, SUGERENCIAS[Rol.VISITANTE])
    return [{"etiqueta": p, "pregunta": p} for p in preguntas]
