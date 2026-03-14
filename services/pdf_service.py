"""
Servicio de generación de PDF de picking.
Produce una hoja A4 con:
  - Encabezado con número y fuente del pedido
  - Tabla de ítemes a preparar (eco-tinta: sin fondos oscuros ni rellenos)
  - Código de barras Code128 del ID del pedido
  - Datos de envío del destinatario
  - Nota del cliente siempre visible si existe
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import List

from reportlab.graphics.barcode import code128 as code128_barcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    Image,
    PageBreak,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# ── Paleta de colores corporativa ────────────────────────────────
COLOR_ACCENT = colors.HexColor("#c8a96e")   # dorado café
COLOR_DARK   = colors.HexColor("#1a1a2e")   # solo para texto, nunca como fondo
COLOR_LIGHT  = colors.HexColor("#f5f0e8")
COLOR_GRAY   = colors.HexColor("#888888")


def _build_barcode(data: str, height_cm: float = 2.5) -> code128_barcode.Code128:
    """Genera un código de barras Code128 como Flowable de ReportLab."""
    return code128_barcode.Code128(
        data,
        barHeight=height_cm * cm,
        barWidth=0.9,
        humanReadable=True,
        fontSize=7,
    )


def _build_order_elements(order: NormalizedOrder, styles) -> list:
    """
    Construye la lista de Flowables para una hoja de picking.
    Reutilizable tanto para PDF individual como para PDF masivo multipágina.
    """
    style_title = ParagraphStyle(
        "title",
        parent=styles["Heading1"],
        fontSize=22,
        textColor=COLOR_DARK,
        spaceAfter=2,
    )
    style_subtitle = ParagraphStyle(
        "subtitle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=COLOR_GRAY,
        spaceAfter=6,
    )
    style_label = ParagraphStyle(
        "label",
        parent=styles["Normal"],
        fontSize=9,
        textColor=COLOR_GRAY,
    )
    style_value = ParagraphStyle(
        "value",
        parent=styles["Normal"],
        fontSize=11,
        textColor=COLOR_DARK,
    )
    style_center = ParagraphStyle(
        "center",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=9,
        textColor=COLOR_DARK,
    )

    elements = []

    # ── Encabezado ───────────────────────────────────────────────
    source_label = order.source.value.upper()
    elements.append(Paragraph(f"Hoja de Picking — {source_label}", style_title))
    ts = datetime.now().strftime("%d/%m/%Y %H:%M")
    elements.append(
        Paragraph(
            f"Generada: {ts} &nbsp;|&nbsp; Pedido: <b>{order.display_id}</b>",
            style_subtitle,
        )
    )
    elements.append(HRFlowable(width="100%", thickness=2, color=COLOR_ACCENT, spaceAfter=8))

    # ── Nota del cliente (siempre primero si existe) ─────────────
    if order.customer_note.strip():
        note_table = Table(
            [[Paragraph(f"<b>Nota del cliente:</b> {order.customer_note}", style_value)]],
            colWidths=["100%"],
        )
        note_table.setStyle(TableStyle([
            ("BOX",          (0, 0), (-1, -1), 1.5, COLOR_ACCENT),
            ("TOPPADDING",   (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
            ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ]))
        elements.append(note_table)
        elements.append(Spacer(1, 0.3 * cm))

    # ── Layout dos columnas: datos envío + código de barras ──────
    barcode_obj = _build_barcode(order.id, height_cm=2.5)

    shipping = order.shipping
    info_data = [
        [Paragraph("<b>Destinatario</b>", style_label), ""],
        [Paragraph(shipping.full_name or "—", style_value), ""],
        [Paragraph(shipping.full_address or "—", style_value), ""],
        [Paragraph(f"{shipping.city} {shipping.postcode} {shipping.country}", style_value), ""],
        [Paragraph(f"Tel: {shipping.phone or '—'}", style_value), ""],
    ]

    info_inner = Table(
        info_data,
        colWidths=["100%"],
        style=TableStyle([
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING",   (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
        ]),
    )

    barcode_caption = Table(
        [[barcode_obj], [Paragraph(order.display_id, style_center)]],
        colWidths=["100%"],
        style=TableStyle([
            ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING",   (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ]),
    )

    header_table = Table(
        [[info_inner, barcode_caption]],
        colWidths=["68%", "32%"],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",        (1, 0), (1, 0),   "CENTER"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.5 * cm))

    # ── Tabla de ítemes (eco-tinta: sin fondos ni grillas) ──────
    elements.append(HRFlowable(width="100%", thickness=1, color=COLOR_ACCENT, spaceAfter=6))

    # Sin columna SKU y sin columna Checkbox: solo #, Producto, Cant.
    table_header = ["#", "Producto", "Cant."]
    table_data = [table_header]

    for idx, item in enumerate(order.items, start=1):
        table_data.append([
            str(idx),
            item.name,
            str(item.quantity),
        ])

    # El espacio del checkbox se suma a Producto (antes 13, ahora 14.5)
    col_widths = [1 * cm, 14.5 * cm, 1.5 * cm]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Encabezado — negrita sin fondo
        ("TEXTCOLOR",    (0, 0), (-1, 0),  COLOR_DARK),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  10),
        ("ALIGN",        (0, 0), (-1, 0),  "CENTER"),
        ("LINEBELOW",    (0, 0), (-1, 0),  1.5, COLOR_ACCENT),
        # Filas de datos — sin relleno ni grilla
        ("FONTSIZE",     (0, 1), (-1, -1), 10),
        ("ALIGN",        (0, 1), (0, -1),  "CENTER"),   # columna #
        ("ALIGN",        (2, 1), (2, -1),  "CENTER"),   # Cant.
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        # Solo línea horizontal entre filas (sin grilla vertical ni caja exterior)
        ("LINEBELOW",    (0, 1), (-1, -1), 0.4, COLOR_GRAY),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.5 * cm))

    # ── Totales ──────────────────────────────────────────────────
    total_items = sum(i.quantity for i in order.items)
    elements.append(
        Paragraph(
            f"<b>Total unidades: {total_items}</b> &nbsp;|&nbsp; "
            f"Total pedido: {order.currency} {order.total:,.0f}",
            style_value,
        )
    )
    elements.append(HRFlowable(width="100%", thickness=1, color=COLOR_GRAY, spaceBefore=8))
    elements.append(
        Paragraph(
            "Upper Logistics - www.upperlogistics.com",
            ParagraphStyle("footer", parent=styles["Normal"], fontSize=8,
                           textColor=COLOR_GRAY, alignment=TA_CENTER),
        )
    )

    return elements


def _make_doc(buf: io.BytesIO) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )


def generate_picking_pdf(order: NormalizedOrder) -> bytes:
    """
    Genera y devuelve el PDF de picking como bytes.
    Lanza ValueError si el pedido no tiene ítemes.
    """
    if not order.items:
        raise ValueError(f"Pedido {order.id} no tiene ítemes")

    buf = io.BytesIO()
    doc = _make_doc(buf)
    styles = getSampleStyleSheet()
    doc.build(_build_order_elements(order, styles))
    buf.seek(0)
    logger.info("PDF de picking generado para pedido %s (%d ítemes)", order.id, len(order.items))
    return buf.read()


def generate_bulk_picking_pdf(orders: List[NormalizedOrder]) -> bytes:
    """
    Genera un único PDF multipágina con la hoja de picking de cada pedido.
    Los pedidos sin ítemes se omiten con una advertencia.
    Lanza ValueError si ningún pedido tiene ítemes.
    """
    buf = io.BytesIO()
    doc = _make_doc(buf)
    styles = getSampleStyleSheet()

    all_elements: list = []
    included = 0
    for order in orders:
        if not order.items:
            logger.warning("Pedido %s sin ítemes, omitido del PDF masivo", order.id)
            continue
        if included > 0:
            all_elements.append(PageBreak())
        all_elements.extend(_build_order_elements(order, styles))
        included += 1

    if included == 0:
        raise ValueError("Ningún pedido en la lista tiene ítemes para generar PDF")

    doc.build(all_elements)
    buf.seek(0)
    logger.info("PDF masivo generado: %d pedidos (%d páginas)", included, included)
    return buf.read()