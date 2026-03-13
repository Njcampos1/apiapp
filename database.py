"""
Capa de persistencia SQLite usando aiosqlite para operaciones asíncronas.
Guarda estados intermedios de pedidos para sobrevivir reinicios del servidor.
"""
import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

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


async def init_db() -> None:
    """Crea las tablas si no existen."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_ORDERS_TABLE)
        await db.execute(CREATE_EVENTS_TABLE)
        await db.commit()
    logger.info("SQLite DB inicializada en %s", DB_PATH)


async def upsert_order(order: NormalizedOrder) -> None:
    """Guarda o actualiza un pedido normalizado."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO orders (id, source, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, source) DO UPDATE SET
                status       = excluded.status,
                payload_json = excluded.payload_json,
                updated_at   = excluded.updated_at
            """,
            (
                order.id,
                order.source.value,
                order.status.value,
                order.model_dump_json(),
                now,
                now,
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
