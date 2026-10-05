"""Herramientas que el modelo puede invocar (function calling).

Cada herramienta declara:
  * esquema JSON  -> lo que ve el LLM
  * roles permitidos -> quien puede pedirlo
  * funcion       -> lo que realmente consulta la BD

El modelo NUNCA ve datos por su cuenta: o llama a una herramienta, o se
queda sin respuesta. Eso elimina las alucinaciones sobre datos de la
plataforma (el problema que arrastra el chatbot actual en C#).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import db
from .roles import (
    ALIAS_MATERIA,
    MATERIAS_CONOCIDAS,
    NOMBRE_MATERIA,
    Contexto,
    Rol,
    db_now,
)

log = logging.getLogger(__name__)

Handler = Callable[[Contexto, dict[str, Any]], Awaitable[dict[str, Any]]]
ROL_GUARD = Callable[[Contexto], bool]

_TODOS_AUTENTICADOS = {Rol.ADMIN, Rol.TITULAR, Rol.USUARIO_EMPRESA}
_CON_CLIENTE = {Rol.ADMIN, Rol.TITULAR, Rol.USUARIO_EMPRESA}


@dataclass(frozen=True, slots=True)
class Herramienta:
    nombre: str
    descripcion: str
    esquema: dict[str, Any]
    handler: Handler
    roles: frozenset[Rol]
    sin_acceso: str = ""

    def a_json(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.nombre,
                "description": self.descripcion,
                "parameters": self.esquema,
            },
        }


REGISTRO: dict[str, Herramienta] = {}


def herramienta(
    nombre: str,
    descripcion: str,
    esquema: dict[str, Any],
    roles: frozenset[Rol],
    sin_acceso: str = "",
) -> Callable[[Handler], Handler]:
    def decorador(fn: Handler) -> Handler:
        REGISTRO[nombre] = Herramienta(
            nombre=nombre,
            descripcion=descripcion,
            esquema=esquema,
            handler=fn,
            roles=roles,
            sin_acceso=sin_acceso,
        )
        return fn

    return decorador


def herramientas_para(ctx: Contexto) -> list[dict[str, Any]]:
    """Solo se exponen al modelo las herramientas que su rol puede usar."""
    visibles = [h for h in REGISTRO.values() if ctx.rol in h.roles]
    return [h.a_json() for h in visibles]


def ejecutar(nombre: str, argumentos: dict[str, Any], ctx: Contexto) -> dict[str, Any]:
    h = REGISTRO.get(nombre)
    if h is None:
        return {"error": f"La herramienta '{nombre}' no existe."}
    if ctx.rol not in h.roles:
        return {"error": h.sin_acceso or "No tienes permiso para usar esa función."}
    try:
        return _await(h.handler(ctx, argumentos))
    except db.ErrorDB as exc:
        log.error("Fallo %s: %s", nombre, exc)
        return {"error": "No se pudo consultar la base de datos en este momento."}
    except Exception as exc:  # noqa: BLE001 - el modelo no debe ver tracebacks
        log.exception("Fallo inesperado en %s", nombre)
        return {"error": f"Error inesperado en {nombre}: {exc}"}


def _await(coroutine: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # Ya hay loop corriendo: se ejecuta en uno temporal para no bloquear.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor).strip()


def _corta(valor: Any, maximo: int) -> str:
    texto = _texto(valor)
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


def _iso(valor: Any) -> str | None:
    return valor.isoformat(timespec="seconds") if valor is not None else None


# ══════════════════════════════════════════════════════════════════════
#  PÚBLICO — cualquier visitante, incluso sin sesion
# ══════════════════════════════════════════════════════════════════════

TODOS = frozenset(Rol)

_ESQUEMA_VACIO = {"type": "object", "properties": {}, "additionalProperties": False}


@herramienta(
    "catalogo_planes",
    "Lista los planes de suscripcion disponibles con nombre, precio en MXN, "
    "duracion en dias y las colecciones de legislacion que incluye. Usar cuando "
    "el usuario pregunte precios, planes, paquetes, cuanto cuesta o que incluye.",
    _ESQUEMA_VACIO,
    TODOS,
)
async def catalogo_planes(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    planes = db.consultar(
        "SELECT Nombre, Descripcion, Precio, DuracionDias, MateriasPermitidas "
        "FROM Planes WHERE Activo = 1 ORDER BY Precio"
    )
    from .roles import _parsear_materias  # noqa: PLC0415

    return {
        "planes": [
            {
                "nombre": p.get("Nombre"),
                "precio_mxn": float(p.get("Precio") or 0),
                "duracion_dias": p.get("DuracionDias"),
                "descripcion": _corta(p.get("Descripcion"), 200),
                "colecciones": [
                    NOMBRE_MATERIA.get(s, s) for s in _parsear_materias(p.get("MateriasPermitidas"))
                ],
            }
            for p in planes
        ]
    }


@herramienta(
    "que_ofrece_la_plataforma",
    "Descripcion funcional de la plataforma: que hace, que ofrece el buscador, "
    "la matriz legal, las alertas, los reportes ADP, el calendario de vigencias y "
    "la ficha 360 de empresas. Usar cuando pregunten que es Norlex, que hace, "
    "para que sirve o como funciona.",
    _ESQUEMA_VACIO,
    TODOS,
)
async def que_ofrece_la_plataforma(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    colecciones = db.consultar(
        "SELECT SIGLAS, NOMBREC FROM MATERIAS WHERE ACTIVO = 1 ORDER BY NOMBREC"
    )
    return {
        "resumen": (
            "Norlex es una plataforma mexicana que centraliza y clasifica la "
            "legislacion federal, estatal y municipal que aplica a la industria, "
            "organizada en colecciones tematicas con suscripciones por empresa."
        ),
        "funciones": [
            "Busqueda de ordenamientos por palabra clave, materia, entidad y numero de norma.",
            "Matriz legal: comparativo de requisitos y fechas de cumplimiento.",
            "Alertas de reformas, nuevas publicaciones y entradas en vigor.",
            "Calendario de vigencias, vencimientos y abrogaciones.",
            "Reportes ADP (Aviso Diario de Publicaciones) en Excel y PDF.",
            "Ficha 360 de empresas.",
            "Listas, favoritos y busquedas guardadas.",
        ],
        "colecciones": [f"{NOMBRE_MATERIA.get(c['SIGLAS'], c['NOMBREC'])} ({c['SIGLAS']})" for c in colecciones],
    }


@herramienta(
    "como_contratar",
    "Explica el proceso comercial: como solicitar un plan, como se aprueba una "
    "solicitud, como se paga y como se activa el acceso. Usar cuando pregunten "
    "como contratar, como paga, como se registra una empresa o que sigue.",
    _ESQUEMA_VACIO,
    TODOS,
)
async def como_contratar(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "proceso": [
            "Se llena el formulario de solicitud en la pagina publica con los datos de la empresa.",
            "El equipo de Norlex revisa y aprueba la solicitud.",
            "Se realiza el pago; la suscripcion se activa y la empresa queda con su plan y sus colecciones.",
            "El titular puede invitar a los usuarios de su empresa, que ven las mismas colecciones.",
            "Las renovaciones son automaticas salvo que se cancelen.",
        ],
        "contacto": "Las solicitudes se capturan en el formulario publico de la plataforma.",
        "tipos_usuario": {
            "titular": "quien contractors el plan; administra los usuarios de su empresa",
            "usuario_empresa": "empleado invited por el titular; consulta pero no administra ni renueva",
        },
    }


# ══════════════════════════════════════════════════════════════════════
#  LEGISLACION — requiere suscripcion activa (o ser admin)
# ══════════════════════════════════════════════════════════════════════

_ACCESO_LEGISLACION = frozenset(
    {Rol.ADMIN, Rol.TITULAR, Rol.USUARIO_EMPRESA}
)


@herramienta(
    "buscar_legislacion",
    "Busca ordenamientos (leyes, codigos, reglamentos, NOMs) en el catalogo. "
    "Respeta las colecciones contratadas por el usuario. Usar cuando pidan una "
    "ley, un reglamento, una norma, una NOM, o busquen por materia o palabra clave.",
    {
        "type": "object",
        "properties": {
            "texto": {
                "type": "string",
                "description": "Palabra clave o fragmento a buscar, por ejemplo 'residuos peligrosos'. Opcional.",
            },
            "materia": {
                "type": "string",
                "description": "Nombre o sigla de la materia, por ejemplo 'ambiental' o 'SEGH'. Opcional.",
            },
            "tipo": {
                "type": "string",
                "description": "Tipo de documento a filtrar, por ejemplo 'LEYES', 'REGLAMENTOS', 'CONVENIOS' o 'NOM'. Opcional; sin este argumento se devuelven todos los tipos.",
            },
            "ordenamiento_id": {
                "type": "integer",
                "description": "ID exacto de un ordenamiento ya conocido. Opcional.",
            },
        },
        "additionalProperties": False,
    },
    _ACCESO_LEGISLACION,
    sin_acceso="Tu empresa no tiene una suscripcion activa, asi que no puedo buscar ordenamientos.",
)
async def buscar_legislacion(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.tiene_suscripcion_activa:
        return {
            "sin_acceso": True,
            "motivo": "No hay una suscripcion activa para esta empresa.",
        }

    texto = _corta(args.get("texto"), 120) or None
    materia = _resolver_materia(args.get("materia"))
    tipo = _corta(args.get("tipo"), 60) or None
    ordenamiento_id = args.get("ordenamiento_id")

    # Filtro de colecciones: lista vacia = sin restriccion.
    permitidas = ctx.siglas_permitidas or None

    cond: list[str] = []
    params: list[Any] = []

    if ordenamiento_id is not None:
        cond.append("ID = ?")
        params.append(ordenamiento_id)
    else:
        if not texto and not materia and not tipo:
            return {"error": "Indica al menos un texto, una materia, un tipo o un ID."}
        if texto:
            cond.append("(Nombrecompleto LIKE ? OR Nombrecorto LIKE ? OR Descripcion LIKE ?)")
            patron = f"%{texto}%"
            params += [patron, patron, patron]
        if materia:
            if permitidas and materia not in permitidas:
                return {
                    "sin_acceso": True,
                    "motivo": (
                        f"La coleccion {NOMBRE_MATERIA.get(materia, materia)} no esta incluida "
                        f"en tu plan. Tus colecciones: {', '.join(NOMBRE_MATERIA.get(s, s) for s in ctx.siglas_permitidas) or 'ninguna'}."
                    ),
                }
            cond.append(_sql_materia(materia))
            params += _params_materia(materia)
        if tipo:
            # Igual que el filtro de tipo del buscador web (exacto, sin acentos):
            # "leyes" -> LEYES, "NOM" -> NOM completo.
            cond.append("UPPER(Tipo) = ?")
            params.append(tipo.upper())
        if permitidas:
            cond.append("(" + " OR ".join(_sql_materia(s) for s in permitidas) + ")")
            for s in permitidas:
                params += _params_materia(s)

    where = " AND ".join(cond) if cond else "1=1"
    filas = db.consultar(
        f"""
        SELECT TOP (10) ID, Nombrecorto, Nombrecompleto, STATUS, Tipo, Emisor,
               FPDOF, EntradaVigor, FechaAbroga
        FROM LEGISXXI
        WHERE {where}
        ORDER BY FPDOF DESC
        """,
        params,
    )
    total = db.escalar(f"SELECT COUNT(*) FROM LEGISXXI WHERE {where}", params)

    if not filas:
        return {
            "resultados": [],
            "total": 0,
            "nota": "No se encontro nada con ese criterio. Prueba con menos palabras o con el numero de la norma (por ejemplo NOM-035).",
        }

    db.registrar_consulta(
        ctx.employee_id,
        texto,
        NOMBRE_MATERIA.get(materia) if materia else None,
        int(total or 0),
    )

    return {
        "total": int(total or 0),
        "muestra_limitada": bool(total and total > len(filas)),
        "resultados": [
            {
                "id": f.get("ID"),
                "clave": f.get("Nombrecorto"),
                "nombre": _corta(f.get("Nombrecompleto") or f.get("Nombrecorto"), 180),
                "estatus": f.get("STATUS"),
                "tipo": f.get("Tipo"),
                "emisor": f.get("Emisor"),
                "publicacion": _iso(f.get("FPDOF")),
                "entrada_vigor": _iso(f.get("EntradaVigor")),
                "abrogacion": _iso(f.get("FechaAbroga")),
            }
            for f in filas
        ],
    }


def _resolver_materia(valor: Any) -> str | None:
    """Acepta sigla o nombre en español. Devuelve la sigla canonica."""
    texto = _texto(valor)
    if not texto:
        return None
    texto = texto.replace(" ", "").upper()
    if texto in MATERIAS_CONOCIDAS:
        return texto
    # "seguridad e higiene" -> SEGH
    clave = _texto(valor).upper()
    if clave in ALIAS_MATERIA:
        return ALIAS_MATERIA[clave]
    for alias, sigla in ALIAS_MATERIA.items():
        if clave.startswith(alias) or alias in clave:
            return sigla
    # Materia desconocida (ej. el modelo invento "AGUA"): NO se filtra por
    # coleccion — eso bloquearia la busqueda con "no tienes acceso". Se
    # devuelve None y la palabra queda como texto dentro de las colecciones
    # contratadas, que es lo que el usuario espera.
    return None


def _sql_materia(sigla: str) -> str:
    """Filtra por la columna Coleccion, que guarda las siglas separadas por comas.

    OJO: la columna `Materia` usa OTRA taxonomia (ENGA, SGIN, TPTE, LBRL...) que
    no coincide con las siglas de Planes.MateriasPermitidas. Verificado contra
    la BD: 0 filas contienen una sigla canonica en `Materia`, asi que filtrar
    ahi no sirve de nada. Solo `Coleccion` reparte las 23 colecciones.
    """
    return (
        "(Coleccion = ? OR Coleccion LIKE ? OR Coleccion LIKE ? OR Coleccion LIKE ?)"
    )


def _params_materia(sigla: str) -> list[str]:
    return [sigla, f"{sigla},%", f"%,{sigla},%", f"%,{sigla}"]


@herramienta(
    "detalle_ordenamiento",
    "Ficha completa de un ordenamiento: descripcion, emisor, fechas, reformas "
    "publicadas y estado vigente/abrogado. Usar despues de buscar_legislacion "
    "cuando el usuario pregunte en detalle sobre una norma concreta.",
    {
        "type": "object",
        "properties": {
            "ordenamiento_id": {"type": "integer", "description": "ID devuelto por buscar_legislacion."}
        },
        "required": ["ordenamiento_id"],
        "additionalProperties": False,
    },
    _ACCESO_LEGISLACION,
    sin_acceso="Tu empresa no tiene una suscripcion activa.",
)
async def detalle_ordenamiento(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    oid = args.get("ordenamiento_id")
    if not isinstance(oid, int):
        return {"error": "Falta ordenamiento_id."}

    permitidas = ctx.siglas_permitidas
    cond = ["ID = ?"]
    params: list[Any] = [oid]
    if permitidas:
        cond.append("(" + " OR ".join(_sql_materia(s) for s in permitidas) + ")")
        for s in permitidas:
            params += _params_materia(s)

    fila = db.consultar_uno(
        f"""
        SELECT ID, Nombrecorto, Nombrecompleto, Descripcion, Rubrica, Emisor, Tipo,
               STATUS, FPDOF, EntradaVigor, FechaAbroga, Fuente, Link, Observaciones
        FROM LEGISXXI WHERE {' AND '.join(cond)}
        """,
        params,
    )
    if fila is None:
        return {
            "error": "No se encontro ese ordenamiento o no pertenece a tus colecciones contratadas.",
        }

    # Modificaciones se enlaza al ordenamiento por NombreCorto, no por Id
    # (ver Core\Entities\Modificacion.cs).
    reformas = db.consultar(
        "SELECT TOP (15) NombreCorto, FechaMod, Boletin "
        "FROM Modificaciones WHERE NombreCorto = ? ORDER BY FechaMod DESC",
        (fila.get("Nombrecorto"),),
    )

    return {
        "id": fila.get("ID"),
        "clave": fila.get("Nombrecorto"),
        "nombre": _corta(fila.get("Nombrecompleto") or fila.get("Nombrecorto"), 300),
        "descripcion": _corta(fila.get("Descripcion"), 800),
        "emisor": fila.get("Emisor"),
        "tipo": fila.get("Tipo"),
        "estatus": fila.get("STATUS"),
        "publicacion": _iso(fila.get("FPDOF")),
        "entrada_vigor": _iso(fila.get("EntradaVigor")),
        "abrogacion": _iso(fila.get("FechaAbroga")),
        "fuente": _corta(fila.get("Fuente"), 200),
        "observaciones": _corta(fila.get("Observaciones"), 400),
        "reformas": [
            {"fecha": _iso(r.get("FechaMod")), "boletin": _corta(r.get("Boletin"), 300)}
            for r in reformas
        ],
    }


# ══════════════════════════════════════════════════════════════════════
#  AUTENTICADO — cualquier usuario con sesion
# ══════════════════════════════════════════════════════════════════════

@herramienta(
    "mi_suscripcion",
    "Estado de TODAS las suscripciones de la empresa del usuario: la vigente "
    "con dias restantes, las proximas a vencer y el historico de anteriores. "
    "Usar cuando pregunten por su plan, su vigencia, cuando vence, por que no "
    "ven algo, o cuando pregunten 'como va mi suscripcion': en ese caso resumir "
    "TODAS las suscripciones del campo 'suscripciones' (no solo la activa), "
    "indicando de cada una plan, estado, fechas y renovacion.",
    _ESQUEMA_VACIO,
    _TODOS_AUTENTICADOS,
    sin_acceso="Necesitas iniciar sesion para ver tu suscripcion.",
)
async def mi_suscripcion(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.empresa_id:
        return {"sin_suscripcion": True, "motivo": "Tu cuenta no esta asociada a una empresa."}

    filas = db.consultar(
        """
        SELECT p.Nombre, p.Precio, p.DuracionDias, s.FechaInicio, s.FechaFin,
               s.Activa, s.RenovacionAutomatica, s.MetodoPago
        FROM Suscripciones s JOIN Planes p ON p.Id = s.PlanId
        WHERE s.ClienteId = ?
        ORDER BY s.Activa DESC, s.FechaFin DESC
        """,
        (ctx.empresa_id,),
    )
    ahora = db_now()
    vigentes = [f for f in filas if f.get("Activa") and f.get("FechaFin") and f["FechaFin"] >= ahora]

    def _estado(f: dict[str, Any]) -> str:
        if not f.get("FechaFin"):
            return "indefinida"
        if f.get("Activa") and f["FechaFin"] >= ahora:
            dias = (f["FechaFin"] - ahora).days
            return "vigente" if dias > 30 else "vigente, por vencer"
        return "finalizada" if f.get("FechaFin") < ahora else "inactiva"

    return {
        "empresa": ctx.empresa_nombre,
        "total_suscripciones": len(filas),
        "suscripcion_activa": bool(vigentes),
        "plan_actual": vigentes[0].get("Nombre") if vigentes else None,
        "precio_mxn": float(vigentes[0].get("Precio") or 0) if vigentes else None,
        "inicio": _iso(vigentes[0].get("FechaInicio")) if vigentes else None,
        "fin": _iso(vigentes[0].get("FechaFin")) if vigentes else None,
        "dias_restantes": (vigentes[0]["FechaFin"] - ahora).days if vigentes else None,
        "renovacion_automatica": bool(vigentes[0].get("RenovacionAutomatica")) if vigentes else None,
        "colecciones_contratadas": [NOMBRE_MATERIA.get(s, s) for s in ctx.siglas_permitidas] or ["Todas"],
        # TODAS las suscripciones (la activa primero). La respuesta al usuario
        # debe cubrir esta lista completa, no unicamente la vigente.
        "suscripciones": [
            {
                "plan": f.get("Nombre"),
                "estado": _estado(f),
                "precio_mxn": float(f.get("Precio") or 0),
                "inicio": _iso(f.get("FechaInicio")),
                "fin": _iso(f.get("FechaFin")),
                "dias_restantes": (f["FechaFin"] - ahora).days if (f.get("Activa") and f.get("FechaFin") and f["FechaFin"] >= ahora) else None,
                "renovacion_automatica": bool(f.get("RenovacionAutomatica")),
                "metodo_pago": f.get("MetodoPago"),
            }
            for f in filas[:10]
        ],
    }


@herramienta(
    "mis_materias",
    "Colecciones de legislacion a las que el usuario tiene acceso, con el nombre "
    "de la coleccion y su sigla. Usar cuando pregunten que pueden ver o que "
    "materias tienen contratadas.",
    _ESQUEMA_VACIO,
    _TODOS_AUTENTICADOS,
    sin_acceso="Necesitas iniciar sesion.",
)
async def mis_materias(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.tiene_suscripcion_activa:
        return {
            "sin_acceso": True,
            "motivo": "Tu empresa no tiene una suscripcion activa, por eso no tienes colecciones.",
        }
    return {
        "acceso": "ilimitado" if not ctx.siglas_permitidas else "restringido",
        "colecciones": [
            {"sigla": s, "nombre": NOMBRE_MATERIA.get(s, s)} for s in ctx.siglas_permitidas
        ]
        or [{"sigla": "TODAS", "nombre": "Todas las colecciones"}],
    }


@herramienta(
    "mis_alertas",
    "Alertas y notificaciones del usuario. Usar cuando pregunten que alertas "
    "tienen, que notificaciones les faltan o que hay pendiente.",
    {
        "type": "object",
        "properties": {
            "solo_no_leidas": {
                "type": "boolean",
                "description": "true para listar solo las no leidas. Por defecto true.",
            }
        },
        "additionalProperties": False,
    },
    _TODOS_AUTENTICADOS,
    sin_acceso="Necesitas iniciar sesion para ver tus alertas.",
)
async def mis_alertas(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    solo_no_leidas = args.get("solo_no_leidas", True)
    cond = "WHERE UsuarioId = ?"
    params: list[Any] = [ctx.employee_id]
    if solo_no_leidas:
        cond += " AND Leido = 0"

    filas = db.consultar(
        f"""
        SELECT TOP (10) Id, TipoAlerta, Mensaje, OrdenamientoId, FechaCreacion, Leido
        FROM Alertas {cond} ORDER BY FechaCreacion DESC
        """,
        params,
    )
    total_no_leidas = db.escalar(
        "SELECT COUNT(*) FROM Alertas WHERE UsuarioId = ? AND Leido = 0", (ctx.employee_id,)
    )
    return {
        "total_no_leidas": int(total_no_leidas or 0),
        "alertas": [
            {
                "tipo": _TIPO_ALERTA.get(f.get("TipoAlerta"), "General"),
                "mensaje": _corta(f.get("Mensaje"), 200),
                "ordenamiento_id": f.get("OrdenamientoId"),
                "fecha": _iso(f.get("FechaCreacion")),
            }
            for f in filas
        ],
    }


@herramienta(
    "mi_empresa",
    "Datos de la empresa del usuario y, si es titular, el numero de usuarios que "
    "tiene. Usar cuando pregunten por su empresa, su RFC, su direccion o quantos "
    "usuarios tiene.",
    _ESQUEMA_VACIO,
    _TODOS_AUTENTICADOS,
    sin_acceso="Necesitas iniciar sesion.",
)
async def mi_empresa(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.empresa_id:
        return {"sin_empresa": True, "motivo": "Tu cuenta no esta asociada a una empresa."}

    cliente = db.consultar_uno(
        """
        SELECT NombreEmpresa, RFC, Direccion, Ciudad, Municipio, Estado, CP,
               Telefono, Email, Contacto, Puesto, Activo, FechaRegistro
        FROM Clientes WHERE Id = ?
        """,
        (ctx.empresa_id,),
    )
    resultado: dict[str, Any] = {"empresa": cliente}
    if ctx.es_titular_empresa:
        resultado["numero_usuarios"] = db.escalar(
            "SELECT COUNT(*) FROM Employees WHERE ClienteId = ? AND Activated = 1",
            (ctx.empresa_id,),
        )
    else:
        resultado["nota"] = "La administracion de los usuarios de la empresa la lleva el titular."
    return resultado


@herramienta(
    "mi_perfil",
    "Datos personales del usuario: nombre, correo, puesto, empresa y si tiene "
    "activado el doble factor. Usar cuando pregunten por sus datos o como cambiar "
    "su contrasena o su telefono.",
    _ESQUEMA_VACIO,
    _TODOS_AUTENTICADOS,
    sin_acceso="Necesitas iniciar sesion.",
)
async def mi_perfil(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    fila = db.consultar_uno(
        """
        SELECT FirstName, LastName, Email, Title, HomePhone, TwoFactorEnabled, Activated
        FROM Employees WHERE EmployeeID = ?
        """,
        (ctx.employee_id,),
    )
    if fila is None:
        return {"error": "No se encontro tu usuario."}
    return {
        "nombre": f"{_texto(fila.get('FirstName'))} {_texto(fila.get('LastName'))}".strip(),
        "email": fila.get("Email"),
        "puesto": fila.get("Title"),
        "telefono": fila.get("HomePhone"),
        "doble_factor": bool(fila.get("TwoFactorEnabled")),
        "empresa": ctx.empresa_nombre,
        "rol": ctx.rol.value,
    }


# ══════════════════════════════════════════════════════════════════════
#  ADMIN
# ══════════════════════════════════════════════════════════════════════

_SOLO_ADMIN = frozenset({Rol.ADMIN})


@herramienta(
    "metricas_negocio",
    "Indicadores del negocio: ingreso mensual (suma de las suscripciones "
    "vigentes), por vencer, empresas activas y sin uso, consultas del mes y "
    "solicitudes pendientes. Usar cuando pregunten como va el negocio, cuanto "
    "entra o el estado general. Al responder di 'ingreso mensual', NUNCA la "
    "sigla 'MRR'.",
    _ESQUEMA_VACIO,
    _SOLO_ADMIN,
    sin_acceso="Esta informacion es solo para administradores de la plataforma.",
)
async def metricas_negocio(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    ahora = db_now()
    en_30 = _sumar_dias(ahora, 30)
    mes = f"{ahora.year}-{ahora.month:02d}"

    return {
        "ingreso_mensual_mxn": float(db.escalar(
            "SELECT ISNULL(SUM(p.Precio), 0) FROM Suscripciones s "
            "JOIN Planes p ON p.Id = s.PlanId "
            "WHERE s.Activa = 1 AND s.FechaFin >= ?", (ahora,)) or 0),
        "suscripciones_vigentes": db.escalar(
            "SELECT COUNT(*) FROM Suscripciones WHERE Activa = 1 AND FechaFin >= ?", (ahora,)),
        "suscripciones_por_vencer": db.escalar(
            "SELECT COUNT(*) FROM Suscripciones "
            "WHERE Activa = 1 AND FechaFin >= ? AND FechaFin <= ?", (ahora, en_30)),
        "ingreso_en_riesgo_mxn": float(db.escalar(
            "SELECT ISNULL(SUM(p.Precio), 0) FROM Suscripciones s "
            "JOIN Planes p ON p.Id = s.PlanId "
            "WHERE s.Activa = 1 AND s.FechaFin >= ? AND s.FechaFin <= ?", (ahora, en_30)) or 0),
        "empresas_activas": db.escalar("SELECT COUNT(*) FROM Clientes WHERE Activo = 1"),
        "solicitudes_pendientes": db.escalar(
            "SELECT COUNT(*) FROM Solicitudes WHERE Estatus = 'Pendiente'"),
        "consultas_mes_actual": db.escalar(
            "SELECT COUNT(*) FROM Consultas WHERE FORMAT(Fecha, 'yyyy-MM') = ?", (mes,)),
        "ordenamientos_catalogo": db.escalar("SELECT COUNT(*) FROM LEGISXXI"),
    }


def _sumar_dias(fecha: Any, dias: int) -> Any:
    import datetime

    return fecha + datetime.timedelta(days=dias)


@herramienta(
    "solicitudes_pendientes",
    "Solicitudes de suscripcion en estado Pendiente, con empresa, estado, plan "
    "solicitado y fecha. Usar cuando pregunten por solicitudes, altas pendientes "
    "o que hay por aprobar.",
    {
        "type": "object",
        "properties": {
            "limite": {"type": "integer", "description": "Maximo de resultados (1-25). Default 10."}
        },
        "additionalProperties": False,
    },
    _SOLO_ADMIN,
    sin_acceso="Solo los administradores pueden ver las solicitudes.",
)
async def solicitudes_pendientes(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    limite = _entero(args.get("limite"), 10, 1, 25)
    filas = db.consultar(
        f"""
        SELECT TOP ({limite}) s.Id, s.NombreEmpresa, s.Email, s.Estado, s.PlanId,
               p.Nombre AS [Plan], s.Estatus, s.PagoConfirmado, s.FechaSolicitud
        FROM Solicitudes s LEFT JOIN Planes p ON p.Id = s.PlanId
        WHERE s.Estatus = 'Pendiente'
        ORDER BY s.FechaSolicitud ASC
        """
    )
    return {
        "total": db.escalar("SELECT COUNT(*) FROM Solicitudes WHERE Estatus = 'Pendiente'"),
        "solicitudes": [
            {
                "id": f.get("Id"),
                "empresa": f.get("NombreEmpresa"),
                "email": f.get("Email"),
                "estado": f.get("Estado"),
                "plan": f.get("Plan"),
                "pago_confirmado": bool(f.get("PagoConfirmado")),
                "fecha": _iso(f.get("FechaSolicitud")),
            }
            for f in filas
        ],
    }


@herramienta(
    "listar_clientes",
    "Empresas registradas con su plan, estado de la suscripcion y vigencia. Usar "
    "cuando pregunten por los clientes, las empresas, que planes tienen o cuantas "
    "hay.",
    {
        "type": "object",
        "properties": {
            "limite": {"type": "integer", "description": "Maximo de resultados (1-50). Default 20."},
            "solo_activas": {"type": "boolean", "description": "true para omitir empresas dadas de baja."},
        },
        "additionalProperties": False,
    },
    _SOLO_ADMIN,
    sin_acceso="Solo los administradores pueden ver la cartera de clientes.",
)
async def listar_clientes(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    limite = _entero(args.get("limite"), 20, 1, 50)
    cond = "WHERE c.Activo = 1" if args.get("solo_activas", True) else ""
    filas = db.consultar(
        f"""
        SELECT TOP ({limite}) c.Id, c.NombreEmpresa, c.RFC, c.Estado, c.Activo,
               (SELECT TOP (1) p.Nombre FROM Suscripciones s JOIN Planes p ON p.Id = s.PlanId
                WHERE s.ClienteId = c.Id AND s.Activa = 1 ORDER BY s.FechaFin DESC) AS [Plan],
               (SELECT TOP (1) s.FechaFin FROM Suscripciones s
                WHERE s.ClienteId = c.Id AND s.Activa = 1 ORDER BY s.FechaFin DESC) AS Fin,
               (SELECT COUNT(*) FROM Employees e WHERE e.ClienteId = c.Id AND e.Activated = 1) AS Usuarios
        FROM Clientes c {cond}
        ORDER BY c.NombreEmpresa
        """
    )
    return {
        "total_empresas_activas": db.escalar("SELECT COUNT(*) FROM Clientes WHERE Activo = 1"),
        "empresas": [
            {
                "id": f.get("Id"),
                "empresa": f.get("NombreEmpresa"),
                "rfc": f.get("RFC"),
                "estado": f.get("Estado"),
                "plan": f.get("Plan"),
                "vence": _iso(f.get("Fin")),
                "usuarios": f.get("Usuarios"),
            }
            for f in filas
        ],
    }


@herramienta(
    "usuarios_registrados",
    "Conteo de usuarios de la plataforma por rol (administradores, titulares de "
    "empresa y usuarios de empresa) mas el numero de visitantes anonimos "
    "conectados en los ultimos 30 minutos (tiempo real). Usar cuando pregunten "
    "cuantos usuarios hay, quien esta registrado o cuantos visitantes hay.",
    {
        "type": "object",
        "properties": {
            "limite": {"type": "integer", "description": "Maximo de listados (1-50). Default 20."}
        },
        "additionalProperties": False,
    },
    _SOLO_ADMIN,
    sin_acceso="Solo los administradores pueden ver el padron de usuarios.",
)
async def usuarios_registrados(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    limite = _entero(args.get("limite"), 20, 1, 50)
    # Criterios alineados con roles.py (resolucion canonica de roles):
    #   admin            := UserLevel = -1 (o correo de admin especial)
    #   titular          := existe Clientes con Clientes.EmployeeId = ese empleado
    #   usuario_empresa  := UserLevel = 4 Y pertenece a una empresa (ClienteId asignado);
    #                       el personal interno de Norlex tambien tiene UserLevel 4 pero
    #                       sin empresa, y NO cuenta como usuario de empresa.
    # Visitantes anonimos EN TIEMPO REAL: IPs con latido en los ultimos 90 s.
    # Cada pagina abierta emite un ping cada 25 s (site.js -> /api/presencia/ping);
    # al cerrar la pestana dejan de latir y salen del conteo.
    try:
        visitantes_conectados = int(db.escalar(
            "SELECT COUNT(*) FROM dbo.PresenciaVisitantes "
            "WHERE UltimaPing >= DATEADD(second, -90, GETDATE())"
        ) or 0)
    except Exception:
        visitantes_conectados = 0
    return {
        "administradores": db.escalar("SELECT COUNT(*) FROM Employees WHERE UserLevel = -1"),
        "titulares": db.escalar("SELECT COUNT(*) FROM Clientes WHERE EmployeeId IS NOT NULL"),
        "usuarios_empresa": db.escalar(
            "SELECT COUNT(*) FROM Employees WHERE UserLevel = 4 AND ClienteId IS NOT NULL"
        ),
        "visitantes_conectados": visitantes_conectados,
        "muestra": [
            {
                "nombre": f"{_texto(f.get('FirstName'))} {_texto(f.get('LastName'))}".strip(),
                "email": f.get("Email"),
                "nivel": _NIVEL.get(f.get("UserLevel"), "Interno"),
                "activo": bool(f.get("Activated")),
                "empresa": f.get("Empresa"),
            }
            for f in db.consultar(
                f"""
                SELECT TOP ({limite}) e.FirstName, e.LastName, e.Email, e.UserLevel, e.Activated,
                       c.NombreEmpresa AS Empresa
                FROM Employees e LEFT JOIN Clientes c ON c.Id = e.ClienteId
                ORDER BY e.EmployeeID
                """
            )
        ],
    }


@herramienta(
    "empresas_sin_uso",
    "Empresas con suscripcion activa que no registran consultas en 30 dias. "
    "Usar cuando pregunten por empresas dormidas, sin uso o en riesgo de cancelar.",
    {
        "type": "object",
        "properties": {
            "limite": {"type": "integer", "description": "Maximo de resultados (1-50). Default 15."}
        },
        "additionalProperties": False,
    },
    _SOLO_ADMIN,
    sin_acceso="Solo los administradores pueden ver la senal de uso.",
)
async def empresas_sin_uso(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    limite = _entero(args.get("limite"), 15, 1, 50)
    filas = db.consultar(
        f"""
        SELECT TOP ({limite}) c.Id, c.NombreEmpresa, c.Estado,
               (SELECT TOP (1) p.Nombre FROM Suscripciones s JOIN Planes p ON p.Id = s.PlanId
                WHERE s.ClienteId = c.Id AND s.Activa = 1 ORDER BY s.FechaFin DESC) AS [Plan],
               (SELECT TOP (1) s.FechaFin FROM Suscripciones s
                WHERE s.ClienteId = c.Id AND s.Activa = 1 ORDER BY s.FechaFin DESC) AS Fin
        FROM Clientes c
        WHERE c.Activo = 1
          AND EXISTS (SELECT 1 FROM Suscripciones s WHERE s.ClienteId = c.Id AND s.Activa = 1)
          AND NOT EXISTS (
              SELECT 1 FROM Consultas co
              WHERE co.UsuarioId IN (SELECT EmployeeID FROM Employees WHERE ClienteId = c.Id)
                AND co.Fecha >= DATEADD(DAY, -30, GETDATE())
          )
        ORDER BY c.NombreEmpresa
        """
    )
    return {
        "total": db.escalar(
            """
            SELECT COUNT(*) FROM Clientes c
            WHERE c.Activo = 1
              AND EXISTS (SELECT 1 FROM Suscripciones s WHERE s.ClienteId = c.Id AND s.Activa = 1)
              AND NOT EXISTS (
                  SELECT 1 FROM Consultas co
                  WHERE co.UsuarioId IN (SELECT EmployeeID FROM Employees WHERE ClienteId = c.Id)
                    AND co.Fecha >= DATEADD(DAY, -30, GETDATE())
              )
            """
        ),
        "empresas": [
            {
                "id": f.get("Id"),
                "empresa": f.get("NombreEmpresa"),
                "estado": f.get("Estado"),
                "plan": f.get("Plan"),
                "vence": _iso(f.get("Fin")),
            }
            for f in filas
        ],
        "nota": "Sin consultas registradas en los ultimos 30 dias.",
    }


@herramienta(
    "actividad_sospechosa",
    "Eventos marcados como sospechosos en la bitacora de auditoria. Usar cuando "
    "pregunten por intentos de acceso no autorizados o actividad anomala.",
    {
        "type": "object",
        "properties": {
            "limite": {"type": "integer", "description": "Maximo de resultados (1-50). Default 15."}
        },
        "additionalProperties": False,
    },
    _SOLO_ADMIN,
    sin_acceso="Solo los administradores pueden ver la bitacora de auditoria.",
)
async def actividad_sospechosa(ctx: Contexto, args: dict[str, Any]) -> dict[str, Any]:
    limite = _entero(args.get("limite"), 15, 1, 50)
    filas = db.consultar(
        f"""
        SELECT TOP ({limite}) Usuario, Email, Accion, Entidad, Detalle, Ip, Fecha
        FROM Auditoria WHERE Sospechosa = 1 ORDER BY Fecha DESC
        """
    )
    return {
        "total": db.escalar("SELECT COUNT(*) FROM Auditoria WHERE Sospechosa = 1"),
        "eventos": [
            {
                "usuario": f.get("Usuario"),
                "email": f.get("Email"),
                "accion": f.get("Accion"),
                "entidad": f.get("Entidad"),
                "detalle": _corta(f.get("Detalle"), 150),
                "ip": f.get("Ip"),
                "fecha": _iso(f.get("Fecha")),
            }
            for f in filas
        ],
    }


# ══════════════════════════════════════════════════════════════════════
#  Utilidades
# ══════════════════════════════════════════════════════════════════════

_TIPO_ALERTA = {
    1: "Reforma", 2: "Nueva publicacion", 3: "Entrada en vigor",
    4: "Suscripcion", 5: "General", 6: "Solicitud",
}

_NIVEL = {-1: "Administrador", 3: "Titular de empresa", 4: "Usuario de empresa"}


def _entero(valor: Any, defecto: int, minimo: int, maximo: int) -> int:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return defecto
    return max(minimo, min(maximo, numero))
