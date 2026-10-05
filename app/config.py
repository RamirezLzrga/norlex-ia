"""Configuracion del servicio, leida de variables de entorno / .env."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(nombre: str, defecto: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return defecto
    return valor.strip().lower() in {"1", "true", "yes", "si", "on"}


class Config:
    # ── Groq ──
    api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    modelo: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

    # ── SQL Server ──
    db_server: str = os.getenv("NORLEX_DB_SERVER", r".\SQLEXPRESS")
    db_name: str = os.getenv("NORLEX_DB_NAME", "BD_Fed")
    db_driver: str = os.getenv("NORLEX_DB_DRIVER", "ODBC Driver 18 for SQL Server")
    db_user: str = os.getenv("NORLEX_DB_USER", "").strip()
    db_password: str = os.getenv("NORLEX_DB_PASSWORD", "")

    # ── Seguridad ──
    shared_secret: str = os.getenv("NORLEX_IA_SHARED_SECRET", "").strip()
    signature_ttl: int = int(os.getenv("NORLEX_IA_SIGNATURE_TTL", "300"))
    allow_anonymous: bool = _bool("NORLEX_IA_ALLOW_ANONYMOUS", True)
    #: Activa /api/demo/* y la pagina de pruebas. NUNCA en produccion:
    #: permite pedir cualquier rol sin firma HMAC.
    demo: bool = _bool("NORLEX_IA_DEMO", False)

    # ── Servidor ──
    host: str = os.getenv("NORLEX_IA_HOST", "127.0.0.1")
    port: int = int(os.getenv("NORLEX_IA_PORT", "8099"))
    cors_origins: list[str] = [
        o.strip()
        for o in os.getenv(
            "NORLEX_IA_CORS_ORIGINS",
            "http://localhost:5129,http://localhost:5130,http://localhost:5001",
        ).split(",")
        if o.strip()
    ]

    # ── Limites ──
    max_mensaje: int = int(os.getenv("NORLEX_IA_MAX_MENSAJE", "500"))
    max_historial: int = int(os.getenv("NORLEX_IA_MAX_HISTORIAL", "8"))
    max_iteraciones: int = int(os.getenv("NORLEX_IA_MAX_ITERACIONES", "6"))

    @property
    def connection_string(self) -> str:
        partes = [
            f"DRIVER={{{self.db_driver}}}",
            f"SERVER={self.db_server}",
            f"DATABASE={self.db_name}",
            "TrustServerCertificate=yes",
            "Encrypt=no",
        ]
        if self.db_user:
            partes.append(f"UID={self.db_user}")
            partes.append(f"PWD={self.db_password}")
        else:
            partes.append("Trusted_Connection=yes")
        return ";".join(partes)

    @property
    def ia_habilitada(self) -> bool:
        return bool(self.api_key)

    @property
    def advertencias(self) -> list[str]:
        """Problemas de configuracion que hay que resolver antes de produccion."""
        avisos: list[str] = []
        if not self.api_key:
            avisos.append("GROQ_API_KEY vacia: el chatbot responde sin IA.")
        if not self.shared_secret:
            avisos.append("NORLEX_IA_SHARED_SECRET vacia: se rechaza toda peticion de .NET.")
        if self.demo:
            avisos.append("NORLEX_IA_DEMO activo: cualquiera puede pedir cualquier rol sin firma.")
        if not self.db_user:
            avisos.append(
                "La BD usa Windows Auth: el servicio debe correr en la misma maquina "
                "que SQL Server. Define NORLEX_DB_USER y NORLEX_DB_PASSWORD para moverlo."
            )
        return avisos


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
