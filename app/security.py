"""Verificacion de la firma HMAC que envia la app .NET.

El navegador nunca habla con este servicio: la app .NET recibe la peticion
(ya autenticada por su cookie) y la reenvia firmada. Asi Python no necesita
reimplementar login ni 2FA, y el visitante sigue sin credenciales.

Cabeceras esperadas:
    X-Norlex-EmployeeId : EmployeeID, o 0 / ausente para visitante
    X-Norlex-Timestamp  : unix epoch en segundos
    X-Norlex-Nonce      : aleatorio, evita reenvios
    X-Norlex-Signature  : hex HMAC-SHA256 de "<id>.<ts>.<nonce>" con el secreto

Nota de seguridad: el EmployeeId llega firmado, pero AUN ASI Python relee
Employees y recalcula el rol. La firma demuestra que la peticion viene de
.NET; el rol se decide contra la base de datos, nunca contra la cabecera.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from hashlib import sha256

from fastapi import Header, HTTPException, status

from .config import get_config

log = logging.getLogger(__name__)


def firmar(employee_id: int, timestamp: int, nonce: str, secreto: str) -> str:
    """Genera la firma. Mismo algoritmo que usa el lado C#."""
    carga = f"{employee_id}.{timestamp}.{nonce}".encode("utf-8")
    return hmac.new(secreto.encode("utf-8"), carga, sha256).hexdigest()


def _rechazar(detalle: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detalle,
    )


def verificar_firma(
    x_norlex_employeeid: str | None = Header(default=None, alias="X-Norlex-EmployeeId"),
    x_norlex_timestamp: str | None = Header(default=None, alias="X-Norlex-Timestamp"),
    x_norlex_nonce: str | None = Header(default=None, alias="X-Norlex-Nonce"),
    x_norlex_signature: str | None = Header(default=None, alias="X-Norlex-Signature"),
) -> int | None:
    """Valida la firma y devuelve el EmployeeId, o None si es visitante.

    Lanza 401 si algo no cuadra. La firma ausente solo se tolera cuando
    NORLEX_IA_ALLOW_ANONYMOUS esta activo (modo visitante publico).
    """
    cfg = get_config()

    if not cfg.shared_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NORLEX_IA_SHARED_SECRET no esta configurado.",
        )

    employee_id = _parsear_employee_id(x_norlex_employeeid)
    es_visitante = employee_id is None

    if not x_norlex_signature:
        if es_visitante and cfg.allow_anonymous:
            return None
        raise _rechazar("Falta la cabecera X-Norlex-Signature.")

    if not x_norlex_timestamp or not x_norlex_nonce:
        raise _rechazar("Faltan las cabeceras X-Norlex-Timestamp o X-Norlex-Nonce.")

    try:
        timestamp = int(x_norlex_timestamp)
    except ValueError:
        raise _rechazar("X-Norlex-Timestamp no es un numero valido.") from None

    if abs(int(time.time()) - timestamp) > cfg.signature_ttl:
        raise _rechazar("La firma expiro. Reintenta la peticion.")

    # Se firma el id tal cual lo recibio (0 para visitante), no el ya parseado.
    id_firmado = _parsear_employee_id(x_norlex_employeeid) or 0
    esperada = firmar(id_firmado, timestamp, x_norlex_nonce, cfg.shared_secret)
    if not hmac.compare_digest(esperada, x_norlex_signature.strip().lower()):
        raise _rechazar("Firma invalida.")

    return employee_id


def _parsear_employee_id(valor: str | None) -> int | None:
    """None si falta, es 0, no es entero o es negativo."""
    if valor is None or not valor.strip():
        return None
    try:
        numero = int(valor)
    except ValueError:
        return None
    return numero if numero > 0 else None


def nuevo_nonce() -> str:
    """Para pruebas desde Python."""
    return secrets.token_hex(16)
