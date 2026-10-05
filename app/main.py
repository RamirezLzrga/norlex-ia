"""API del servicio de IA de Norlex.

Endpoints:
    GET  /health        estado del servicio y de la conexion
    POST /api/chat/enviar   peticion de chat (firmada por .NET)
    POST /api/busqueda/interpretar  lenguaje natural -> filtros de busqueda (firmada por .NET)
    GET  /api/chat/herramientas  que herramientas ve cada rol (diagnostico)

La app .NET es la unica que llama a este servicio. El navegador nunca lo
hace: la peticion llega a .NET, se autentica con la cookie de sesion y se
reenvia firmada. Por eso /api/chat/enviar exige la firma HMAC.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, db, interpretar, tools, ia_general
from .config import get_config
from .roles import Contexto, Rol, resolver_contexto
from .security import verificar_firma

DIR_ESTATICO = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("norlex-ia")

cfg = get_config()


@asynccontextmanager
async def _vida(app: FastAPI):
    log.info("Norlex IA arrancando. BD=%s/%s IA=%s", cfg.db_server, cfg.db_name, cfg.ia_habilitada)
    if not cfg.ia_habilitada:
        log.warning("GROQ_API_KEY vacia: el chatbot respondera sin IA.")
    if not cfg.shared_secret:
        log.error("NORLEX_IA_SHARED_SECRET vacio: se rechazara toda peticion.")
    yield
    log.info("Norlex IA detenido.")


app = FastAPI(
    title="Norlex IA",
    version="0.1.0",
    description="Chatbot de Norlex con function calling sobre Groq.",
    lifespan=_vida,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-Norlex-EmployeeId", "X-Norlex-Timestamp", "X-Norlex-Nonce", "X-Norlex-Signature"],
)


# ── Esquemas ────────────────────────────────────────────────────────────

class Turno(BaseModel):
    rol: str = Field(pattern="^(user|assistant)$")
    contenido: str = Field(min_length=1, max_length=2000)


class Pregunta(BaseModel):
    mensaje: str = Field(min_length=1, max_length=500)
    historial: list[Turno] = Field(default_factory=list, max_length=12)
    # Contexto de la pagina que esta viendo el usuario, para que el chatbot
    # sepa de que ordenamiento estan preguntando ("este", "el anterior").
    ordenamiento_id: int | None = None
    pagina: str | None = None


class Sugerencia(BaseModel):
    etiqueta: str
    pregunta: str


class Respuesta(BaseModel):
    mensaje: str
    sugerencias: list[Sugerencia] = []
    rol: str
    herramientas_usadas: list[str] = []
    iteraciones: int = 0
    sin_ia: bool = False


class PeticionBusqueda(BaseModel):
    mensaje: str = Field(min_length=1, max_length=400)
    ip: str | None = None


class RespuestaBusqueda(BaseModel):
    filtros: dict[str, Any]
    origen: str  # "ia" | "heuristica"
    rol: str


# ── Endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    info = db.health()
    info["firma_configurada"] = bool(cfg.shared_secret)
    info["demo"] = cfg.demo
    return info


@app.get("/")
async def raiz() -> RedirectResponse:
    return RedirectResponse("/demo" if cfg.demo else "/docs")


@app.get("/api/chat/herramientas")
async def ver_herramientas(employee_id: int | None = Depends(verificar_firma)) -> dict[str, Any]:
    """Diagnostico: que herramientas puede invocar cada rol."""
    ctx = resolver_contexto(employee_id)
    return {
        "rol": ctx.rol.value,
        "autenticado": ctx.autenticado,
        "empresa": ctx.empresa_nombre,
        "suscripcion_activa": ctx.tiene_suscripcion_activa,
        "siglas_permitidas": ctx.siglas_permitidas or "TODAS",
        "herramientas": sorted(h.nombre for h in tools.REGISTRO.values() if ctx.rol in h.roles),
        "avisos": ctx.avisos,
    }


@app.post("/api/chat/enviar", response_model=Respuesta)
async def enviar(
    pregunta: Pregunta,
    request: Request,
    employee_id: int | None = Depends(verificar_firma),
) -> Respuesta:
    inicio = time.perf_counter()

    ctx = resolver_contexto(employee_id)
    if ctx.avisos and not ctx.autenticado and employee_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"mensaje": " ".join(ctx.avisos)},
        )

    historial = [{"rol": t.rol, "contenido": t.contenido} for t in pregunta.historial]

    try:
        resultado = await agent.responder(pregunta.mensaje, ctx, historial)
    except agent.ErrorAgente as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"mensaje": str(exc)}
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Fallo el agente")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"mensaje": f"El asistente no pudo responder en este momento. ({type(exc).__name__})"},
        ) from exc

    ip = request.headers.get("x-norlex-ip") or (request.client.host if request.client else None)
    db.insertar_auditoria(
        usuario_id=ctx.employee_id,
        usuario=ctx.nombre if ctx.autenticado else None,
        email=ctx.email,
        accion="ChatbotIA",
        entidad="Chatbot",
        detalle=f"{ctx.rol.value}: {pregunta.mensaje[:900]}"
        + (f" [ord={pregunta.ordenamiento_id}]" if pregunta.ordenamiento_id else ""),
        ip=ip,
    )

    ms = (time.perf_counter() - inicio) * 1000
    log.info(
        "rol=%s %dms iters=%d herramientas=%s",
        ctx.rol.value, ms, resultado.iteraciones, ",".join(resultado.herramientas_usadas) or "-",
    )

    return Respuesta(
        mensaje=resultado.mensaje,
        sugerencias=agent.sugerencias(ctx),
        rol=resultado.rol,
        herramientas_usadas=resultado.herramientas_usadas,
        iteraciones=resultado.iteraciones,
        sin_ia=resultado.sin_ia,
    )


@app.post("/api/busqueda/interpretar")
async def interpretar_busqueda(
    peticion: PeticionBusqueda,
    employee_id: int | None = Depends(verificar_firma),
) -> RespuestaBusqueda:
    """Lenguaje natural -> filtros para la busqueda de ordenamientos.

    El navegador NUNCA llama aqui: lo hace la app .NET (ya autenticada por
    cookie), que reenvia la peticion firmada y luego aplica los filtros
    contra la BD con los permisos de colecciones del usuario.
    """
    ctx = resolver_contexto(employee_id)
    if ctx.avisos and not ctx.autenticado and employee_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"mensaje": " ".join(ctx.avisos)},
        )

    # La llamada a Groq es bloqueante: se corre en un hilo para no
    # congelar el event loop (y con el, el chatbot).
    filtros = await asyncio.to_thread(interpretar.interpretar, peticion.mensaje)
    origen = "ia"
    if filtros is None:
        # Sin IA: heuristica local para que el boton nunca quede muerto.
        filtros = interpretar.heuristica(peticion.mensaje)
        origen = "heuristica"
    else:
        filtros = interpretar.normalizar(filtros)

    try:
        db.insertar_auditoria(
            usuario_id=ctx.employee_id,
            usuario=ctx.nombre if ctx.autenticado else None,
            email=ctx.email,
            accion="BusquedaIA",
            entidad="Ordenamientos",
            detalle=f"{ctx.rol.value}: {peticion.mensaje[:300]} -> "
                    f"{filtros} ({origen})",
            ip=peticion.ip,
        )
    except Exception:  # noqa: BLE001 — la auditoria no debe romper la busqueda
        log.exception("No se pudo auditar la busqueda IA")

    return RespuestaBusqueda(filtros=filtros, origen=origen, rol=ctx.rol.value)


# ── IA de proposito general (fichas, insights, reportes) ───────────────

class PeticionResumen(BaseModel):
    ordenamiento_id: int


class PeticionArticulo(BaseModel):
    ordenamiento_id: int
    articulo: str = Field(min_length=1, max_length=60)
    texto: str | None = Field(default=None, max_length=12000)
    contexto: str | None = Field(default=None, max_length=300)


def _ficha_ordenamiento(ordenamiento_id: int) -> dict[str, Any] | None:
    """Ficha minima pero completa de LEGISXXI para alimentar a la IA."""
    fila = db.consultar_uno(
        """
        SELECT TOP (1) ID, Nombrecorto, Nombrecompleto, Descripcion, STATUS,
               Tipo, Emisor, FPDOF, EntradaVigor, FechaAbroga
        FROM LEGISXXI WHERE ID = ?
        """,
        (ordenamiento_id,),
    )
    if not fila:
        return None
    return {
        "id": fila.get("ID"),
        "clave": fila.get("Nombrecorto"),
        "nombre": fila.get("Nombrecompleto") or fila.get("Nombrecorto"),
        "descripcion": fila.get("Descripcion"),
        "estado": fila.get("STATUS"),
        "tipo": fila.get("Tipo"),
        "emisor": fila.get("Emisor"),
        "publicacion": _iso_fecha(fila.get("FPDOF")),
        "entrada_vigor": _iso_fecha(fila.get("EntradaVigor")),
        "abrogacion": _iso_fecha(fila.get("FechaAbroga")),
    }


def _iso_fecha(valor: Any) -> str | None:
    """Fechas de pyodbc (datetime) -> ISO corto, tolerante a None."""
    if valor is None:
        return None
    iso = str(valor)
    return iso[:10] if len(iso) > 10 else iso


@app.post("/api/ia/resumen-ordenamiento")
async def api_resumen_ordenamiento(
    peticion: PeticionResumen, employee_id: int | None = Depends(verificar_firma)
) -> dict[str, Any]:
    """Resumen ejecutivo de un ordenamiento (boton de la ficha).
    Exige sesion: es una funcion para usuarios de la plataforma."""
    ctx = resolver_contexto(employee_id)
    if not ctx.autenticado:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"mensaje": "Inicia sesion para usar el resumen con IA."})
    ficha = _ficha_ordenamiento(peticion.ordenamiento_id)
    if not ficha:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"mensaje": "No existe ese ordenamiento."})
    try:
        resumen = await asyncio.to_thread(ia_general.resumir_ordenamiento, ficha)
    except ia_general.ErrorIA as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"mensaje": f"La IA no esta disponible ahora: {exc}"}) from exc
    db.insertar_auditoria(usuario_id=ctx.employee_id, usuario=ctx.nombre, email=ctx.email,
                          accion="IAResumenOrdenamiento", entidad="LEGISXXI",
                          detalle=f"ord={peticion.ordenamiento_id}", ip=None)
    return {"resumen": resumen}


@app.post("/api/ia/explicar-articulo")
async def api_explicar_articulo(
    peticion: PeticionArticulo, employee_id: int | None = Depends(verificar_firma)
) -> dict[str, Any]:
    ctx = resolver_contexto(employee_id)
    if not ctx.autenticado:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"mensaje": "Inicia sesion para usar esta funcion."})
    ficha = _ficha_ordenamiento(peticion.ordenamiento_id)
    if not ficha:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"mensaje": "No existe ese ordenamiento."})
    try:
        explicacion = await asyncio.to_thread(
            ia_general.explicar_articulo, ficha, peticion.articulo, peticion.texto, peticion.contexto)
    except ia_general.ErrorIA as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"mensaje": f"La IA no esta disponible ahora: {exc}"}) from exc
    db.insertar_auditoria(usuario_id=ctx.employee_id, usuario=ctx.nombre, email=ctx.email,
                          accion="IAExplicarArticulo", entidad="LEGISXXI",
                          detalle=f"ord={peticion.ordenamiento_id} art={peticion.articulo[:50]}", ip=None)
    return {"explicacion": explicacion}


@app.post("/api/ia/insights-admin")
async def api_insights_admin(employee_id: int | None = Depends(verificar_firma)) -> dict[str, Any]:
    """Interpretacion de metricas del negocio para el Dashboard admin.
    Solo rol admin (recalculado aqui contra la BD)."""
    ctx = resolver_contexto(employee_id)
    if not ctx.autenticado or ctx.rol != Rol.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"mensaje": "Solo para administradores."})

    metricas = {
        "clientes_activos": db.escalar("SELECT COUNT(*) FROM Clientes WHERE Activo = 1"),
        "empresas_sin_uso_30d": db.escalar(
            "SELECT COUNT(*) FROM Clientes c WHERE c.Activo = 1 AND NOT EXISTS ("
            "SELECT 1 FROM Consultas q WHERE q.UsuarioId = c.EmployeeId AND q.Fecha >= DATEADD(day, -30, GETDATE()))"),
        "suscripciones_vigentes": db.escalar("SELECT COUNT(*) FROM Suscripciones WHERE Activa = 1 AND FechaFin >= GETDATE()"),
        "suscripciones_por_vencer_30d": db.escalar(
            "SELECT COUNT(*) FROM Suscripciones WHERE Activa = 1 AND FechaFin BETWEEN GETDATE() AND DATEADD(day, 30, GETDATE())"),
        "ingreso_mensual_mxn": db.escalar(
            "SELECT COALESCE(SUM(p.Precio), 0) FROM Suscripciones s JOIN Planes p ON p.Id = s.PlanId "
            "WHERE s.Activa = 1 AND s.FechaFin >= GETDATE()"),
        "ingreso_en_riesgo_mxn": db.escalar(
            "SELECT COALESCE(SUM(p.Precio), 0) FROM Suscripciones s JOIN Planes p ON p.Id = s.PlanId "
            "WHERE s.Activa = 1 AND s.FechaFin BETWEEN GETDATE() AND DATEADD(day, 30, GETDATE())"),
        "consultas_mes": db.escalar(
            "SELECT COUNT(*) FROM Consultas WHERE Fecha >= DATEADD(month, DATEDIFF(month, 0, GETDATE()), 0)"),
        "solicitudes_pendientes": db.escalar("SELECT COUNT(*) FROM Solicitudes WHERE Estatus = 'Pendiente'"),
        "eventos_sospechosos": db.escalar("SELECT COUNT(*) FROM Auditoria WHERE Sospechosa = 1"),
        "usuarios_activos": db.escalar("SELECT COUNT(*) FROM Employees WHERE Activated = 1"),
    }
    try:
        datos = await asyncio.to_thread(ia_general.insights_admin, metricas)
    except ia_general.ErrorIA as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"mensaje": f"La IA no esta disponible ahora: {exc}"}) from exc
    db.insertar_auditoria(usuario_id=ctx.employee_id, usuario=ctx.nombre, email=ctx.email,
                          accion="IAInsightsAdmin", entidad="Dashboard",
                          detalle="metricas interpretadas", ip=None)
    return datos


class PeticionComparador(BaseModel):
    texto: str = Field(min_length=2, max_length=120)
    tipo: str | None = Field(default=None, max_length=60)
    columnas: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


@app.post("/api/ia/resumen-comparador")
async def api_resumen_comparador(
    peticion: PeticionComparador, employee_id: int | None = Depends(verificar_firma)
) -> dict[str, Any]:
    """Lectura ejecutiva del comparador entre jurisdicciones.
    El .NET ya hizo la busqueda y manda los conteos/titulos por columna;
    aqui solo se interpretan (la IA no consulta la BD estatal)."""
    ctx = resolver_contexto(employee_id)
    if not ctx.autenticado:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"mensaje": "Inicia sesion para usar esta funcion."})

    columnas_limpias: list[dict[str, Any]] = []
    for col in peticion.columnas[:12]:
        try:
            columnas_limpias.append({
                "jurisdiccion": str(col.get("nombre") or col.get("key") or "")[:60],
                "total": int(col.get("total") or 0),
                "ordenamientos": [
                    {
                        "nombre": str(o.get("nombrecompleto") or o.get("nombrecorto") or "")[:200],
                        "tipo": str(o.get("tipo") or "")[:60],
                        "publicacion": str(o.get("fpdof") or "")[:10],
                    }
                    for o in (col.get("items") or [])[:5]
                    if isinstance(o, dict)
                ],
                "error": str(col.get("error"))[:120] if col.get("error") else None,
            })
        except Exception:
            continue

    consulta = {
        "tema": peticion.texto,
        "tipo_filtro": peticion.tipo,
        "jurisdicciones": columnas_limpias,
    }
    try:
        resumen = await asyncio.to_thread(ia_general.resumen_comparador, consulta)
    except ia_general.ErrorIA as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"mensaje": f"La IA no esta disponible ahora: {exc}"}) from exc
    db.insertar_auditoria(usuario_id=ctx.employee_id, usuario=ctx.nombre, email=ctx.email,
                          accion="IAResumenComparador", entidad="Comparador",
                          detalle=f"tema={peticion.texto[:80]}", ip=None)
    return {"resumen": resumen}


class PeticionTexto(BaseModel):
    texto: str = Field(min_length=1, max_length=8000)


@app.post("/api/ia/resumen-texto")
async def api_resumen_texto(
    peticion: PeticionTexto, employee_id: int | None = Depends(verificar_firma)
) -> dict[str, Any]:
    """Parrafo ejecutivo para textos del sistema (reporte semanal).
    Lo llama la app .NET con firma de servicio (id 0 permitido)."""
    try:
        resumen = await asyncio.to_thread(ia_general.resumen_reporte, peticion.texto)
    except ia_general.ErrorIA as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"mensaje": f"La IA no esta disponible ahora: {exc}"}) from exc
    return {"resumen": resumen}


@app.get("/api/chat/bienvenida")
async def bienvenida(employee_id: int | None = Depends(verificar_firma)) -> dict[str, Any]:
    """Mensaje y sugerencias de arranque para el widget."""
    ctx = resolver_contexto(employee_id)
    return {
        "mensaje": _saludo(ctx),
        "sugerencias": agent.sugerencias(ctx),
        "rol": ctx.rol.value,
    }


def _saludo(ctx: Contexto) -> str:
    nombres = {
        Rol.ADMIN: "Aqui tienes el pulso de la plataforma. Preguntame por el negocio, "
                   "solicitudes, clientes o la auditoria.",
        Rol.TITULAR: f"Bienvenido, {ctx.nombre_para_mensaje}. Te ayudo con tu suscripcion, "
                     "tus colecciones o cualquier busqueda de legislacion.",
        Rol.USUARIO_EMPRESA: f"Hola, {ctx.nombre_para_mensaje}. Consulto la suscripcion de "
                              "tu empresa y la legislacion que tienen contratada.",
        Rol.VISITANTE: "Hola. Soy el asistente de Norlex. Con gusto te explico que hacemos, "
                       "cuanto cuestan los planes y como se contrata.",
    }
    return nombres.get(ctx.rol, nombres[Rol.VISITANTE])


# ══════════════════════════════════════════════════════════════════════
#  MODO DEMO — pagina de pruebas
#
#  DESACTIVADO salvo NORLEX_IA_DEMO=true. NO usar en produccion: estos
#  endpoints eligen el rol desde un parametro de query y NO exigen firma
#  HMAC, asi que cualquiera en internet podria pedir datos de admin.
# ══════════════════════════════════════════════════════════════════════

app.mount("/static", StaticFiles(directory=DIR_ESTATICO), name="static")


def _exigir_demo() -> None:
    if not cfg.demo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Modo demo desactivado. Activalo con NORLEX_IA_DEMO=true.",
        )


def _contexto_demo(rol: str | None) -> Contexto:
    """Resuelve un rol por nombre para poder probarlos sin cookie."""
    from . import db as _db

    if not rol:
        return resolver_contexto(None)

    if rol == "admin":
        employee_id = _db.escalar(
            "SELECT TOP (1) EmployeeID FROM Employees WHERE UserLevel = -1 AND Activated = 1"
        )
    elif rol == "titular":
        employee_id = _db.escalar(
            "SELECT TOP (1) e.EmployeeID FROM Employees e "
            "JOIN Clientes c ON c.EmployeeId = e.EmployeeID WHERE e.Activated = 1"
        )
    elif rol == "empleado":
        employee_id = _db.escalar(
            "SELECT TOP (1) EmployeeID FROM Employees "
            "WHERE UserLevel = 4 AND Activated = 1 AND ClienteId IS NOT NULL"
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rol desconocido: {rol}. Usa visitante, titular, empleado o admin.",
        )

    if not employee_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No hay ningun usuario con rol '{rol}' en la base de datos.",
        )
    return resolver_contexto(int(employee_id))


@app.get("/demo", include_in_schema=False)
async def pagina_demo() -> FileResponse:
    _exigir_demo()
    return FileResponse(DIR_ESTATICO / "demo.html")


@app.get("/api/demo/contexto")
async def demo_contexto(rol: str | None = None) -> dict[str, Any]:
    _exigir_demo()
    ctx = _contexto_demo(rol)
    return {
        "rol": ctx.rol.value,
        "employee_id": ctx.employee_id,
        "nombre": ctx.nombre,
        "autenticado": ctx.autenticado,
        "empresa": ctx.empresa_nombre,
        "suscripcion_activa": ctx.tiene_suscripcion_activa,
        "siglas_permitidas": ctx.siglas_permitidas or "TODAS (sin filtro)",
        "herramientas": sorted(h.nombre for h in tools.REGISTRO.values() if ctx.rol in h.roles),
        "avisos": ctx.avisos,
    }


@app.get("/api/demo/herramienta")
async def demo_herramienta(herramienta: str, rol: str | None = None) -> Any:
    """Ejecuta una herramienta suelta para ver el dato crudo de la BD."""
    _exigir_demo()
    ctx = _contexto_demo(rol)
    from . import tools as _tools

    if herramienta not in _tools.REGISTRO:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe la herramienta '{herramienta}'.",
        )
    return _tools.ejecutar(herramienta, {}, ctx)


@app.post("/api/demo/chat")
async def demo_chat(pregunta: Pregunta, rol: str | None = None) -> dict[str, Any]:
    _exigir_demo()
    ctx = _contexto_demo(rol)
    historial = [{"rol": t.rol, "contenido": t.contenido} for t in pregunta.historial]

    try:
        resultado = await agent.responder(pregunta.mensaje, ctx, historial)
    except agent.ErrorAgente as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"mensaje": str(exc)}
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Fallo el agente (demo)")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"mensaje": f"{type(exc).__name__}: {exc}"},
        ) from exc

    return {
        "mensaje": resultado.mensaje,
        "sugerencias": agent.sugerencias(ctx),
        "rol": resultado.rol,
        "herramientas_usadas": resultado.herramientas_usadas,
        "iteraciones": resultado.iteraciones,
        "sin_ia": resultado.sin_ia,
        "ultima_herramienta": resultado.resultados,
    }


@app.get("/api/demo/bienvenida")
async def demo_bienvenida(rol: str | None = None) -> dict[str, Any]:
    _exigir_demo()
    ctx = _contexto_demo(rol)
    return {
        "mensaje": _saludo(ctx),
        "sugerencias": agent.sugerencias(ctx),
        "rol": ctx.rol.value,
    }
