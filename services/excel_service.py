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


def _get_courier(ciudad: str, rm_comunas: frozenset[str]) -> str:
    return "Rocket" if _normalize_text(ciudad.strip()) in rm_comunas else "Chilexpress"


def _qty_chocolate(order: NormalizedOrder) -> int:
    return sum(item.quantity for item in order.items if item.sku in CHOCOLATE_SKUS)


def _qty_cafe(order: NormalizedOrder) -> int:
    return sum(item.quantity for item in order.items if item.sku not in CHOCOLATE_SKUS)


def _qty_by_name(order: NormalizedOrder, keyword: str) -> int:
    return sum(
        item.quantity for item in order.items if keyword in item.name.lower()
    )


# ── Función principal ─────────────────────────────────────────────────────────

def generate_excel(orders: List[NormalizedOrder]) -> bytes:
    """
    Genera un archivo .xlsx a partir de una lista de pedidos normalizados.
    Devuelve los bytes del archivo listo para enviar como respuesta HTTP.
    """
    rm_comunas = _load_rm_comunas()
    rows = []

    for order in orders:
        ciudad = order.shipping.city
        completed_at_str = (
            order.completed_at.isoformat() if order.completed_at else ""
        )
        # Timestamp exacto de cuándo se imprimió la etiqueta
        label_printed_at_str = (
            order.label_printed_at.isoformat() if order.label_printed_at else ""
        )

        rows.append({
            "Página":           order.source.value,
            "Cliente":          order.shipping.full_name,
            "Dirección":        order.shipping.full_address,
            "Comuna":           ciudad,
            "Factura":          order.platform_meta.get("invoice", ""),
            "N° Pedido":        order.id,
            "Valor":            order.total,
            "Seguimiento":      order.platform_meta.get("tracking_number", ""),
            "Despacho":         _get_courier(ciudad, rm_comunas),
            "Cobertor":         _qty_by_name(order, "cobertor"),
            "Detergente":       _qty_by_name(order, "detergente"),
            "Chocolate":        _qty_chocolate(order),
            "Cafe":             _qty_cafe(order),
            "Etiqueta_Impresa": label_printed_at_str,
            "Completado":       completed_at_str,
        })

    df = pd.DataFrame(rows, columns=[
        "Página", "Cliente", "Dirección", "Comuna", "Factura",
        "N° Pedido", "Valor", "Seguimiento", "Despacho",
        "Cobertor", "Detergente", "Chocolate", "Cafe",
        "Etiqueta_Impresa", "Completado",
    ])

    buffer = BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl")
    buffer.seek(0)
    return buffer.getvalue()
