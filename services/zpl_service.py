"""
Servicio ZPL — Generación de etiquetas y envío por Socket TCP/IP.

Formato de etiqueta: 10 × 5 cm (100 × 50 mm)
Compatible con 203 DPI y 300 DPI (configurado vía ZEBRA_DPI en .env).

Genera dos etiquetas por pedido:
  1. Etiqueta principal: número de pedido, nombre, apellido, ciudad, dirección, email, teléfono
  2. Etiqueta de nota (solo si existe customer_note): número de pedido + nota del cliente

Protocolo de impresión: ZPL II enviado directamente al puerto 9100
de la impresora Zebra mediante socket TCP sin estado persistente,
equivalente al comportamiento de una app de escritorio.

Manejo de errores:
  - Timeout de conexión: 5 segundos
  - Timeout de envío:    10 segundos
  - Si la impresora está offline devuelve (False, mensaje) sin lanzar excepción.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# Anchos máximos por línea (caracteres). Se usan para envolver texto, no truncar.
_MAX_NAME_LINE = 24
_MAX_ADDR_LINE = 24
_MAX_CITY_LINE = 24
_MAX_PHONE_LINE = 24
_MAX_EMAIL_LINE = 24
_MAX_NOTE_LINE = 34

_LABEL_WIDTH_MM = 100.0
_LABEL_HEIGHT_MM = 50.0

# ── Helpers ──────────────────────────────────────────────────────

def _safe(text: str, max_len: int = 0) -> str:
    """Escapa texto para ZPL ^FD (sin truncar si max_len=0)."""
    text = (text or "").strip()
    text = text.replace("^", "").replace("~", "")   # Caracteres reservados ZPL
    if max_len > 0:
        return text[:max_len]
    return text


def _wrap_text(text: str, max_chars: int) -> List[str]:
    """Divide texto en líneas respetando espacios, sin truncar."""
    lines = []
    while len(text) > max_chars:
        cut_pos = text.rfind(" ", 0, max_chars)
        if cut_pos == -1:
            cut_pos = max_chars
        lines.append(text[:cut_pos])
        text = text[cut_pos:].lstrip()
    if text:
        lines.append(text)
    return lines if lines else [""]


def _dots(mm_val: float, dpi: int) -> int:
    """Convierte milímetros a dots según DPI de la impresora."""
    return round(mm_val * dpi / 25.4)


def _split_address_lines(address: str, max_chars: int) -> List[str]:
    """Divide dirección por comas y envuelve cada tramo sin truncar."""
    result: List[str] = []
    for chunk in (address or "").split(","):
        piece = _safe(chunk)
        if not piece:
            continue
        result.extend(_wrap_text(piece, max_chars))
    return result if result else [""]


# ── Generación ZPL ───────────────────────────────────────────────

def build_zpl_main(order: NormalizedOrder, dpi: int = 203) -> str:
    """
    Construye la etiqueta principal de 100 × 50 mm.
    Incluye:
      - Número de pedido (destacado arriba)
      - Nombre completo (multilinea si es necesario)
      - Apellido (multilinea si es necesario)
      - Ciudad
      - Dirección (multilinea si es necesaria)
      - Email
      - Teléfono
    """
    s = order.shipping

    # No truncar campos: envolver en múltiples líneas.
    first_name_lines = _wrap_text(_safe(s.first_name), _MAX_NAME_LINE)
    last_name_lines = _wrap_text(_safe(s.last_name), _MAX_NAME_LINE)

    city_lines = _wrap_text(_safe(s.city), _MAX_CITY_LINE)
    email_lines = _wrap_text(_safe(s.email), _MAX_EMAIL_LINE)
    phone_lines = _wrap_text(_safe(s.phone), _MAX_PHONE_LINE)
    order_id = _safe(str(order.id))

    # Dirección multilinea: separar por comas y envolver cada tramo.
    addr_lines = _split_address_lines(s.full_address, _MAX_ADDR_LINE)

    body_lines = (
        len(first_name_lines)
        + len(last_name_lines)
        + len(city_lines)
        + len(addr_lines)
        + len(email_lines)
        + len(phone_lines)
    )

    # Escala dinámica con margen de seguridad inferior para evitar cortes físicos.
    title_font_mm = 5.5
    body_font_mm = 4.0
    line_height_mm = 5.0
    title_step_mm = 6.5
    top_margin_mm = 2.0
    bottom_margin_mm = 2.0
    usable_height_mm = _LABEL_HEIGHT_MM - 2.0
    required_mm = top_margin_mm + title_step_mm + (body_lines * line_height_mm) + bottom_margin_mm
    scale = min(1.0, usable_height_mm / required_mm)

    # Dimensiones de etiqueta fijas 100x50 mm.
    margin_x = _dots(5, dpi)
    w = _dots(100.0, dpi)
    h = _dots(_LABEL_HEIGHT_MM, dpi)

    title_font_h = max(1, _dots(title_font_mm * scale, dpi))
    title_font_w = max(1, _dots((title_font_mm * 0.78) * scale, dpi))
    body_font_h = max(1, _dots(body_font_mm * scale, dpi))
    body_font_w = max(1, _dots((body_font_mm * 0.72) * scale, dpi))
    title_step = max(1, _dots(title_step_mm * scale, dpi))
    line_height = max(1, _dots(line_height_mm * scale, dpi))
    y_current = _dots(2, dpi)

    # Líneas de ZPL
    lines = []

    def add_field(label: str, wrapped_values: List[str]) -> None:
        nonlocal y_current
        continuation_indent = margin_x + 18
        lines.append(
            f"^FO{margin_x},{y_current}^A0N,{body_font_h},{body_font_w}^FD{label}: {wrapped_values[0]}^FS"
        )
        y_current += line_height
        for extra_line in wrapped_values[1:]:
            lines.append(
                f"^FO{continuation_indent},{y_current}^A0N,{body_font_h},{body_font_w}^FD{extra_line}^FS"
            )
            y_current += line_height

    # Pedido
    lines.append(
        f"^FO{margin_x},{y_current}^A0N,{title_font_h},{title_font_w}^FDPedido: {order_id}^FS"
    )
    y_current += title_step

    add_field("Nombre", first_name_lines)
    add_field("Apellido", last_name_lines)
    add_field("Ciudad", city_lines)
    add_field("Dirección", addr_lines)
    add_field("Email", email_lines)
    add_field("Teléfono", phone_lines)

    zpl_body = "\n".join(lines)

    zpl = f"""\
