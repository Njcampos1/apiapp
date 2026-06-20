"""
Tests de SEGURIDAD DE FLUJO para la propuesta de extender el control de
"hoja-sin-etiqueta" a MercadoLibre (marcar PREPARING en bulk-pdf + ampliar el
preflight).

Objetivo: dar confianza de que ese cambio NO romperá el flujo actual. Para eso
fijamos (caracterizamos) las invariantes de la capa de persistencia
(`upsert_order`) de las que depende todo el ciclo pedido → manifiesto:

  · un re-sync desde la plataforma NUNCA degrada un estado avanzado;
  · marcar PREPARING NO "atrapa" al pedido: puede avanzar a COMPLETED normal;
  · completed_at, label_printed_at y manifest_id son inmutables una vez fijados;
  · completar asigna automáticamente el manifiesto abierto.

Si la futura implementación MeLi viola cualquiera de estas, estos tests fallan
ANTES de llegar a producción.

El único test que se espera que CAMBIE al implementar la extensión está marcado
explícitamente abajo (TestPuntoDeCambioIntencional): hoy `get_preparing_orders`
filtra source='woocommerce'. Ese es, a propósito, el lugar exacto a modificar.

NOTAS DE ENTORNO
- Importar `database` instancia `config.settings` → exige SECRET_KEY y
  DEFAULT_ADMIN_PASSWORD. Las definimos ANTES del import.
- No se usa pytest-asyncio: cada test corre su cuerpo async con asyncio.run().
- No se toca la BD real: redirigimos database.DB_PATH a un archivo temporal por
  test y reseteamos la conexión global.
"""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import database
from database import (
    upsert_order, get_local_status, get_preparing_orders,
    get_preparing_orders_all_sources, get_open_manifest_info, close_manifest,
)
from models.order import (
    NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress,
)


# ── Infraestructura de test: BD temporal aislada por test ─────────────────────

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


def _make_order(
    order_id: str = "1001",
    source: str = "woocommerce",
    status: OrderStatus = OrderStatus.PROCESSING,
    completed_at=None,
    label_printed_at=None,
) -> NormalizedOrder:
    return NormalizedOrder(
        id=order_id,
        source=OrderSource(source),
        status=status,
        shipping=ShippingAddress(first_name="Juan", last_name="Perez", city="Santiago"),
        items=[OrderItem(sku="A", name="Cafe", quantity=1, price=1000)],
        total=1000,
        completed_at=completed_at,
        label_printed_at=label_printed_at,
    )


