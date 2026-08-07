"""
Configuración centralizada vía variables de entorno.
Usa python-dotenv para cargar el archivo .env en desarrollo local.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Directorio del repositorio (donde vive este archivo). Sirve para construir
# rutas por defecto robustas frente al working directory desde el que se ejecute.
_BASE_DIR = Path(__file__).resolve().parent


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

    # Zebra de respaldo (failover de impresión). Si ZEBRA_BACKUP_IP queda
    # vacío el failover está deshabilitado y el comportamiento es idéntico
    # al de una sola Zebra: un único intento contra ZEBRA_IP:ZEBRA_PORT.
    ZEBRA_BACKUP_IP:   str = ""
    ZEBRA_BACKUP_PORT: int = 9101
    # Timeout de conexión por intento, en segundos. Con failover activo el
    # peor caso es el doble (un intento por Zebra).
    ZEBRA_TIMEOUT: float = 3.0

    @property
    def has_zebra_backup(self) -> bool:
        """True si hay una Zebra de respaldo configurada."""
        return bool(self.ZEBRA_BACKUP_IP.strip())

    # ── Aplicación ────────────────────────────────────────────────
    APP_HOST:   str = "0.0.0.0"
    APP_PORT:   int = 8000
    DEBUG:      bool = False
    DB_PATH:    str = "pedidos.db"
    # Catálogo de packs/SKUs editable en runtime (PUT /api/skus).
    # En producción debe apuntar al volumen persistente, p.ej. /data/skus.json.
    # El default apunta al catálogo "semilla" versionado en el repo.
    SKUS_PATH:  str = str(_BASE_DIR / "data" / "skus.json")
    SECRET_KEY: str
    JWT_EXPIRE_MINUTES: int = 720
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str
    ALLOWED_ORIGINS: list[str] = ["http://localhost:8000"]

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Alias conveniente para importaciones
settings = get_settings()
