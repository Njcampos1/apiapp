"""
Capa de persistencia SQLite usando aiosqlite para operaciones asíncronas.
Guarda estados intermedios de pedidos para sobrevivir reinicios del servidor.
"""
import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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
    """Crea las tablas si no existen y aplica migraciones pendientes."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_ORDERS_TABLE)
        await db.execute(CREATE_EVENTS_TABLE)
        await db.commit()
        # Migración: añadir completed_at si la columna aún no existe
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
            await db.commit()
            logger.info("Columna completed_at añadida a orders")
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

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO orders (id, source, status, payload_json, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
                                    ELSE orders.completed_at END
            """,
            (
                order.id,
                order.source.value,
                order.status.value,
                order.model_dump_json(),
                now,
                now,
                completed_at,
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
