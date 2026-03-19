"""
Servicio de desglose de Packs.

Enriquece los pedidos con información detallada sobre el contenido de los Packs
basándose en el catálogo de skus.json.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from models.order import NormalizedOrder, OrderItem, PackItem

_SKU_JSON_PATH = Path(__file__).parent.parent / "data" / "skus.json"


@lru_cache(maxsize=1)
def _load_pack_catalog() -> Dict[str, dict]:
    """
    Carga el catálogo de Packs desde skus.json.
    Retorna un diccionario {sku: pack_info} para fácil búsqueda.

    El resultado se cachea en memoria para evitar lecturas repetidas del disco.
    """
    with open(_SKU_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    catalog = data.get("catalogo", {})
    pack_map = {}

    # Cargar packs prearmados
    for pack in catalog.get("packs_prearmados", []):
        sku = pack.get("sku")
        if sku:
            pack_map[sku] = {
                "nombre": pack.get("nombre", ""),
                "es_personalizable": pack.get("es_personalizable", False),
                "contenido": pack.get("contenido", []),
            }

    # Los mix_personalizables no tienen contenido predefinido,
    # así que no los incluimos en el pack_map

    return pack_map


def enrich_order_with_pack_info(order: NormalizedOrder) -> NormalizedOrder:
    """
    Enriquece un pedido con información de desglose de Packs.

    - Si un item es un Pack (su SKU está en packs_prearmados),
      marca is_pack=True y llena pack_breakdown con el contenido.
    - Si es un Mix o producto individual, lo deja sin modificar.
    - Si el nombre contiene "Pack" pero el SKU no está en el catálogo,
      marca catalog_warning=True para alertar al personal de bodega.

    Esta función modifica los items del pedido in-place.
    """
    pack_catalog = _load_pack_catalog()

    for item in order.items:
        if item.sku in pack_catalog:
            pack_info = pack_catalog[item.sku]
            item.is_pack = True

            # Convertir el contenido a PackItems
            # Multiplicar las cantidades por item.quantity para reflejar el total real
            pack_breakdown = []
            for content_item in pack_info["contenido"]:
                pack_breakdown.append(PackItem(
                    sku_unitario=content_item.get("sku_unitario", ""),
                    sabor=content_item.get("sabor", ""),
                    cajas_de_10=content_item.get("cajas_de_10", 0) * item.quantity,
                ))

            item.pack_breakdown = pack_breakdown
        else:
            # Verificar si el nombre contiene "Pack" pero no está en el catálogo
            if "pack" in item.name.lower() and item.sku not in pack_catalog:
                item.catalog_warning = True

    return order


def enrich_orders_with_pack_info(orders: List[NormalizedOrder]) -> List[NormalizedOrder]:
    """
    Enriquece una lista de pedidos con información de desglose de Packs.
    """
    return [enrich_order_with_pack_info(order) for order in orders]
