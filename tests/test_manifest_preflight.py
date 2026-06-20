"""
Tests del pre-chequeo de cierre de manifiesto (`build_preflight_payload`).

Contexto / incidencia que motiva el control:
  Una hoja de picking se extravió en bodega. El pedido quedó en estado
  'preparing', su etiqueta nunca se imprimió, por lo que nunca pasó a
  'completed' ni se asignó al manifiesto del día. Al cerrar el día nadie
  lo notó y el cliente no recibió su pedido.

`build_preflight_payload` serializa esa "diferencia entre hojas de picking y
etiquetas" (pedidos con hoja generada pero sin etiqueta) para mostrarla en un
aviso antes de cerrar el manifiesto. Estos tests fijan ese contrato.

NOTA: importar `main` instancia `config.settings`, que exige SECRET_KEY y
DEFAULT_ADMIN_PASSWORD. Las definimos ANTES del import para que el test no
dependa de un .env presente. No se construyen proveedores ni se toca la BD
al importar (eso ocurre solo en el lifespan de la app).
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from models.order import (
    NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress,
)
from main import build_preflight_payload, reconcile_local_status


# ── Fixtures helpers ──────────────────────────────────────────────────────────

def _make_order(
    status: OrderStatus = OrderStatus.PREPARING,
    order_id: str = "1001",
    source: str = "woocommerce",
    first_name: str = "Juan",
    last_name: str = "Perez",
    items: list | None = None,
    platform_meta: dict | None = None,
) -> NormalizedOrder:
    if items is None:
        items = [OrderItem(sku="101000-1", name="Cafe Premium", quantity=1, price=10000)]
    return NormalizedOrder(
        id=order_id,
        source=OrderSource(source),
        status=status,
        shipping=ShippingAddress(
            first_name=first_name,
            last_name=last_name,
            address_1="Av. Principal 100",
            city="Santiago",
            state="RM",
            country="CL",
            phone="931311932",
            email="juan@example.com",
        ),
        items=items,
        total=10000,
        platform_meta=(platform_meta or {}),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Estructura del payload
# ══════════════════════════════════════════════════════════════════════════════

class TestPreflightShape:

    def test_lista_vacia_cuenta_cero(self):
        payload = build_preflight_payload([])
        assert payload == {"count": 0, "orders": []}

    def test_count_coincide_con_numero_de_pedidos(self):
        payload = build_preflight_payload([_make_order(order_id="1"), _make_order(order_id="2")])
        assert payload["count"] == 2
        assert len(payload["orders"]) == 2

    def test_campos_expuestos_por_pedido(self):
        payload = build_preflight_payload([_make_order(order_id="1001")])
        row = payload["orders"][0]
        # El aviso de la UI depende exactamente de estas claves.
        assert set(row.keys()) == {"id", "display_id", "customer", "status", "item_count"}


# ══════════════════════════════════════════════════════════════════════════════
# 2. Contenido de cada fila
# ══════════════════════════════════════════════════════════════════════════════

class TestPreflightContent:

    def test_woocommerce_usa_id_como_display_id(self):
        payload = build_preflight_payload([_make_order(order_id="1001")])
        row = payload["orders"][0]
        assert row["id"] == "1001"
        assert row["display_id"] == "1001"
        assert row["customer"] == "Juan Perez"
        assert row["status"] == "preparing"
        assert row["item_count"] == 1

    def test_meli_usa_shipping_id_como_display_id(self):
        # En MeLi el operario identifica el pedido por el shipping_id, no por el id interno.
        order = _make_order(
            order_id="2000003456",
            source="mercadolibre",
            platform_meta={"shipping_id": "44455667"},
        )
        row = build_preflight_payload([order])["orders"][0]
        assert row["id"] == "2000003456"
        assert row["display_id"] == "44455667"

    def test_status_labeled_se_preserva(self):
        # 'labeled' también es diferencia: etiqueta generada pero pedido aún no completado.
        row = build_preflight_payload([_make_order(status=OrderStatus.LABELED)])["orders"][0]
        assert row["status"] == "labeled"

    def test_item_count_suma_cantidades(self):
        order = _make_order(items=[
            OrderItem(sku="A", name="Cafe", quantity=2, price=1000),
            OrderItem(sku="B", name="Te", quantity=3, price=1000),
        ])
        assert build_preflight_payload([order])["orders"][0]["item_count"] == 5

    def test_cliente_sin_nombre_usa_guion(self):
        order = _make_order(first_name="", last_name="")
        assert build_preflight_payload([order])["orders"][0]["customer"] == "—"


# ══════════════════════════════════════════════════════════════════════════════
# 3. reconcile_local_status — regla de sincronización (seguridad del flujo MeLi)
# ══════════════════════════════════════════════════════════════════════════════

class TestReconcileLocalStatus:
    """
    Verifica que extender PREPARING a MeLi NO cambia el comportamiento actual y
    NO 'atrapa' pedidos cuyo envío ya se resolvió en la plataforma.
    """

    # — Comportamiento ACTUAL que debe preservarse —
    def test_sin_estado_local_usa_plataforma(self):
        assert reconcile_local_status(OrderStatus.COMPLETED, None) == OrderStatus.COMPLETED
        assert reconcile_local_status(OrderStatus.PROCESSING, None) == OrderStatus.PROCESSING

    def test_completed_local_se_preserva_sobre_processing(self):
        # 'completed en verde tras refresh' aunque la plataforma aún diga processing
        assert reconcile_local_status(OrderStatus.PROCESSING, OrderStatus.COMPLETED) == OrderStatus.COMPLETED

    def test_completed_local_se_preserva_aunque_plataforma_cancele(self):
        # Caso borde actual: un completado local NO es degradado por un error de plataforma
        assert reconcile_local_status(OrderStatus.ERROR, OrderStatus.COMPLETED) == OrderStatus.COMPLETED

    # — Comportamiento NUEVO (extensión MeLi) —
    def test_preparing_local_se_preserva_si_plataforma_no_es_terminal(self):
        # Hoja generada, envío aún pendiente → sigue en preparing (lo detecta el preflight)
        assert reconcile_local_status(OrderStatus.PROCESSING, OrderStatus.PREPARING) == OrderStatus.PREPARING

    def test_preparing_local_cede_ante_plataforma_completed(self):
        # CLAVE: si el envío ya se despachó, gana la plataforma → no queda atrapado
        assert reconcile_local_status(OrderStatus.COMPLETED, OrderStatus.PREPARING) == OrderStatus.COMPLETED

    def test_preparing_local_cede_ante_plataforma_error(self):
        assert reconcile_local_status(OrderStatus.ERROR, OrderStatus.PREPARING) == OrderStatus.ERROR

    def test_labeled_local_cede_ante_plataforma_completed(self):
        assert reconcile_local_status(OrderStatus.COMPLETED, OrderStatus.LABELED) == OrderStatus.COMPLETED
