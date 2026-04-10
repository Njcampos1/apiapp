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
import math
import logging
from typing import Tuple, List

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# Anchos máximos de texto por campo (en caracteres ZPL ~^FD)
_MAX_NAME_LINE = 30  # Caracteres por línea para nombre/apellido (sin truncar)
_MAX_ADDR    = 35
_MAX_CITY    = 25
_MAX_PHONE   = 20
_MAX_ID      = 20
_MAX_EMAIL   = 35
_MAX_NOTE_LINE = 45  # Para customer_note

_LABEL_WIDTH_MM = 100.0
_LABEL_HEIGHT_MM = 50.0
_MIN_SCALE = 0.75

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


def _fit_lines_to_budget(
    first_name_lines: List[str],
    last_name_lines: List[str],
    addr_lines: List[str],
    max_body_lines: int,
) -> Tuple[List[str], List[str], List[str]]:
    """Ajusta líneas para que entren en la etiqueta manteniendo contenido crítico."""
    first = first_name_lines[:] if first_name_lines else [""]
    last = last_name_lines[:] if last_name_lines else [""]
    addr = addr_lines[:] if addr_lines else [""]

    min_lines = 6  # nombre, apellido, ciudad, dirección, email, teléfono
    budget = max(max_body_lines, min_lines)

    def current_total() -> int:
        return len(first) + len(last) + 1 + len(addr) + 2

    trimmed_addr = False
    trimmed_last = False
    trimmed_first = False

    while current_total() > budget and len(addr) > 1:
        addr.pop()
        trimmed_addr = True
    while current_total() > budget and len(last) > 1:
        last.pop()
        trimmed_last = True
    while current_total() > budget and len(first) > 1:
        first.pop()
        trimmed_first = True

    # Marca visual mínima si hubo recorte.
    if trimmed_addr and addr:
        addr[-1] = addr[-1].rstrip(".") + "..."
    if trimmed_last and last:
        last[-1] = last[-1].rstrip(".") + "..."
    if trimmed_first and first:
        first[-1] = first[-1].rstrip(".") + "..."

    return first, last, addr


# ── Generación ZPL ───────────────────────────────────────────────

def build_zpl_main(order: NormalizedOrder, dpi: int = 300) -> str:
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

    # No truncar nombres, dividir en líneas si es necesario
    first_name_lines = _wrap_text(_safe(s.first_name), _MAX_NAME_LINE)
    last_name_lines  = _wrap_text(_safe(s.last_name),  _MAX_NAME_LINE)

    city       = _safe(s.city,       _MAX_CITY)
    email      = _safe(s.email,      _MAX_EMAIL)
    phone      = _safe(s.phone,      _MAX_PHONE)
    order_id   = _safe(str(order.id), _MAX_ID)

    # Dirección puede ser multilinea (separar por comas)
    addr_full = s.full_address
    addr_lines = [line.strip() for line in addr_full.split(",") if line.strip()]

    body_lines = len(first_name_lines) + len(last_name_lines) + 1 + len(addr_lines) + 2

    # Ajuste de escala para mantener SIEMPRE 100x50 mm.
    required_mm = 2.0 + 6.0 + (body_lines * 4.5) + 2.0
    scale = min(1.0, _LABEL_HEIGHT_MM / required_mm)
    scale = max(scale, _MIN_SCALE)

    max_body_lines = max(1, math.floor((_LABEL_HEIGHT_MM - (2.0 + 6.0 * scale + 2.0)) / (4.5 * scale)))
    first_name_lines, last_name_lines, addr_lines = _fit_lines_to_budget(
        first_name_lines,
        last_name_lines,
        addr_lines,
        max_body_lines,
    )

    # Dimensiones de etiqueta fijas 100x50 mm.
    margin_x = _dots(3, dpi)
    w = _dots(_LABEL_WIDTH_MM, dpi)
    h = _dots(_LABEL_HEIGHT_MM, dpi)

    title_font = _dots(5 * scale, dpi)
    body_font = _dots(3.5 * scale, dpi)
    title_step = _dots(6 * scale, dpi)
    line_height = _dots(4.5 * scale, dpi)
    y_current = _dots(2, dpi)

    # Líneas de ZPL
    lines = []

    # Pedido
    lines.append(f"^FO{margin_x},{y_current}^A0N,{title_font},{title_font}^FDPedido: {order_id}^FS")
    y_current += title_step

    # Nombre (multilinea)
    lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDNombre: {first_name_lines[0]}^FS")
    y_current += line_height
    for extra_line in first_name_lines[1:]:
        indent = _dots(12, dpi)
        lines.append(f"^FO{indent},{y_current}^A0N,{body_font},{body_font}^FD{extra_line}^FS")
        y_current += line_height

    # Apellido (multilinea)
    lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDApellido: {last_name_lines[0]}^FS")
    y_current += line_height
    for extra_line in last_name_lines[1:]:
        indent = _dots(12, dpi)
        lines.append(f"^FO{indent},{y_current}^A0N,{body_font},{body_font}^FD{extra_line}^FS")
        y_current += line_height

    # Ciudad
    lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDCiudad: {city}^FS")
    y_current += line_height

    # Dirección (multilinea)
    for i, addr_line in enumerate(addr_lines):
        if i == 0:
            lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDDirección: {addr_line}^FS")
        else:
            indent = _dots(15, dpi)
            lines.append(f"^FO{indent},{y_current}^A0N,{body_font},{body_font}^FD{addr_line}^FS")
        y_current += line_height

    # Email
    lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDEmail: {email}^FS")
    y_current += line_height

    # Teléfono
    lines.append(f"^FO{margin_x},{y_current}^A0N,{body_font},{body_font}^FDTeléfono: {phone}^FS")

    zpl = f"""\
^XA
^PW{w}
^LL{h}
^CI28

{"".join(lines)}

^XZ"""
    return zpl


