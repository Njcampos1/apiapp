"""
Tests del reaper de reconciliación de pedidos MeLi atascados.

Incidencia que motiva el control:
  Al descargar el PDF masivo de picking, los pedidos MeLi se marcan PREPARING.
  La salida normal a COMPLETED depende de que /api/orders los siga viendo en el
  feed 'paid & not_delivered'. Cuando el envío se despacha, sale del feed y nunca
  se reconcilia: queda como zombie en 'preparing'. El aviso de pre-cierre los
  listaba a todos (decenas de pedidos viejos ya despachados).

El reaper consulta el estado REAL en MeLi y pasa los despachados/cancelados a
COMPLETED/ERROR, archivándolos en un manifiesto de reconciliación CERRADO para
que NO entren al manifiesto activo. Estos tests fijan ese contrato.

NOTA: importar `main` exige SECRET_KEY/DEFAULT_ADMIN_PASSWORD; se definen antes
del import. La BD se redirige a un archivo temporal por test.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import asyncio
import tempfile
from pathlib import Path

import database
from database import upsert_order, get_local_status, get_db
import main
from models.order import (
    NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress,
)


def _run(async_body):
    """Ejecuta una corutina sobre una BD SQLite temporal recién inicializada."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    async def _wrapper():
        database.DB_PATH = Path(path)
        database._DB_CONN = None
        await database.init_db()
        try:
            await async_body()
        finally:
            await database.close_db()

    try:
        asyncio.run(_wrapper())
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def _meli_order(order_id: str, shipping_id: str | None = None,
                status: OrderStatus = OrderStatus.PREPARING) -> NormalizedOrder:
    meta = {"shipping_id": shipping_id} if shipping_id else {}
    return NormalizedOrder(
        id=order_id,
        source=OrderSource.MERCADOLIBRE,
        status=status,
        shipping=ShippingAddress(first_name="Ana", last_name="Soto", city="Santiago"),
        items=[OrderItem(sku="A", name="Cafe", quantity=1, price=1000)],
        total=1000,
        platform_meta=meta,
    )


class _FakeMeli:
    """Provider falso: devuelve el estado configurado por id (o None)."""
    def __init__(self, mapping: dict[str, OrderStatus | None]):
        self.mapping = mapping
        self.calls: list[str] = []

    async def get_current_status(self, order_id: str):
        self.calls.append(order_id)
        return self.mapping.get(order_id)


def _with_provider(mapping):
    original = main._providers
    main._providers = {OrderSource.MERCADOLIBRE.value: _FakeMeli(mapping)}
    return original


async def _set_updated_at(order_id: str, iso: str):
    async with get_db() as db:
        await db.execute(
            "UPDATE orders SET updated_at = ? WHERE id = ? AND source = 'mercadolibre'",
            (iso, order_id),
        )
        await db.commit()


async def _manifest_status(manifest_id):
    async with get_db() as db:
        async with db.execute("SELECT status FROM manifests WHERE id = ?", (manifest_id,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


# ── Dedup (función pura) ──────────────────────────────────────────────────────

class TestDedup:
    def test_mismo_envio_se_lista_una_vez(self):
        """Dos pedidos MeLi del mismo carrito comparten shipping_id → una fila."""
        a = _meli_order("2000A", shipping_id="555")
        b = _meli_order("2000B", shipping_id="555")
        c = _meli_order("2000C", shipping_id="777")
        out = main.build_preflight_payload([a, b, c])
        assert out["count"] == 2
        assert {r["display_id"] for r in out["orders"]} == {"555", "777"}


# ── Reaper ──────────────────────────────────────────────────────────────────

class TestReaper:
    def test_zombie_despachado_pasa_a_completed_y_no_entra_al_manifiesto_abierto(self):
        async def body():
            original = _with_provider({"Z1": OrderStatus.COMPLETED})
            try:
                await upsert_order(_meli_order("Z1", shipping_id="900"))
                assert await get_local_status("Z1", "mercadolibre") == OrderStatus.PREPARING

                result = await main.reconcile_stuck_meli_orders()
                assert result["reconciled"] == 1

                # Pasó a completed
                assert await get_local_status("Z1", "mercadolibre") == OrderStatus.COMPLETED

                # Quedó asignado a un manifiesto CERRADO (no al abierto del día)
                async with get_db() as db:
                    async with db.execute(
                        "SELECT manifest_id FROM orders WHERE id='Z1' AND source='mercadolibre'"
                    ) as cur:
                        mid = (await cur.fetchone())[0]
                assert mid is not None
                assert await _manifest_status(mid) == "closed"

                # Ya no aparece en el aviso de pre-cierre
                from database import get_preparing_orders_all_sources
                assert await get_preparing_orders_all_sources() == []
            finally:
                main._providers = original
        _run(body)

    def test_pendiente_reciente_se_mantiene(self):
        async def body():
            original = _with_provider({"P1": None})  # MeLi: aún pendiente
            try:
                await upsert_order(_meli_order("P1", shipping_id="901"))
                result = await main.reconcile_stuck_meli_orders()
                assert result["reconciled"] == 0
                assert await get_local_status("P1", "mercadolibre") == OrderStatus.PREPARING
            finally:
                main._providers = original
        _run(body)

    def test_salvaguarda_por_antiguedad(self):
        """MeLi no resuelve (None) pero lleva días atascado → se asume despachado."""
        async def body():
            original = _with_provider({"OLD": None})
            try:
                await upsert_order(_meli_order("OLD", shipping_id="902"))
                await _set_updated_at("OLD", "2026-01-01T00:00:00")  # muy viejo
                result = await main.reconcile_stuck_meli_orders()
                assert result["reconciled"] == 1
                assert await get_local_status("OLD", "mercadolibre") == OrderStatus.COMPLETED
            finally:
                main._providers = original
        _run(body)

    def test_cancelado_pasa_a_error_sin_manifiesto(self):
        async def body():
            original = _with_provider({"C1": OrderStatus.ERROR})
            try:
                await upsert_order(_meli_order("C1", shipping_id="903"))
                result = await main.reconcile_stuck_meli_orders()
                assert result["reconciled"] == 1
                assert await get_local_status("C1", "mercadolibre") == OrderStatus.ERROR
                async with get_db() as db:
                    async with db.execute(
                        "SELECT manifest_id FROM orders WHERE id='C1' AND source='mercadolibre'"
                    ) as cur:
                        mid = (await cur.fetchone())[0]
                assert mid is None  # un cancelado no se manifiesta
            finally:
                main._providers = original
        _run(body)
