"""
Configuración centralizada vía variables de entorno.
Usa python-dotenv para cargar el archivo .env en desarrollo local.
"""
from functools import lru_cache
from typing import Any
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── WooCommerce ───────────────────────────────────────────────
    WOO_URL:    str = ""   # https://tienda.ejemplo.cl
    WOO_KEY:    str = ""   # ck_xxxxx
    WOO_SECRET: str = ""   # cs_xxxxx

    # ── Impresora Zebra ───────────────────────────────────────────
    ZEBRA_IP:   str = "192.168.1.100"
    ZEBRA_PORT: int = 9100
    ZEBRA_DPI:  int = 300   # 203 o 300

    # ── Aplicación ────────────────────────────────────────────────
    APP_HOST:   str = "0.0.0.0"
    APP_PORT:   int = 8000
    DEBUG:      bool = False
    DB_PATH:    str = "pedidos.db"

    # ── Azure (no requerido en local) ─────────────────────────────
    AZURE_REGISTRY:   str = ""
    AZURE_IMAGE_NAME: str = "upperapp-logistics"

    # ── Mercado Libre ─────────────────────────────────────────────
    # Obtenidos en https://developers.mercadolibre.cl/es_ar/registra-tu-aplicacion
    MELI_APP_ID:       str = ""   # App ID (Client ID) de tu aplicación MeLi
    MELI_CLIENT_SECRET: str = "" # Secret Key de tu aplicación MeLi
    # URI registrada en el panel de MeLi → debe coincidir exactamente.
    # Ejemplo: https://tu-dominio.azure.com/api/meli/callback
    MELI_REDIRECT_URI: str = ""

    # ── Auth0 / JWT ───────────────────────────────────────────────
    AUTH0_DOMAIN: str = ""
    AUTH0_CLIENT_ID: str = ""
    AUTH0_API_AUDIENCE: str = ""
    AUTH0_ISSUER: str = ""
    AUTH0_ALGORITHMS: list[str] = ["RS256"]

    @field_validator("AUTH0_ALGORITHMS", mode="before")
    @classmethod
    def _parse_auth0_algorithms(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [algorithm.strip() for algorithm in value.split(",") if algorithm.strip()]
        if isinstance(value, list):
            return [str(algorithm).strip() for algorithm in value if str(algorithm).strip()]
        return ["RS256"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Alias conveniente para importaciones
settings = get_settings()
