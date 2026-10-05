"""Funciones IA de proposito general para el resto de la plataforma.

Aqui vive la IA que NO es chatbot:
  * resumir_ordenamiento   -> ficha de ordenamiento (boton "Resumir con IA")
  * explicar_articulo      -> "Explicar este articulo" en el visor
  * insights_admin         -> panel de insights del Dashboard admin
  * resumen_reporte        -> parrafo ejecutivo del reporte semanal

Todas comparten dos reglas:
  * Reintentos con espera ante 429 (cuota por minuto de Groq): el plan
    gratuito corta por tokens/minuto y una espera de 1-3s lo resuelve.
  * Nunca lanzan hacia fuera mas alla del ErrorIA declarado: la app .NET
    decide que hacer si la IA no esta disponible.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, TypeVar

from .config import get_config

log = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorIA(RuntimeError):
    """La IA no pudo producir respuesta (cuota agotada, sin key, etc.)."""


def _llamar(messages: list[dict[str, str]], max_tokens: int = 900, temperature: float = 0.2) -> str:
    """Llamada a Groq con reintentos ante 429 (tokens/minuto del plan gratis)."""
    cfg = get_config()
    if not cfg.ia_habilitada:
        raise ErrorIA("IA deshabilitada: falta GROQ_API_KEY.")

    from groq import Groq, RateLimitError

    cliente = Groq(api_key=cfg.api_key, timeout=30.0, max_retries=1)

    esperas = [1.5, 4.0, 9.0]  # el 429 de Groq suele liberar en <10s
    ultimo_error: Exception | None = None
    for intento in range(len(esperas) + 1):
        try:
            respuesta = cliente.chat.completions.create(
                model=cfg.modelo,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (respuesta.choices[0].message.content or "").strip()
        except RateLimitError as exc:
            ultimo_error = exc
            if intento >= len(esperas):
                break
            espera = esperas[intento]
            log.warning("Groq 429 (intento %d): esperando %.1fs", intento + 1, espera)
            time.sleep(espera)
        except Exception as exc:  # noqa: BLE001 — otros fallos no se reintentan
            ultimo_error = exc
            break
    raise ErrorIA(f"Groq no respondio: {type(ultimo_error).__name__}") from ultimo_error


def _json_de(crudo: str) -> dict[str, Any] | None:
    ini = crudo.find("{")
    fin = crudo.rfind("}")
    if ini < 0 or fin <= ini:
        return None
    try:
        datos = json.loads(crudo[ini : fin + 1])
    except json.JSONDecodeError:
        return None
    return datos if isinstance(datos, dict) else None


# ── Ficha de ordenamiento ───────────────────────────────────────────────

_RESUMEN_SYS = (
    "Eres analista juridico de la plataforma Norlex. Con la ficha del "
    "ordenamiento que te paso (en JSON), escribe un resumen util para un "
    "usuario de la industria.\n"
    "Reglas:\n"
    "- Responde SOLO con 3 a 5 lineas que empiecen con '- ' (guion + espacio).\n"
    "- Primera linea: que es y a quien aplica. Despues: fechas clave "
    "(publicacion, vigor, abrogacion) y estado. Si hay descripcion, para que "
    "sirve u objetos de regulacion.\n"
    "- Espanol claro, sin jerga innecesaria. Cifras y fechas SOLO del JSON.\n"
    "- PROHIBIDO usar tablas ni barras verticales. Sin encabezados ni markdown "
    "adicional."
)


def resumir_ordenamiento(ficha: dict[str, Any]) -> str:
    texto = _llamar(
        [
            {"role": "system", "content": _RESUMEN_SYS},
            {"role": "user", "content": json.dumps(ficha, ensure_ascii=False, default=str)[:6000]},
        ],
        max_tokens=800,
    )
    return texto[:2500]


_ARTICULO_SYS = (
    "Eres asesor tecnico de cumplimiento de Norlex. Un usuario esta leyendo un "
    "articulo de un ordenamiento y pide explicacion.\n"
    "Con el JSON que te paso (ordenamiento + texto del articulo), responde:\n"
    "- Solo 3 a 6 lineas que empiecen con '- '.\n"
    "- En lenguaje llano: que obliga, a quien aplica, que pasa si no se cumple\n"
    "  (si el texto lo dice). Si el texto no viene o esta vacio, explica por el\n"
    "  contexto del ordenamiento y aclara que no tienes el texto del articulo.\n"
    "- NO des asesoria legal de un caso concreto; termina sugiriendo revision\n"
    "  por un especialista cuando aplique.\n"
    "- PROHIBIDO usar tablas ni barras verticales."
)


def explicar_articulo(
    ordenamiento: dict[str, Any], articulo: str, texto: str | None, contexto: str | None = None
) -> str:
    carga = {
        "ordenamiento": {
            "nombre": ordenamiento.get("Nombrecompleto") or ordenamiento.get("Nombrecorto"),
            "tipo": ordenamiento.get("Tipo"),
            "emisor": ordenamiento.get("Emisor"),
            "estado": ordenamiento.get("STATUS"),
        },
        "articulo": articulo[:200],
        "texto_del_articulo": (texto or "")[:8000],
        "contexto_pagina": (contexto or "")[:300],
    }
    return _llamar(
        [
            {"role": "system", "content": _ARTICULO_SYS},
            {"role": "user", "content": json.dumps(carga, ensure_ascii=False, default=str)},
        ],
        max_tokens=800,
    )[:2500]


# ── Insights del Dashboard admin ────────────────────────────────────────

_INSIGHTS_SYS = (
    "Eres analista de negocio de Norlex (plataforma de suscripciones a "
    "legislacion). Te paso metricas actuales del negocio en JSON.\n"
    "Devuelve SOLO un objeto JSON valido, sin texto fuera del JSON:\n"
    '{"resumen": "2-3 frases ejecutivas del estado del negocio",\n'
    ' "hallazgos": ["3 a 5 hallazgos concretos, cada uno una frase con cifras"],\n'
    ' "acciones": ["2 a 4 acciones recomendadas, cada una una frase concreta"]}\n'
    "Reglas: interpreta (no repitas) las cifras: tendencias, riesgos (suscripciones\n"
    "por vencer, ingresos en riesgo, empresas dormidas), salud del negocio y uso.\n"
    "No inventes metricas que no esten en el JSON. Espanol claro. Sin tablas.\n"
    "Lenguaje plano para no expertos: di 'ingreso mensual' (NUNCA 'MRR'),\n"
    "'suscripciones que vencen pronto', 'empresas sin usar la plataforma'."
)


def insights_admin(metricas: dict[str, Any]) -> dict[str, Any]:
    crudo = _llamar(
        [
            {"role": "system", "content": _INSIGHTS_SYS},
            {"role": "user", "content": json.dumps(metricas, ensure_ascii=False, default=str)[:8000]},
        ],
        max_tokens=900,
        temperature=0.3,
    )
    datos = _json_de(crudo)
    if datos is None:
        # El modelo no devolvio JSON: lo entregamos como resumen plano.
        datos = {"resumen": crudo[:1200], "hallazgos": [], "acciones": []}
    return {
        "resumen": str(datos.get("resumen") or "")[:2000],
        "hallazgos": [str(h)[:400] for h in (datos.get("hallazgos") or [])[:6] if str(h).strip()],
        "acciones": [str(a)[:400] for a in (datos.get("acciones") or [])[:5] if str(a).strip()],
    }


# ── Comparador entre jurisdicciones ─────────────────────────────────────

_COMPARADOR_SYS = (
    "Eres analista juridico de Norlex. Un usuario comparo un tema de "
    "legislacion entre jurisdicciones de Mexico (federal y estados) y te paso "
    "los conteos y titulos de ordenamientos encontrados en cada una, en JSON.\n"
    "Escribe una lectura ejecutiva del panorama:\n"
    "- 1 a 3 lineas que empiecen con '- ' (guion + espacio), lo mas importante "
    "  primero.\n"
    "- En la primera linea di en cuantas jurisdicciones aparece el tema y cual "
    "  concentra mas ordenamientos.\n"
    "- Menciona por nombre las normas mas relevantes que veas en el JSON (sin "
    "  inventar ninguna). Si una jurisdiccion no tiene resultados, dilo sin "
    "  rodeos.\n"
    "- Cierra recordando que esta lectura no sustituye la revision de las "
    "  normas ni la asesoria de un especialista.\n"
    "- Espanol claro. PROHIBIDO usar tablas ni barras verticales."
)


def resumen_comparador(consulta: dict[str, Any]) -> str:
    texto = _llamar(
        [
            {"role": "system", "content": _COMPARADOR_SYS},
            {"role": "user", "content": json.dumps(consulta, ensure_ascii=False, default=str)[:6000]},
        ],
        # holgura para el razonamiento del modelo (max_tokens corto = vacio)
        max_tokens=900,
    )[:2000]
    return texto


# ── Reporte semanal ─────────────────────────────────────────────────────

_REPORTE_SYS = (
    "Eres analista de negocio de Norlex. Te paso el resumen numerico del\n"
    "reporte semanal. Escribe 2-3 frases ejecutivas (maximo 80 palabras) que\n"
    "interpreten las cifras: tendencia de consultas, riesgo de ingresos,\n"
    "adopcion y 1 recomendacion concreta para la semana siguiente. Espanol\n"
    "claro, tono directo. Sin tablas, sin encabezados, sin saludos."
)


def resumen_reporte(texto_reporte: str) -> str:
    return _llamar(
        [
            {"role": "system", "content": _REPORTE_SYS},
            {"role": "user", "content": texto_reporte[:3000]},
        ],
        # holgura: el modelo razona antes de responder y un presupuesto
        # corto devuelve contenido vacio (lecciones de interpretar.py).
        max_tokens=1000,
    )[:1500]
