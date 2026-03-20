"""
Servicio de desglose de Packs.

Enriquece los pedidos con información detallada sobre el contenido de los Packs
basándose en el catálogo de skus.json.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models.order import NormalizedOrder, OrderItem, OrderSource, PackItem

_SKU_JSON_PATH = Path(__file__).parent.parent / "data" / "skus.json"


@lru_cache(maxsize=1)
def _load_full_catalog() -> Dict:
    """
    Carga el catálogo completo desde skus.json.

    El resultado se cachea en memoria para evitar lecturas repetidas del disco.
    """
    with open(_SKU_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("catalogo", {})


def get_product_by_sku(sku: str) -> Optional[Tuple[dict, str]]:
    """
    Búsqueda unificada de productos por SKU.

    Busca en:
    - packs_prearmados
    - mix_personalizables
    - otros_productos

    Busca tanto en sku_principal como en skus_alias.

    Returns:
        Tupla (producto_dict, categoria) si se encuentra, None si no existe.
        categoria puede ser: "packs_prearmados", "mix_personalizables", "otros_productos"
    """
    catalog = _load_full_catalog()

    # Buscar en las tres categorías
    for categoria in ["packs_prearmados", "mix_personalizables", "otros_productos"]:
        for producto in catalog.get(categoria, []):
            # Buscar en sku_principal
            if producto.get("sku_principal") == sku:
                return (producto, categoria)

            # Buscar en skus_alias
            if sku in producto.get("skus_alias", []):
                return (producto, categoria)

    return None


def _extract_mix_from_meta_data(meta_data: List[Dict]) -> List[PackItem]:
    """
    Extrae el contenido personalizado de un Mix desde los meta_data de WooCommerce.

    Los meta_data de WooCommerce contienen la selección del cliente en formato:
    [
        {"key": "sabor_1", "value": "Milano x10"},
        {"key": "sabor_2", "value": "Roma x10"},
        ...
    ]

    Returns:
        Lista de PackItems con la selección del cliente.
    """
    pack_breakdown = []

    for meta in meta_data:
        key = meta.get("key", "")
        value = meta.get("value", "")

        # Buscar claves que empiecen con "sabor_" o contengan información de selección
        if key.startswith("sabor_") or key.startswith("caja_"):
            # Parsear el formato "Milano x10" o "Milano - 10 cápsulas"
            # Intentar diferentes formatos comunes
            sabor = ""
            cajas = 0

            if " x" in value.lower():
                # Formato: "Milano x10"
                parts = value.split(" x")
                if len(parts) == 2:
                    sabor = parts[0].strip()
                    try:
                        cajas = int(parts[1].strip()) // 10
                    except ValueError:
                        pass
            elif " - " in value and "cápsula" in value.lower():
                # Formato: "Milano - 10 cápsulas"
                parts = value.split(" - ")
                if len(parts) == 2:
                    sabor = parts[0].strip()
                    try:
                        num_str = parts[1].split()[0]
                        cajas = int(num_str) // 10
                    except (ValueError, IndexError):
                        pass

            if sabor and cajas > 0:
                pack_breakdown.append(PackItem(
                    sku_unitario="",  # No tenemos el SKU unitario desde meta_data
                    sabor=sabor,
                    cajas_de_10=cajas,
                ))

    return pack_breakdown


def _extract_mix_from_customer_note(customer_note: str) -> List[PackItem]:
    """
    Extrae el contenido de un Mix personalizable desde la nota del cliente (MercadoLibre).

    El cliente envía un mensaje con el formato:
    - "Milano x20, Roma x10, Colombia x30"
    - "Milano: 2 cajas, Roma: 1 caja"
    - u otros formatos similares

    Returns:
        Lista de PackItems parseada desde la nota.
    """
    pack_breakdown = []

    if not customer_note:
        return pack_breakdown

    # Dividir por comas o saltos de línea
    lines = customer_note.replace("\n", ",").split(",")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        sabor = ""
        cajas = 0

        # Intentar formato "Milano x20"
        if " x" in line.lower():
            parts = line.split(" x")
            if len(parts) == 2:
                sabor = parts[0].strip()
                try:
                    num_capsulas = int(parts[1].strip())
                    cajas = num_capsulas // 10
                except ValueError:
                    pass
        # Intentar formato "Milano: 2 cajas"
        elif ":" in line and "caja" in line.lower():
            parts = line.split(":")
            if len(parts) == 2:
                sabor = parts[0].strip()
                try:
                    # Extraer el primer número encontrado
                    num_str = "".join(c for c in parts[1] if c.isdigit())
                    if num_str:
                        cajas = int(num_str)
                except ValueError:
                    pass

        if sabor and cajas > 0:
            pack_breakdown.append(PackItem(
                sku_unitario="",
                sabor=sabor,
                cajas_de_10=cajas,
            ))

    return pack_breakdown


def enrich_order_with_pack_info(order: NormalizedOrder) -> NormalizedOrder:
    """
    Enriquece un pedido con información de desglose de Packs y Mixes.

    Lógica por tienda y tipo de producto:

    WooCommerce:
    - Packs prearmados: usar contenido del JSON
    - Mixes personalizables: extraer de meta_data del line_item

    MercadoLibre:
    - Packs prearmados: usar contenido del JSON
    - Mixes personalizables: extraer de customer_note del pedido

    Esta función modifica los items del pedido in-place.
    """
    for item in order.items:
        # Buscar el producto en el catálogo usando búsqueda unificada
        resultado = get_product_by_sku(item.sku)

        if resultado is None:
            # Verificar si el nombre contiene "Pack" o "Mix" pero no está en el catálogo
            nombre_lower = item.name.lower()
            if "pack" in nombre_lower or "mix" in nombre_lower:
                item.catalog_warning = True
            continue

        producto, categoria = resultado
        es_personalizable = producto.get("es_personalizable", False)

        # ── PACKS PREARMADOS ──
        if categoria == "packs_prearmados":
            item.is_pack = True

            # Los packs prearmados SIEMPRE usan el contenido del JSON (tanto WC como ML)
            pack_breakdown = []
            for content_item in producto.get("contenido", []):
                pack_breakdown.append(PackItem(
                    sku_unitario=content_item.get("sku_unitario", ""),
                    sabor=content_item.get("sabor", ""),
                    cajas_de_10=content_item.get("cajas_de_10", 0) * item.quantity,
                ))
            item.pack_breakdown = pack_breakdown

        # ── MIXES PERSONALIZABLES ──
        elif categoria == "mix_personalizables":
            item.is_pack = True

            # Lógica diferente según la tienda
            if order.source == OrderSource.WOOCOMMERCE:
                # WooCommerce: extraer de meta_data del line_item
                if item.raw_meta_data:
                    pack_breakdown = _extract_mix_from_meta_data(item.raw_meta_data)
                    if pack_breakdown:
                        # Multiplicar por quantity
                        for pack_item in pack_breakdown:
                            pack_item.cajas_de_10 *= item.quantity
                        item.pack_breakdown = pack_breakdown
                    else:
                        # No se pudo extraer, marcar advertencia
                        item.catalog_warning = True
                else:
                    item.catalog_warning = True

            elif order.source == OrderSource.MERCADOLIBRE:
                # MercadoLibre: extraer de customer_note del pedido
                pack_breakdown = _extract_mix_from_customer_note(order.customer_note)
                if pack_breakdown:
                    # Multiplicar por quantity
                    for pack_item in pack_breakdown:
                        pack_item.cajas_de_10 *= item.quantity
                    item.pack_breakdown = pack_breakdown
                else:
                    # No se pudo extraer, marcar advertencia
                    item.catalog_warning = True

        # ── OTROS PRODUCTOS ──
        # No necesitan procesamiento especial, solo marcar que no son packs
        # (ya tienen is_pack=False por defecto)

    return order


def enrich_orders_with_pack_info(orders: List[NormalizedOrder]) -> List[NormalizedOrder]:
    """
    Enriquece una lista de pedidos con información de desglose de Packs.
    """
    return [enrich_order_with_pack_info(order) for order in orders]
