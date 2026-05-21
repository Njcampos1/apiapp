"""
Servicio de exportación de pedidos a formato CSV para Chilexpress.

Genera un archivo CSV compatible con el sistema de carga masiva de Chilexpress
con todos los campos requeridos según sus especificaciones.
"""
from __future__ import annotations

import csv
import json
import re
import logging
import unicodedata
from io import StringIO
from pathlib import Path
from typing import List, Tuple

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

_RM_JSON_PATH = Path(__file__).parent.parent / "data" / "rm.json"
DECLARED_PRODUCT_VALUE = "50000"


def _normalize_text(text: str) -> str:
    """Quita diacríticos/acentos y convierte a minúsculas para comparación robusta."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _load_rm_comunas() -> frozenset[str]:
    """Carga y normaliza las comunas de RM desde el JSON."""
    with open(_RM_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return frozenset(_normalize_text(c.strip()) for c in data.get("comunas", []))


def _is_regional_order(order: NormalizedOrder) -> bool:
    """
    Determina si un pedido es de región (no tiene cobertura propia).
    Filtra validando si la comuna NO está en el data/rm.json.
    """
    rm_comunas = _load_rm_comunas()
    ciudad = _normalize_text(order.shipping.city.strip())
    return ciudad not in rm_comunas


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


def _normalize_phone(phone: str) -> str:
    """
    Normaliza teléfonos chilenos al formato de 9 dígitos (ej: 931311932).

    Corrige formatos frecuentes:
    - +56931311932 -> 931311932
    - 56931311932 -> 931311932
    """
    if not phone:
        return ""

    # Mantener solo dígitos para limpiar prefijos como '+' y separadores.
    digits = re.sub(r"\D", "", phone)

    # Formato internacional chileno (56 + 9 dígitos)
    if len(digits) == 11 and digits.startswith("569"):
        return digits[2:]

    # Si ya viene en formato local de 9 dígitos, se respeta.
    if len(digits) == 9 and digits.startswith("9"):
        return digits

    # Fallback: retornar solo dígitos para no romper exportación.
    return digits


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
    # Log de resumen de pedidos recibidos por fuente para debugging
    from collections import Counter
    sources_count = Counter(o.source.value for o in orders)
    logger.info("Pedidos recibidos para Chilexpress CSV: %s", dict(sources_count))

    # Filtrar solo pedidos de WooCommerce regionales
    regional_orders = [o for o in orders if _is_woocommerce_regional(o)]

    # Log de pedidos excluidos de MercadoLibre para verificación
    meli_orders = [o for o in orders if o.source.value == "mercadolibre"]
    if meli_orders:
        logger.warning(
            "ATENCIÓN: %d pedidos de MercadoLibre fueron EXCLUIDOS del CSV Chilexpress (IDs: %s)",
            len(meli_orders),
            [o.id for o in meli_orders]
        )

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
        phone = _normalize_phone(order.shipping.phone)

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
            "VALOR_DECLARADO_PRODUCTO": DECLARED_PRODUCT_VALUE,
            "EMAIL": order.shipping.email,
            "CELULAR": phone,
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
