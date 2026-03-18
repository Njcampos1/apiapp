"""
Servicio de exportación de pedidos a formato Excel (.xlsx).

Reglas de negocio:
  - Courier: 'Rocket' si la comuna está en data/rm.json, 'Chilexpress' en caso contrario.
  - Chocolate: items cuyo SKU pertenece a CHOCOLATE_SKUS.
  - Cafe: todos los demás SKUs.
  - Cobertor / Detergente: items cuyo nombre contiene la palabra (case-insensitive).
"""
from __future__ import annotations

import json
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import List

import pandas as pd

from models.order import NormalizedOrder

# ── Constantes ────────────────────────────────────────────────────────────────

CHOCOLATE_SKUS: frozenset[str] = frozenset({
    "101000-1",
    "101000-1-1",
    "101000-1-1-1",
    "101000-1-2",
    "101000-1-2-1",
    "101000-1-2-1-1",
    "103001-1",
    "101000-1-3",
    "103001-2",
    "103001-2-1",
    "101000-1-1-1-1",
})

_RM_JSON_PATH = Path(__file__).parent.parent / "data" / "rm.json"
_SKU_JSON_PATH = Path(__file__).parent.parent / "data" / "skus.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """Quita diacríticos/acentos y convierte a minúsculas para comparación robusta."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _load_rm_comunas() -> frozenset[str]:
    """Devuelve el set de comunas RM normalizadas (sin tildes, minúsculas)."""
    with open(_RM_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return frozenset(_normalize_text(c.strip()) for c in data.get("comunas", []))


def _load_sku_data() -> tuple[dict[str, int], set[str]]:
    """
    Devuelve:
    - Diccionario de multiplicadores por SKU: {sku: cantidad_total_capsulas}
    - Set de SKUs unitarios (sku_unitario) que son componentes de packs
    """
    with open(_SKU_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    catalogo = data.get("catalogo", {})
    multipliers = {}
    unit_skus = set()

    # Procesar packs prearmados
    for pack in catalogo.get("packs_prearmados", []):
        sku = pack.get("sku")
        cantidad = pack.get("cantidad_total_capsulas")
        if sku and cantidad:
            multipliers[sku] = cantidad
        # Recolectar SKUs unitarios
        for item in pack.get("contenido", []):
            unit_sku = item.get("sku_unitario")
            if unit_sku:
                unit_skus.add(unit_sku)

    # Procesar mix personalizables
    for mix in catalogo.get("mix_personalizables", []):
        sku = mix.get("sku")
        cantidad = mix.get("cantidad_total_capsulas")
        if sku and cantidad:
            multipliers[sku] = cantidad

    return multipliers, unit_skus


def _get_courier(ciudad: str, source: str, rm_comunas: frozenset[str]) -> str:
    """
    Determina el courier según la ciudad y el origen del pedido.
    - WooCommerce: RM → 'Rocket' | Región → 'Chilexpress'
    - Mercado Libre: RM → 'Rocket' | Región → 'Mercado'
    """
    is_rm = _normalize_text(ciudad.strip()) in rm_comunas

    if source.lower() == "woocommerce":
        return "Rocket" if is_rm else "Chilexpress"
    else:  # Mercado Libre
        return "Rocket" if is_rm else "Mercado"


def _qty_chocolate(
    order: NormalizedOrder,
    sku_multipliers: dict[str, int],
    unit_skus: set[str]
) -> int:
    """
    Calcula la cantidad total de unidades de chocolate.

    Lógica anti-doble conteo:
    - Si el item es un pack (SKU en multipliers), usa quantity * multiplicador
    - Si el item es un SKU unitario (componente de pack), lo ignora
    - Solo cuenta items que no sean componentes de packs
    """
    total = 0
    for item in order.items:
        if item.sku not in CHOCOLATE_SKUS:
            continue

        # Si es un pack, usar multiplicador
        if item.sku in sku_multipliers:
            total += item.quantity * sku_multipliers[item.sku]
        # Si es un SKU unitario (parte de un pack), ignorarlo
        elif item.sku in unit_skus:
            continue
        # Items adicionales que no son packs ni componentes
        else:
            total += item.quantity
    return total


def _qty_cafe(
    order: NormalizedOrder,
    sku_multipliers: dict[str, int],
    unit_skus: set[str]
) -> int:
    """
    Calcula la cantidad total de unidades de café.

    Lógica anti-doble conteo:
    - Si el item es un pack (SKU en multipliers), usa quantity * multiplicador
    - Si el item es un SKU unitario (componente de pack), lo ignora
    - Solo cuenta items que no sean componentes de packs
    """
    total = 0
    for item in order.items:
        if item.sku in CHOCOLATE_SKUS:
            continue

        # Si es un pack, usar multiplicador
        if item.sku in sku_multipliers:
            total += item.quantity * sku_multipliers[item.sku]
        # Si es un SKU unitario (parte de un pack), ignorarlo
        elif item.sku in unit_skus:
            continue
        # Items adicionales que no son packs ni componentes
        else:
            total += item.quantity
    return total


def _qty_by_name(order: NormalizedOrder, keyword: str) -> int:
    return sum(
        item.quantity for item in order.items if keyword in item.name.lower()
    )


# ── Función principal ─────────────────────────────────────────────────────────

def generate_excel(orders: List[NormalizedOrder]) -> bytes:
    """
    Genera un archivo .xlsx a partir de una lista de pedidos normalizados.
    Devuelve los bytes del archivo listo para enviar como respuesta HTTP.

    NOTA: Excluye pedidos Full (fulfillment) ya que no se preparan en bodega.
    Los pedidos se ordenan por fecha de impresión de etiqueta (label_printed_at).
    """
    rm_comunas = _load_rm_comunas()
    sku_multipliers, unit_skus = _load_sku_data()
    rows = []

    for order in orders:
        # FILTRO: Excluir pedidos Full completamente del Excel
        logistic_type = order.platform_meta.get("logistic_type", "")
        logistic_label = order.platform_meta.get("logistic_label", "")

        if logistic_type == "fulfillment" or logistic_label == "Full":
            continue

        ciudad = order.shipping.city

        # Usamos prioritariamente label_printed_at, fallback a completed_at
        fecha_etiqueta_str = ""
        if order.label_printed_at:
            fecha_etiqueta_str = order.label_printed_at.isoformat()
        elif order.completed_at:
            fecha_etiqueta_str = order.completed_at.isoformat()

        rows.append({
            "Página":           order.source.value,
            "Cliente":          order.shipping.full_name,
            "Dirección":        order.shipping.full_address,
            "Comuna":           ciudad,
            "Factura":          order.platform_meta.get("invoice", ""),
            "N° Pedido":        order.id,
            "Valor":            order.total,
            "Seguimiento":      order.platform_meta.get("tracking_number", ""),
            "Despacho":         _get_courier(ciudad, order.source.value, rm_comunas),
            "Cobertor":         _qty_by_name(order, "cobertor"),
            "Detergente":       _qty_by_name(order, "detergente"),
            "Chocolate":        _qty_chocolate(order, sku_multipliers, unit_skus),
            "Cafe":             _qty_cafe(order, sku_multipliers, unit_skus),
            "Fecha_Etiqueta":   fecha_etiqueta_str,
        })

    df = pd.DataFrame(rows, columns=[
        "Página", "Cliente", "Dirección", "Comuna", "Factura",
        "N° Pedido", "Valor", "Seguimiento", "Despacho",
        "Cobertor", "Detergente", "Chocolate", "Cafe",
        "Fecha_Etiqueta",
    ])

    # Ordenar por fecha de etiqueta (de más antiguo a más reciente)
    # Los pedidos sin fecha irán al final
    df["_sort_key"] = pd.to_datetime(df["Fecha_Etiqueta"], errors="coerce")
    df = df.sort_values("_sort_key", na_position="last")
    df = df.drop(columns=["_sort_key"])

    buffer = BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl")
    buffer.seek(0)
    return buffer.getvalue()
