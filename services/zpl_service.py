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

Failover: si hay una Zebra de respaldo configurada (ZEBRA_BACKUP_IP), todo
envío intenta primero la Zebra principal y, si falla, reintenta contra la de
respaldo. El reintento es seguro porque la Zebra no imprime un formato hasta
recibir `^XZ`: un envío cortado a media transmisión queda descartado en su
buffer y no produce una etiqueta ZPL duplicada ni a medias.

Manejo de errores:
  - Timeout de conexión: ZEBRA_TIMEOUT (3 s por defecto), por intento
  - Timeout de envío:    10 segundos, por intento
  - Si ninguna Zebra responde devuelve (False, mensaje) sin lanzar excepción.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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

ROLE_PRIMARY = "principal"
ROLE_BACKUP  = "respaldo"

# Nombre legible por rol. Se separa del valor de `role` porque ese viaja en el
# JSON de la API y debe permanecer estable; esto es sólo texto para el operador.
_ROLE_LABELS = {
    ROLE_PRIMARY: "Zebra principal",
    ROLE_BACKUP:  "Zebra de respaldo",
}


class PrinterTarget(NamedTuple):
    """Un destino de impresión: una Zebra concreta y su rol en el failover."""
    host: str
    port: int
    role: str   # ROLE_PRIMARY | ROLE_BACKUP

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        """Nombre legible para logs y mensajes al operador."""
        return f"{_ROLE_LABELS.get(self.role, 'Zebra')} {self.address}"

    def as_dict(self) -> Dict[str, Any]:
        return {"host": self.host, "port": self.port, "role": self.role}


class ZPLService:
    def __init__(
        self,
        host: str,
        port: int = 9100,
        dpi: int = 203,
        backup_host: Optional[str] = None,
        backup_port: int = 9101,
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.dpi  = dpi
        self.backup_host = (backup_host or "").strip() or None
        self.backup_port = backup_port
        self.timeout = timeout

        # Orden de preferencia. Sin respaldo configurado queda un solo
        # destino y el comportamiento es el de siempre: un único intento.
        self.targets: List[PrinterTarget] = [PrinterTarget(host, port, ROLE_PRIMARY)]
        if self.backup_host:
            self.targets.append(
                PrinterTarget(self.backup_host, backup_port, ROLE_BACKUP)
            )

        # Última Zebra que aceptó una etiqueta ZPL en esta instancia. Los
        # endpoints la leen para informar quién imprimió de verdad.
        self.last_target: Optional[PrinterTarget] = None

    @property
    def has_backup(self) -> bool:
        return len(self.targets) > 1

    @property
    def used_failover(self) -> bool:
        """True si lo último que se imprimió salió por la Zebra de respaldo."""
        return self.last_target is not None and self.last_target.role == ROLE_BACKUP

    def _ordered_targets(self) -> List[PrinterTarget]:
        """
        Destinos a intentar, en orden. Si esta instancia ya cayó a la Zebra de
        respaldo, esa pasa a ser la primera opción: así las dos etiquetas de un
        mismo pedido (principal + nota) nunca se reparten entre dos Zebras.
        """
        if self.last_target is None:
            return list(self.targets)
        rest = [t for t in self.targets if t != self.last_target]
        return [self.last_target, *rest]

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

    async def send_raw_zpl(self, zpl: str) -> Tuple[bool, str]:
        """
        Envía una etiqueta ZPL ya construida (p. ej. el ZPL nativo de Mercado
        Libre) aplicando el failover. Alias público de `_send`.
        """
        return await self._send(zpl)

    async def _send(self, zpl: str) -> Tuple[bool, str]:
        """
        Envía la etiqueta ZPL a la primera Zebra que la acepte.

        Intenta los destinos en orden y cae a la Zebra de respaldo ante
        timeout, conexión rechazada o cualquier otro error de red.
        Retorna (True, "") o (False, mensaje). No lanza excepciones.
        """
        errors: List[str] = []
        targets = self._ordered_targets()

        for index, target in enumerate(targets):
            ok, error = await self._send_to(target, zpl)
            if ok:
                if index > 0:
                    logger.info(
                        "Etiqueta ZPL enviada a %s tras el failover", target.label
                    )
                self.last_target = target
                return True, ""

            errors.append(f"{target.label}: {error}")
            remaining = targets[index + 1:]
            if remaining:
                logger.warning(
                    "%s no responde (%s) — reintentando la etiqueta ZPL en %s",
                    target.label, error, remaining[0].label,
                )

        if len(errors) > 1:
            msg = "Ninguna Zebra disponible — " + " | ".join(errors)
            logger.critical(
                "%s. Verificar ambas Zebras y el nodo puente de bodega.", msg
            )
        else:
            msg = errors[0]
            logger.error("No se pudo imprimir la etiqueta ZPL — %s", msg)

        return False, msg

    async def _send_to(self, target: PrinterTarget, zpl: str) -> Tuple[bool, str]:
        """Abre conexión TCP contra una Zebra concreta, envía el ZPL y cierra."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(target.host, target.port),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return False, "no responde (timeout de conexión)"
        except OSError as exc:
            return False, f"no se pudo conectar — {exc}"

        try:
            data = zpl.encode("utf-8")
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=10.0)
            logger.info(
                "Etiqueta ZPL enviada a %s (%d bytes)", target.label, len(data)
            )
            return True, ""
        except asyncio.TimeoutError:
            return False, "timeout al enviar la etiqueta ZPL"
        except OSError as exc:
            return False, f"error de red al enviar la etiqueta ZPL — {exc}"
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _probe(self, target: PrinterTarget) -> Tuple[bool, str]:
        """Comprueba conectividad con una Zebra sin enviar datos reales."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(target.host, target.port),
                timeout=self.timeout,
            )
            writer.close()
            await writer.wait_closed()
            return True, f"{target.label} alcanzable"
        except asyncio.TimeoutError:
            return False, f"{target.label} no responde (timeout)"
        except OSError as exc:
            return False, f"{target.label} rechazó la conexión — {exc}"

    async def test_targets(self) -> List[Dict[str, Any]]:
        """Estado de conectividad de cada Zebra configurada, en orden."""
        results: List[Dict[str, Any]] = []
        for target in self.targets:
            ok, msg = await self._probe(target)
            results.append({**target.as_dict(), "reachable": ok, "message": msg})
        return results

    async def test_connection(self) -> Tuple[bool, str]:
        """
        Prueba la conectividad de impresión. Es exitosa si responde al menos
        una Zebra, de modo que con la principal caída pero el respaldo vivo el
        diagnóstico siga siendo "alcanzable".
        """
        results = await self.test_targets()
        for result in results:
            if result["reachable"]:
                return True, result["message"]
        return False, " | ".join(r["message"] for r in results)
