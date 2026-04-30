"""
Capa de persistencia SQLite usando aiosqlite para operaciones asíncronas.
Guarda estados intermedios de pedidos para sobrevivir reinicios del servidor.
"""
import aiosqlite
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TypedDict

from config import settings
from models.order import NormalizedOrder, OrderStatus

logger = logging.getLogger(__name__)

DB_PATH = Path(settings.DB_PATH)
_DB_CONN: aiosqlite.Connection | None = None


async def _ensure_db_connection() -> aiosqlite.Connection:
    global _DB_CONN
    if _DB_CONN is None:
        _DB_CONN = await aiosqlite.connect(DB_PATH)
        await _DB_CONN.execute("PRAGMA journal_mode=WAL")
        await _DB_CONN.execute("PRAGMA synchronous=NORMAL")
        await _DB_CONN.execute("PRAGMA busy_timeout=5000")
        await _DB_CONN.commit()
    return _DB_CONN


@asynccontextmanager
async def get_db() -> aiosqlite.Connection:
    db = await _ensure_db_connection()
    try:
        yield db
    finally:
        pass


async def close_db() -> None:
    global _DB_CONN
    if _DB_CONN is not None:
        await _DB_CONN.close()
        _DB_CONN = None

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

CREATE_MANIFESTS_TABLE = """
CREATE TABLE IF NOT EXISTS manifests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    CHECK (status IN ('open', 'closed'))
);
"""

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user',
    CHECK (role IN ('admin', 'user'))
);
"""


async def init_db() -> None:
    """Crea las tablas si no existen y aplica migraciones pendientes."""
    async with get_db() as db:
        await db.execute(CREATE_ORDERS_TABLE)
        await db.execute(CREATE_EVENTS_TABLE)
        await db.execute(CREATE_MELI_TOKENS_TABLE)
        await db.execute(CREATE_MANIFESTS_TABLE)
        await db.execute(CREATE_USERS_TABLE)
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

        # Migración: añadir manifest_id a orders si la columna aún no existe
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN manifest_id INTEGER REFERENCES manifests(id)")
            await db.commit()
            logger.info("Columna manifest_id añadida a orders")
        except aiosqlite.OperationalError:
            pass  # La columna ya existe

        # Migración: añadir role a users si la columna aún no existe
        try:
            await db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            await db.commit()
            logger.info("Columna role añadida a users")
        except aiosqlite.OperationalError:
            pass  # La columna ya existe

    # Migrar pedidos huérfanos a manifest retroactivo
    await migrate_orphan_orders()

    logger.info("SQLite DB inicializada en %s", DB_PATH)


async def upsert_order(order: NormalizedOrder) -> None:
    """Guarda o actualiza un pedido normalizado."""
    now = datetime.utcnow().isoformat()

    # Si se está marcando como completado, registrar el timestamp Y asignar a manifest
    manifest_id = None
    if order.status == OrderStatus.COMPLETED:
        # Establecer completed_at si no existe
        if order.completed_at is None:
            order.completed_at = datetime.utcnow()

        # Auto-asignar al manifest abierto actual si no tiene uno ya asignado
        async with get_db() as db:
            async with db.execute(
                "SELECT manifest_id FROM orders WHERE id = ? AND source = ?",
                (order.id, order.source.value)
            ) as cursor:
                existing = await cursor.fetchone()

        # Solo asignar nuevo manifest_id si el pedido no tiene uno asignado
        if not existing or existing[0] is None:
            manifest_id = await get_or_create_open_manifest()

    completed_at = order.completed_at.isoformat() if order.completed_at else None
    label_printed_at = order.label_printed_at.isoformat() if order.label_printed_at else None

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO orders (id, source, status, payload_json, created_at, updated_at, completed_at, label_printed_at, manifest_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, source) DO UPDATE SET
                status       = CASE
                                   WHEN orders.status IN ('preparing', 'labeled', 'completed')
                                        AND excluded.status = 'processing'
                                   THEN orders.status
                                   ELSE excluded.status
                               END,
                payload_json = excluded.payload_json,
                updated_at   = excluded.updated_at,
                completed_at = CASE
                                   -- Preservar el completed_at existente si ya hay uno en BD
                                   WHEN orders.completed_at IS NOT NULL
                                   THEN orders.completed_at
                                   -- Solo actualizar si el nuevo valor no es NULL
                                   WHEN excluded.completed_at IS NOT NULL
                                   THEN excluded.completed_at
                                   ELSE orders.completed_at
                               END,
                label_printed_at = CASE
                                       -- Preservar el label_printed_at existente si ya hay uno en BD
                                       WHEN orders.label_printed_at IS NOT NULL
                                       THEN orders.label_printed_at
                                       -- Solo actualizar si el nuevo valor no es NULL y no había uno previo
                                       WHEN excluded.label_printed_at IS NOT NULL
                                       THEN excluded.label_printed_at
                                       ELSE orders.label_printed_at
                                   END,
                manifest_id = CASE
                                  WHEN orders.manifest_id IS NOT NULL
                                  THEN orders.manifest_id
                                  WHEN excluded.manifest_id IS NOT NULL
                                  THEN excluded.manifest_id
                                  ELSE orders.manifest_id
                              END
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
                manifest_id,
            ),
        )
        await db.commit()


async def get_local_status(order_id: str, source: str) -> Optional[OrderStatus]:
    """Devuelve el estado local de un pedido o None si no está en la BD."""
    async with get_db() as db:
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
    async with get_db() as db:
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
    async with get_db() as db:
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
    async with get_db() as db:
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


class UserRow(TypedDict):
    id: int
    username: str
    hashed_password: str
    role: str


class PublicUserRow(TypedDict):
    id: int
    username: str
    role: str


async def get_user_by_username(username: str) -> Optional[UserRow]:
    """Busca un usuario por username."""
    async with get_db() as db:
        async with db.execute(
            "SELECT id, username, hashed_password, role FROM users WHERE username = ?",
            (username,),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            row = await cursor.fetchone()

    if row is None:
        return None

    return UserRow(
        id=row["id"],
        username=row["username"],
        hashed_password=row["hashed_password"],
        role=row["role"],
    )


async def create_user(username: str, hashed_password: str, role: str = "user") -> int:
    """Crea un usuario y devuelve su ID."""
    normalized_role = role if role in {"admin", "user"} else "user"
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO users (username, hashed_password, role) VALUES (?, ?, ?)",
            (username, hashed_password, normalized_role),
        )
        await db.commit()
        return cursor.lastrowid


async def get_all_users() -> List[PublicUserRow]:
    """Lista todos los usuarios sin exponer contraseñas."""
    users: List[PublicUserRow] = []
    async with get_db() as db:
        async with db.execute(
            "SELECT id, username, role FROM users ORDER BY username ASC"
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            rows = await cursor.fetchall()

    for row in rows:
        users.append(
            PublicUserRow(
                id=row["id"],
                username=row["username"],
                role=row["role"],
            )
        )

    return users


async def ensure_default_admin_user(username: str, hashed_password: str) -> None:
    """Crea el usuario admin por defecto si aún no existe."""
    existing = await get_user_by_username(username)
    if existing:
        if existing["role"] != "admin":
            await update_user_role(existing["id"], "admin")
            logger.info("Usuario administrador por defecto actualizado a rol admin: %s", username)
        return

    await create_user(username, hashed_password, role="admin")
    logger.info("Usuario administrador por defecto creado: %s", username)


async def get_user_by_id(user_id: int) -> Optional[PublicUserRow]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (user_id,),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            row = await cursor.fetchone()

    if row is None:
        return None

    return PublicUserRow(
        id=row["id"],
        username=row["username"],
        role=row["role"],
    )


async def update_user_role(user_id: int, role: str) -> bool:
    if role not in {"admin", "user"}:
        return False

    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (role, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_username(user_id: int, username: str) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE users SET username = ? WHERE id = ?",
            (username, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_user(user_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM users WHERE id = ?",
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


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
    async with get_db() as db:
        async with db.execute(
            "SELECT access_token, refresh_token, expires_at, seller_id "
            "FROM meli_tokens WHERE id = 1"
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
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
    async with get_db() as db:
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


# ── Manifiestos (Lotes de Despacho) ──────────────────────────────────────────


async def get_or_create_open_manifest() -> int:
    """
    Devuelve el ID del manifest abierto actualmente.
    Si no existe ninguno, crea uno nuevo.
    """
    now = datetime.utcnow().isoformat()
    async with get_db() as db:
        # Intentar obtener manifest abierto
        async with db.execute(
            "SELECT id FROM manifests WHERE status = 'open' ORDER BY created_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return row[0]

        # No existe, crear uno nuevo
        cursor = await db.execute(
            "INSERT INTO manifests (status, created_at) VALUES ('open', ?)",
            (now,)
        )
        await db.commit()
        logger.info("Manifest #%d creado automáticamente", cursor.lastrowid)
        return cursor.lastrowid


async def close_manifest(manifest_id: int) -> bool:
    """
    Cierra un manifest específico.
    Retorna True si se cerró exitosamente, False si no existe o ya estaba cerrado.
    """
    now = datetime.utcnow().isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            """
            UPDATE manifests
            SET status = 'closed', closed_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (now, manifest_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_manifest_orders(manifest_id: int) -> List[NormalizedOrder]:
    """
    Devuelve todos los pedidos asociados a un manifest específico.
    """
    results: List[NormalizedOrder] = []
    async with get_db() as db:
        async with db.execute(
            """
            SELECT payload_json FROM orders
            WHERE manifest_id = ?
            ORDER BY label_printed_at DESC, completed_at DESC
            """,
            (manifest_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        try:
            results.append(NormalizedOrder.model_validate_json(row[0]))
        except Exception as exc:
            logger.warning("No se pudo deserializar pedido de manifest %s: %s", manifest_id, exc)

    return results


async def get_open_manifest_info() -> Optional[dict]:
    """
    Devuelve información del manifest abierto actual o None si no existe.
    Retorna: {"id": int, "created_at": str, "order_count": int}
    """
    async with get_db() as db:
        async with db.execute(
            """
            SELECT m.id, m.created_at, COUNT(o.id) as order_count
            FROM manifests m
            LEFT JOIN orders o ON o.manifest_id = m.id
            WHERE m.status = 'open'
            GROUP BY m.id
            ORDER BY m.created_at DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "created_at": row[1],
        "order_count": row[2]
    }


async def migrate_orphan_orders() -> None:
    """
    Asigna un manifest retroactivo a pedidos completados que no tienen manifest_id.
    Crea un manifest histórico cerrado para agruparlos.
    """
    now = datetime.utcnow().isoformat()
    async with get_db() as db:
        # Verificar si hay pedidos huérfanos
        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'completed' AND manifest_id IS NULL"
        ) as cursor:
            count = (await cursor.fetchone())[0]

        if count == 0:
            return

        # Crear manifest retroactivo cerrado
        cursor = await db.execute(
            "INSERT INTO manifests (status, created_at, closed_at) VALUES ('closed', ?, ?)",
            (now, now)
        )
        retroactive_manifest_id = cursor.lastrowid

        # Asignar todos los pedidos huérfanos a este manifest
        await db.execute(
            """
            UPDATE orders
            SET manifest_id = ?
            WHERE status = 'completed' AND manifest_id IS NULL
            """,
            (retroactive_manifest_id,)
        )
        await db.commit()

        logger.info("Migrados %d pedidos huérfanos al manifest retroactivo #%d",
                    count, retroactive_manifest_id)

