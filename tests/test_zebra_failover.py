"""
Tests del failover de impresión entre la Zebra principal y la de respaldo.

Topología que motiva el control:
  Ambas Zebras se alcanzan por el mismo nodo Tailscale del PC puente de bodega,
  la principal en el puerto 9100 y la de respaldo en el 9101 (portproxy hacia
  dos equipos físicamente distintos). Si la principal está apagada o sin red,
  la etiqueta ZPL debe salir igual por la de respaldo, sin que el operador
  tenga que hacer nada.

Estos tests NO necesitan impresoras físicas: se sustituye
`asyncio.open_connection` por un doble que decide según (host, port), de modo
que se puede simular cualquier combinación de Zebras caídas.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import asyncio
from typing import Dict, List, Optional, Tuple

import pytest

from models.order import (
    NormalizedOrder, OrderItem, OrderSource, OrderStatus, ShippingAddress,
)
from services import zpl_service
from services.zpl_service import ROLE_BACKUP, ROLE_PRIMARY, ZPLService

PRIMARY = ("100.76.207.59", 9100)
BACKUP  = ("100.76.207.59", 9101)


# ── Dobles de socket ─────────────────────────────────────────────

class FakeWriter:
    """Writer asíncrono que acumula lo escrito en el destino que lo creó."""

    def __init__(self, sink: List[bytes], fail_on_drain: bool = False) -> None:
        self._sink = sink
        self._fail_on_drain = fail_on_drain
        self._buffer = b""

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        if self._fail_on_drain:
            raise ConnectionResetError("conexión reiniciada por la Zebra")
        self._sink.append(self._buffer)
        self._buffer = b""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class FakeNetwork:
    """
    Sustituto de `asyncio.open_connection`.

    `failures` mapea (host, port) → excepción a lanzar al conectar.
    `drain_failures` marca destinos que aceptan la conexión pero cortan al
    enviar. Todo lo que se logra enviar queda en `received[(host, port)]`.
    """

    def __init__(
        self,
        failures: Optional[Dict[Tuple[str, int], BaseException]] = None,
        drain_failures: Optional[set] = None,
    ) -> None:
        self.failures = failures or {}
        self.drain_failures = drain_failures or set()
        self.attempts: List[Tuple[str, int]] = []
        self.received: Dict[Tuple[str, int], List[bytes]] = {}

    async def __call__(self, host: str, port: int):
        target = (host, port)
        self.attempts.append(target)
        if target in self.failures:
            raise self.failures[target]
        sink = self.received.setdefault(target, [])
        return None, FakeWriter(sink, fail_on_drain=target in self.drain_failures)

    def payloads(self, target: Tuple[str, int]) -> List[str]:
        return [b.decode("utf-8") for b in self.received.get(target, [])]


@pytest.fixture
def net(monkeypatch):
    """Instala una red falsa; los tests la configuran vía `configure`."""
    holder = {}

    def configure(**kwargs) -> FakeNetwork:
        fake = FakeNetwork(**kwargs)
        monkeypatch.setattr(zpl_service.asyncio, "open_connection", fake)
        holder["fake"] = fake
        return fake

    return configure


def make_service(backup: bool = True) -> ZPLService:
    return ZPLService(
        host=PRIMARY[0], port=PRIMARY[1], dpi=203,
        backup_host=BACKUP[0] if backup else "",
        backup_port=BACKUP[1],
        timeout=3.0,
    )


def make_order(note: str = "") -> NormalizedOrder:
    return NormalizedOrder(
        id="12345",
        source=OrderSource.WOOCOMMERCE,
        status=OrderStatus.PROCESSING,
        shipping=ShippingAddress(
            first_name="Ana", last_name="Pérez", city="Santiago",
            address_1="Av. Siempre Viva 742", email="ana@ejemplo.cl",
            phone="+56912345678",
        ),
        items=[OrderItem(sku="CAFE-001", name="Café molido", quantity=1, price=7990.0)],
        total=7990.0,
        customer_note=note,
    )


# ── Camino feliz ─────────────────────────────────────────────────

def test_principal_ok_no_toca_el_respaldo(net):
    fake = net()
    svc = make_service()

    ok, msg = asyncio.run(svc.send_raw_zpl("^XA^FDprueba^FS^XZ"))

    assert ok and msg == ""
    assert fake.attempts == [PRIMARY], "no debe contactar la Zebra de respaldo"
    assert svc.last_target.role == ROLE_PRIMARY
    assert svc.used_failover is False


# ── Failover por cada tipo de fallo ──────────────────────────────

@pytest.mark.parametrize("error", [
    asyncio.TimeoutError(),                       # Zebra apagada
    ConnectionRefusedError("conexión rechazada"),  # puerto cerrado
    OSError("red inalcanzable"),                   # nodo puente sin ruta
])
def test_falla_la_principal_imprime_el_respaldo(net, error):
    fake = net(failures={PRIMARY: error})
    svc = make_service()
    zpl = "^XA^FDetiqueta^FS^XZ"

    ok, msg = asyncio.run(svc.send_raw_zpl(zpl))

    assert ok and msg == ""
    assert fake.attempts == [PRIMARY, BACKUP]
    assert svc.last_target.role == ROLE_BACKUP
    assert svc.used_failover is True
    # El failover no debe alterar la etiqueta ZPL: byte a byte igual.
    assert fake.payloads(BACKUP) == [zpl]
    assert fake.payloads(PRIMARY) == []


def test_falla_durante_el_envio_tambien_cae_al_respaldo(net):
    """La principal acepta la conexión pero corta al enviar."""
    fake = net(drain_failures={PRIMARY})
    svc = make_service()

    ok, _ = asyncio.run(svc.send_raw_zpl("^XA^FDx^FS^XZ"))

    assert ok
    assert fake.attempts == [PRIMARY, BACKUP]
    assert svc.last_target.role == ROLE_BACKUP


# ── Ambas caídas ─────────────────────────────────────────────────

def test_ambas_caidas_devuelve_error_sin_lanzar(net):
    fake = net(failures={
        PRIMARY: asyncio.TimeoutError(),
        BACKUP:  ConnectionRefusedError("rechazada"),
    })
    svc = make_service()

    ok, msg = asyncio.run(svc.send_raw_zpl("^XA^XZ"))

    assert ok is False
    assert fake.attempts == [PRIMARY, BACKUP]
    assert svc.last_target is None
    # El mensaje debe nombrar ambas Zebras para que el operador sepa
    # que se agotaron las dos rutas y no sólo una.
    assert "9100" in msg and "9101" in msg
    assert ROLE_PRIMARY in msg and ROLE_BACKUP in msg


def test_print_label_no_lanza_con_ambas_caidas(net):
    net(failures={
        PRIMARY: asyncio.TimeoutError(),
        BACKUP:  asyncio.TimeoutError(),
    })
    svc = make_service()

    ok, msg = asyncio.run(svc.print_label(make_order()))

    assert ok is False and msg != ""


# ── No regresión: sin respaldo configurado ───────────────────────

def test_sin_respaldo_un_solo_intento(net):
    fake = net(failures={PRIMARY: asyncio.TimeoutError()})
    svc = make_service(backup=False)

    ok, msg = asyncio.run(svc.send_raw_zpl("^XA^XZ"))

    assert ok is False
    assert fake.attempts == [PRIMARY], "sin respaldo no debe haber reintento"
    assert svc.has_backup is False
    # Mensaje de un solo destino: no debe hablar de "ninguna Zebra".
    assert "9101" not in msg


def test_sin_respaldo_camino_feliz_intacto(net):
    fake = net()
    svc = make_service(backup=False)

    ok, msg = asyncio.run(svc.send_raw_zpl("^XA^XZ"))

    assert ok and msg == ""
    assert fake.attempts == [PRIMARY]


# ── Sticky failover: pedido con nota ─────────────────────────────

def test_etiqueta_y_nota_salen_de_la_misma_zebra(net):
    """
    Un pedido con nota genera DOS etiquetas ZPL. Si la principal está caída,
    ambas deben salir de la de respaldo: repartirlas entre dos Zebras dejaría
    al operador con media etiqueta en cada equipo.
    """
    fake = net(failures={PRIMARY: asyncio.TimeoutError()})
    svc = make_service()

    ok, msg = asyncio.run(svc.print_label(make_order(note="Dejar en conserjería")))

    assert ok and msg == ""
    assert len(fake.payloads(BACKUP)) == 2, "principal + nota en el respaldo"
    assert fake.payloads(PRIMARY) == []
    # La principal se intenta una sola vez: tras caer, el respaldo queda fijo.
    assert fake.attempts == [PRIMARY, BACKUP, BACKUP]


# ── Diagnóstico de conectividad ──────────────────────────────────

def test_test_targets_reporta_estado_de_cada_zebra(net):
    net(failures={PRIMARY: asyncio.TimeoutError()})
    svc = make_service()

    targets = asyncio.run(svc.test_targets())

    assert [t["role"] for t in targets] == [ROLE_PRIMARY, ROLE_BACKUP]
    assert targets[0]["reachable"] is False
    assert targets[1]["reachable"] is True
    assert targets[1]["port"] == 9101


def test_test_connection_ok_si_responde_el_respaldo(net):
    """Con la principal caída el diagnóstico sigue siendo alcanzable."""
    net(failures={PRIMARY: asyncio.TimeoutError()})
    svc = make_service()

    ok, msg = asyncio.run(svc.test_connection())

    assert ok is True
    assert ROLE_BACKUP in msg


def test_test_connection_falla_si_no_responde_ninguna(net):
    net(failures={
        PRIMARY: asyncio.TimeoutError(),
        BACKUP:  asyncio.TimeoutError(),
    })
    svc = make_service()

    ok, msg = asyncio.run(svc.test_connection())

    assert ok is False
    assert "9100" in msg and "9101" in msg


# ── Vocabulario de negocio (AGENTS.md §2.4) ──────────────────────

def test_contrato_json_de_printer_test_no_rompe_al_frontend(net):
    """
    `static/js/api.js` sólo lee `reachable` y `message` de /api/printer/test.
    El failover puede AGREGAR claves, nunca quitar esas dos ni cambiar su tipo.
    """
    from fastapi.testclient import TestClient

    import main

    net(failures={PRIMARY: asyncio.TimeoutError()})
    main.app.dependency_overrides[main.get_current_user] = lambda: {"username": "test"}
    original_factory = main.build_zpl_service
    monkeypatched = make_service()
    main.build_zpl_service = lambda: monkeypatched
    try:
        # Sin `with`: no se dispara el lifespan (init de BD, validación de
        # SECRET_KEY). Aquí sólo interesa la forma del JSON del endpoint.
        body = TestClient(main.app).get("/api/printer/test").json()
    finally:
        main.build_zpl_service = original_factory
        main.app.dependency_overrides.clear()

    # Claves históricas, intactas.
    assert body["reachable"] is True
    assert isinstance(body["message"], str)
    # Detalle nuevo: la principal cayó, respondió el respaldo.
    assert body["failover"] is True
    assert body["active_printer"]["role"] == ROLE_BACKUP
    assert body["active_printer"]["port"] == 9101
    assert [t["role"] for t in body["targets"]] == [ROLE_PRIMARY, ROLE_BACKUP]


def test_mensajes_usan_vocabulario_obligatorio(net):
    net(failures={
        PRIMARY: asyncio.TimeoutError(),
        BACKUP:  asyncio.TimeoutError(),
    })
    svc = make_service()

    _, msg = asyncio.run(svc.send_raw_zpl("^XA^XZ"))

    assert "Zebra" in msg
    for prohibido in ("impresora genérica", "sticker", "etiqueta de papel"):
        assert prohibido not in msg.lower()