async def _row(order_id: str, source: str) -> dict:
    """Lee la fila cruda de un pedido (status + timestamps + manifest_id)."""
    async with database.get_db() as db:
        async with db.execute(
            "SELECT status, completed_at, label_printed_at, manifest_id "
            "FROM orders WHERE id = ? AND source = ?",
            (order_id, source),
        ) as cursor:
            r = await cursor.fetchone()
    return {
        "status": r[0],
        "completed_at": r[1],
        "label_printed_at": r[2],
        "manifest_id": r[3],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. No degradación: un re-sync desde la plataforma no pisa un estado avanzado
# ══════════════════════════════════════════════════════════════════════════════

class TestNoDegradacion:

    def test_processing_no_degrada_preparing(self):
        """Re-sincronizar un pedido como 'processing' NO borra su hoja generada."""
        async def body():
            await upsert_order(_make_order(status=OrderStatus.PREPARING))
            # La plataforma lo devuelve otra vez como 'processing' (caso real de sync)
            await upsert_order(_make_order(status=OrderStatus.PROCESSING))
            assert await get_local_status("1001", "woocommerce") == OrderStatus.PREPARING
        _run(body)

    def test_processing_no_degrada_completed(self):
        async def body():
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            await upsert_order(_make_order(status=OrderStatus.PROCESSING))
            assert await get_local_status("1001", "woocommerce") == OrderStatus.COMPLETED
        _run(body)

    def test_no_degradacion_aplica_a_mercadolibre(self):
        """La misma protección debe valer para MeLi (relevante para la extensión)."""
        async def body():
            await upsert_order(_make_order(source="mercadolibre", status=OrderStatus.PREPARING))
            await upsert_order(_make_order(source="mercadolibre", status=OrderStatus.PROCESSING))
            assert await get_local_status("1001", "mercadolibre") == OrderStatus.PREPARING
        _run(body)


# ══════════════════════════════════════════════════════════════════════════════
# 2. CLAVE del caveat: marcar PREPARING NO atrapa al pedido
# ══════════════════════════════════════════════════════════════════════════════

class TestPreparingNoAtrapa:

    def test_preparing_puede_avanzar_a_completed(self):
        """
        El corazón de la propuesta MeLi: aunque marquemos PREPARING al descargar
        la hoja, el pedido debe poder completarse normalmente (imprimir etiqueta)
        y entrar al manifiesto. La capa de persistencia NO lo bloquea.
        """
        async def body():
            await upsert_order(_make_order(source="mercadolibre", status=OrderStatus.PREPARING))
            await upsert_order(_make_order(source="mercadolibre", status=OrderStatus.COMPLETED))

            row = await _row("1001", "mercadolibre")
            assert row["status"] == "completed"
            assert row["completed_at"] is not None       # se fijó al completar
            assert row["manifest_id"] is not None         # entró al manifiesto
        _run(body)

    def test_labeled_puede_avanzar_a_completed(self):
        async def body():
            await upsert_order(_make_order(status=OrderStatus.LABELED))
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            assert await get_local_status("1001", "woocommerce") == OrderStatus.COMPLETED
        _run(body)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Inmutabilidad de timestamps y manifest_id
# ══════════════════════════════════════════════════════════════════════════════

class TestInmutabilidad:

    def test_completed_at_inmutable(self):
        async def body():
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            original = (await _row("1001", "woocommerce"))["completed_at"]
            # Re-upsert posterior (otro sync): completed_at no debe cambiar
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            assert (await _row("1001", "woocommerce"))["completed_at"] == original
        _run(body)

    def test_label_printed_at_inmutable(self):
        async def body():
            from datetime import datetime, timezone
            stamp = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
            await upsert_order(_make_order(status=OrderStatus.COMPLETED, label_printed_at=stamp))
            original = (await _row("1001", "woocommerce"))["label_printed_at"]
            assert original is not None
            # Un sync posterior sin label_printed_at no debe borrarlo
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            assert (await _row("1001", "woocommerce"))["label_printed_at"] == original
        _run(body)

    def test_manifest_id_inmutable_tras_reupsert(self):
        async def body():
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            m1 = (await _row("1001", "woocommerce"))["manifest_id"]
            assert m1 is not None
            await upsert_order(_make_order(status=OrderStatus.COMPLETED))
            assert (await _row("1001", "woocommerce"))["manifest_id"] == m1
        _run(body)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Asignación de manifiesto al completar
# ══════════════════════════════════════════════════════════════════════════════

class TestManifiesto:

    def test_completados_comparten_manifiesto_abierto(self):
        async def body():
            await upsert_order(_make_order(order_id="1", status=OrderStatus.COMPLETED))
            await upsert_order(_make_order(order_id="2", status=OrderStatus.COMPLETED))
            m1 = (await _row("1", "woocommerce"))["manifest_id"]
            m2 = (await _row("2", "woocommerce"))["manifest_id"]
            assert m1 == m2
            info = await get_open_manifest_info()
            assert info["order_count"] == 2
        _run(body)

    def test_tras_cierre_nuevo_completado_no_entra_al_manifiesto_viejo(self):
        async def body():
            await upsert_order(_make_order(order_id="1", status=OrderStatus.COMPLETED))
            m1 = (await _row("1", "woocommerce"))["manifest_id"]
            assert await close_manifest(m1) is True
            # Pedido completado DESPUÉS del cierre debe ir a un manifiesto nuevo
            await upsert_order(_make_order(order_id="2", status=OrderStatus.COMPLETED))
            m2 = (await _row("2", "woocommerce"))["manifest_id"]
            assert m2 is not None and m2 != m1
        _run(body)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Punto de cambio INTENCIONAL (caracteriza el comportamiento de HOY)
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardRecoverySigueWooCommerce:
    """
    get_preparing_orders alimenta el filtro de recuperación del DASHBOARD y debe
    seguir siendo SOLO WooCommerce (no se tocó, para no cambiar esa pestaña).
    La extensión a MeLi vive en una función paralela (ver clase siguiente).
    """

    def test_get_preparing_devuelve_woocommerce(self):
        async def body():
            await upsert_order(_make_order(order_id="W1", status=OrderStatus.PREPARING))
            ids = {o.id for o in await get_preparing_orders()}
            assert "W1" in ids
        _run(body)

    def test_get_preparing_sigue_excluyendo_mercadolibre(self):
        async def body():
            await upsert_order(_make_order(order_id="M1", source="mercadolibre", status=OrderStatus.PREPARING))
            ids = {o.id for o in await get_preparing_orders()}
            assert "M1" not in ids
        _run(body)


class TestPreflightAllSources:
    """
    get_preparing_orders_all_sources alimenta el pre-chequeo de cierre y SÍ debe
    incluir MeLi: es lo que cierra la brecha de 'hoja sin etiqueta' en MeLi.
    """

    def test_incluye_woocommerce_y_mercadolibre(self):
        async def body():
            await upsert_order(_make_order(order_id="W1", status=OrderStatus.PREPARING))
            await upsert_order(_make_order(order_id="M1", source="mercadolibre", status=OrderStatus.LABELED))
            ids = {o.id for o in await get_preparing_orders_all_sources()}
            assert ids == {"W1", "M1"}
        _run(body)

    def test_excluye_processing_y_completed(self):
        async def body():
            await upsert_order(_make_order(order_id="P1", status=OrderStatus.PROCESSING))
            await upsert_order(_make_order(order_id="C1", source="mercadolibre", status=OrderStatus.COMPLETED))
            ids = {o.id for o in await get_preparing_orders_all_sources()}
            assert "P1" not in ids
            assert "C1" not in ids
        _run(body)
