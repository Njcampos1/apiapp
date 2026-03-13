"""
Servicio de generación de PDF de picking.
Produce una hoja A4 con:
  - Encabezado con número y fuente del pedido
  - Tabla de ítemes a preparar
  - QR con el ID del pedido para escaner RF
  - Datos de envío del destinatario
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    Image,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# ── Paleta de colores corporativa ────────────────────────────────
COLOR_DARK   = colors.HexColor("#1a1a2e")
COLOR_ACCENT = colors.HexColor("#c8a96e")   # dorado café
COLOR_LIGHT  = colors.HexColor("#f5f0e8")
COLOR_GRAY   = colors.HexColor("#888888")


def _build_qr_image(data: str, size_cm: float = 4.0) -> Image:
    """Genera un objeto QR como Image de ReportLab."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    pil_img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)

    side = size_cm * cm
    return Image(buf, width=side, height=side)


def generate_picking_pdf(order: NormalizedOrder) -> bytes:
    """
    Genera y devuelve el PDF de picking como bytes.
    Lanza ValueError si el pedido no tiene ítemes.
    """
    if not order.items:
        raise ValueError(f"Pedido {order.id} no tiene ítemes")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
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
        fontSize=10,
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

    # ── Layout de dos columnas: datos envío + QR ─────────────────
    qr_img = _build_qr_image(order.id, size_cm=4.0)

    shipping = order.shipping
    info_data = [
        [Paragraph("<b>Destinatario</b>", style_label), ""],
        [Paragraph(shipping.full_name or "—", style_value), ""],
        [Paragraph(shipping.full_address or "—", style_value), ""],
        [Paragraph(f"{shipping.city} {shipping.postcode} {shipping.country}", style_value), ""],
        [Paragraph(f"Tel: {shipping.phone or '—'}", style_value), ""],
    ]

    header_table = Table(
        [[
            Table(
                info_data,
                colWidths=["100%"],
                style=TableStyle([
                    ("LEFTPADDING",  (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING",   (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
                ]),
            ),
            qr_img,
        ]],
        colWidths=["75%", "25%"],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("ALIGN",        (1, 0), (1, 0),   "CENTER"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)

    # QR caption
    elements.append(Paragraph(f"Escanear: {order.display_id}", style_center))
    elements.append(Spacer(1, 0.5 * cm))

    # Nota de cliente si existe
    if order.customer_note:
        note_table = Table(
            [[Paragraph(f"<b>Nota del cliente:</b> {order.customer_note}", style_value)]],
            colWidths=["100%"],
        )
        note_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, -1), COLOR_LIGHT),
            ("BOX",         (0, 0), (-1, -1), 1, COLOR_ACCENT),
            ("TOPPADDING",  (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0,0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(note_table)
        elements.append(Spacer(1, 0.4 * cm))

    # ── Tabla de ítemes ──────────────────────────────────────────
    elements.append(HRFlowable(width="100%", thickness=1, color=COLOR_ACCENT, spaceAfter=6))

    table_header = ["#", "SKU", "Producto", "Cant.", "✓"]
    table_data = [table_header]

    for idx, item in enumerate(order.items, start=1):
        table_data.append([
            str(idx),
            item.sku,
            item.name,
            str(item.quantity),
            "",   # checkbox manual para el operario
        ])

    col_widths = [1 * cm, 3 * cm, 10 * cm, 1.5 * cm, 1.5 * cm]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Encabezado
        ("BACKGROUND",   (0, 0), (-1, 0),  COLOR_DARK),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  10),
        ("ALIGN",        (0, 0), (-1, 0),  "CENTER"),
        # Filas alternas
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_LIGHT]),
        ("FONTSIZE",     (0, 1), (-1, -1), 10),
        ("ALIGN",        (0, 1), (1, -1),  "CENTER"),   # # y SKU
        ("ALIGN",        (3, 1), (4, -1),  "CENTER"),   # Cant y checkbox
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        # Bordes
        ("GRID",         (0, 0), (-1, -1), 0.3, COLOR_GRAY),
        ("BOX",          (0, 0), (-1, -1), 1,   COLOR_DARK),
        # Columna checkbox con borde grueso visual
        ("BOX",          (4, 1), (4, -1),  1,   COLOR_DARK),
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
            "Upper Coffee — Sistema de Gestión Logística &nbsp;|&nbsp; Documento interno",
            ParagraphStyle("footer", parent=styles["Normal"], fontSize=8,
                           textColor=COLOR_GRAY, alignment=TA_CENTER),
        )
    )

    doc.build(elements)
    buf.seek(0)
    logger.info("PDF de picking generado para pedido %s (%d ítemes)", order.id, len(order.items))
    return buf.read()
