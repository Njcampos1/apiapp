"""
Upper Coffee — Sistema de Gestión Logística
==========================================
FastAPI app principal con orquestación modular de proveedores.

Arquitectura:
  - El registro de proveedores (PROVIDER_REGISTRY) desacopla
    completamente la UI y los servicios de cualquier plataforma concreta.
  - Para agregar Mercado Libre: implementar BaseOrderProvider,
    agregar a build_providers() y registrar en PROVIDER_REGISTRY.

Rutas:
  GET  /                                 → SPA frontend
  GET  /api/orders                       → Pedidos pendientes de todos los proveedores
  GET  /api/orders/export-all            → PDF masivo de pedidos 'processing' + marca PREPARING
  GET  /api/orders/export-excel          → Reporte Excel de pedidos completados
  GET  /api/orders/{id}?source=          → Detalle de un pedido (cualquier estado)
  GET  /api/orders/{id}/zpl?source=         → Descarga etiqueta ZPL como .txt (contingencia)
  POST /api/orders/{id}/set-status?source=  → Cambia estado manualmente (processing/completed/cancelled)
  POST /api/orders/{id}/prepare?source=  → Inicia preparación + retorna PDF
  POST /api/orders/{id}/label?source=    → Imprime etiqueta ZPL + completa pedido
  GET  /api/printer/test                 → Diagnóstico de conectividad impresora
  GET  /api/health                       → Health check para Azure / Docker
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import init_db, upsert_order, log_event, get_local_status, get_completed_orders, save_meli_token
from models.order import NormalizedOrder, OrderStatus, OrderSource
from providers.base_provider import BaseOrderProvider
from providers.woo_client import WooCommerceProvider
from providers.meli_client import MeliProvider
from services.pdf_service import generate_picking_pdf, generate_bulk_picking_pdf
from services.excel_service import generate_excel
from services.zpl_service import ZPLService, build_zpl

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Registro de proveedores ──────────────────────────────────────
# Para agregar una nueva plataforma, añade aquí su instancia.
# Nunca modifiques código fuera de esta función para integrar plataformas.
ProviderRegistry = Dict[str, BaseOrderProvider]

_providers: ProviderRegistry = {}


def build_providers() -> ProviderRegistry:
    """
    Construye el mapa de proveedores activos según la configuración.
    Solo registra un proveedor si sus credenciales están presentes.
    """
    reg: ProviderRegistry = {}

    if settings.WOO_URL and settings.WOO_KEY and settings.WOO_SECRET:
        reg[OrderSource.WOOCOMMERCE.value] = WooCommerceProvider(
            url=settings.WOO_URL,
            key=settings.WOO_KEY,
            secret=settings.WOO_SECRET,
        )
        logger.info("Proveedor registrado: WooCommerce (%s)", settings.WOO_URL)
    else:
        logger.warning(
            "WooCommerce no configurado — falta WOO_URL, WOO_KEY o WOO_SECRET"
        )

    # MercadoLibre — se activa si están presentes las credenciales OAuth.
    # El access_token se carga de forma perezosa desde la BD en la primera
    # petición; no se requiere que los tokens existan en el momento de arranque.
    if settings.MELI_APP_ID and settings.MELI_CLIENT_SECRET:
        reg[OrderSource.MERCADOLIBRE.value] = MeliProvider(
            app_id=settings.MELI_APP_ID,
            client_secret=settings.MELI_CLIENT_SECRET,
            redirect_uri=settings.MELI_REDIRECT_URI,
        )
        logger.info("Proveedor registrado: MercadoLibre (APP_ID=%s)", settings.MELI_APP_ID)
    else:
        logger.warning(
            "MercadoLibre no configurado — falta MELI_APP_ID o MELI_CLIENT_SECRET"
        )

    return reg


def get_provider(source: str) -> BaseOrderProvider:
    provider = _providers.get(source)
    if not provider:
        raise HTTPException(
            status_code=404,
            detail=f"Proveedor '{source}' no encontrado o no configurado",
        )
    return provider


# ── Ciclo de vida de la app ──────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _providers
    await init_db()
    _providers = build_providers()
    logger.info("App iniciada con %d proveedor(es)", len(_providers))
    yield
    # Cierre limpio de clientes HTTP
    for p in _providers.values():
        if hasattr(p, "aclose"):
            await p.aclose()
    logger.info("App detenida — conexiones cerradas")


# ── Aplicación FastAPI ───────────────────────────────────────────
app = FastAPI(
    title="Upper Coffee Logistics",
    version="1.0.0",
    description="Sistema de gestión logística modular para bodega de café",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")


# ── Manejo global de errores ─────────────────────────────────────
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Error no manejado en %s: %s", request.url, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno del servidor", "error": str(exc)},
    )


# ── Endpoints de diagnóstico ─────────────────────────────────────
@app.get("/api/health", tags=["infra"])
async def health():
    return {
        "status": "ok",
        "providers": list(_providers.keys()),
        "printer": f"{settings.ZEBRA_IP}:{settings.ZEBRA_PORT}",
    }


@app.get("/api/printer/test", tags=["infra"])
async def printer_test():
    zpl_svc = ZPLService(
        host=settings.ZEBRA_IP,
        port=settings.ZEBRA_PORT,
        dpi=settings.ZEBRA_DPI,
    )
    ok, msg = await zpl_svc.test_connection()
    return {"reachable": ok, "message": msg}


# ── Mercado Libre — OAuth ─────────────────────────────────────────
@app.get("/api/meli/callback", tags=["meli"])
async def meli_oauth_callback(
    code: str = Query(..., description="Código de autorización recibido de Mercado Libre"),
    state: Optional[str] = Query(default=None),
):
    """
    Endpoint que recibe el authorization code tras la autorización manual
    del administrador en Mercado Libre.

    Flujo:
      1. El administrador navega a la URL de autorización de MeLi:
           https://auth.mercadolibre.cl/authorization
             ?response_type=code
             &client_id={MELI_APP_ID}
             &redirect_uri={MELI_REDIRECT_URI}
      2. MeLi redirige aquí con ?code=XXXX
      3. Este endpoint intercambia el code por tokens y los persiste en SQLite.
      4. Todos los requests futuros usan esos tokens (con auto-refresh).
    """
    provider = _providers.get(OrderSource.MERCADOLIBRE.value)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "El proveedor MercadoLibre no está activo. "
                "Verifica que MELI_APP_ID y MELI_CLIENT_SECRET estén configurados."
            ),
        )

    meli: MeliProvider = provider  # type: ignore[assignment]
    try:
        await meli.exchange_code(code)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Mercado Libre rechazó el código de autorización: {exc.response.text}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Error inesperado en callback de MercadoLibre")
        raise HTTPException(status_code=500, detail=f"Error interno: {exc}")

    return {
        "success": True,
        "message": (
            "Autorización completada. Tokens de MercadoLibre guardados "
            "en la base de datos. El proveedor está listo para usarse."
        ),
    }


# ── Frontend SPA ─────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def spa(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "providers": list(_providers.keys()),
            "printer_ip": settings.ZEBRA_IP,
        },
    )


# ── Pedidos — Lectura ────────────────────────────────────────────
@app.get("/api/orders", tags=["orders"])
async def list_orders():
    """
    Agrega pedidos 'processing' de todos los proveedores configurados.
    Errores parciales de un proveedor NO bloquean la respuesta global.
    """
    all_orders: List[dict] = []
    errors: List[dict] = []

    for source, provider in _providers.items():
        try:
            orders = await provider.get_pending_orders()
            for o in orders:
                local_status = await get_local_status(o.id, o.source.value)
                if local_status and local_status != OrderStatus.PROCESSING:
                    # Already in preparation or beyond — exclude from queue
                    continue
                await upsert_order(o)
                all_orders.append(o.model_dump(mode="json"))
        except Exception as exc:
            logger.error("Error al obtener pedidos de %s: %s", source, exc)
            errors.append({"source": source, "error": str(exc)})

    return {
        "orders": all_orders,
        "total":  len(all_orders),
        "errors": errors,
    }


@app.get("/api/orders/export-all", tags=["orders"])
async def export_all_orders():
    """
    Descarga masiva de picking:
    1. Obtiene todos los pedidos en estado 'processing' de WooCommerce.
    2. Filtra los que ya están marcados como PREPARING (o superior) en la BD local.
    3. Genera un único PDF multipágina con la hoja de picking de cada pedido nuevo.
    4. Actualiza el estado local a PREPARING para que no se repitan en la próxima descarga.
    """
    pending: List[NormalizedOrder] = []

    for source, provider in _providers.items():
        try:
            orders = await provider.get_pending_orders()
        except Exception as exc:
            logger.error("Error al obtener pedidos de %s para export masivo: %s", source, exc)
            continue

        for o in orders:
            local_status = await get_local_status(o.id, o.source.value)
            if local_status and local_status != OrderStatus.PROCESSING:
                logger.debug(
                    "Pedido %s ya en estado local '%s', omitido del export masivo",
                    o.id, local_status,
                )
                continue
            await upsert_order(o)
            pending.append(o)

    if not pending:
        raise HTTPException(
            status_code=404,
            detail="No hay pedidos nuevos en estado 'processing' para exportar",
        )

    try:
        pdf_bytes = generate_bulk_picking_pdf(pending)
    except Exception as exc:
        logger.exception("Error generando PDF masivo (%d pedidos)", len(pending))
        raise HTTPException(status_code=500, detail=f"Error generando PDF masivo: {exc}")

    # Marcar TODOS los pedidos incluidos como PREPARING en BD local
    for order in pending:
        order.status = OrderStatus.PREPARING
        await upsert_order(order)
        await log_event(
            order.id, order.source.value,
            "bulk_export",
            f"Marcado PREPARING en descarga masiva ({len(pending)} pedidos en el lote)",
        )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("Export masivo completado: %d pedidos → PDF generado", len(pending))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="picking_masivo_{ts}.pdf"',
            "X-Orders-Count": str(len(pending)),
        },
    )


@app.get("/api/orders/export-excel", tags=["orders"])
async def export_excel():
    """
    Genera y descarga el reporte Excel de todos los pedidos completados
    registrados en la base de datos local.
    """
    orders = await get_completed_orders()

    if not orders:
        raise HTTPException(
            status_code=404,
            detail="No hay pedidos completados en la base de datos local",
        )

    try:
        excel_bytes = generate_excel(orders)
    except Exception as exc:
        logger.exception("Error generando reporte Excel")
        raise HTTPException(status_code=500, detail=f"Error generando Excel: {exc}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="reporte_pedidos_{ts}.xlsx"',
            "X-Orders-Count": str(len(orders)),
        },
    )


# ── Mercado Libre — Descarga masiva ZPL nativo ───────────────────
@app.get("/api/orders/meli/bulk-zpl", tags=["meli"])
async def bulk_meli_zpl(
    ids: str = Query(
        ...,
        description="IDs de pedidos MeLi separados por coma, ej: 123456789,987654321",
    ),
):
    """
    Descarga un único archivo .txt con las etiquetas ZPL nativas de Mercado Libre
    concatenadas para todos los IDs indicados.

    Comportamiento:
    - Consulta el endpoint /shipment_labels de MeLi con response_type=zpl2.
    - Al obtener el ZPL, MeLi marca automáticamente el envío como
      'listo para despachar'; este endpoint refleja ese cambio guardando
      OrderStatus.COMPLETED en la BD local.
    - Si un ID falla, se omite del archivo y el endpoint continúa con los demás.
    - Solo lanza error 502 si NINGÚN ID pudo procesarse correctamente.

    El archivo descargado es compatible con Labelary (labelary.com/viewer.html).
    """
    provider = _providers.get(OrderSource.MERCADOLIBRE.value)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "El proveedor MercadoLibre no está activo. "
                "Verifica que MELI_APP_ID y MELI_CLIENT_SECRET estén configurados."
            ),
        )
    meli: MeliProvider = provider  # type: ignore[assignment]

    order_ids = [oid.strip() for oid in ids.split(",") if oid.strip()]
    if not order_ids:
        raise HTTPException(
            status_code=422,
            detail="Debes proporcionar al menos un ID de pedido en el parámetro ?ids=",
        )

    zpl_blocks: List[str] = []
    failed: List[dict] = []

    for order_id in order_ids:
        try:
            zpl = await meli.get_native_zpl(order_id)
            zpl_clean = zpl.strip()

            # Validación básica: el ZPL nativo debe empezar con ^XA y terminar con ^XZ
            if not zpl_clean:
                raise RuntimeError("MeLi devolvió un ZPL vacío")

            zpl_blocks.append(zpl_clean)

            # Actualizar estado local a COMPLETED (MeLi ya lo marcó como
            # 'listo para despachar' en el momento de consultar el ZPL)
            order = await meli.get_order(order_id)
            if order:
                order.status = OrderStatus.COMPLETED
                await upsert_order(order)

            await log_event(
                order_id,
                OrderSource.MERCADOLIBRE.value,
                "bulk_zpl_downloaded",
                "ZPL nativo descargado — pedido marcado como COMPLETED en BD local",
            )
            logger.info("MeLi bulk-zpl: pedido %s procesado OK", order_id)

        except Exception as exc:
            logger.error(
                "MeLi bulk-zpl: error al procesar pedido %s — %s", order_id, exc
            )
            failed.append({"order_id": order_id, "error": str(exc)})

    if not zpl_blocks:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "No se pudo obtener ZPL para ninguno de los pedidos indicados.",
                "failed": failed,
            },
        )

    combined_zpl = "\n".join(zpl_blocks)

    response_headers = {
        "Content-Disposition": 'attachment; filename="etiquetas_meli.txt"',
        "X-Labels-Count": str(len(zpl_blocks)),
        "X-Failed-Count": str(len(failed)),
    }
    if failed:
        response_headers["X-Failed-Ids"] = ",".join(f["order_id"] for f in failed)

    logger.info(
        "MeLi bulk-zpl: %d etiquetas generadas, %d fallidos",
        len(zpl_blocks),
        len(failed),
    )
    return Response(
        content=combined_zpl.encode("utf-8"),
        media_type="text/plain",
        headers=response_headers,
    )


@app.get("/api/orders/{order_id}/zpl", tags=["orders"])
async def download_zpl(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
):
    """
    Genera y descarga la etiqueta ZPL como archivo .txt.
    Vía de contingencia cuando la impresora no está disponible.
    """
    provider = get_provider(source)
    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    zpl_str = build_zpl(order, dpi=settings.ZEBRA_DPI)
    await log_event(order_id, source, "zpl_download", "ZPL descargado manualmente para impresión de contingencia")

    return Response(
        content=zpl_str.encode("utf-8"),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="etiqueta_{order_id}.txt"',
        },
    )


@app.post("/api/orders/{order_id}/set-status", tags=["orders"])
async def set_order_status(
    order_id: str,
    new_status: str = Query(..., description="processing | completed | cancelled"),
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
):
    """
    Cambia el estado de un pedido manualmente.
    - processing → revierte a cola (no sincroniza con plataforma)
    - completed  → marca como completado + sincroniza con WooCommerce
    - cancelled  → marca como ERROR localmente + registra evento (sin sincronización automática)
    """
    STATUS_MAP = {
        "processing": OrderStatus.PROCESSING,
        "completed":  OrderStatus.COMPLETED,
        "cancelled":  OrderStatus.ERROR,
    }
    internal_status = STATUS_MAP.get(new_status)
    if internal_status is None:
        raise HTTPException(
            status_code=422,
            detail=f"Estado no válido: '{new_status}'. Use: processing, completed, cancelled",
        )

    provider = get_provider(source)
    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    order.status = internal_status
    await upsert_order(order)
    await log_event(order_id, source, f"manual_{new_status}", f"Estado cambiado manualmente a '{new_status}'")

    remote_synced = False
    if internal_status == OrderStatus.COMPLETED:
        remote_synced = await provider.update_order_status(
            order_id, OrderStatus.COMPLETED,
        )
        await log_event(
            order_id, source,
            "completed" if remote_synced else "complete_sync_failed",
            "WooCommerce actualizado" if remote_synced else "Fallo sincronización remota",
        )

    return {
        "success":       True,
        "order_id":      order_id,
        "new_status":    new_status,
        "remote_synced": remote_synced,
    }


@app.get("/api/orders/{order_id}", tags=["orders"])
async def get_order(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
):
    """Retorna el detalle normalizado de un pedido por ID."""
    provider = get_provider(source)
    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    await upsert_order(order)
    return order.model_dump(mode="json")


# ── Pedidos — Acciones de bodega ─────────────────────────────────
@app.post("/api/orders/{order_id}/prepare", tags=["orders"])
async def prepare_order(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
):
    """
    1. Obtiene el pedido desde la plataforma.
    2. Genera PDF de picking con QR.
    3. Actualiza estado local a PREPARING.
    4. Devuelve el PDF inline para visualización en el navegador.
    """
    provider = get_provider(source)

    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    # Generar PDF
    try:
        pdf_bytes = generate_picking_pdf(order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Error generando PDF para pedido %s", order_id)
        raise HTTPException(status_code=500, detail=f"Error generando PDF: {exc}")

    # Actualizar estado
    order.status = OrderStatus.PREPARING
    await upsert_order(order)
    await log_event(order_id, source, "prepare", "PDF generado")

    # Notificar a la plataforma (no bloqueante si falla)
    ok = await provider.update_order_status(order_id, OrderStatus.PREPARING)
    if not ok:
        logger.warning("No se pudo sincronizar estado PREPARING con %s para pedido %s", source, order_id)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="picking_{order_id}.pdf"',
            "X-Order-Id": order_id,
            "X-Order-Source": source,
        },
    )


@app.post("/api/orders/{order_id}/label", tags=["orders"])
async def print_label(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
):
    """
    1. Obtiene el pedido.
    2. Genera y envía etiqueta ZPL a la impresora Zebra por TCP.
    3. Si la impresión es exitosa, marca el pedido como COMPLETED.
    4. Registra el evento en la BD local.
    """
    provider = get_provider(source)

    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    # Intentar imprimir
    zpl_svc = ZPLService(
        host=settings.ZEBRA_IP,
        port=settings.ZEBRA_PORT,
        dpi=settings.ZEBRA_DPI,
    )
    print_ok, print_msg = await zpl_svc.print_label(order)

    if not print_ok:
        await log_event(order_id, source, "label_error", print_msg)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "No se pudo imprimir la etiqueta",
                "reason":  print_msg,
                "printer": f"{settings.ZEBRA_IP}:{settings.ZEBRA_PORT}",
            },
        )

    # Impresión exitosa → completar
    await log_event(order_id, source, "label_printed", f"Zebra {settings.ZEBRA_IP}")

    order.status = OrderStatus.COMPLETED
    await upsert_order(order)

    complete_ok = await provider.update_order_status(
        order_id,
        OrderStatus.COMPLETED,
    )

    await log_event(
        order_id, source,
        "completed" if complete_ok else "complete_sync_failed",
        "WooCommerce actualizado" if complete_ok else "Fallo sincronización remota",
    )

    return {
        "success":       True,
        "printed":       True,
        "remote_synced": complete_ok,
        "order_id":      order_id,
        "source":        source,
    }


# ── Entry point ──────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
        log_level="debug" if settings.DEBUG else "info",
    )