^XA
^MTT
^MMT
^JUS
^PW{w}
^LL{h}
^CI28

{zpl_body}

^XZ"""
    return zpl


def build_zpl_note(order: NormalizedOrder, dpi: int = 203) -> str:
    """
    Construye etiqueta secundaria para customer_note (solo si existe).
    Formato: 100 × altura dinámica según cantidad de líneas.
    """
    note = (order.customer_note or "").strip()
    if not note:
        return ""

    order_id = _safe(str(order.id))

    # Dividir nota en líneas sin truncar.
    lines = _wrap_text(_safe(note), _MAX_NOTE_LINE)
    title_lines = _wrap_text(f"Pedido {order_id} - Nota cliente:", _MAX_NOTE_LINE)

    # Escala dinámica con margen de seguridad inferior para evitar cortes físicos.
    title_font_mm = 5.5
    body_font_mm = 4.0
    line_height_mm = 5.0
    top_margin_mm = 1.0
    bottom_margin_mm = 2.0
    usable_height_mm = _LABEL_HEIGHT_MM - 2.0
    required_mm = (
        top_margin_mm
        + (line_height_mm * len(title_lines))
        + (line_height_mm * len(lines))
        + bottom_margin_mm
    )
    scale = min(1.0, usable_height_mm / required_mm)

    # Dimensiones fijas
    margin_x = _dots(5, dpi)
    w = _dots(100.0, dpi)
    h = _dots(_LABEL_HEIGHT_MM, dpi)

    title_font_h = max(1, _dots(title_font_mm * scale, dpi))
    title_font_w = max(1, _dots((title_font_mm * 0.78) * scale, dpi))
    body_font_h = max(1, _dots(body_font_mm * scale, dpi))
    body_font_w = max(1, _dots((body_font_mm * 0.72) * scale, dpi))
    y_current = _dots(1, dpi)
    title_line_height = max(1, _dots(line_height_mm * scale, dpi))
    line_height = max(1, _dots(line_height_mm * scale, dpi))

    # Construir líneas de nota
    note_lines: List[str] = []
    for line in title_lines:
        note_lines.append(
            f"^FO{margin_x},{y_current}^A0N,{title_font_h},{title_font_w}^FD{line}^FS"
        )
        y_current += title_line_height

    for i, line in enumerate(lines):
        y = y_current + line_height * i
        note_lines.append(
            f"^FO{margin_x},{y}^A0N,{body_font_h},{body_font_w}^FD{line}^FS"
        )

    zpl_body = "\n".join(note_lines)

    zpl = f"""\
