"""
Interfaz abstracta para proveedores de pedidos.
Cualquier nueva plataforma (Mercado Libre, Shopify, etc.) debe
implementar esta clase base — la UI y los servicios de impresión
nunca conocen la plataforma concreta.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional

from models.order import NormalizedOrder, OrderStatus


class BaseOrderProvider(ABC):
    """Contrato que toda integración de plataforma debe cumplir."""

    source_name: str  # Identificador único de la plataforma

    # ── Lectura ──────────────────────────────────────────────────

    @abstractmethod
    async def get_pending_orders(self) -> List[NormalizedOrder]:
        """
        Devuelve los pedidos en estado 'processing' listos para preparar.
        Implementaciones deben manejar errores de red internamente y
        propagar solo excepciones fatales.
        """

    @abstractmethod
    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]:
        """Devuelve un pedido por ID, o None si no existe."""

    # ── Escritura ────────────────────────────────────────────────

    @abstractmethod
    async def update_order_status(
        self, order_id: str, status: OrderStatus
    ) -> bool:
        """
        Actualiza el estado del pedido en la plataforma remota.
        Retorna True si tuvo éxito, False en caso contrario.
        """

    # ── Normalización ────────────────────────────────────────────

    @abstractmethod
    def normalize(self, raw: dict) -> NormalizedOrder:
        """
        Transforma el objeto crudo de la plataforma al modelo interno.
        Siempre se llama antes de devolver datos a la capa de negocio.
        """
