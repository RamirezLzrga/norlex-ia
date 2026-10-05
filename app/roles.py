"""Resolucion del rol, replicando la logica de Autorizacion de la app .NET.

Reglas canónicas (identicas a las que se usan en Login.cshtml.cs,
UsuarioService.cs, ClientesController.cs y AlertaService.cs):

    es_admin    := Employees.UserLevel = -1  OR  Email = admin@norlexinternacional.com
    es_empleado := Employees.UserLevel = 4                      (usuario de empresa)
    es_titular  := existe Clientes con Clientes.EmployeeId = id  (titular de empresa)

Ojo: el claim `UserLevel` de la cookie de sesion vale 1 o 2 (sintetico) y NO
coincide con Employees.UserLevel (-1 / 3 / 4). Por eso aqui siempre se relee
la base de datos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import db

log = logging.getLogger(__name__)

ADMIN_EMAIL = "admin@norlexinternacional.com"

NIVEL_ADMIN = -1
NIVEL_TITULAR = 3
NIVEL_EMPLEADO = 4


class Rol(str, Enum):
    VISITANTE = "visitante"
    USUARIO_EMPRESA = "usuario_empresa"
    TITULAR = "titular"
    ADMIN = "admin"


#: Etiquetas que el modelo puede usar al hablarle al usuario.
DESCRIPCION_ROL: dict[Rol, str] = {
    Rol.VISITANTE: "visitante sin iniciar sesion",
    Rol.USUARIO_EMPRESA: "usuario de empresa",
    Rol.TITULAR: "titular de empresa",
    Rol.ADMIN: "administrador de la plataforma",
}


@dataclass(slots=True)
class Contexto:
    """Todo lo que el agente necesita saber de quien pregunta."""

    rol: Rol = Rol.VISITANTE
    employee_id: int | None = None
    nombre: str = "Visitante"
    email: str | None = None
    empresa_id: int | None = None
    empresa_nombre: str | None = None
    es_titular_empresa: bool = False
    #: Siglas de colecciones permitidas. Lista vacia = SIN RESTRICCION
    #: (admin/interno, o plan con "TODAS"). None = cliente sin suscripcion.
    siglas_permitidas: list[str] = field(default_factory=list)
    tiene_suscripcion_activa: bool = False
    es_cliente: bool = False
    #: Errores de BD al armar el contexto; el agente los recibe como aviso.
    avisos: list[str] = field(default_factory=list)

    @property
    def autenticado(self) -> bool:
        return self.employee_id is not None

    @property
    def nombre_para_mensaje(self) -> str:
        if not self.autenticado:
            return ""
        return self.nombre.split()[0] if self.nombre else ""


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def es_admin(empleado: dict[str, Any]) -> bool:
    return bool(
        (empleado.get("UserLevel") == NIVEL_ADMIN)
        or ((_texto(empleado.get("Email")) or "").lower() == ADMIN_EMAIL)
    )


def resolver_contexto(employee_id: int | None) -> Contexto:
    """Arma el Contexto desde la BD. Nunca lanza: degrada a visitante."""
    if employee_id is None:
        return Contexto()

    ctx = Contexto(employee_id=employee_id)

    empleado = db.consultar_uno(
        """
        SELECT EmployeeID, FirstName, LastName, Username, Email, Title,
               UserLevel, Activated, ClienteId
        FROM Employees
        WHERE EmployeeID = ?
        """,
        (employee_id,),
    )

    if empleado is None:
        ctx.avisos.append(f"El usuario {employee_id} no existe en Employees.")
        return ctx

    if not empleado.get("Activated"):
        ctx.avisos.append("La cuenta del usuario esta desactivada.")
        return ctx

    ctx.nombre = (
        " ".join(
            parte
            for parte in (_texto(empleado.get("FirstName")), _texto(empleado.get("LastName")))
            if parte
        )
        or _texto(empleado.get("Username"))
        or _texto(empleado.get("Email"))
        or "Usuario"
    )
    ctx.email = _texto(empleado.get("Email"))

    if es_admin(empleado):
        ctx.rol = Rol.ADMIN
        ctx.tiene_suscripcion_activa = True
        ctx.siglas_permitidas = []  # admin: sin restriccion
        return ctx

    # ── Resolver la empresa a la que pertenece (mismo criterio que
    #    PermisoMateriaService.ObtenerAccesoColeccionesAsync) ──────────
    cliente = None
    empresa_id = empleado.get("ClienteId")
    if empresa_id is not None:
        cliente = db.consultar_uno(
            "SELECT Id, EmployeeId, NombreEmpresa, Activo FROM Clientes WHERE Id = ?",
            (empresa_id,),
        )
    if cliente is None:
        # Ruta B: es el titular (Clientes.EmployeeId == su EmployeeID)
        cliente = db.consultar_uno(
            "SELECT Id, EmployeeId, NombreEmpresa, Activo FROM Clientes WHERE EmployeeId = ?",
            (employee_id,),
        )

    if cliente is None:
        # Internos no admin: sin empresa -> sin restricciones, como en .NET
        ctx.rol = _rol_interno(empleado)
        ctx.tiene_suscripcion_activa = True
        ctx.siglas_permitidas = _materias_manuales(employee_id)
        return ctx

    ctx.es_cliente = True
    ctx.empresa_id = cliente.get("Id")
    ctx.empresa_nombre = _texto(cliente.get("NombreEmpresa"))
    ctx.es_titular_empresa = cliente.get("EmployeeId") == employee_id
    ctx.rol = Rol.TITULAR if ctx.es_titular_empresa else Rol.USUARIO_EMPRESA

    _cargar_acceso(ctx, employee_id)
    return ctx


def _rol_interno(empleado: dict[str, Any]) -> Rol:
    """Staff de Norlex que no es admin: lo tratamos como admin operativo."""
    return Rol.ADMIN


def _materias_manuales(usuario_id: int) -> list[str]:
    filas = db.consultar(
        "SELECT MateriaSiglas FROM PermisosMateria WHERE UsuarioId = ?",
        (usuario_id,),
    )
    return sorted({(f.get("MateriaSiglas") or "").strip().upper() for f in filas if f.get("MateriaSiglas")})


def _cargar_acceso(ctx: Contexto, usuario_id: int) -> None:
    """Replica paso 3-5 de ObtenerAccesoColeccionesAsync."""
    suscripciones = db.consultar(
        """
        SELECT p.MateriasPermitidas
        FROM Suscripciones s
        JOIN Planes p ON p.Id = s.PlanId
        WHERE s.ClienteId = ?
          AND s.Activa = 1
          AND s.FechaInicio <= ?
          AND s.FechaFin    >= ?
        """,
        (ctx.empresa_id, db_now(), db_now()),
    )

    manuales = _materias_manuales(usuario_id)

    if not suscripciones and not manuales:
        ctx.tiene_suscripcion_activa = False
        ctx.siglas_permitidas = []
        return

    ctx.tiene_suscripcion_activa = True
    siglas: set[str] = set()

    for fila in suscripciones:
        for sigla in _parsear_materias(fila.get("MateriasPermitidas")):
            if sigla == "TODAS":
                # Plan con acceso total: lista vacia significa SIN RESTRICCION
                ctx.siglas_permitidas = []
                return
            if sigla in MATERIAS_CONOCIDAS or len(sigla) <= 20:
                siglas.add(sigla)

    siglas.update(manuales)
    ctx.siglas_permitidas = sorted(siglas)


def _parsear_materias(valor: Any) -> list[str]:
    """MateriasPermitidas es un JSON array; algunos planes traen CSV."""
    if not valor:
        return []
    texto = str(valor).strip()
    if texto.startswith("["):
        import json

        try:
            datos = json.loads(texto)
            if isinstance(datos, list):
                return [n.upper() for n in (str(d).strip().upper() for d in datos) if n]
        except json.JSONDecodeError:
            pass
    return [p.strip().upper() for p in texto.split(",") if p.strip()]


def db_now() -> Any:
    """Como en .NET, las vigencias se comparan contra la hora local del servidor."""
    import datetime

    return datetime.datetime.now()


#: Siglas canonicas (PermisoMateriaService.MapaMaterias).
MATERIAS_CONOCIDAS: frozenset[str] = frozenset(
    {
        "TODAS", "ECOL", "SEGH", "SALU", "TRAN", "ENER", "LABO", "INDU", "AGRO",
        "FISC", "COME", "SEPU", "NOTA", "ELEC", "DEHU", "CTES", "CLYR", "EDUC",
        "TCOM", "AIPD", "TURI", "ADMP", "ENTIDADES", "MUNICIPIOS",
    }
)

#: Alias que el usuario puede decir en español -> sigla.
ALIAS_MATERIA: dict[str, str] = {
    "AMBIENTAL": "ECOL", "AMBIENTE": "ECOL", "MEDIO AMBIENTE": "ECOL",
    "SEGURIDAD E HIGIENE": "SEGH", "SEGURIDAD": "SEGH", "HIGIENE": "SEGH",
    "SALUD": "SALU", "TRANSPORTE": "TRAN", "ENERGIA": "ENER", "LABORAL": "LABO",
    "INDUSTRIAL": "INDU", "AGROPECUARIA": "AGRO", "FISCAL": "FISC",
    "COMERCIO": "COME", "SEGURIDAD PUBLICA": "SEPU",
    "SEGURIDAD PUBLICA NACIONAL": "SEPU",
    "NOTARIAL": "NOTA", "CORREDURIA": "NOTA",
    "NOTARIAL CORREDURIA PUBLICA": "NOTA",
    "ELECTORAL": "ELEC", "DERECHOS HUMANOS": "DEHU",
    "CONSTRUCCIONES": "CTES", "CODIGOS": "CLYR",
    "CODIGOS LEYES Y REGLAMENTOS": "CLYR",
    "LEYES Y REGLAMENTOS": "CLYR",
    "EDUCACION": "EDUC", "TELECOMUNICACIONES": "TCOM",
    "PROTECCION DE DATOS": "AIPD", "PROTECCION DE DATOS PERSONALES": "AIPD",
    "INFORMACION PUBLICA": "AIPD", "TURISMO": "TURI",
    "ADMINISTRACION PUBLICA": "ADMP",
    "ENTIDADES FEDERATIVAS": "ENTIDADES", "EFED": "ENTIDADES",
    "MUNICIPIOS": "MUNICIPIOS", "MUNI": "MUNICIPIOS",
}

#: Sigla -> nombre legible.
NOMBRE_MATERIA: dict[str, str] = {
    "ECOL": "Medio Ambiente", "SEGH": "Seguridad e Higiene", "SALU": "Salud",
    "TRAN": "Transporte", "ENER": "Energía", "LABO": "Laboral",
    "INDU": "Industrial", "AGRO": "Agropecuaria", "FISC": "Fiscal",
    "COME": "Comercio", "SEPU": "Seguridad Pública", "NOTA": "Notarial y Correduría",
    "ELEC": "Electoral", "DEHU": "Derechos Humanos", "CTES": "Construcciones",
    "CLYR": "Códigos, Leyes y Reglamentos", "EDUC": "Educación",
    "TCOM": "Telecomunicaciones", "AIPD": "Protección de Datos Personales",
    "TURI": "Turismo", "ADMP": "Administración Pública",
    "ENTIDADES": "Entidades Federativas", "MUNICIPIOS": "Municipios",
    "TODAS": "Todas las colecciones",
}
