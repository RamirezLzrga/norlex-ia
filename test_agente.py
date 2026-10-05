"""Prueba del loop de agente con un Groq simulado.

No necesita API key: verifica de punta a punta que el modelo recibe solo las
herramientas de su rol, que las herramientas consultan la BD real, que el
resultado vuelve al modelo y que el texto final sale bien.

    python test_agente.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

from app import agent, tools
from app.roles import db_now, resolver_contexto


class LlamadaFalsa:
    def __init__(self, nombre: str, argumentos: str, id_: str) -> None:
        self.id = id_
        self.type = "function"
        self.function = SimpleNamespace(name=nombre, arguments=argumentos)


class GroqSimulado:
    """Replica el contrato que usa agent.responder."""

    def __init__(self, guion: list[dict[str, Any]]) -> None:
        self.guion = list(guion)
        self.llamadas: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools=None, tool_choice=None, temperature=None, max_tokens=None):
        self.llamadas.append({"model": model, "tools": [t["function"]["name"] for t in (tools or [])]})

        paso = self.guion.pop(0) if self.guion else {"content": "Sin guion."}
        herramientas = []
        contenido = None
        for llamada in paso.get("tool_calls", []):
            herramientas.append(LlamadaFalsa(**llamada))
        contenido = paso.get("content")
        mensaje = SimpleNamespace(content=contenido, tool_calls=herramientas or None)
        return SimpleNamespace(choices=[SimpleNamespace(message=mensaje)])


def instalar_simulacion(guion: list[dict[str, Any]]) -> GroqSimulado:
    simulado = GroqSimulado(guion)
    agent._cliente = simulado  # type: ignore[assignment]
    return simulado


def _t(nombre: str, argumentos: dict[str, Any], id_: str) -> dict[str, Any]:
    return {"nombre": nombre, "argumentos": json.dumps(argumentos), "id_": id_}


def _contenido(texto: str) -> dict[str, Any]:
    return {"content": texto}


async def _responder(mensaje: str, employee_id: int | None, guion: list[dict[str, Any]]):
    simulado = instalar_simulacion(guion)
    ctx = resolver_contexto(employee_id)
    return await agent.responder(mensaje, ctx), ctx, simulado


def prueba_admin() -> None:
    print("\n" + "=" * 76)
    print("  ADMIN pide metricas -> debe ver metricas_negocio y devolver cifras reales")
    print("=" * 76)
    from app import db

    admin = db.escalar("SELECT TOP (1) EmployeeID FROM Employees WHERE UserLevel = -1 AND Activated = 1")
    if not admin:
        print("No hay admin en la BD, se omite.")
        return

    guion = [
        {"tool_calls": [_t("metricas_negocio", {}, "c1")]},
        _contenido("El MRR actual es X con Y suscripciones vigentes."),
    ]
    resp, ctx, sim = asyncio.run(_responder("Como va el negocio?", int(admin), guion))
    print(f"rol={ctx.rol.value}  herramientas={resp.herramientas_usadas}")
    print(f"respuesta: {resp.mensaje}")
    assert ctx.rol.value == "admin", "no resolvio el rol admin"
    assert resp.herramientas_usadas == ["metricas_negocio"], "no llamo a la herramienta"
    assert "metricas_negocio" in sim.llamadas[0]["tools"], "no le ofrecio la herramienta admin"
    assert len(sim.llamadas[0]["tools"]) > 10, "un admin deberia ver muchas herramientas"


def prueba_visitante_bloqueado() -> None:
    print("\n" + "=" * 76)
    print("  VISITANTE pide metricas -> el modelo NO debe ver la herramienta")
    print("=" * 76)
    guion = [_contenido("No tengo acceso a esas cifras.")]
    resp, ctx, sim = asyncio.run(_responder("Cuanto es el MRR?", None, guion))
    print(f"rol={ctx.rol.value}  herramientas={resp.herramientas_usadas}")
    print(f"visibles: {sim.llamadas[0]['tools']}")
    assert "metricas_negocio" not in sim.llamadas[0]["tools"], "¡le filtro una herramienta admin!"
    assert "catalogo_planes" in sim.llamadas[0]["tools"], "le faltan las publicas"


def prueba_visitante_intenta_escalar() -> None:
    """Aunque el modelo intente llamar a una herramienta prohibida, el servidor la rechaza."""
    print("\n" + "=" * 76)
    print("  ESCALAMIENTO: el modelo pide una herramienta de admin siendo visitante")
    print("=" * 76)
    guion = [
        {"tool_calls": [_t("listar_clientes", {}, "c1")]},
        _contenido("Esa informacion solo la ven los administradores."),
    ]
    resp, ctx, _ = asyncio.run(_responder("dame la lista de clientes", None, guion))
    print(f"rol={ctx.rol.value}  respuesta: {resp.mensaje}")
    resultado = tools.ejecutar("listar_clientes", {}, ctx)
    print(f"ejecucion directa: {resultado}")
    assert "error" in resultado, "¡dejo pasar una herramienta de admin!"


def prueba_legislacion_con_permisos() -> None:
    print("\n" + "=" * 76)
    print("  LEGISLACION: titular busca en una coleccion que SI tiene")
    print("=" * 76)
    from app import db

    titular = db.escalar(
        "SELECT TOP (1) e.EmployeeID FROM Employees e "
        "JOIN Clientes c ON c.EmployeeId = e.EmployeeID WHERE e.Activated = 1"
    )
    if not titular:
        print("No hay titular en la BD, se omite.")
        return

    ctx = resolver_contexto(titular)
    print(f"empresa={ctx.empresa_nombre}  siglas={ctx.siglas_permitidas}")

    permitida = ctx.siglas_permitidas[0] if ctx.siglas_permitidas else None
    if not permitida:
        print("El titular no tiene siglas: esperaria acceso total.")
        return

    guion = [
        {"tool_calls": [_t("buscar_legislacion", {"materia": permitida}, "c1")]},
        _contenido(f"Encontre normas de {permitida}."),
    ]
    resp, ctx2, _ = asyncio.run(_responder(f"Busca leyes de {permitida}", titular, guion))
    print(f"respuesta: {resp.mensaje}")
    assert resp.herramientas_usadas == ["buscar_legislacion"]


def prueba_legislacion_bloqueada() -> None:
    print("\n" + "=" * 76)
    print("  LEGISLACION: pide una coleccion que NO tiene contratada")
    print("=" * 76)
    from app import db

    titular = db.escalar(
        "SELECT TOP (1) e.EmployeeID FROM Employees e "
        "JOIN Clientes c ON c.EmployeeId = e.EmployeeID WHERE e.Activated = 1"
    )
    if not titular:
        print("No hay titular en la BD, se omite.")
        return

    ctx = resolver_contexto(titular)
    prohibida = next(
        (s for s in ["TCOM", "AIPD", "TURI", "EDUC", "DEHU", "ELEC", "NOTA"]
         if s not in ctx.siglas_permitidas),
        None,
    )
    if not prohibida:
        print("El titular tiene todas las colecciones, se omite.")
        return

    resultado = tools.ejecutar("buscar_legislacion", {"materia": prohibida}, ctx)
    print(f"coleccion pedida: {prohibida}")
    print(f"resultado: {resultado.get('sin_acceso')} -> {resultado.get('motivo')}")
    assert resultado.get("sin_acceso") is True, "¡dejo buscar en una coleccion no contratada!"


def prueba_datos_reales() -> None:
    print("\n" + "=" * 76)
    print("  HERRAMIENTAS CONTRA LA BD REAL")
    print("=" * 76)
    from app import db

    titular = db.escalar(
        "SELECT TOP (1) e.EmployeeID FROM Employees e "
        "JOIN Clientes c ON c.EmployeeId = e.EmployeeID WHERE e.Activated = 1"
    )
    admin = db.escalar("SELECT TOP (1) EmployeeID FROM Employees WHERE UserLevel = -1 AND Activated = 1")

    casos = [
        ("visitante", resolver_contexto(None), "catalogo_planes", {}),
        ("visitante", resolver_contexto(None), "que_ofrece_la_plataforma", {}),
        ("admin", resolver_contexto(int(admin) if admin else None), "metricas_negocio", {}),
        ("admin", resolver_contexto(int(admin) if admin else None), "solicitudes_pendientes", {"limite": 3}),
        ("admin", resolver_contexto(int(admin) if admin else None), "listar_clientes", {"limite": 3}),
        ("admin", resolver_contexto(int(admin) if admin else None), "usuarios_registrados", {"limite": 3}),
        ("admin", resolver_contexto(int(admin) if admin else None), "empresas_sin_uso", {"limite": 3}),
        ("admin", resolver_contexto(int(admin) if admin else None), "actividad_sospechosa", {"limite": 3}),
    ]
    if titular:
        ctx = resolver_contexto(int(titular))
        casos += [
            ("titular", ctx, "mi_suscripcion", {}),
            ("titular", ctx, "mis_materias", {}),
            ("titular", ctx, "mi_empresa", {}),
            ("titular", ctx, "mi_perfil", {}),
            ("titular", ctx, "mis_alertas", {"solo_no_leidas": True}),
            ("titular", ctx, "buscar_legislacion", {"texto": "proteccion de datos"}),
        ]

    fallos = 0
    for etiqueta, ctx, nombre, argumentos in casos:
        resultado = tools.ejecutar(nombre, argumentos, ctx)
        ok = "error" not in resultado
        marcador = "ok " if ok else "ERR"
        resumen = json.dumps(resultado, ensure_ascii=False, default=str)
        print(f"  [{marcador}] {etiqueta:10} {nombre:24} {resumen[:110]}")
        if not ok:
            fallos += 1
    print(f"\n  {len(casos) - fallos}/{len(casos)} herramientas sin error")


def main() -> int:
    pruebas = [
        prueba_visitante_bloqueado,
        prueba_visitante_intenta_escalar,
        prueba_admin,
        prueba_legislacion_con_permisos,
        prueba_legislacion_bloqueada,
        prueba_datos_reales,
    ]
    for fn in pruebas:
        try:
            fn()
        except AssertionError as exc:
            print(f"  !! FALLO: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"  !! ERROR: {type(exc).__name__}: {exc}")
            return 1
    print("\nTODAS LAS PRUEBAS PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