def build_zpl_note(order: NormalizedOrder, dpi: int = 300) -> str:
    """
    Construye etiqueta secundaria para customer_note (solo si existe).
    Formato: 100 × altura dinámica según cantidad de líneas.
    """
    note = (order.customer_note or "").strip()
    if not note:
        return ""

    # Dividir nota en líneas de máximo ~45 caracteres
    lines = _wrap_text(_safe(note), _MAX_NOTE_LINE)

    # Ajuste de escala para mantener SIEMPRE 100x50 mm.
    required_mm = 1.0 + 6.0 + (4.0 * len(lines)) + 2.0
    scale = min(1.0, _LABEL_HEIGHT_MM / required_mm)
    scale = max(scale, _MIN_SCALE)

    max_note_lines = max(1, math.floor((_LABEL_HEIGHT_MM - (7.0 * scale + 2.0)) / (4.0 * scale)))
    if len(lines) > max_note_lines:
        lines = lines[:max_note_lines]
        if lines:
            lines[-1] = lines[-1].rstrip(".") + "..."

    # Dimensiones fijas
    margin_x = _dots(3, dpi)
    w = _dots(_LABEL_WIDTH_MM, dpi)
    h = _dots(_LABEL_HEIGHT_MM, dpi)

    order_id = _safe(str(order.id), _MAX_ID)
    title_font = _dots(5 * scale, dpi)
    body_font = _dots(3.5 * scale, dpi)
    y_title = _dots(1, dpi)
    y_line = _dots(7 * scale, dpi)
    line_height = _dots(4 * scale, dpi)

    # Construir líneas de nota
    note_lines = []
    for i, line in enumerate(lines):
        y = y_line + line_height * i
        note_lines.append(f"^FO{margin_x},{y}^A0N,{body_font},{body_font}^FD{line}^FS")

    zpl = f"""\
^XA
^PW{w}
^LL{h}
^CI28

^FO{margin_x},{y_title}^A0N,{title_font},{title_font}^FDPedido {order_id} - Nota cliente:^FS

{"".join(note_lines)}

^XZ"""
    return zpl


# ── Comunicación TCP ─────────────────────────────────────────────

class ZPLService:
    def __init__(self, host: str, port: int = 9100, dpi: int = 300) -> None:
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
