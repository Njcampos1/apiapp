"""
Servicio ZPL — Generación de etiquetas y envío por Socket TCP/IP.

Formato de etiqueta: 10 × 5 cm (100 × 50 mm)
Compatible con 203 DPI y 300 DPI (configurado vía ZEBRA_DPI en .env).

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
import textwrap
from typing import Tuple

from models.order import NormalizedOrder

logger = logging.getLogger(__name__)

# Anchos máximos de texto por campo (en caracteres ZPL ~^FD)
_MAX_NAME    = 30
_MAX_ADDR    = 35
_MAX_CITY    = 25
_MAX_PHONE   = 20
_MAX_ID      = 20

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

def build_zpl(order: NormalizedOrder, dpi: int = 300) -> str:
    """
    Construye el string ZPL II para una etiqueta de 100 × 50 mm.
    Incluye:
      - Nombre del destinatario (tipografía grande)
      - Dirección + Ciudad
      - Teléfono
      - Número de pedido + fuente (texto + code128)
      - Barcode Code128 del ID del pedido
    """
    s = order.shipping

    name    = _safe(s.full_name,    _MAX_NAME)
    addr    = _safe(s.full_address, _MAX_ADDR)
    city    = _safe(s.city,         _MAX_CITY)
    phone   = _safe(s.phone,        _MAX_PHONE)
    order_id = _safe(str(order.id), _MAX_ID)
    source  = order.source.value.upper()[:10]

    # Posiciones en dots
    margin_x = _dots(3, dpi)
    w        = _dots(100, dpi)
    h        = _dots(50,  dpi)

    # ── Columna izquierda: datos ──
    y_name  = _dots(4,  dpi)
    y_addr  = _dots(14, dpi)
    y_city  = _dots(21, dpi)
    y_phone = _dots(28, dpi)
    y_order = _dots(35, dpi)

    # ── Columna derecha: barcode ──
    bc_x = _dots(70, dpi)
    bc_y = _dots(3,  dpi)
    bc_h = _dots(25, dpi)

    # Fuentes: A=pequeña, D=media, 0=grande (escalada)
    zpl = f"""\
^XA
^PW{w}
^LL{h}
^CI28

^FO{margin_x},{y_name}^A0N,{_dots(7,dpi)},{_dots(7,dpi)}^FD{name}^FS

^FO{margin_x},{y_addr}^ADN,18,10^FD{addr}^FS
^FO{margin_x},{y_city}^ADN,18,10^FD{city}^FS
^FO{margin_x},{y_phone}^ADN,16,8^FDTel: {phone}^FS
^FO{margin_x},{y_order}^ADN,14,7^FD{source} #{order_id}^FS

^FO{bc_x},{bc_y}^BCN,{bc_h},Y,N,N^FD{order_id}^FS

^XZ"""
    return textwrap.dedent(zpl)


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
        Retorna (True, "") o (False, mensaje_de_error).
        No lanza excepciones — todos los errores son capturados.
        """
        zpl_str = build_zpl(order, dpi=self.dpi)
        return await self._send(zpl_str)

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
