"""Acceso a BD_Fed con pyodbc.

Solo lectura para el chatbot, salvo `insertar_auditoria` (bitacora) y
`registrar_consulta` (historial de busquedas), que replican lo que ya hace
hoy ChatbotService en C#.

Todas las consultas usan parametros: nunca se concatena entrada del usuario.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Sequence

import pyodbc

from .config import Config, get_config

log = logging.getLogger(__name__)

_local = threading.local()
_lock = threading.Lock()

#: Cold start de Render: la BD puede tardar unos segundos en despertar.
_REINTENTOS = 3
_ESPERA_REINTENTO = 1.0


class ErrorDB(RuntimeError):
    """Fallo al hablar con la base de datos."""


def _conectar(cfg: Config) -> pyodbc.Connection:
    try:
        return pyodbc.connect(cfg.connection_string, timeout=10, autocommit=True)
    except pyodbc.Error as exc:  # pragma: no cover - depende del entorno
        raise ErrorDB(f"No se pudo conectar a {cfg.db_server}/{cfg.db_name}: {exc}") from exc


def _obtener_conexion() -> pyodbc.Connection:
    """Una conexion por hilo, reutilizada entre peticiones.

    En Render el contenedor se suspende y la BD puede tardar en despertar, asi
    que se reintenta y la conexion muerta se descarta antes de reintentar.
    """
    con = getattr(_local, "conexion", None)
    if con is not None:
        try:
            if con.cursor().execute("SELECT 1"):
                return con
        except pyodbc.Error:
            _cerrar_conexion()

    cfg = get_config()
    ultimo_error: Exception | None = None
    for intento in range(1, _REINTENTOS + 1):
        try:
            con = _conectar(cfg)
            _local.conexion = con
            return con
        except ErrorDB as exc:
            ultimo_error = exc
            if intento < _REINTENTOS:
                log.warning("Conexion fallida (intento %d/%d): %s", intento, _REINTENTOS, exc)
                time.sleep(_ESPERA_REINTENTO * intento)
    assert ultimo_error is not None
    raise ultimo_error


def _cerrar_conexion() -> None:
    con = getattr(_local, "conexion", None)
    if con is not None:
        try:
            con.close()
        except pyodbc.Error:
            pass
        _local.conexion = None


@contextmanager
def cursor() -> Iterator[pyodbc.Cursor]:
    """Cursor parametrizado. No hace commit: la conexion va en autocommit."""
    cur = _obtener_conexion().cursor()
    try:
        yield cur
    finally:
        cur.close()


def consultar(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Ejecuta un SELECT y devuelve filas como diccionarios."""
    with cursor() as cur:
        try:
            cur.execute(sql, tuple(params))
        except pyodbc.Error as exc:
            log.error("Error SQL: %s | %s", exc, sql)
            raise ErrorDB(str(exc)) from exc
        if cur.description is None:
            return []
        columnas = [c[0] for c in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def consultar_uno(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    filas = consultar(sql, params)
    return filas[0] if filas else None


def escalar(sql: str, params: Sequence[Any] = ()) -> Any:
    """Primer valor de la primera fila (util para COUNT/SUM)."""
    with cursor() as cur:
        try:
            cur.execute(sql, tuple(params))
            fila = cur.fetchone()
            return fila[0] if fila else None
        except pyodbc.Error as exc:
            log.error("Error SQL: %s | %s", exc, sql)
            raise ErrorDB(str(exc)) from exc


def ejecutar(sql: str, params: Sequence[Any] = ()) -> None:
    with cursor() as cur:
        try:
            cur.execute(sql, tuple(params))
        except pyodbc.Error as exc:
            log.error("Error SQL: %s | %s", exc, sql)
            raise ErrorDB(str(exc)) from exc


# ── Escrituras de la bitacora (replican ChatbotService.cs:96) ───────────

def insertar_auditoria(
    usuario_id: int | None,
    usuario: str | None,
    email: str | None,
    accion: str,
    entidad: str,
    detalle: str,
    ip: str | None = None,
    sospechosa: bool = False,
) -> None:
    """Nunca debe romper la respuesta al usuario: se traga sus propios fallos."""
    try:
        ejecutar(
            """
            INSERT INTO Auditoria
                (UsuarioId, Usuario, Email, Accion, Entidad, Detalle, Ip, Sospechosa, Fecha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usuario_id,
                (usuario or "")[:255],
                (email or "")[:255],
                accion[:100],
                entidad[:100],
                detalle[:1000],
                (ip or "")[:50],
                1 if sospechosa else 0,
                datetime.now(),
            ),
        )
    except ErrorDB as exc:
        log.warning("No se pudo auditar: %s", exc)


def registrar_consulta(
    usuario_id: int | None,
    texto: str | None,
    materia: str | None,
    resultados: int,
) -> None:
    try:
        ejecutar(
            "INSERT INTO Consultas (UsuarioId, Texto, Materia, Resultados, Fecha) VALUES (?, ?, ?, ?, ?)",
            (usuario_id, (texto or "")[:200], (materia or "")[:100], int(resultados), datetime.now()),
        )
    except ErrorDB as exc:
        log.warning("No se pudo registrar la consulta: %s", exc)


def health() -> dict[str, Any]:
    """Estado de la conexion, para GET /health."""
    cfg = get_config()
    info: dict[str, Any] = {
        "servidor": cfg.db_server,
        "base": cfg.db_name,
        "ia_habilitada": cfg.ia_habilitada,
        "modelo": cfg.modelo if cfg.ia_habilitada else None,
    }
    try:
        with _lock:
            info["ordenamientos"] = escalar("SELECT COUNT(*) FROM LEGISXXI")
        info["estado"] = "ok"
    except ErrorDB as exc:
        info["estado"] = "degradado"
        info["error"] = str(exc)
    return info
