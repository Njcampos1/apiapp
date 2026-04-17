"""
Servicio de exportación de pedidos a formato Excel (.xlsx).

Reglas de negocio:
  - Courier: 'Rocket' si la comuna está en data/rm.json, 'Chilexpress' en caso contrario.
  - Chocolate: items cuyo SKU pertenece a CHOCOLATE_SKUS.
    - Cafe: SKUs de café; para Mercado Libre excluye SKUs de 'otros_productos'
        (ej. detergentes/cobertores) y fallback por nombre.
    - Cobertor: items cuyo nombre contiene la palabra (case-insensitive).
    - Detergente 60 / 35: cálculo por SKU específico, con fallback por nombre.
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

DETERGENTE_35_SKU_MULTIPLIERS: dict[str, int] = {
    "203193": 1,
    "203195": 2,
    "203196": 4,
}

DETERGENTE_60_SKU_MULTIPLIERS: dict[str, int] = {
    "203192": 1,
    "203194": 2,
    "203198": 3,
}

DETERGENTE_MIXED_SKU = "203197"


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


def _load_sku_data() -> tuple[dict[str, int], set[str], set[str]]:
    """
    Devuelve:
    - Diccionario de multiplicadores por SKU: {sku: cantidad_total_capsulas}
    - Set de SKUs unitarios (sku_unitario) que son componentes de packs
    - Set de SKUs de Mercado Libre que NO deben contar como café
      (otros_productos: limpieza/hogar)
    """
    with open(_SKU_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    catalogo = data.get("catalogo", {})
    multipliers = {}
    unit_skus = set()
    non_cafe_meli_skus = set()

    # Procesar packs prearmados
    for pack in catalogo.get("packs_prearmados", []):
        sku_principal = pack.get("sku_principal")
        cantidad = pack.get("cantidad_total_capsulas")

        # Agregar sku_principal al diccionario
        if sku_principal and cantidad:
            multipliers[sku_principal] = cantidad

        # Agregar también los skus_alias
        for sku_alias in pack.get("skus_alias", []):
            if sku_alias and cantidad:
                multipliers[sku_alias] = cantidad

        # Recolectar SKUs unitarios
        for item in pack.get("contenido", []):
            unit_sku = item.get("sku_unitario")
            if unit_sku:
                unit_skus.add(unit_sku)

    # Procesar mix personalizables
    for mix in catalogo.get("mix_personalizables", []):
        sku_principal = mix.get("sku_principal")
        cantidad = mix.get("cantidad_total_capsulas")

        # Agregar sku_principal al diccionario
        if sku_principal and cantidad:
            multipliers[sku_principal] = cantidad

        # Agregar también los skus_alias
        for sku_alias in mix.get("skus_alias", []):
            if sku_alias and cantidad:
                multipliers[sku_alias] = cantidad

    # SKUs de otros productos de Mercado Libre (detergentes, cobertores, etc.)
    for item in catalogo.get("otros_productos", []):
        canales = set(item.get("canales_venta", []))
        if "mercadolibre" not in canales:
            continue

        sku_principal = item.get("sku_principal")
        if sku_principal:
            non_cafe_meli_skus.add(str(sku_principal))

        for sku_alias in item.get("skus_alias", []):
            if sku_alias:
                non_cafe_meli_skus.add(str(sku_alias))

    return multipliers, unit_skus, non_cafe_meli_skus


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
    unit_skus: set[str],
    non_cafe_meli_skus: set[str],
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

        # Mercado Libre: excluir SKUs no-café (ej. limpieza/hogar).
        # Fallback por nombre para casos donde el SKU venga alterado.
        if order.source.value == "mercadolibre":
            normalized_name = _normalize_text(item.name or "")
            if (
                item.sku in non_cafe_meli_skus
                or "detergente" in normalized_name
                or "cobertor" in normalized_name
            ):
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


def _qty_detergentes(order: NormalizedOrder) -> tuple[int, int]:
    """
    Devuelve (det60, det35) según SKUs de detergente.

    Reglas:
    - 35: 203193→1, 203195→2, 203196→4 por unidad.
    - 60: 203192→1, 203194→2, 203198→3 por unidad.
    - Mixto 203197: suma 1 a det60 y 1 a det35 por unidad.

    Fallback:
    - Si no hay SKUs explícitos y existe detergente por nombre,
      asigna todo el total detectado a Detergente 60.
    """
    det60 = 0
    det35 = 0

    for item in order.items:
        sku = item.sku
        qty = item.quantity

        if sku in DETERGENTE_35_SKU_MULTIPLIERS:
            det35 += qty * DETERGENTE_35_SKU_MULTIPLIERS[sku]
            continue

        if sku in DETERGENTE_60_SKU_MULTIPLIERS:
            det60 += qty * DETERGENTE_60_SKU_MULTIPLIERS[sku]
            continue

        if sku == DETERGENTE_MIXED_SKU:
            det60 += qty
            det35 += qty

    detergente_fallback = _qty_by_name(order, "detergente")
    if det60 == 0 and det35 == 0 and detergente_fallback > 0:
        det60 = int(detergente_fallback)

    return int(det60), int(det35)


# ── Función principal ─────────────────────────────────────────────────────────

def generate_excel(orders: List[NormalizedOrder]) -> bytes:
    """
    Genera un archivo .xlsx a partir de una lista de pedidos normalizados.
    Devuelve los bytes del archivo listo para enviar como respuesta HTTP.

    NOTA: Excluye pedidos Full (fulfillment) ya que no se preparan en bodega.
    Los pedidos se ordenan por fecha de impresión de etiqueta (label_printed_at).
    """
    rm_comunas = _load_rm_comunas()
    sku_multipliers, unit_skus, non_cafe_meli_skus = _load_sku_data()
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

        det60, det35 = _qty_detergentes(order)

        order_number = str(order.id)
        if order.source.value == "mercadolibre":
            shipping_id = order.platform_meta.get("shipping_id")
            if shipping_id is not None and str(shipping_id).strip():
                order_number = str(shipping_id)
            order_number = order_number[-4:]

        rows.append({
            "Página":           order.source.value,
            "Cliente":          order.shipping.full_name,
            "Dirección":        order.shipping.full_address,
            "Comuna":           ciudad,
            "Factura":          order.platform_meta.get("invoice", ""),
            "N° Pedido":        order_number,
            "Valor":            order.total,
            "Seguimiento":      order.platform_meta.get("tracking_number", ""),
            "Despacho":         _get_courier(ciudad, order.source.value, rm_comunas),
            "Cobertor":         _qty_by_name(order, "cobertor"),
            "Detergente 60":    det60,
            "Detergente 35":    det35,
            "Chocolate":        _qty_chocolate(order, sku_multipliers, unit_skus),
            "Cafe":             _qty_cafe(order, sku_multipliers, unit_skus, non_cafe_meli_skus),
            "Fecha_Etiqueta":   fecha_etiqueta_str,
        })

    df = pd.DataFrame(rows, columns=[
        "Página", "Cliente", "Dirección", "Comuna", "Factura",
        "N° Pedido", "Valor", "Seguimiento", "Despacho",
        "Cobertor", "Detergente 60", "Detergente 35", "Chocolate", "Cafe",
        "Fecha_Etiqueta",
    ])

    # Ordenar por fecha de etiqueta (de más reciente a más antiguo)
    # Los pedidos sin fecha irán al final
    df["_sort_key"] = pd.to_datetime(df["Fecha_Etiqueta"], errors="coerce")
    df = df.sort_values("_sort_key", ascending=False, na_position="last")
    df = df.drop(columns=["_sort_key"])

    buffer = BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl")
    buffer.seek(0)
    return buffer.getvalue()
