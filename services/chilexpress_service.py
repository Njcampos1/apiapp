"""
Servicio de exportación de pedidos a formato CSV para Chilexpress.

Genera un archivo CSV compatible con el sistema de carga masiva de Chilexpress
con todos los campos requeridos según sus especificaciones.
"""
from __future__ import annotations

import csv
import re
import logging
from io import StringIO
from typing import List, Tuple

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# Comunas de región metropolitana (normalized)
RM_STATES = {"rm", "metropolitana", "región metropolitana", "region metropolitana"}


def _normalize_state(state: str) -> str:
    """Normaliza el nombre de la región para comparación."""
    return state.strip().lower()


def _is_regional_order(order: NormalizedOrder) -> bool:
    """
    Determina si un pedido es de región (no RM).
    Filtra pedidos con state en ["RM", "METROPOLITANA", etc.].
    """
    state_normalized = _normalize_state(order.shipping.state)
    return state_normalized not in RM_STATES


def _is_woocommerce_regional(order: NormalizedOrder) -> bool:
    """
    Determina si un pedido debe incluirse en el CSV de Chilexpress.

    Requisitos:
    - Debe ser de WooCommerce (MercadoLibre NO va en esta planilla)
    - Debe ser de región (NO RM)
    """
    return (
        order.source.value == "woocommerce" and
        _is_regional_order(order)
    )


def _parse_address(address: str) -> Tuple[str, str]:
    """
    Extrae calle y número de una dirección chilena.

    Formatos comunes:
    - "Av. Los Leones 123" -> ("Av. Los Leones", "123")
    - "Calle Principal 456-A" -> ("Calle Principal", "456-A")
    - "Los Boldos 789, Depto 12" -> ("Los Boldos", "789")
    - "Pasaje sin número" -> ("Pasaje sin número", "S/N")

    Returns:
        Tuple[calle, numero]
    """
    if not address:
        return ("", "S/N")

    # Patrón: captura todo antes del último número, luego el número con posible letra
    # Ejemplo: "Av. Los Leones 123-A" -> grupos: ("Av. Los Leones", "123-A")
    pattern = r'^(.+?)\s+(\d+[-\w]*)(?:\s|,|$)'
    match = re.search(pattern, address.strip())

    if match:
        calle = match.group(1).strip()
        numero = match.group(2).strip()
        return (calle, numero)

    # Si no hay número reconocible, toda la dirección es la calle
    return (address.strip(), "S/N")


def generate_chilexpress_csv(orders: List[NormalizedOrder]) -> bytes:
    """
    Genera un archivo CSV para carga masiva en Chilexpress.

    Solo incluye pedidos de WooCommerce donde shipping.state NO es RM/METROPOLITANA.
    Los pedidos de MercadoLibre NO se incluyen en esta planilla.

    Args:
        orders: Lista de pedidos normalizados (usualmente de un manifest cerrado)

    Returns:
        Bytes del archivo CSV codificado en UTF-8 con BOM (compatible Excel)
    """
    # Filtrar solo pedidos de WooCommerce regionales
    regional_orders = [o for o in orders if _is_woocommerce_regional(o)]

    logger.info("Generando CSV Chilexpress: %d pedidos WooCommerce regionales de %d totales",
                len(regional_orders), len(orders))

    # Definir columnas según especificación Chilexpress
    columns = [
        "PRODUCTO",
        "SERVICIO",
        "DESTINO_COMUNA",
        "PESO",
        "DESTINATARIO",
        "CALLE",
        "NUMERO",
        "COMPLEMENTO_DIRECCION",
        "REFERENCIA",
        "ALTO",
        "ANCHO",
        "LARGO",
        "CARGOEMPRESA",
        "MONTO_COBRO_COD",
        "VALOR_DECLARADO_PRODUCTO",
        "EMAIL",
        "CELULAR",
        "TIPO_DE_DIRECCION",
        "INFOADICIONAL",
        "AGRAGRUPADA",
        "AGRRTOTALPIEZAS",
        "AGRPIEZANUMERO",
        "EMPRESA",
        "DESTINATARIOSECUNDARIO",
        "CONTENIDO_DECLARADO_PRODUCTO",
        "DESCRIPCION_CONTENIDO",
    ]

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator='\n')

    # Escribir encabezados
    writer.writeheader()

    # Escribir filas
    for order in regional_orders:
        calle, numero = _parse_address(order.shipping.address_1)

        row = {
            "PRODUCTO": "3",
            "SERVICIO": "3",
            "DESTINO_COMUNA": order.shipping.city,
            "PESO": "1",
            "DESTINATARIO": f"{order.shipping.first_name} {order.shipping.last_name}".strip(),
            "CALLE": calle,
            "NUMERO": numero,
            "COMPLEMENTO_DIRECCION": order.shipping.address_2,
            "REFERENCIA": order.id,
            "ALTO": "8",
            "ANCHO": "16",
            "LARGO": "40",
            "CARGOEMPRESA": "",
            "MONTO_COBRO_COD": "",
            "VALOR_DECLARADO_PRODUCTO": "",
            "EMAIL": order.shipping.email,
            "CELULAR": order.shipping.phone,
            "TIPO_DE_DIRECCION": "2",
            "INFOADICIONAL": "",
            "AGRAGRUPADA": "",
            "AGRRTOTALPIEZAS": "",
            "AGRPIEZANUMERO": "",
            "EMPRESA": "",
            "DESTINATARIOSECUNDARIO": "",
            "CONTENIDO_DECLARADO_PRODUCTO": "5",
            "DESCRIPCION_CONTENIDO": "cafe",
        }

        writer.writerow(row)

    # Convertir a bytes con BOM para compatibilidad con Excel
    csv_string = output.getvalue()
    return '\ufeff'.encode('utf-8') + csv_string.encode('utf-8')