^XA
^MTT
^MMT
^JUS
^PW{w}
^LL{h}
^CI28

{zpl_body}

^XZ"""
    return zpl


# ── Comunicación TCP ─────────────────────────────────────────────

class ZPLService:
    def __init__(self, host: str, port: int = 9100, dpi: int = 203) -> None:
        self.host = host
        self.port = port
        self.dpi  = dpi

    async def print_label(
        self, order: NormalizedOrder
    ) -> Tuple[bool, str]:
        """
        Genera el ZPL y lo envía a la impresora por TCP.
        Si hay customer_note, imprime dos etiquetas:
          1. Etiqueta principal con datos del pedido
          2. Etiqueta con la nota del cliente
        Retorna (True, "") o (False, mensaje_de_error).
        No lanza excepciones — todos los errores son capturados.
        """
        # Etiqueta principal
        zpl_main = build_zpl_main(order, dpi=self.dpi)
        success, error = await self._send(zpl_main)
        if not success:
            return False, f"Error en etiqueta principal: {error}"

        # Etiqueta de customer_note (si existe)
        if order.customer_note and order.customer_note.strip():
            zpl_note = build_zpl_note(order, dpi=self.dpi)
            if zpl_note:
                success_note, error_note = await self._send(zpl_note)
                if not success_note:
                    return False, f"Etiqueta principal OK, error en nota: {error_note}"

        return True, ""

    async def _send(self, zpl: str) -> Tuple[bool, str]:
        """Abre conexión TCP, envía ZPL y cierra. Totalmente asíncrono."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            msg = f"Impresora {self.host}:{self.port} no responde (timeout de conexión)"
            logger.error(msg)
            return False, msg
        except OSError as exc:
            msg = f"No se pudo conectar a {self.host}:{self.port} — {exc}"
            logger.error(msg)
            return False, msg

        try:
            data = zpl.encode("utf-8")
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=10.0)
            logger.info(
                "ZPL enviado a %s:%s (%d bytes)", self.host, self.port, len(data)
            )
            return True, ""
        except asyncio.TimeoutError:
            msg = f"Timeout al enviar datos a {self.host}:{self.port}"
            logger.error(msg)
            return False, msg
        except OSError as exc:
            msg = f"Error de red al enviar ZPL — {exc}"
            logger.error(msg)
            return False, msg
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def test_connection(self) -> Tuple[bool, str]:
        """Prueba la conectividad con la impresora sin enviar datos reales."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0,
            )
            writer.close()
            await writer.wait_closed()
            return True, f"Impresora alcanzable en {self.host}:{self.port}"
        except asyncio.TimeoutError:
            return False, f"Timeout — {self.host}:{self.port} no responde"
        except OSError as exc:
            return False, f"Conexión rechazada — {exc}"
