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
from typing import Tuple

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# Anchos máximos de texto por campo (en caracteres ZPL ~^FD)
_MAX_NAME    = 25  # Reducido para evitar desbordamiento
_MAX_ADDR    = 35
_MAX_CITY    = 25
_MAX_PHONE   = 20
_MAX_ID      = 20
_MAX_EMAIL   = 30
_MAX_NOTE_LINE = 45  # Para customer_note

# ── Helpers ──────────────────────────────────────────────────────

def _safe(text: str, max_len: int) -> str:
    """Trunca y escapa texto para ZPL ^FD."""
    text = (text or "").strip()
    text = text.replace("^", "").replace("~", "")   # Caracteres reservados ZPL
    return text[:max_len]


def _dots(mm_val: float, dpi: int) -> int:
    """Convierte milímetros a dots según DPI de la impresora."""
    return round(mm_val * dpi / 25.4)


# ── Generación ZPL ───────────────────────────────────────────────

def build_zpl_main(order: NormalizedOrder, dpi: int = 300) -> str:
    """
    Construye la etiqueta principal de 100 × 50 mm.
    Incluye:
      - Número de pedido (destacado arriba)
      - Nombre completo
      - Apellido
      - Ciudad
      - Dirección (multilinea si es necesaria)
      - Email
      - Teléfono
    """
    s = order.shipping

    first_name = _safe(s.first_name, _MAX_NAME)
    last_name  = _safe(s.last_name,  _MAX_NAME)
    city       = _safe(s.city,       _MAX_CITY)
    email      = _safe(s.email,      _MAX_EMAIL)
    phone      = _safe(s.phone,      _MAX_PHONE)
    order_id   = _safe(str(order.id), _MAX_ID)

    # Dirección puede ser multilinea (separar por comas)
    addr_full = s.full_address
    addr_lines = [line.strip() for line in addr_full.split(",") if line.strip()]

    # Dimensiones de etiqueta
    margin_x = _dots(3, dpi)
    w        = _dots(100, dpi)

    # Altura dinámica: base + extra por líneas de dirección adicionales
    base_height = 50
    extra_height = 4 * (len(addr_lines) - 1) if len(addr_lines) > 1 else 0
    h = _dots(base_height + extra_height, dpi)

    # Posiciones Y
    y_order = _dots(2,  dpi)
    y_name  = _dots(8,  dpi)
    y_last  = _dots(13, dpi)
    y_city  = _dots(18, dpi)
    y_addr  = _dots(23, dpi)

    # Construir líneas de dirección
    addr_section = []
    line_height = _dots(4, dpi)
    for i, line in enumerate(addr_lines):
        if i == 0:
            addr_section.append(f"^FO{margin_x},{y_addr}^A0N,{_dots(3.5,dpi)},{_dots(3.5,dpi)}^FDDirección: {line}^FS")
        else:
            y_extra = y_addr + line_height * i
            indent = _dots(15, dpi)  # Indentar líneas adicionales
            addr_section.append(f"^FO{indent},{y_extra}^A0N,{_dots(3.5,dpi)},{_dots(3.5,dpi)}^FD{line}^FS")

    # Posiciones después de dirección
    y_after_addr = y_addr + line_height * len(addr_lines)
    y_email = y_after_addr
    y_phone = y_email + _dots(5, dpi)

    zpl = f"""\
^XA
^PW{w}
^LL{h}
^CI28

^FO{margin_x},{y_order}^A0N,{_dots(5,dpi)},{_dots(5,dpi)}^FDPedido: {order_id}^FS

^FO{margin_x},{y_name}^A0N,{_dots(4,dpi)},{_dots(4,dpi)}^FDNombre: {first_name}^FS
^FO{margin_x},{y_last}^A0N,{_dots(4,dpi)},{_dots(4,dpi)}^FDApellido: {last_name}^FS
^FO{margin_x},{y_city}^A0N,{_dots(4,dpi)},{_dots(4,dpi)}^FDCiudad: {city}^FS

{"".join(addr_section)}

^FO{margin_x},{y_email}^A0N,{_dots(3.5,dpi)},{_dots(3.5,dpi)}^FDEmail: {email}^FS
^FO{margin_x},{y_phone}^A0N,{_dots(3.5,dpi)},{_dots(3.5,dpi)}^FDTeléfono: {phone}^FS

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
    lines = []
    while len(note) > _MAX_NOTE_LINE:
        cut_pos = note.rfind(" ", 0, _MAX_NOTE_LINE)
        if cut_pos == -1:
            cut_pos = _MAX_NOTE_LINE
        lines.append(note[:cut_pos])
        note = note[cut_pos:].lstrip()
    lines.append(note)

    # Dimensiones
    margin_x = _dots(3, dpi)
    w        = _dots(100, dpi)
    h        = _dots(7 + 4 * len(lines), dpi)

    order_id = _safe(str(order.id), _MAX_ID)
    y_title  = _dots(1, dpi)
    y_line   = _dots(7, dpi)
    line_height = _dots(4, dpi)

    # Construir líneas de nota
    note_lines = []
    for i, line in enumerate(lines):
        y = y_line + line_height * i
        note_lines.append(f"^FO{margin_x},{y}^A0N,{_dots(3.5,dpi)},{_dots(3.5,dpi)}^FD{line}^FS")

    zpl = f"""\
^XA
^PW{w}
^LL{h}
^CI28

^FO{margin_x},{y_title}^A0N,{_dots(5,dpi)},{_dots(5,dpi)}^FDPedido {order_id} - Nota cliente:^FS

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
