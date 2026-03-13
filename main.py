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
  GET  /api/orders/{id}?source=          → Detalle de un pedido
  POST /api/orders/{id}/prepare?source=  → Inicia preparación + retorna PDF
  POST /api/orders/{id}/label?source=    → Imprime etiqueta ZPL + completa pedido
  GET  /api/printer/test                 → Diagnóstico de conectividad impresora
  GET  /api/health                       → Health check para Azure / Docker
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Dict, List

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import init_db, upsert_order, log_event
from models.order import NormalizedOrder, OrderStatus, OrderSource
from providers.base_provider import BaseOrderProvider
from providers.woo_client import WooCommerceProvider
from services.pdf_service import generate_picking_pdf
from services.zpl_service import ZPLService

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

    # Ejemplo futuro:
    # if settings.MELI_TOKEN:
    #     reg[OrderSource.MERCADOLIBRE.value] = MeliProvider(token=settings.MELI_TOKEN)

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
            # Persistir estado local para sobrevivir reinicios
            for o in orders:
                await upsert_order(o)
            all_orders.extend([o.model_dump(mode="json") for o in orders])
        except Exception as exc:
            logger.error("Error al obtener pedidos de %s: %s", source, exc)
            errors.append({"source": source, "error": str(exc)})

    return {
        "orders": all_orders,
        "total":  len(all_orders),
        "errors": errors,
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
        note="Pedido preparado y etiquetado por bodega.",
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
