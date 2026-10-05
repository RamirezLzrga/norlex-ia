"""Cliente de pruebas que replica la firma HMAC que hara la app .NET.

    python pruebas.py

Requiere el servicio corriendo y un .env con NORLEX_IA_SHARED_SECRET
y GROQ_API_KEY.
"""
from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.request

from app.config import get_config
from app.security import firmar

cfg = get_config()
BASE = f"http://{cfg.host}:{cfg.port}"

ROLES: dict[str, int | None] = {
    "visitante": None,
    "admin": 1,
}


def pedir(ruta: str, employee_id: int | None = None, cuerpo: dict | None = None) -> dict:
    url = f"{BASE}{ruta}"
    ahora = int(time.time())
    nonce = secrets.token_hex(16)
    id_firmado = employee_id or 0

    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(url, data=datos, method="POST" if datos else "GET")
    peticion.add_header("Content-Type", "application/json")
    peticion.add_header("X-Norlex-EmployeeId", str(id_firmado))
    peticion.add_header("X-Norlex-Timestamp", str(ahora))
    peticion.add_header("X-Norlex-Nonce", nonce)
    peticion.add_header("X-Norlex-Signature", firmar(id_firmado, ahora, nonce, cfg.shared_secret))

    try:
        with urllib.request.urlopen(peticion, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, **json.loads(e.read() or b"{}")}
    except urllib.error.URLError as e:
        print(f"No se pudo llamar al servicio: {e.reason}")
        print("Arrancalo con:  .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8099")
        sys.exit(1)


def seccion(titulo: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {titulo}")
    print("=" * 78)


def probar_health() -> None:
    seccion("HEALTH")
    print(json.dumps(pedir("/health"), indent=2, ensure_ascii=False))


def probar_herramientas(employee_id: int | None, etiqueta: str) -> None:
    seccion(f"ROL {etiqueta}: herramientas visibles")
    datos = pedir("/api/chat/herramientas", employee_id)
    print(f"rol={datos.get('rol')}  empresa={datos.get('empresa')}  "
          f"suscripcion={datos.get('suscripcion_activa')}")
    print(f"siglas={datos.get('siglas_permitidas')}")
    print("herramientas:", ", ".join(datos.get("herramientas", [])))
    if datos.get("avisos"):
        print("avisos:", datos["avisos"])


def probar_chat(employee_id: int | None, etiqueta: str, mensajes: list[str]) -> None:
    for pregunta in mensajes:
        seccion(f"{etiqueta.upper()} :: {pregunta}")
        datos = pedir("/api/chat/enviar", employee_id, {"mensaje": pregunta})
        if "_http" in datos:
            print(f"HTTP {datos['_http']}: {datos.get('detail')}")
            continue
        print(datos.get("mensaje", "").strip())
        print(f"\n  [rol={datos.get('rol')} herramientas={datos.get('herramientas_usadas')} "
              f"iteraciones={datos.get('iteraciones')}]")


def main() -> None:
    # Se resuelven usuarios reales de la BD para los roles de empresa.
    from app import db

    usuarios = {}
    if db.health().get("estado") == "ok":
        usuarios["titular"] = db.escalar(
            "SELECT TOP (1) e.EmployeeID FROM Employees e "
            "JOIN Clientes c ON c.EmployeeId = e.EmployeeID WHERE e.Activated = 1"
        )
        usuarios["usuario_empresa"] = db.escalar(
            "SELECT TOP (1) EmployeeID FROM Employees "
            "WHERE UserLevel = 4 AND Activated = 1 AND ClienteId IS NOT NULL"
        )
        admin = db.escalar(
            "SELECT TOP (1) EmployeeID FROM Employees WHERE UserLevel = -1 AND Activated = 1"
        )
        ROLES["admin"] = int(admin) if admin else None

    probar_health()

    seccion("ROLES DETECTADOS EN LA BD")
    for nombre, valor in ROLES.items():
        print(f"  {nombre:16} employee_id={valor}")
    for nombre in ("titular", "usuario_empresa"):
        if nombre in usuarios:
            ROLES[nombre] = usuarios[nombre]

    if "--solo-roles" in sys.argv:
        for nombre, valor in ROLES.items():
            probar_herramientas(valor, nombre)
        return

    for nombre, valor in ROLES.items():
        probar_herramientas(valor, nombre)

    probar_chat(ROLES["visitante"], "visitante", [
        "¿Que es Norlex y para que sirve?",
        "¿Cuanto cuestan los planes y que incluyen?",
        "Muestra mi suscripcion",          # debe pedir iniciar sesion
    ])

    if ROLES.get("usuario_empresa"):
        probar_chat(ROLES["usuario_empresa"], "usuario de empresa", [
            "¿Como va la suscripcion de mi empresa?",
            "¿Que colecciones tengo contratadas?",
            "¿Cuantos usuarios tiene mi empresa?",   # debe negar: es del titular
            "Busca normas de seguridad e higiene",
        ])

    if ROLES.get("titular"):
        probar_chat(ROLES["titular"], "titular", [
            "¿Cuando vence mi plan?",
            "¿Cuantos usuarios tiene mi empresa?",
        ])

    if ROLES.get("admin"):
        probar_chat(ROLES["admin"], "admin", [
            "¿Como va el negocio este mes?",
            "¿Cuantas solicitudes estan pendientes?",
            "¿Cuantos usuarios registrados hay?",
            "¿Cuales son los MRR de mis clientes?",  # no existe esa herramienta
        ])


if __name__ == "__main__":
    main()
