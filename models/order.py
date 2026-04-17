"""
Modelo normalizado de Pedido.
Toda fuente (WooCommerce, Mercado Libre, etc.) debe transformar
sus datos crudos a esta estructura antes de llegar a la UI o
a los servicios de impresión.
"""
from __future__ import annotations
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime, timezone


class OrderStatus(str, Enum):
    PROCESSING  = "processing"   # Recibido desde la plataforma
    PREPARING   = "preparing"    # Picking iniciado (PDF generado)
    LABELED     = "labeled"      # Etiqueta impresa
    COMPLETED   = "completed"    # Completado en la plataforma
    ERROR       = "error"        # Error en algún paso


class OrderSource(str, Enum):
    WOOCOMMERCE  = "woocommerce"
    MERCADOLIBRE = "mercadolibre"
    MANUAL       = "manual"


class PackItem(BaseModel):
    """Detalle de un componente dentro de un Pack"""
    sku_unitario: str
    sabor: str
    cajas_de_10: int


class OrderItem(BaseModel):
    sku:      str
    name:     str
    quantity: int
    price:    float
    # Información de desglose para Packs
    is_pack: bool = False
    pack_breakdown: Optional[List[PackItem]] = None
    # Alerta si el producto tiene "Pack" en el nombre pero no está en el catálogo
    catalog_warning: bool = False
    # Meta_data crudo del line_item (para extraer selección de mixes)
    raw_meta_data: Optional[List[Dict[str, Any]]] = None


class ShippingAddress(BaseModel):
    first_name: str = ""
    last_name:  str = ""
    address_1:  str = ""
    address_2:  str = ""
    city:       str = ""
    state:      str = ""
    postcode:   str = ""
    country:    str = ""
    phone:      str = ""
    email:      str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def full_address(self) -> str:
        parts = [self.address_1]
        if self.address_2:
            parts.append(self.address_2)
        return ", ".join(parts)


class NormalizedOrder(BaseModel):
    """Objeto interno estandarizado. Nunca debe contener lógica de plataforma."""
    id:               str
    source:           OrderSource
    status:           OrderStatus
    customer_note:    str = ""
    shipping:         ShippingAddress
    items:            List[OrderItem]
    total:            float
    currency:         str = "CLP"
    created_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at:     Optional[datetime] = None
    label_printed_at: Optional[datetime] = None  # Timestamp exacto de impresión de etiqueta
    # Metadatos opacos para round-trip a la plataforma original
    platform_meta:    Dict[str, Any] = Field(default_factory=dict)

    @property
    def display_id(self) -> str:
        if self.source == OrderSource.MERCADOLIBRE:
            shipping_id = self.platform_meta.get("shipping_id")
            if shipping_id is not None and str(shipping_id).strip():
                return str(shipping_id)
        return str(self.id)

    @property
    def item_count(self) -> int:
        return sum(i.quantity for i in self.items)
