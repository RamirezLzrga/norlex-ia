"""Traduccion de lenguaje natural a filtros de busqueda (NL -> filtros).

La caja de texto del usuario dice "hasta cuatro palabras (sin articulos)".
Este modulo levanta esa restriccion: una frase libre como
"normas de residuos peligrosos publicadas en 2024" se convierte en los
filtros exactos que entiende la busqueda de la app .NET:

    { "texto": "residuos peligrosos", "publicacion_desde": "2024-01-01",
      "publicacion_hasta": "2024-12-31" }

El .NET aplicara los filtros contra la BD (cache incluido), igual que en
una busqueda manual, asi que aqui NO se toca la coleccion: la IA solo
interpreta. Si Groq no responde, se devuelve texto=None y el cliente C#
cae a su heuristic local (regex de fechas/materia).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from .config import get_config

log = logging.getLogger(__name__)

# Materias y nombres canonicos (los mismos que usa buscar_legislacion).
MATERIAS: list[str] = [
    "ECOL", "SEGH", "SALU", "TRAN", "ENER", "LABO", "INDU", "AGRO",
    "FISC", "COME", "SEPU", "NOTA", "ELEC", "DEHU", "CTES", "CLYR",
    "EDUC", "TCOM", "AIPD", "TURI", "ADMP",
]
NOMBRE_MATERIA: dict[str, str] = {
    "ECOL": "Medio Ambiente", "SEGH": "Seguridad e Higiene", "SALU": "Salud",
    "TRAN": "Transporte", "ENER": "Energía", "LABO": "Laboral",
    "INDU": "Industrial", "AGRO": "Agropecuaria", "FISC": "Fiscal",
    "COME": "Comercio", "SEPU": "Seguridad Pública", "NOTA": "Notarial y Correduría",
    "ELEC": "Electoral", "DEHU": "Derechos Humanos", "CTES": "Construcciones",
    "CLYR": "Códigos, Leyes y Reglamentos", "EDUC": "Educación",
    "TCOM": "Telecomunicaciones", "AIPD": "Protección de Datos Personales",
    "TURI": "Turismo", "ADMP": "Administración Pública",
}
ALIAS_MATERIA: dict[str, str] = {n.lower(): s for s, n in NOMBRE_MATERIA.items()}
ALIAS_MATERIA.update({
    "ambiental": "ECOL", "ecologia": "ECOL", "residuos": "ECOL",
    "higiene": "SEGH", "seguridad": "SEPU", "medico": "SALU",
    "laborales": "LABO", "trabajo": "LABO", "fiscal": "FISC",
    "impuestos": "FISC", "aduanas": "FISC", "comercio": "COME",
    "electrico": "ENER", "energetico": "ENER", "telecom": "TCOM",
    "turistico": "TURI", "educativo": "EDUC", "electoral": "ELEC",
    "humanos": "DEHU",
})

# Valores EXACTOS de la columna LEGISXXI.Tipo (verificado contra la BD:
# estan en plural y mayusculas: 'LEYES', 'REGLAMENTOS',
# 'NORMAS OFICIALES MEXICANAS', ...). Cualquier otro valor no devuelve nada.
TIPOS: list[str] = [
    "LEYES", "REGLAMENTOS", "NORMAS OFICIALES MEXICANAS", "CODIGOS",
    "DECRETOS", "ACUERDOS", "CONVENIOS", "RESOLUCIONES", "CIRCULARES",
]

_MAPA_TIPOS: dict[str, str] = {
    "ley": "LEYES", "leyes": "LEYES",
    "reglamento": "REGLAMENTOS", "reglamentos": "REGLAMENTOS",
    "nom": "NORMAS OFICIALES MEXICANAS",
    "norma oficial mexicana": "NORMAS OFICIALES MEXICANAS",
    "normas oficiales mexicanas": "NORMAS OFICIALES MEXICANAS",
    "normas oficiales": "NORMAS OFICIALES MEXICANAS",
    "codigo": "CODIGOS", "codigos": "CODIGOS",
    "decreto": "DECRETOS", "decretos": "DECRETOS",
    "acuerdo": "ACUERDOS", "acuerdos": "ACUERDOS",
    "convenio": "CONVENIOS", "convenios": "CONVENIOS",
    "resolucion": "RESOLUCIONES", "resoluciones": "RESOLUCIONES",
    "circular": "CIRCULARES", "circulares": "CIRCULARES",
}

_INSTRUCCIONES = (
    "Traduce la peticion del usuario a filtros de busqueda de una coleccion "
    "de legislacion federal mexicana (leyes, codigos, reglamentos, NOMs, decretos).\n"
    "Devuelve SOLO un objeto JSON valido, sin explicaciones, con las llaves que apliquen:\n"
    '{"texto": "2-4 palabras clave SIN articulos (el, la, los, de), tomadas del tema que '
    'busca el usuario (ej: \'residuos peligrosos\', \'NOM-035\', \'facturacion\')",\n'
    ' "materia": una de [' + ", ".join(MATERIAS) + "] si la peticion cae claramente en una materia, si no omitela,\n"
    ' "tipo": si pide explicitamente un tipo de documento, usa EXACTAMENTE uno de [' + ", ".join(TIPOS) + "] (son los valores exactos de la BD),\n"
    ' "publicacion_desde": "AAAA-MM-DD" y "publicacion_hasta": "AAAA-MM-DD" si menciona anio o fechas de publicacion,\n'
    ' "vigentes_desde": "AAAA-MM-DD" si pide normas que entren/en entren en vigor en una fecha,\n'
    ' "explicacion": una frase corta en espanol con lo que va a buscar}\n'
    "No inventes filtros que el usuario no pido. Si la frase es demasiado ambigua para "
    "deducir filtros utiles, devuelve {\"ambigua\": true, \"explicacion\": \"que falta por aclarar\"}."
)


def interpretar(mensaje: str) -> dict[str, Any] | None:
    """Llama a Groq y devuelve los filtros, o None si no se pudo interpretar.

    Nunca lanza: los fallos se loguean y el llamador decide el fallback.
    """
    cfg = get_config()
    if not cfg.ia_habilitada:
        return None

    try:
        from groq import Groq

        cliente = _obtener_cliente(cfg)
        respuesta = cliente.chat.completions.create(
            model=cfg.modelo,
            messages=[
                {"role": "system", "content": _INSTRUCCIONES},
                {"role": "user", "content": mensaje[:400]},
            ],
            temperature=0.0,
            # holgura: gpt-oss razona antes de responder y se come el
            # presupuesto en el canal de reasoning si es muy corto.
            max_tokens=700,
        )
        crudo = (respuesta.choices[0].message.content or "").strip()
        filtros = _parsear_json(crudo)
        if filtros is None:
            log.warning("IA no devolvio JSON util (len=%d): %r", len(crudo), crudo[:200])
        return filtros
    except Exception as exc:  # noqa: BLE001 — la IA es optativa, nunca revienta
        log.warning("No se pudo interpretar la busqueda con IA: %s: %s", type(exc).__name__, exc)
        return None


_cliente_cache: Any = None


def _obtener_cliente(cfg: Any) -> Any:
    global _cliente_cache
    if _cliente_cache is None:
        from groq import Groq

        _cliente_cache = Groq(api_key=cfg.api_key, timeout=15.0, max_retries=1)
    return _cliente_cache


def _parsear_json(crudo: str) -> dict[str, Any] | None:
    """El modelo suele responder JSON limpio, pero por si acaso se recorta
    al primer bloque {...} aunque venga dentro de texto o ```json fences."""
    if not crudo:
        return None
    ini = crudo.find("{")
    fin = crudo.rfind("}")
    if ini < 0 or fin <= ini:
        return None
    try:
        datos = json.loads(crudo[ini : fin + 1])
    except json.JSONDecodeError:
        return None
    return datos if isinstance(datos, dict) else None


# ── Normalizacion (defensiva: el modelo puede traer basura) ────────────

_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalizar(bruto: dict[str, Any]) -> dict[str, Any]:
    """Acepta solo filtros conocidos y con tipos correctos."""
    filtros: dict[str, Any] = {}

    texto = _texto(bruto.get("texto"))
    if texto:
        filtros["texto"] = texto[:120]

    materia = _materia(bruto.get("materia"))
    if materia:
        filtros["materia"] = materia

    tipo = _texto(bruto.get("tipo"))
    if tipo:
        clave = tipo.lower().strip()
        canonico = _MAPA_TIPOS.get(clave)
        if canonico is None:
            for alias, valor in _MAPA_TIPOS.items():
                if clave.startswith(alias) or alias in clave:
                    canonico = valor
                    break
        filtros["tipo"] = canonico or tipo[:40]

    for clave in ("publicacion_desde", "publicacion_hasta", "vigentes_desde"):
        fecha = _fecha(bruto.get(clave))
        if fecha:
            filtros[clave] = fecha

    explicacion = _texto(bruto.get("explicacion"))
    if explicacion:
        filtros["explicacion"] = explicacion[:200]

    if _bool(bruto.get("ambigua")):
        filtros["ambigua"] = True

    return filtros


def _materia(valor: Any) -> str | None:
    """Acepta sigla o nombre/alias en espanol. Devuelve la sigla canonica."""
    texto = _texto(valor)
    if not texto:
        return None
    limpio = texto.replace(" ", "").upper()
    if limpio in MATERIAS:
        return limpio
    if texto.upper() in ALIAS_MATERIA:
        return ALIAS_MATERIA[texto.upper()]
    baja = texto.lower()
    for alias, sigla in ALIAS_MATERIA.items():
        if re.search(rf"\b{re.escape(alias)}\b", baja):
            return sigla
    return None


def _texto(valor: Any) -> str | None:
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return None


def _bool(valor: Any) -> bool:
    return isinstance(valor, bool) and valor


def _fecha(valor: Any) -> str | None:
    """Acepta ISO directo o un anio suelto (2024 -> 2024-01-01 si viene en
    un campo *_desde, 2024-12-31 en *_hasta — el ajuste lo hace el llamador)."""
    if isinstance(valor, (int, float)):
        anio = int(valor)
        if 1990 <= anio <= date.today().year + 1:
            return f"{anio}-01-01"
        return None
    texto = _texto(valor)
    if not texto:
        return None
    texto = texto.strip()
    if _RE_ISO.match(texto):
        try:
            date.fromisoformat(texto)
            return texto
        except ValueError:
            return None
    m = re.match(r"^(\d{4})\b", texto)
    if m:
        anio = int(m.group(1))
        if 1990 <= anio <= date.today().year + 1:
            return f"{anio}-01-01"
    return None


def heuristica(mensaje: str) -> dict[str, Any]:
    """Interpretacion local sin IA: regex de fechas/materias. Se usa cuando
    Groq no responde, para que el boton nunca quede muerto."""
    filtros: dict[str, Any] = {}
    texto = mensaje.strip()

    # anio de publicacion: "2024", "del 2023", "publicadas en 2024"
    m = re.search(r"\b(20[0-2][0-9])\b", texto)
    if m:
        anio = int(m.group(1))
        filtros["publicacion_desde"] = f"{anio}-01-01"
        filtros["publicacion_hasta"] = f"{anio}-12-31"

    # materia por alias
    baja = texto.lower()
    for alias, sigla in ALIAS_MATERIA.items():
        if re.search(rf"\b{re.escape(alias)}\b", baja):
            filtros["materia"] = sigla
            break

    # tipo de documento (valores exactos de la columna Tipo)
    for clave, tipo in _MAPA_TIPOS.items():
        if re.search(rf"\b{re.escape(clave)}s?\b", baja):
            filtros["tipo"] = tipo
            break

    # texto: se le quitan anios, articulos y la palabra clave del tipo/materia
    limpio = re.sub(r"\b(20[0-2][0-9])\b", " ", texto)
    limpio = re.sub(r"\b(ley|leyes|reglamento|reglamentos|codigo|codigos|norma|normas|nom|noms|decreto|decretos|acuerdo|acuerdos|del|de|la|el|los|las|en|para|con|sobre|vigentes|vigente|publicadas|publicados|publicada|publicado)\b",
                    " ", limpio, flags=re.IGNORECASE)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    if limpio:
        filtros["texto"] = limpio[:120]

    return filtros
