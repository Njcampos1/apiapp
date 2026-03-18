"""
Capa de persistencia SQLite usando aiosqlite para operaciones asíncronas.
Guarda estados intermedios de pedidos para sobrevivir reinicios del servidor.
"""
import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TypedDict

from config import settings
from models.order import NormalizedOrder, OrderStatus

logger = logging.getLogger(__name__)

DB_PATH = Path(settings.DB_PATH)

CREATE_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    id           TEXT NOT NULL,
    source       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'processing',
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (id, source)
);
"""

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS order_events (
    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT DEFAULT '',
    ts         TEXT NOT NULL
);
"""

# Un solo registro activo (id = 1 siempre). CHECK garantiza la unicidad semántica.
CREATE_MELI_TOKENS_TABLE = """
CREATE TABLE IF NOT EXISTS meli_tokens (
    id            INTEGER PRIMARY KEY DEFAULT 1,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    seller_id     TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    CHECK (id = 1)
);
"""


async def init_db() -> None:
    """Crea las tablas si no existen y aplica migraciones pendientes."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_ORDERS_TABLE)
        await db.execute(CREATE_EVENTS_TABLE)
        await db.execute(CREATE_MELI_TOKENS_TABLE)
        await db.commit()
        # Migración: añadir completed_at si la columna aún no existe
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
            await db.commit()
            logger.info("Columna completed_at añadida a orders")
        except aiosqlite.OperationalError:
            pass  # La columna ya existe

        # Migración: añadir label_printed_at si la columna aún no existe
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN label_printed_at TEXT")
            await db.commit()
            logger.info("Columna label_printed_at añadida a orders")
        except aiosqlite.OperationalError:
            pass  # La columna ya existe

        # Migración: añadir seller_id a meli_tokens si la columna aún no existe
        try:
            await db.execute(
                "ALTER TABLE meli_tokens ADD COLUMN seller_id TEXT NOT NULL DEFAULT ''"
            )
            await db.commit()
            logger.info("Columna seller_id añadida a meli_tokens")
        except aiosqlite.OperationalError:
            pass  # La columna ya existe
    logger.info("SQLite DB inicializada en %s", DB_PATH)


async def upsert_order(order: NormalizedOrder) -> None:
    """Guarda o actualiza un pedido normalizado."""
    now = datetime.utcnow().isoformat()

    # Si se está marcando como completado, registrar el timestamp
    if order.status == OrderStatus.COMPLETED and order.completed_at is None:
        order.completed_at = datetime.utcnow()

    completed_at = order.completed_at.isoformat() if order.completed_at else None
    label_printed_at = order.label_printed_at.isoformat() if order.label_printed_at else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO orders (id, source, status, payload_json, created_at, updated_at, completed_at, label_printed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, source) DO UPDATE SET
                status       = CASE
                                   WHEN orders.status IN ('preparing', 'labeled', 'completed')
                                        AND excluded.status = 'processing'
                                   THEN orders.status
                                   ELSE excluded.status
                               END,
                payload_json = excluded.payload_json,
                updated_at   = excluded.updated_at,
                completed_at = CASE WHEN excluded.status = 'completed'
                                    THEN excluded.completed_at
                                    ELSE orders.completed_at END,
                label_printed_at = CASE WHEN excluded.label_printed_at IS NOT NULL
                                        THEN excluded.label_printed_at
                                        ELSE orders.label_printed_at END
            """,
            (
                order.id,
                order.source.value,
                order.status.value,
                order.model_dump_json(),
                now,
                now,
                completed_at,
                label_printed_at,
            ),
        )
        await db.commit()


async def get_local_status(order_id: str, source: str) -> Optional[OrderStatus]:
    """Devuelve el estado local de un pedido o None si no está en la BD."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM orders WHERE id = ? AND source = ?",
            (order_id, source),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        try:
            return OrderStatus(row[0])
        except ValueError:
            return None
    return None


async def log_event(order_id: str, source: str, event: str, detail: str = "") -> None:
    """Registra un evento de auditoría en la BD."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO order_events (order_id, source, event, detail, ts) VALUES (?,?,?,?,?)",
            (order_id, source, event, detail, now),
        )
        await db.commit()


async def get_preparing_orders() -> List[NormalizedOrder]:
    """
    Devuelve los pedidos de WooCommerce en estado PREPARING o LABELED desde la BD local.
    Se usan en el dashboard para el filtro 'Ver hojas impresas (recuperar)',
    permitiendo re-imprimir el PDF si la hoja se perdió en bodega.
    """
    results: List[NormalizedOrder] = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT payload_json FROM orders "
            "WHERE status IN ('preparing', 'labeled') AND source = 'woocommerce' "
            "ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    for row in rows:
        try:
            results.append(NormalizedOrder.model_validate_json(row[0]))
        except Exception as exc:
            logger.warning("No se pudo deserializar pedido PREPARING/LABELED para recuperación: %s", exc)
    return results


async def get_completed_orders() -> List[NormalizedOrder]:
    """Devuelve todos los pedidos completados desde la BD local para reporte Excel."""
    results: List[NormalizedOrder] = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT payload_json FROM orders WHERE status = 'completed' ORDER BY completed_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    for row in rows:
        try:
            results.append(NormalizedOrder.model_validate_json(row[0]))
        except Exception as exc:
            logger.warning("No se pudo deserializar pedido para Excel: %s", exc)
    return results


# ── Tokens de Mercado Libre ──────────────────────────────────────────────────

class MeliTokenRow(TypedDict):
    access_token:  str
    refresh_token: str
    expires_at:    datetime
    seller_id:     str


async def get_meli_token() -> Optional[MeliTokenRow]:
    """
    Devuelve el único registro de tokens de Mercado Libre o None si aún
    no se ha completado el flujo OAuth.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT access_token, refresh_token, expires_at, seller_id "
            "FROM meli_tokens WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None

    return MeliTokenRow(
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
        seller_id=row["seller_id"] or "",
    )


async def save_meli_token(
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    seller_id: str = "",
) -> None:
    """
    Guarda o actualiza el par de tokens de Mercado Libre.
    El campo seller_id se preserva si se llama con cadena vacía
    (útil al refrescar tokens sin conocer el seller_id todavía).
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO meli_tokens (id, access_token, refresh_token, expires_at, seller_id, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at,
                seller_id     = CASE
                                    WHEN excluded.seller_id != ''
                                    THEN excluded.seller_id
                                    ELSE meli_tokens.seller_id
                                END,
                updated_at    = excluded.updated_at
            """,
            (access_token, refresh_token, expires_at.isoformat(), seller_id, now),
        )
        await db.commit()
    logger.debug("Tokens de MeLi guardados (expiran: %s)", expires_at.isoformat())
