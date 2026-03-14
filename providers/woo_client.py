"""
Proveedor WooCommerce — implementa BaseOrderProvider.

Usa la WooCommerce REST API v3 con autenticación Basic (consumer key/secret).
Referencia: https://woocommerce.github.io/woocommerce-rest-api-docs/

Para agregar Mercado Libre, crea providers/meli_client.py implementando
BaseOrderProvider y regístralo en main.py dentro de build_providers().
"""
from __future__ import annotations

import logging
from typing import List, Optional
from datetime import datetime

import httpx

from models.order import (
    NormalizedOrder,
    OrderItem,
    OrderStatus,
    OrderSource,
    ShippingAddress,
)
from providers.base_provider import BaseOrderProvider

logger = logging.getLogger(__name__)

# Mapeo entre estados WooCommerce y estados internos
WOO_STATUS_MAP = {
    "pending":    OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "on-hold":    OrderStatus.PROCESSING,
    "completed":  OrderStatus.COMPLETED,
    "cancelled":  OrderStatus.ERROR,
    "refunded":   OrderStatus.ERROR,
    "failed":     OrderStatus.ERROR,
    "trash":      OrderStatus.ERROR,
}

# Estados internos → estados WooCommerce
INTERNAL_TO_WOO = {
    OrderStatus.PREPARING:  "processing",    # sigue en processing en WC
    OrderStatus.LABELED:    "processing",
    OrderStatus.COMPLETED:  "completed",
    OrderStatus.ERROR:      "on-hold",
}


class WooCommerceProvider(BaseOrderProvider):
    source_name = OrderSource.WOOCOMMERCE.value

    def __init__(self, url: str, key: str, secret: str) -> None:
        self._base = url.rstrip("/") + "/wp-json/wc/v3"
        self._auth = (key, secret)
        # httpx.AsyncClient reutilizable con timeout conservador
        self._client = httpx.AsyncClient(
            auth=self._auth,
            timeout=httpx.Timeout(15.0, connect=5.0),
            verify=True,   # Cambia a False solo en entornos de prueba sin TLS válido
        )

    # ── API interna ──────────────────────────────────────────────

    async def get_pending_orders(self) -> List[NormalizedOrder]:
        """Obtiene pedidos en estado 'processing' de WooCommerce (todas las páginas)."""
        normalized = []
        page = 1
        per_page = 50

        while True:
            try:
                resp = await self._client.get(
                    f"{self._base}/orders",
                    params={
                        "status":   "processing",
                        "per_page": per_page,
                        "page":     page,
                        "orderby":  "date",
                        "order":    "asc",
                        "after":    "2026-01-01T00:00:00Z",
                    },
                )
                resp.raise_for_status()
            except httpx.TimeoutException:
                logger.error("WooCommerce: timeout al obtener pedidos (página %d)", page)
                raise RuntimeError("WooCommerce no respondió a tiempo")
            except httpx.HTTPStatusError as e:
                logger.error("WooCommerce HTTP %s: %s", e.response.status_code, e.response.text)
                raise RuntimeError(f"WooCommerce error HTTP {e.response.status_code}")

            orders = resp.json()
            for raw in orders:
                try:
                    normalized.append(self.normalize(raw))
                except Exception as exc:
                    logger.warning("No se pudo normalizar pedido %s: %s", raw.get("id"), exc)

            if len(orders) < per_page:
                break
            page += 1

        logger.debug("WooCommerce: %d pedidos obtenidos en %d página(s)", len(normalized), page)
        return normalized

    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]:
        try:
            resp = await self._client.get(f"{self._base}/orders/{order_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self.normalize(resp.json())
        except httpx.TimeoutException:
            logger.error("WooCommerce: timeout al obtener pedido %s", order_id)
            raise RuntimeError("WooCommerce no respondió a tiempo")
        except httpx.HTTPStatusError as e:
            logger.error("WooCommerce HTTP %s al obtener pedido %s", e.response.status_code, order_id)
            raise RuntimeError(f"Error HTTP {e.response.status_code}")

    async def update_order_status(
        self, order_id: str, status: OrderStatus
    ) -> bool:
        woo_status = INTERNAL_TO_WOO.get(status)
        if woo_status is None:
            logger.warning("Estado %s no tiene mapeo a WooCommerce", status)
            return False

        payload: dict = {"status": woo_status}

        try:
            resp = await self._client.put(
                f"{self._base}/orders/{order_id}", json=payload
            )
            resp.raise_for_status()
            logger.info("Pedido %s → WooCommerce estado '%s' OK", order_id, woo_status)
            return True
        except httpx.TimeoutException:
            logger.error("WooCommerce: timeout al actualizar pedido %s", order_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.error(
                "WooCommerce HTTP %s al actualizar pedido %s: %s",
                e.response.status_code, order_id, e.response.text,
            )
            return False

    # ── Normalización ────────────────────────────────────────────

    def normalize(self, raw: dict) -> NormalizedOrder:
        """Transforma el payload crudo de WC al modelo interno."""
        ship = raw.get("shipping") or raw.get("billing") or {}
        billing = raw.get("billing") or {}

        shipping_addr = ShippingAddress(
            first_name=ship.get("first_name", ""),
            last_name=ship.get("last_name", ""),
            address_1=ship.get("address_1", ""),
            address_2=ship.get("address_2", ""),
            city=ship.get("city", ""),
            state=ship.get("state", ""),
            postcode=ship.get("postcode", ""),
            country=ship.get("country", ""),
            phone=billing.get("phone", ship.get("phone", "")),
        )

        items = [
            OrderItem(
                sku=li.get("sku") or str(li.get("product_id", "")),
                name=li.get("name", "Producto sin nombre"),
                quantity=int(li.get("quantity", 1)),
                price=float(li.get("price", 0)),
            )
            for li in raw.get("line_items", [])
        ]

        # Determinar estado: si hay meta_data de sistema interno, usarlo
        wc_status = raw.get("status", "processing")
        internal_status = WOO_STATUS_MAP.get(wc_status, OrderStatus.PROCESSING)

        # Buscar override de estado en meta_data (sincronización bidireccional)
        for meta in raw.get("meta_data", []):
            if meta.get("key") == "_upperapp_status":
                try:
                    internal_status = OrderStatus(meta["value"])
                except ValueError:
                    pass
                break

        created_raw = raw.get("date_created_gmt") or raw.get("date_created", "")
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            created_at = datetime.utcnow()

        return NormalizedOrder(
            id=str(raw["id"]),
            source=OrderSource.WOOCOMMERCE,
            status=internal_status,
            customer_note=raw.get("customer_note", ""),
            shipping=shipping_addr,
            items=items,
            total=float(raw.get("total", 0)),
            currency=raw.get("currency", "CLP"),
            created_at=created_at,
            platform_meta={
                "wc_status":  wc_status,
                "wc_number":  raw.get("number", raw["id"]),
                "payment_method_title": raw.get("payment_method_title", ""),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()
