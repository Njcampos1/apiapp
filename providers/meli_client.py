"""
Proveedor Mercado Libre — implementa BaseOrderProvider.

Flujo OAuth 2.0:
  1. El administrador accede manualmente a la URL de autorización de MeLi:
       https://auth.mercadolibre.cl/authorization
         ?response_type=code
         &client_id={MELI_APP_ID}
         &redirect_uri={MELI_REDIRECT_URI}
         &state=<valor_aleatorio>
  2. Tras autorizar, MeLi redirige a MELI_REDIRECT_URI con ?code=XXXX.
  3. El endpoint GET /api/meli/callback recibe ese code y llama a
     MeliProvider.exchange_code(), que intercambia el code por tokens
     y los persiste en SQLite.
  4. A partir de ahí, todos los requests usan el access_token almacenado.
     Cuando está a punto de vencer (o devuelve 401), _do_refresh() obtiene
     un nuevo par de tokens y los persiste automáticamente.

Referencias:
  https://developers.mercadolibre.cl/es_ar/autenticacion-y-autorizacion
  https://developers.mercadolibre.cl/es_ar/referencia-de-la-api
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from config import settings
from database import get_meli_token, save_meli_token
from models.order import (
    NormalizedOrder,
    OrderItem,
    OrderSource,
    OrderStatus,
    ShippingAddress,
)
from providers.base_provider import BaseOrderProvider

logger = logging.getLogger(__name__)

# ── Constantes de la API ──────────────────────────────────────────────────────

_BASE_API = "https://api.mercadolibre.com"
_TOKEN_URL = f"{_BASE_API}/oauth/token"

# Segundos antes de la expiración real en que se adelanta el refresh
_REFRESH_MARGIN_SECONDS = 300  # 5 minutos

# Mapeo de estado MeLi → estado interno
_MELI_STATUS_MAP: dict[str, OrderStatus] = {
    "confirmed":           OrderStatus.PROCESSING,
    "payment_in_process":  OrderStatus.PROCESSING,
    "paid":                OrderStatus.PROCESSING,
    "partially_refunded":  OrderStatus.PROCESSING,
    "pending_cancel":      OrderStatus.ERROR,
    "cancelled":           OrderStatus.ERROR,
    "invalid":             OrderStatus.ERROR,
}

# Estado interno → acción sobre la API de MeLi
# MeLi no expone un endpoint genérico de cambio de estado de pedido;
# el ciclo lo gestiona la plataforma. Solo se mapea COMPLETED → feedback.
_INTERNAL_TO_MELI_FEEDBACK: dict[OrderStatus, bool] = {
    OrderStatus.COMPLETED: True,
}


# ── Dataclass de token en memoria ─────────────────────────────────────────────

@dataclass
class _TokenData:
    access_token:  str
    refresh_token: str
    expires_at:    datetime
    seller_id:     str = ""

    @property
    def is_expiring(self) -> bool:
        """True si el token vence en menos de _REFRESH_MARGIN_SECONDS."""
        margin = timedelta(seconds=_REFRESH_MARGIN_SECONDS)
        return datetime.utcnow() >= (self.expires_at - margin)


# ── Proveedor ─────────────────────────────────────────────────────────────────

class MeliProvider(BaseOrderProvider):
    """
    Integración con Mercado Libre.

    El token se carga de forma perezosa desde la BD en la primera petición
    y se mantiene en memoria hasta que el proceso se reinicia o se refresca.
    """

    source_name = OrderSource.MERCADOLIBRE.value

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._app_id = app_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._token: Optional[_TokenData] = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers={"Accept": "application/json"},
        )

    # ── OAuth — uso público ───────────────────────────────────────────────────

    async def exchange_code(self, code: str) -> None:
        """
        Intercambia el authorization code por access_token + refresh_token
        y los persiste en la BD.  Llamar desde el endpoint /api/meli/callback.

        Lanza httpx.HTTPStatusError si MeLi rechaza el código.
        """
        redirect_uri = settings.MELI_REDIRECT_URI.strip()
        logger.debug(
            "MeLi: intercambiando code por token — redirect_uri enviado: %r",
            redirect_uri,
        )
        try:
            resp = await self._client.post(
                _TOKEN_URL,
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     self._app_id,
                    "client_secret": self._client_secret,
                    "code":          code,
                    "redirect_uri":  redirect_uri,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            logger.error("MeLi: timeout al intercambiar authorization code")
            raise RuntimeError("Mercado Libre no respondió al intercambio de código")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi: error HTTP %s al intercambiar código: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

        data = resp.json()
        expires_at = datetime.utcnow() + timedelta(seconds=data["expires_in"])
        seller_id  = str(data.get("user_id", ""))

        await save_meli_token(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at,
            seller_id=seller_id,
        )
        self._token = _TokenData(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at,
            seller_id=seller_id,
        )
        logger.info(
            "MeLi: autorización exitosa. seller_id=%s, expira=%s",
            seller_id,
            expires_at.isoformat(),
        )

    # ── BaseOrderProvider ─────────────────────────────────────────────────────

    async def get_pending_orders(self) -> List[NormalizedOrder]:
        """
        Devuelve pedidos en estado 'paid' (pagados, pendientes de despacho)
        creados en los últimos 30 días.

        Filtros aplicados:
        - order.status=paid          → solo pagados.
        - order.date_created.from    → evita deep-pagination sobre el historial
                                       completo (causaba HTTP 500 en MeLi).
        - tags=not_delivered         → excluye pedidos ya entregados al comprador;
                                       no se omite aunque el status sea 'paid'
                                       porque MeLi mantiene ese status durante
                                       todo el ciclo (pago → despacho → entrega).
        """
        seller_id = await self._resolve_seller_id()
        normalized: List[NormalizedOrder] = []
        limit  = 50
        offset = 0

        # Ventana de 30 días para evitar iterar el historial completo.
        from_date = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

        while True:
            try:
                data = await self._get(
                    "/orders/search",
                    params={
                        "seller":                 seller_id,
                        "order.status":           "paid",
                        "order.date_created.from": from_date,
                        "tags":                   "not_delivered",
                        "sort":                   "date_asc",
                        "limit":                  limit,
                        "offset":                 offset,
                    },
                )
            except RuntimeError:
                raise  # Ya logueado en _get

            results = data.get("results", [])
            for raw in results:
                # Omitir pedidos Full (logística fulfillment gestionada por MeLi)
                logistic_type = (raw.get("shipping") or {}).get("logistic_type", "")
                if logistic_type == "fulfillment":
                    logger.debug(
                        "MeLi: pedido %s omitido (Full/fulfillment)",
                        raw.get("id"),
                    )
                    continue
                try:
                    normalized.append(self.normalize(raw))
                except Exception as exc:
                    logger.warning(
                        "MeLi: no se pudo normalizar pedido %s: %s",
                        raw.get("id"),
                        exc,
                    )

            paging = data.get("paging", {})
            offset += len(results)
            if not results or offset >= paging.get("total", 0):
                break

        logger.debug("MeLi: %d pedidos obtenidos", len(normalized))
        return normalized

    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]:
        try:
            data = await self._get(f"/orders/{order_id}")

            # Desde los cambios PII de MeLi, /orders/{id} solo devuelve shipping.id.
            # Mezclamos (update) en lugar de reemplazar para preservar cualquier clave
            # que ya venga de la orden y fusionar status, logistic_type,
            # receiver_address, etc., que vienen de /shipments/{id}.
            shipping_id = (data.get("shipping") or {}).get("id")
            if shipping_id:
                try:
                    shipment = await self._get(f"/shipments/{shipping_id}")
                    data["shipping"].update(shipment)
                except RuntimeError as exc:
                    logger.warning(
                        "MeLi: no se pudo enriquecer /shipments/%s para pedido %s: %s",
                        shipping_id,
                        order_id,
                        exc,
                    )

            return self.normalize(data)
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    async def update_order_status(
        self, order_id: str, status: OrderStatus
    ) -> bool:
        """
        MeLi gestiona su propio ciclo de vida; solo se envía feedback
        cuando el pedido está COMPLETADO (entregado al courier).
        Para otros estados, retorna False sin error (no-op intencional).
        """
        if status != OrderStatus.COMPLETED:
            logger.debug(
                "MeLi: estado %s no requiere acción remota para pedido %s",
                status.value,
                order_id,
            )
            return False

        try:
            resp = await self._authenticated_post(
                f"/orders/{order_id}/feedback",
                json={"fulfilled": True},
            )
            resp.raise_for_status()
            logger.info("MeLi: pedido %s marcado como cumplido", order_id)
            return True
        except httpx.TimeoutException:
            logger.error("MeLi: timeout al enviar feedback para pedido %s", order_id)
            return False
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi HTTP %s al enviar feedback para pedido %s: %s",
                exc.response.status_code,
                order_id,
                exc.response.text,
            )
            return False

    def normalize(self, raw: dict) -> NormalizedOrder:
        """
        Mapea el JSON crudo de un pedido de Mercado Libre al modelo interno.

        Estructura relevante del payload MeLi:
          raw.buyer            → datos del comprador (nickname, phone)
          raw.shipping         → objeto con receiver_address anidado (nombre real, dirección)
          raw.billing_info     → datos de facturación (doc_number = RUT)
          raw.order_items[]    → líneas de pedido
          raw.total_amount     → monto total
          raw.currency_id      → código de moneda (ej. 'CLP')
          raw.status           → estado MeLi ('paid', 'cancelled', etc.)
        """
        # ── Dirección de envío ──────────────────────────────────────────────
        shipping_raw  = raw.get("shipping") or {}
        receiver      = shipping_raw.get("receiver_address") or {}
        city_obj      = receiver.get("city")    or {}
        state_obj     = receiver.get("state")   or {}
        country_obj   = receiver.get("country") or {}

        buyer      = raw.get("buyer") or {}
        # Teléfono: priorizar receiver_address (más fiable en resultados de búsqueda)
        # ya que buyer.phone suele llegar vacío en /orders/search.
        phone_str  = ""
        rec_phone  = receiver.get("phone") or {}
        if isinstance(rec_phone, dict) and rec_phone.get("number"):
            area_code = str(rec_phone.get("area_code", "")).strip()
            num       = str(rec_phone.get("number", "")).strip()
            phone_str = f"{area_code}{num}" if area_code and not num.startswith(area_code) else num
        if not phone_str:
            # Fallback: buyer.phone
            phone_data = buyer.get("phone") or {}
            if isinstance(phone_data, dict):
                area_code = str(phone_data.get("area_code", "")).strip()
                num       = str(phone_data.get("number", "")).strip()
                if num:
                    phone_str = f"{area_code}{num}" if area_code and not num.startswith(area_code) else num

        # ── Nombre del destinatario ─────────────────────────────────────────
        # Priorizar receiver_name de la dirección de envío (más confiable que buyer).
        # El objeto buyer a veces contiene datos del titular de la cuenta, no del receptor.
        receiver_name_raw = receiver.get("receiver_name", "").strip()
        if receiver_name_raw:
            name_parts = receiver_name_raw.split(" ", 1)
            first_name = name_parts[0]
            last_name  = name_parts[1] if len(name_parts) > 1 else ""
        else:
            first_name = buyer.get("first_name", "")
            last_name  = buyer.get("last_name", "")

        # ── RUT de facturación/envío ────────────────────────────────────────
        # Intentar en billing_info de nivel raíz y luego en buyer.billing_info
        billing_info = raw.get("billing_info") or buyer.get("billing_info") or {}
        rut = (billing_info.get("doc_number") or "").strip()
        # Segundo fallback: dentro del array billing_info si viene como lista
        if not rut and isinstance(billing_info, list):
            for entry in billing_info:
                rut = (entry.get("doc_number") or "").strip()
                if rut:
                    break

        # ── Estado logístico real del envío en MeLi ─────────────────────────
        shipping_status = shipping_raw.get("status", "")

        # ── Tipo de envío — etiqueta legible ────────────────────────────────
        logistic_type_raw = shipping_raw.get("logistic_type", "")
        lt = logistic_type_raw.lower()
        if "fulfillment" in lt:
            logistic_label = "Full"
        elif "cross_docking" in lt or "xd_drop_off" in lt or "flex" in lt:
            logistic_label = "Flex"
        elif logistic_type_raw:
            logistic_label = "Despacho normal"
        else:
            logistic_label = ""

        # ── Comentario / nota del cliente ───────────────────────────────────
        # MeLi no expone notas en la API estándar, pero algunos endpoints incluyen
        # 'notes' en el root o 'additional_info' en billing_info del buyer.
        customer_note = (
            (raw.get("notes") or "")
            or (buyer.get("billing_info") or {}).get("additional_info", "")
            or ""
        ).strip()

        street   = receiver.get("street_name", "")
        number   = str(receiver.get("street_number", "") or "")
        address1 = f"{street} {number}".strip()

        shipping_addr = ShippingAddress(
            first_name=first_name,
            last_name=last_name,
            address_1=address1,
            address_2=receiver.get("comment", "") or "",
            city=city_obj.get("name", "") if isinstance(city_obj, dict) else "",
            state=state_obj.get("name", "") if isinstance(state_obj, dict) else "",
            postcode=receiver.get("zip_code", "") or "",
            country=country_obj.get("id", "") if isinstance(country_obj, dict) else "",
            phone=phone_str,
        )

        # ── Líneas de pedido ────────────────────────────────────────────────
        items: List[OrderItem] = []
        for oi in raw.get("order_items", []):
            item_data = oi.get("item") or {}
            sku = (
                item_data.get("seller_sku")
                or item_data.get("seller_custom_field")
                or str(item_data.get("id", ""))
            )
            items.append(
                OrderItem(
                    sku=sku,
                    name=item_data.get("title", "Producto sin nombre"),
                    quantity=int(oi.get("quantity", 1)),
                    price=float(oi.get("unit_price", 0)),
                )
            )

        # ── Estado ──────────────────────────────────────────────────────────
        meli_status     = raw.get("status", "paid")
        internal_status = _MELI_STATUS_MAP.get(meli_status, OrderStatus.PROCESSING)

        # ── Fecha de creación ────────────────────────────────────────────────
        created_raw = raw.get("date_created", "")
        try:
            created_at = datetime.fromisoformat(created_raw)
        except (ValueError, AttributeError):
            created_at = datetime.utcnow()

        # ── shipping_id: loguear si falta para facilitar depuración del ZPL ─
        shipping_id = shipping_raw.get("id") or None
        if not shipping_id:
            logger.warning(
                "MeLi: pedido %s sin shipping_id (shipping_raw keys: %s)",
                raw.get("id"),
                list(shipping_raw.keys()),
            )

        return NormalizedOrder(
            id=str(raw["id"]),
            source=OrderSource.MERCADOLIBRE,
            status=internal_status,
            customer_note=customer_note,
            shipping=shipping_addr,
            items=items,
            total=float(raw.get("total_amount", 0)),
            currency=raw.get("currency_id", "CLP"),
            created_at=created_at,
            platform_meta={
                "meli_status":     meli_status,
                "shipping_id":     shipping_id,
                "logistic_type":   logistic_type_raw,
                "logistic_label":  logistic_label,
                "buyer_nickname":  buyer.get("nickname", ""),
                "tags":            raw.get("tags", []),
                # Campos extendidos para el modal de detalle en el frontend
                "shipping_status": shipping_status,   # Estado logístico real de MeLi
                "rut":             rut,               # RUT de facturación/envío
                "receiver_name":   receiver_name_raw, # Nombre completo del receptor
            },
        )

    async def get_native_zpl(self, order_id: str) -> str:
        """
        Obtiene la etiqueta ZPL nativa de MeLi para el envío asociado al pedido.
        Usa el endpoint /shipment_labels con response_type=zpl2.

        Al consultar este endpoint, MeLi marca automáticamente el envío como
        'listo para despachar'; no es necesario enviar feedback adicional.

        Lanza RuntimeError si el pedido no existe, no tiene shipping_id,
        el envío no está en estado ready_to_ship, o si la API de MeLi devuelve
        un error HTTP (el body de la respuesta se incluye en el mensaje).
        """
        order = await self.get_order(order_id)
        if order is None:
            raise RuntimeError(f"Pedido {order_id} no encontrado en Mercado Libre")

        shipping_id     = order.platform_meta.get("shipping_id")
        shipping_status = order.platform_meta.get("shipping_status", "")

        logger.info(
            "MeLi: get_native_zpl — pedido=%s, shipping_id=%r, shipping_status=%r",
            order_id,
            shipping_id,
            shipping_status,
        )

        if not shipping_id:
            raise RuntimeError(
                f"Pedido {order_id} no tiene shipping_id asociado "
                f"(platform_meta={order.platform_meta!r}). "
                "Puede ser un pedido sin envío asignado o de retiro en punto."
            )

        # MeLi solo genera ZPL cuando el envío está en ready_to_ship.
        # Validar antes de hacer la petición evita un error HTTP confuso.
        if shipping_status != "ready_to_ship":
            raise RuntimeError(
                f"El envío aún no está listo para imprimir "
                f"(estado actual: {shipping_status!r}). "
                "Mercado Libre requiere estado 'ready_to_ship' para generar la etiqueta ZPL."
            )

        seller_id    = await self._resolve_seller_id()
        access_token = await self._ensure_valid_token()

        url    = f"{_BASE_API}/shipment_labels"
        params = {
            "shipment_ids":  str(shipping_id),
            "response_type": "zpl2",
            "caller.id":     seller_id,
        }

        logger.debug(
            "MeLi: solicitando ZPL nativo — shipping_id=%s, seller_id=%s (pedido %s)",
            shipping_id,
            seller_id,
            order_id,
        )

        try:
            resp = await self._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )

            # Reintento único por 401 inesperado, igual que _get() y _get_text()
            if resp.status_code == 401:
                logger.warning(
                    "MeLi: 401 en GET /shipment_labels, forzando refresh de token..."
                )
                token = await self._load_token()
                await self._do_refresh(token.refresh_token)
                resp = await self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token.access_token}"},  # type: ignore
                )

            resp.raise_for_status()
            raw_bytes = resp.content
            if raw_bytes.startswith(b"PK"):
                # MeLi devolvió un ZIP — extraer el .txt interior con el ZPL
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    txt_names = [n for n in zf.namelist() if n.endswith(".txt")]
                    if not txt_names:
                        raise RuntimeError(
                            f"MeLi devolvió ZIP sin archivo .txt para pedido {order_id}"
                        )
                    zpl = zf.read(txt_names[0]).decode("utf-8", errors="replace")
                logger.debug(
                    "MeLi: ZIP descomprimido — archivo=%s, pedido=%s",
                    txt_names[0],
                    order_id,
                )
            else:
                zpl = raw_bytes.decode("utf-8", errors="replace")

        except httpx.TimeoutException:
            logger.error("MeLi: timeout al obtener ZPL para pedido %s", order_id)
            raise RuntimeError(
                f"Mercado Libre no respondió a tiempo al solicitar ZPL del pedido {order_id}"
            )
        except httpx.HTTPStatusError as exc:
            meli_error = exc.response.text
            logger.error(
                "MeLi HTTP %s al obtener ZPL — pedido=%s, shipping_id=%s, respuesta=%s",
                exc.response.status_code,
                order_id,
                shipping_id,
                meli_error,
            )
            raise RuntimeError(
                f"Mercado Libre rechazó la etiqueta ZPL para pedido {order_id} "
                f"(HTTP {exc.response.status_code}): {meli_error}"
            )

        if not zpl or not zpl.strip():
            raise RuntimeError(
                f"MeLi devolvió ZPL vacío para pedido {order_id} (shipping_id={shipping_id})"
            )
        logger.info(
            "MeLi: ZPL obtenido — pedido %s, shipping_id=%s (%d bytes)",
            order_id,
            shipping_id,
            len(zpl),
        )
        return zpl

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Métodos privados — autenticación ──────────────────────────────────────

    async def _load_token(self) -> _TokenData:
        """Carga el token desde BD si no está en memoria."""
        if self._token is not None:
            return self._token

        row = await get_meli_token()
        if row is None:
            raise RuntimeError(
                "MercadoLibre no tiene tokens guardados. "
                "Complete el flujo OAuth en /api/meli/callback primero."
            )

        self._token = _TokenData(
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=row["expires_at"],
            seller_id=row["seller_id"],
        )
        return self._token

    async def _ensure_valid_token(self) -> str:
        """
        Devuelve un access_token válido.
        Si el token está próximo a vencer, hace el refresh antes de continuar.
        """
        token = await self._load_token()
        if token.is_expiring:
            logger.info("MeLi: token próximo a vencer, refrescando...")
            await self._do_refresh(token.refresh_token)
        return self._token.access_token  # type: ignore[union-attr]

    async def _do_refresh(self, refresh_token: str) -> None:
        """Obtiene un nuevo par de tokens usando el refresh_token y los persiste."""
        try:
            resp = await self._client.post(
                _TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     self._app_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            logger.error("MeLi: timeout al refrescar token")
            raise RuntimeError("Mercado Libre no respondió al refresh de token")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi: error HTTP %s al refrescar token: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise RuntimeError(
                f"Mercado Libre rechazó el refresh de token (HTTP {exc.response.status_code})"
            )

        data       = resp.json()
        expires_at = datetime.utcnow() + timedelta(seconds=data["expires_in"])
        seller_id  = self._token.seller_id if self._token else ""

        await save_meli_token(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at,
        )
        # Actualizar caché en memoria
        self._token = _TokenData(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at,
            seller_id=seller_id,
        )
        logger.info("MeLi: token refrescado exitosamente (expira: %s)", expires_at.isoformat())

    # ── Métodos privados — HTTP ───────────────────────────────────────────────

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """
        GET autenticado con reintentos automáticos en caso de 401
        (token expirado fuera de banda, ej. revocado desde el panel MeLi).
        """
        access_token = await self._ensure_valid_token()
        url = f"{_BASE_API}{path}"

        try:
            resp = await self._client.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            # Reintento único por 401 (token expirado de forma inesperada)
            if resp.status_code == 401:
                logger.warning("MeLi: 401 en GET %s, forzando refresh de token...", path)
                token = await self._load_token()
                await self._do_refresh(token.refresh_token)
                resp = await self._client.get(
                    url,
                    params=params or {},
                    headers={"Authorization": f"Bearer {self._token.access_token}"},  # type: ignore
                )

            if resp.status_code == 404:
                raise RuntimeError(f"404 — recurso no encontrado: {path}")

            resp.raise_for_status()
            return resp.json()

        except httpx.TimeoutException:
            logger.error("MeLi: timeout en GET %s", path)
            raise RuntimeError(f"Mercado Libre no respondió a tiempo en GET {path}")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi HTTP %s en GET %s: %s",
                exc.response.status_code,
                path,
                exc.response.text,
            )
            raise RuntimeError(
                f"Mercado Libre error HTTP {exc.response.status_code} en {path}"
            )

    async def _authenticated_post(self, path: str, **kwargs) -> httpx.Response:
        """POST autenticado simple (sin reintentos — usado para feedback)."""
        access_token = await self._ensure_valid_token()
        return await self._client.post(
            f"{_BASE_API}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            **kwargs,
        )

    async def _get_text(self, path: str, params: Optional[dict] = None) -> str:
        """
        GET autenticado que retorna la respuesta como texto plano (no JSON).
        Usado para endpoints que devuelven ZPL u otros formatos no-JSON.
        Incluye reintento único por 401, idéntico al comportamiento de _get().
        """
        access_token = await self._ensure_valid_token()
        url = f"{_BASE_API}{path}"

        try:
            resp = await self._client.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if resp.status_code == 401:
                logger.warning("MeLi: 401 en GET %s, forzando refresh de token...", path)
                token = await self._load_token()
                await self._do_refresh(token.refresh_token)
                resp = await self._client.get(
                    url,
                    params=params or {},
                    headers={"Authorization": f"Bearer {self._token.access_token}"},  # type: ignore
                )

            resp.raise_for_status()
            return resp.text

        except httpx.TimeoutException:
            logger.error("MeLi: timeout en GET %s", path)
            raise RuntimeError(f"Mercado Libre no respondió a tiempo en GET {path}")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi HTTP %s en GET %s: %s",
                exc.response.status_code,
                path,
                exc.response.text,
            )
            raise RuntimeError(
                f"Mercado Libre error HTTP {exc.response.status_code} en {path}"
            )

    async def _get_bytes(self, path: str, params: Optional[dict] = None) -> bytes:
        """
        GET autenticado que retorna la respuesta como bytes crudos (resp.content).
        Usado cuando el endpoint puede devolver contenido binario (ej.: ZIP con ZPL).
        Incluye reintento único por 401, idéntico al comportamiento de _get_text().
        """
        access_token = await self._ensure_valid_token()
        url = f"{_BASE_API}{path}"

        try:
            resp = await self._client.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if resp.status_code == 401:
                logger.warning("MeLi: 401 en GET %s, forzando refresh de token...", path)
                token = await self._load_token()
                await self._do_refresh(token.refresh_token)
                resp = await self._client.get(
                    url,
                    params=params or {},
                    headers={"Authorization": f"Bearer {self._token.access_token}"},  # type: ignore
                )

            resp.raise_for_status()
            return resp.content

        except httpx.TimeoutException:
            logger.error("MeLi: timeout en GET %s", path)
            raise RuntimeError(f"Mercado Libre no respondió a tiempo en GET {path}")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "MeLi HTTP %s en GET %s: %s",
                exc.response.status_code,
                path,
                exc.response.text,
            )
            raise RuntimeError(
                f"Mercado Libre error HTTP {exc.response.status_code} en {path}"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _resolve_seller_id(self) -> str:
        """
        Devuelve el seller_id almacenado. Si aún no está guardado (tokens
        obtenidos con una versión anterior del sistema), lo obtiene de
        GET /users/me y lo persiste.
        """
        token = await self._load_token()
        if token.seller_id:
            return token.seller_id

        logger.info("MeLi: seller_id no almacenado, consultando /users/me...")
        data      = await self._get("/users/me")
        seller_id = str(data["id"])

        await save_meli_token(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            seller_id=seller_id,
        )
        self._token.seller_id = seller_id  # type: ignore[union-attr]
        logger.info("MeLi: seller_id=%s guardado", seller_id)
        return seller_id
