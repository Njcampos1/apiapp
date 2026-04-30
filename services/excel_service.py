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
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Collection, List

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


@dataclass(frozen=True)
class SkuCatalogData:
    multipliers: dict[str, int] = field(default_factory=dict)
    unit_skus: frozenset[str] = field(default_factory=frozenset)
    non_cafe_meli_skus: frozenset[str] = field(default_factory=frozenset)
    chocolate_skus: frozenset[str] = field(default_factory=frozenset)
    detergente_35: dict[str, int] = field(default_factory=dict)
    detergente_60: dict[str, int] = field(default_factory=dict)
    detergente_mixed_skus: frozenset[str] = field(default_factory=frozenset)


@lru_cache(maxsize=1)
def _load_sku_data() -> SkuCatalogData:
    """
    Devuelve catálogo SKU normalizado. Cacheado con lru_cache(maxsize=1).
    Invalidar con: _load_sku_data.cache_clear().
    """
    multipliers: dict[str, int] = {}
    unit_skus: set[str] = set()
    non_cafe_meli_skus: set[str] = set()
    chocolate_skus: set[str] = set()
    detergente_35_multipliers: dict[str, int] = {}
    detergente_60_multipliers: dict[str, int] = {}
    detergente_mixed_skus: set[str] = set()

    try:
        with open(_SKU_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return SkuCatalogData(
            multipliers=multipliers,
            unit_skus=frozenset(unit_skus),
            non_cafe_meli_skus=frozenset(non_cafe_meli_skus),
            chocolate_skus=frozenset(CHOCOLATE_SKUS),
            detergente_35=dict(DETERGENTE_35_SKU_MULTIPLIERS),
            detergente_60=dict(DETERGENTE_60_SKU_MULTIPLIERS),
            detergente_mixed_skus=frozenset({DETERGENTE_MIXED_SKU}),
        )

    catalogo = data.get("catalogo", {})

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

    def _get_detergente_tipo(item_data: dict) -> str | None:
        tipo_raw = item_data.get("detergente_tipo")
        if isinstance(tipo_raw, str):
            tipo = _normalize_text(tipo_raw)
            if tipo in {"35", "60", "mixto", "mixed"}:
                return "mixto" if tipo in {"mixto", "mixed"} else tipo
        return None

    # SKUs de otros productos (detergentes, cobertores, chocolates, etc.)
    for item in catalogo.get("otros_productos", []):
        sku_principal = item.get("sku_principal")
        sku_aliases = item.get("skus_alias", [])
        canales = set(item.get("canales_venta", []))

        # Excluir de café solo en Mercado Libre
        if "mercadolibre" in canales:
            if sku_principal:
                non_cafe_meli_skus.add(str(sku_principal))
            for sku_alias in sku_aliases:
                if sku_alias:
                    non_cafe_meli_skus.add(str(sku_alias))

        # Chocolate por categoría
        categoria = item.get("categoria")
        if isinstance(categoria, str) and _normalize_text(categoria) == "chocolate":
            if sku_principal:
                chocolate_skus.add(str(sku_principal))
            for sku_alias in sku_aliases:
                if sku_alias:
                    chocolate_skus.add(str(sku_alias))

        # Detergentes por multiplicador_unidades
        multiplicador = item.get("multiplicador_unidades")
        if isinstance(multiplicador, int) and multiplicador > 0:
            tipo_detergente = _get_detergente_tipo(item)
            if tipo_detergente == "35":
                if sku_principal:
                    detergente_35_multipliers[str(sku_principal)] = multiplicador
                for sku_alias in sku_aliases:
                    if sku_alias:
                        detergente_35_multipliers[str(sku_alias)] = multiplicador
            elif tipo_detergente == "60":
                if sku_principal:
                    detergente_60_multipliers[str(sku_principal)] = multiplicador
                for sku_alias in sku_aliases:
                    if sku_alias:
                        detergente_60_multipliers[str(sku_alias)] = multiplicador
            elif tipo_detergente == "mixto":
                if sku_principal:
                    detergente_mixed_skus.add(str(sku_principal))
                for sku_alias in sku_aliases:
                    if sku_alias:
                        detergente_mixed_skus.add(str(sku_alias))

    if not chocolate_skus:
        chocolate_skus = set(CHOCOLATE_SKUS)

    if not detergente_35_multipliers and not detergente_60_multipliers and not detergente_mixed_skus:
        detergente_35_multipliers = dict(DETERGENTE_35_SKU_MULTIPLIERS)
        detergente_60_multipliers = dict(DETERGENTE_60_SKU_MULTIPLIERS)
        detergente_mixed_skus = {DETERGENTE_MIXED_SKU}
    elif not detergente_mixed_skus:
        detergente_mixed_skus = {DETERGENTE_MIXED_SKU}

    return SkuCatalogData(
        multipliers=multipliers,
        unit_skus=frozenset(unit_skus),
        non_cafe_meli_skus=frozenset(non_cafe_meli_skus),
        chocolate_skus=frozenset(chocolate_skus),
        detergente_35=detergente_35_multipliers,
        detergente_60=detergente_60_multipliers,
        detergente_mixed_skus=frozenset(detergente_mixed_skus),
    )


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
    unit_skus: Collection[str],
    chocolate_skus: Collection[str],
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
        if item.sku not in chocolate_skus:
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
    unit_skus: Collection[str],
    non_cafe_meli_skus: Collection[str],
    chocolate_skus: Collection[str],
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
        if item.sku in chocolate_skus:
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


def _qty_detergentes(
    order: NormalizedOrder,
    detergente_35_multipliers: dict[str, int],
    detergente_60_multipliers: dict[str, int],
    detergente_mixed_skus: Collection[str],
) -> tuple[int, int]:
    """
    Devuelve (det60, det35) según SKUs de detergente.

    Reglas:
    - 35: 203193→1, 203195→2, 203196→4 por unidad.
    - 60: 203192→1, 203194→2, 203198→3 por unidad.
    - Mixto 203197: suma 1 a det60 y 1 a det35 por unidad.

    """
    det60 = 0
    det35 = 0

    for item in order.items:
        sku = item.sku
        qty = item.quantity

        if sku in detergente_35_multipliers:
            det35 += qty * detergente_35_multipliers[sku]
            continue

        if sku in detergente_60_multipliers:
            det60 += qty * detergente_60_multipliers[sku]
            continue

        if sku in detergente_mixed_skus:
            det60 += qty
            det35 += qty

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
    catalog = _load_sku_data()
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

        det60, det35 = _qty_detergentes(
            order,
            catalog.detergente_35,
            catalog.detergente_60,
            catalog.detergente_mixed_skus,
        )

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
            "Chocolate":        _qty_chocolate(order, catalog.multipliers, catalog.unit_skus, catalog.chocolate_skus),
            "Cafe":             _qty_cafe(
                order,
                catalog.multipliers,
                catalog.unit_skus,
                catalog.non_cafe_meli_skus,
                catalog.chocolate_skus,
            ),
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
