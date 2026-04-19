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

import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
import zipfile

import httpx
import jwt
import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from config import settings
from database import (
    create_user,
    ensure_default_admin_user,
    get_all_users,
    get_user_by_username,
    init_db,
    upsert_order,
    log_event,
    get_local_status,
    get_completed_orders,
    get_preparing_orders,
    save_meli_token,
    get_or_create_open_manifest,
    close_manifest,
    get_manifest_orders,
    get_open_manifest_info,
)
from models.order import NormalizedOrder, OrderStatus, OrderSource
from providers.base_provider import BaseOrderProvider
from providers.woo_client import WooCommerceProvider
from providers.meli_client import MeliProvider
from services.pdf_service import generate_picking_pdf, generate_bulk_picking_pdf
from services.excel_service import generate_excel
from services.zpl_service import ZPLService, build_zpl_main, build_zpl_note
from services.pack_service import enrich_order_with_pack_info, enrich_orders_with_pack_info
from services import pack_service
from services.chilexpress_service import generate_chilexpress_csv

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
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)
JWT_ALGORITHM = "HS256"
SKUS_JSON_PATH = Path("data") / "skus.json"
SKUS_AUDIT_PATH = Path("data") / "skus_audit.jsonl"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=8, max_length=256)


class UserResponse(BaseModel):
    id: int
    username: str


def _require_non_empty_string(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field_path}' debe ser un string no vacío",
        )
    return value.strip()


def _require_int(value: Any, field_path: str, min_value: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field_path}' debe ser un número entero",
        )

    if value < min_value:
        comparator = ">" if min_value > 0 else ">="
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field_path}' debe ser {comparator} {min_value if min_value > 0 else 0}",
        )
    return value


def _require_string_list(value: Any, field_path: str) -> list[str]:
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field_path}' debe ser una lista",
        )

    validated: list[str] = []
    for idx, item in enumerate(value):
        validated.append(_require_non_empty_string(item, f"{field_path}[{idx}]"))
    return validated


def validate_skus_schema(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El JSON debe ser un objeto en la raíz",
        )

    catalogo = payload.get("catalogo")
    if not isinstance(catalogo, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El JSON debe contener 'catalogo' como objeto",
        )

    required_categories = ("packs_prearmados", "mix_personalizables", "otros_productos")
    for category in required_categories:
        if category not in catalogo:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Falta la categoría obligatoria 'catalogo.{category}'",
            )

        if not isinstance(catalogo[category], list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'catalogo.{category}' debe ser una lista",
            )

    def validate_common_product_fields(item: dict[str, Any], path: str) -> None:
        _require_non_empty_string(item.get("sku_principal"), f"{path}.sku_principal")
        _require_non_empty_string(item.get("nombre"), f"{path}.nombre")
        _require_string_list(item.get("skus_alias"), f"{path}.skus_alias")
        channels = _require_string_list(item.get("canales_venta"), f"{path}.canales_venta")
        if not channels:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.canales_venta' no puede estar vacío",
            )

    for idx, pack in enumerate(catalogo["packs_prearmados"]):
        path = f"catalogo.packs_prearmados[{idx}]"
        if not isinstance(pack, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}' debe ser un objeto",
            )

        validate_common_product_fields(pack, path)
        _require_int(pack.get("cantidad_total_capsulas"), f"{path}.cantidad_total_capsulas", min_value=1)

        if not isinstance(pack.get("es_personalizable"), bool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.es_personalizable' debe ser booleano",
            )

        contenido = pack.get("contenido")
        if not isinstance(contenido, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.contenido' debe ser una lista",
            )

        if not contenido:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.contenido' no puede estar vacío",
            )

        for content_idx, item in enumerate(contenido):
            content_path = f"{path}.contenido[{content_idx}]"
            if not isinstance(item, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{content_path}' debe ser un objeto",
                )

            _require_non_empty_string(item.get("sku_unitario"), f"{content_path}.sku_unitario")
            _require_non_empty_string(item.get("sabor"), f"{content_path}.sabor")
            _require_int(item.get("cajas_de_10"), f"{content_path}.cajas_de_10", min_value=1)

    for idx, mix in enumerate(catalogo["mix_personalizables"]):
        path = f"catalogo.mix_personalizables[{idx}]"
        if not isinstance(mix, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}' debe ser un objeto",
            )

        validate_common_product_fields(mix, path)
        _require_int(mix.get("cantidad_total_capsulas"), f"{path}.cantidad_total_capsulas", min_value=1)
        _require_int(mix.get("cajas_a_elegir"), f"{path}.cajas_a_elegir", min_value=1)
        _require_non_empty_string(mix.get("restricciones_sabores"), f"{path}.restricciones_sabores")

        if not isinstance(mix.get("es_personalizable"), bool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.es_personalizable' debe ser booleano",
            )

        contenido = mix.get("contenido")
        if not isinstance(contenido, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}.contenido' debe ser una lista",
            )

        for content_idx, item in enumerate(contenido):
            content_path = f"{path}.contenido[{content_idx}]"
            if not isinstance(item, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{content_path}' debe ser un objeto",
                )

            _require_non_empty_string(item.get("sku_unitario"), f"{content_path}.sku_unitario")
            _require_non_empty_string(item.get("sabor"), f"{content_path}.sabor")
            _require_int(item.get("cajas_de_10"), f"{content_path}.cajas_de_10", min_value=1)

    for idx, item in enumerate(catalogo["otros_productos"]):
        path = f"catalogo.otros_productos[{idx}]"
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{path}' debe ser un objeto",
            )

        validate_common_product_fields(item, path)
        _require_non_empty_string(item.get("categoria"), f"{path}.categoria")


def _build_sku_dict(catalogo: dict[str, Any], category: str) -> dict[str, dict[str, Any]]:
    items = catalogo.get(category, [])
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku_principal")
        if isinstance(sku, str) and sku:
            result[sku] = item
    return result


def summarize_sku_changes(old_payload: dict[str, Any], new_payload: dict[str, Any]) -> dict[str, Any]:
    categories = ("packs_prearmados", "mix_personalizables", "otros_productos")
    old_catalog = old_payload.get("catalogo", {}) if isinstance(old_payload, dict) else {}
    new_catalog = new_payload.get("catalogo", {}) if isinstance(new_payload, dict) else {}

    by_category: dict[str, dict[str, Any]] = {}
    totals = {
        "added": 0,
        "removed": 0,
        "modified": 0,
    }

    for category in categories:
        old_map = _build_sku_dict(old_catalog, category)
        new_map = _build_sku_dict(new_catalog, category)

        old_skus = set(old_map.keys())
        new_skus = set(new_map.keys())

        added = sorted(new_skus - old_skus)
        removed = sorted(old_skus - new_skus)
        common = old_skus & new_skus

        modified = sorted(
            sku
            for sku in common
            if json.dumps(old_map[sku], ensure_ascii=False, sort_keys=True)
            != json.dumps(new_map[sku], ensure_ascii=False, sort_keys=True)
        )

        totals["added"] += len(added)
        totals["removed"] += len(removed)
        totals["modified"] += len(modified)

        by_category[category] = {
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
            "added_skus": added[:20],
            "removed_skus": removed[:20],
            "modified_skus": modified[:20],
        }

    return {
        "totals": totals,
        "by_category": by_category,
    }


def create_skus_backup() -> Path | None:
    if not SKUS_JSON_PATH.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SKUS_JSON_PATH.with_name(f"skus.backup.{timestamp}.json")
    shutil.copy2(SKUS_JSON_PATH, backup_path)
    return backup_path


def append_skus_audit_entry(
    admin_username: str,
    summary: dict[str, Any],
    backup_path: Path | None,
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "admin": admin_username,
        "backup_file": backup_path.name if backup_path else None,
        "summary": summary,
    }

    with open(SKUS_AUDIT_PATH, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    role = "admin" if username == settings.DEFAULT_ADMIN_USERNAME else "user"
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict[str, Any]:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No autenticado",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized

    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except InvalidTokenError:
        raise unauthorized

    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise unauthorized

    user = await get_user_by_username(username)
    if user is None:
        raise unauthorized

    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": user["username"] == settings.DEFAULT_ADMIN_USERNAME,
    }


async def get_admin_user(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if not current_user.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso restringido a administrador",
        )
    return current_user


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

    if not settings.SECRET_KEY.strip():
        raise RuntimeError("SECRET_KEY no está configurada. Define una clave segura en .env")

    if settings.DEFAULT_ADMIN_PASSWORD == "admin123":
        logger.warning(
            "Se está usando DEFAULT_ADMIN_PASSWORD por defecto. Cámbiala en producción."
        )
    await ensure_default_admin_user(
        settings.DEFAULT_ADMIN_USERNAME,
        hash_password(settings.DEFAULT_ADMIN_PASSWORD),
    )

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

# ── Archivos estáticos ───────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


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


@app.post("/api/login", tags=["auth"])
async def login(payload: LoginRequest):
    user = await get_user_by_username(payload.username)
    if not user or not verify_password(payload.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )

    access_token = create_access_token(user["username"])
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "is_admin": user["username"] == settings.DEFAULT_ADMIN_USERNAME,
    }


@app.post("/api/users", tags=["users"], response_model=UserResponse)
async def create_user_endpoint(
    payload: UserCreate,
    _admin_user: dict[str, Any] = Depends(get_admin_user),
):
    existing_user = await get_user_by_username(payload.username)
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario ya existe",
        )

    user_id = await create_user(
        username=payload.username,
        hashed_password=hash_password(payload.password),
    )

    return UserResponse(id=user_id, username=payload.username)


@app.get("/api/users", tags=["users"], response_model=list[UserResponse])
async def list_users_endpoint(
    _admin_user: dict[str, Any] = Depends(get_admin_user),
):
    users = await get_all_users()
    return [UserResponse(id=u["id"], username=u["username"]) for u in users]


@app.get("/api/skus", tags=["packs"])
async def get_skus(_current_user: dict[str, Any] = Depends(get_current_user)):
    try:
        with open(SKUS_JSON_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró el archivo de SKUs",
        )
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="El archivo de SKUs contiene JSON inválido",
        )


@app.put("/api/skus", tags=["packs"])
async def update_skus(
    payload: Any = Body(...),
    _admin_user: dict[str, Any] = Depends(get_admin_user),
):
    validate_skus_schema(payload)

    old_payload: dict[str, Any] = {}
    if SKUS_JSON_PATH.exists():
        try:
            with open(SKUS_JSON_PATH, encoding="utf-8") as file:
                loaded = json.load(file)
                if isinstance(loaded, dict):
                    old_payload = loaded
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="El archivo actual de SKUs contiene JSON inválido; no se puede versionar",
            )

    change_summary = summarize_sku_changes(old_payload, payload)

    try:
        backup_path = create_skus_backup()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No se pudo generar respaldo de SKUs: {exc}",
        )

    try:
        with open(SKUS_JSON_PATH, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No se pudo guardar el archivo de SKUs: {exc}",
        )

    pack_service._load_full_catalog.cache_clear()

    admin_username = str(_admin_user.get("username", "admin"))
    append_skus_audit_entry(
        admin_username=admin_username,
        summary=change_summary,
        backup_path=backup_path,
    )

    summary_text = (
        f"admin={admin_username}; "
        f"added={change_summary['totals']['added']}; "
        f"removed={change_summary['totals']['removed']}; "
        f"modified={change_summary['totals']['modified']}; "
        f"backup={(backup_path.name if backup_path else 'none')}"
    )
    await log_event("skus", "system", "skus_updated", summary_text)

    return {
        "message": "SKUs actualizados correctamente",
        "backup_file": backup_path.name if backup_path else None,
        "changed": change_summary,
    }


@app.get("/api/printer/test", tags=["infra"])
async def printer_test(_current_user: dict[str, Any] = Depends(get_current_user)):
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
    _current_user: dict[str, Any] = Depends(get_current_user),
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
async def list_orders(_current_user: dict[str, Any] = Depends(get_current_user)):
    """
    Agrega pedidos 'processing' de todos los proveedores configurados, más
    los pedidos WooCommerce en estado 'preparing' (hojas impresas recuperables).

    IMPORTANTE: Los pedidos de Mercado Libre que fueron marcados localmente como
    COMPLETED se incluyen en la respuesta con su estado local preservado. Esto
    permite que el frontend los muestre en verde (como 'Etiqueta generada')
    incluso después de refrescar la página, hasta que Mercado Libre confirme
    que el envío ya no está pendiente (ej. estado shipped/delivered en la API).

    Errores parciales de un proveedor NO bloquean la respuesta global.
    """
    all_orders: List[dict] = []
    errors: List[dict] = []

    for source, provider in _providers.items():
        try:
            orders = await provider.get_pending_orders()
            for o in orders:
                local_status = await get_local_status(o.id, o.source.value)

                # Para pedidos de WooCommerce: excluir si ya están en preparación o más allá
                # (estos se obtienen por separado de get_preparing_orders más abajo)
                if o.source == OrderSource.WOOCOMMERCE:
                    if local_status and local_status != OrderStatus.PROCESSING:
                        continue

                # Para Mercado Libre: preservar estado local si existe
                # Esto permite mostrar pedidos completados localmente en verde
                # hasta que MeLi confirme el cambio de estado en su API
                if local_status:
                    o.status = local_status

                await upsert_order(o)
                # Enriquecer con información de desglose de Packs
                o = enrich_order_with_pack_info(o)
                all_orders.append(o.model_dump(mode="json"))
        except Exception as exc:
            logger.error("Error al obtener pedidos de %s: %s", source, exc)
            errors.append({"source": source, "error": str(exc)})

    # Incluir pedidos WooCommerce en estado PREPARING para recuperación de hojas.
    # Permite al operario re-imprimir el PDF si la hoja se extravió en bodega.
    try:
        existing_ids = {o["id"] for o in all_orders}
        preparing = await get_preparing_orders()
        for o in preparing:
            if o.id not in existing_ids:
                # Enriquecer con información de desglose de Packs
                o = enrich_order_with_pack_info(o)
                all_orders.append(o.model_dump(mode="json"))
    except Exception as exc:
        logger.error("Error al obtener pedidos PREPARING para recuperación: %s", exc)

    return {
        "orders": all_orders,
        "total":  len(all_orders),
        "errors": errors,
    }


@app.get("/api/orders/export-all", tags=["orders"])
async def export_all_orders(_current_user: dict[str, Any] = Depends(get_current_user)):
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

    # Enriquecer pedidos con información de Packs antes de generar PDF
    pending = enrich_orders_with_pack_info(pending)

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
async def export_excel(_current_user: dict[str, Any] = Depends(get_current_user)):
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


# ── Manifiestos (Lotes de Despacho) ──────────────────────────────
@app.post("/api/manifests/close", tags=["manifests"])
async def close_daily_manifest(_current_user: dict[str, Any] = Depends(get_current_user)):
    """
    Cierra el manifest abierto actual y genera un ZIP con:
    - Excel de todos los pedidos del manifest
    - CSV de Chilexpress (solo pedidos regionales)

    El ZIP se nombra: despachos_YYYYMMDD.zip

    Retorna:
        - 404 si no hay manifest abierto
        - 422 si el manifest está vacío
        - 200 con el archivo ZIP
    """
    # Obtener manifest abierto
    manifest_info = await get_open_manifest_info()

    if not manifest_info:
        raise HTTPException(
            status_code=404,
            detail="No hay manifest abierto para cerrar"
        )

    manifest_id = manifest_info["id"]
    order_count = manifest_info["order_count"]

    if order_count == 0:
        raise HTTPException(
            status_code=422,
            detail="El manifest está vacío. No se puede cerrar un manifest sin pedidos."
        )

    # Obtener pedidos del manifest
    orders = await get_manifest_orders(manifest_id)

    if not orders:
        raise HTTPException(
            status_code=422,
            detail="No se pudieron recuperar los pedidos del manifest"
        )

    # Cerrar el manifest
    closed = await close_manifest(manifest_id)

    if not closed:
        raise HTTPException(
            status_code=409,
            detail="El manifest ya estaba cerrado o no existe"
        )

    # Registrar evento
    await log_event(
        str(manifest_id),
        "system",
        "manifest_closed",
        f"Manifest #{manifest_id} cerrado con {order_count} pedidos"
    )

    # Generar archivos
    try:
        excel_bytes = generate_excel(orders)
    except Exception as exc:
        logger.exception("Error generando Excel para manifest #%d", manifest_id)
        raise HTTPException(
            status_code=500,
            detail=f"Error generando Excel: {exc}"
        )

    try:
        chilexpress_bytes = generate_chilexpress_csv(orders)
    except Exception as exc:
        logger.exception("Error generando CSV Chilexpress para manifest #%d", manifest_id)
        raise HTTPException(
            status_code=500,
            detail=f"Error generando CSV Chilexpress: {exc}"
        )

    # Crear ZIP
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Agregar Excel
        zip_file.writestr("planilla_despachos.xlsx", excel_bytes)
        # Agregar CSV
        zip_file.writestr("chilexpress_regional.csv", chilexpress_bytes)

    zip_buffer.seek(0)
    zip_bytes = zip_buffer.getvalue()

    # Nombre del archivo con fecha del cierre
    fecha = datetime.utcnow().strftime("%Y%m%d")
    filename = f"despachos_{fecha}.zip"

    logger.info(
        "Manifest #%d cerrado exitosamente. ZIP generado: %s (%d pedidos)",
        manifest_id, filename, order_count
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Manifest-Id": str(manifest_id),
            "X-Orders-Count": str(order_count),
        },
    )


@app.get("/api/manifests/current", tags=["manifests"])
async def get_current_manifest(_current_user: dict[str, Any] = Depends(get_current_user)):
    """
    Devuelve información del manifest abierto actual.
    Útil para mostrar en el frontend cuántos pedidos lleva el día.
    """
    manifest_info = await get_open_manifest_info()

    if not manifest_info:
        return {
            "exists": False,
            "id": None,
            "created_at": None,
            "order_count": 0
        }

    return {
        "exists": True,
        **manifest_info
    }


# ── Mercado Libre — Descarga masiva ZPL nativo ───────────────────
@app.get("/api/orders/meli/bulk-zpl", tags=["meli"])
async def bulk_meli_zpl(
    ids: str = Query(
        ...,
        description="IDs de pedidos MeLi separados por coma, ej: 123456789,987654321",
    ),
    _current_user: dict[str, Any] = Depends(get_current_user),
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
    already_processed: List[str] = []

    for order_id in order_ids:
        try:
            zpl = await meli.get_native_zpl(order_id)
            zpl_clean = zpl.strip()

            # Validación básica: el ZPL nativo debe empezar con ^XA y terminar con ^XZ
            if not zpl_clean:
                raise RuntimeError("MeLi devolvió un ZPL vacío")

            zpl_blocks.append(zpl_clean)

            # Actualizar estado local a COMPLETED y registrar timestamp de impresión
            # (MeLi ya lo marcó como 'listo para despachar' en el momento de consultar el ZPL)
            order = await meli.get_order(order_id)
            if order:
                # Solo asignar label_printed_at si es la primera vez (no está en BD)
                existing_status = await get_local_status(order_id, OrderSource.MERCADOLIBRE.value)
                if not existing_status or existing_status != OrderStatus.COMPLETED:
                    order.label_printed_at = datetime.utcnow()
                order.status = OrderStatus.COMPLETED
                await upsert_order(order)

            await log_event(
                order_id,
                OrderSource.MERCADOLIBRE.value,
                "bulk_zpl_downloaded",
                "ZPL nativo descargado — pedido marcado como COMPLETED en BD local",
            )
            logger.info("MeLi bulk-zpl: pedido %s procesado OK", order_id)

        except RuntimeError as exc:
            error_msg = str(exc)
            # Detectar si el pedido ya fue procesado/entregado (picked_up, shipped, delivered)
            if "ya fue procesado" in error_msg.lower() or \
               "picked_up" in error_msg.lower() or \
               "shipped" in error_msg.lower() or \
               "delivered" in error_msg.lower() or \
               "dropped_off" in error_msg.lower():
                logger.info(
                    "MeLi bulk-zpl: pedido %s ya fue procesado/entregado externamente, "
                    "marcando como COMPLETED sin agregar a planilla de despachos",
                    order_id
                )
                already_processed.append(order_id)

                # Marcar como COMPLETED en BD local para que no aparezca como pendiente,
                # pero SIN establecer label_printed_at ni asignar a manifest (se procesó fuera)
                try:
                    order = await meli.get_order(order_id)
                    if order:
                        order.status = OrderStatus.COMPLETED
                        order.completed_at = datetime.utcnow()
                        # Guardar solo actualizando el estado, sin asignar a manifest
                        await upsert_order(order)

                        await log_event(
                            order_id,
                            OrderSource.MERCADOLIBRE.value,
                            "already_delivered_externally",
                            "Pedido ya procesado externamente - marcado COMPLETED sin planilla"
                        )
                except Exception as sub_exc:
                    logger.warning(
                        "No se pudo marcar pedido %s como completado externamente: %s",
                        order_id, sub_exc
                    )
                continue

            # Otros errores: registrar y continuar
            logger.error(
                "MeLi bulk-zpl: error al procesar pedido %s — %s", order_id, exc
            )
            failed.append({"order_id": order_id, "error": error_msg})
        except Exception as exc:
            logger.error(
                "MeLi bulk-zpl: error inesperado al procesar pedido %s — %s", order_id, exc
            )
            failed.append({"order_id": order_id, "error": str(exc)})

    if not zpl_blocks:
        detail_msg = {
            "message": "No se pudo obtener ZPL para ninguno de los pedidos indicados.",
            "failed": failed,
        }
        if already_processed:
            detail_msg["already_processed"] = already_processed
            detail_msg["message"] += " Algunos pedidos ya fueron procesados externamente."
        raise HTTPException(status_code=502, detail=detail_msg)

    # Concatenación con doble salto de línea entre bloques ZPL.
    # Cada bloque es un documento completo ^XA ... ^XZ; el doble \n
    # garantiza legibilidad al pegar en visores como Labelary.
    combined_zpl = "\n\n".join(zpl_blocks)

    response_headers = {
        "Content-Disposition": 'attachment; filename="etiquetas_meli.txt"',
        "X-Labels-Count": str(len(zpl_blocks)),
        "X-Failed-Count": str(len(failed)),
        "X-Already-Processed-Count": str(len(already_processed)),
    }
    if failed:
        response_headers["X-Failed-Ids"] = ",".join(f["order_id"] for f in failed)
    if already_processed:
        response_headers["X-Already-Processed-Ids"] = ",".join(already_processed)

    logger.info(
        "MeLi bulk-zpl: %d etiquetas generadas, %d fallidos, %d ya procesados externamente",
        len(zpl_blocks),
        len(failed),
        len(already_processed),
    )
    return Response(
        content=combined_zpl.encode("utf-8"),
        media_type="text/plain",
        headers=response_headers,
    )


# ── Mercado Libre — Descarga masiva PDF picking ──────────────────
@app.get("/api/orders/meli/bulk-pdf", tags=["meli"])
async def bulk_meli_pdf(
    ids: str = Query(
        ...,
        description="IDs de pedidos MeLi separados por coma, ej: 123456789,987654321",
    ),
    _current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    Descarga un único PDF multipágina con las hojas de picking de los pedidos
    de Mercado Libre indicados.

    Comportamiento:
    - Consulta cada pedido desde la API de MeLi.
    - Genera un PDF multipágina con la hoja de picking de cada pedido.
    - Si un ID falla, se omite del PDF y el endpoint continúa con los demás.
    - Solo lanza error 502 si NINGÚN ID pudo procesarse correctamente.

    El archivo descargado contiene todas las hojas en un solo documento.
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

    orders: List[NormalizedOrder] = []
    failed: List[dict] = []

    for order_id in order_ids:
        try:
            order = await meli.get_order(order_id)
            if not order:
                raise RuntimeError(f"Pedido {order_id} no encontrado")
            orders.append(order)
            logger.info("MeLi bulk-pdf: pedido %s obtenido OK", order_id)
        except Exception as exc:
            logger.error(
                "MeLi bulk-pdf: error al obtener pedido %s — %s", order_id, exc
            )
            failed.append({"order_id": order_id, "error": str(exc)})

    if not orders:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "No se pudo obtener ninguno de los pedidos indicados.",
                "failed": failed,
            },
        )

    # Enriquecer pedidos con información de Packs antes de generar PDF
    orders = enrich_orders_with_pack_info(orders)

    # Generar PDF multipágina
    try:
        pdf_bytes = generate_bulk_picking_pdf(orders)
    except Exception as exc:
        logger.exception("Error generando PDF masivo de picking para MeLi (%d pedidos)", len(orders))
        raise HTTPException(
            status_code=500,
            detail=f"Error generando PDF masivo: {exc}"
        )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    response_headers = {
        "Content-Disposition": f'attachment; filename="picking_meli_masivo_{ts}.pdf"',
        "X-Orders-Count": str(len(orders)),
        "X-Failed-Count": str(len(failed)),
    }
    if failed:
        response_headers["X-Failed-Ids"] = ",".join(f["order_id"] for f in failed)

    logger.info(
        "MeLi bulk-pdf: %d hojas generadas, %d fallidos",
        len(orders),
        len(failed),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers=response_headers,
    )


# ── Mercado Libre — Pedidos Full (informativos) ──────────────────
# NOTA: Endpoint deshabilitado - funcionalidad removida del frontend
# @app.get("/api/orders/meli/full", tags=["meli"])
# async def list_full_orders():
#     """
#     Lista pedidos Full (fulfillment) de Mercado Libre de los últimos 30 días.
#
#     Estos pedidos NO se preparan en bodega ya que MeLi los gestiona
#     directamente desde sus centros de distribución. Este endpoint es
#     solo informativo para control y seguimiento.
#
#     Los datos de RUT/teléfono pueden estar bloqueados por PII si el
#     pedido ya fue despachado.
#     """
#     provider = _providers.get(OrderSource.MERCADOLIBRE.value)
#     if provider is None:
#         raise HTTPException(
#             status_code=503,
#             detail=(
#                 "El proveedor MercadoLibre no está activo. "
#                 "Verifica que MELI_APP_ID y MELI_CLIENT_SECRET estén configurados."
#             ),
#         )
#
#     meli: MeliProvider = provider  # type: ignore[assignment]
#
#     try:
#         orders = await meli.get_full_orders()
#         return {
#             "orders": [o.model_dump(mode="json") for o in orders],
#             "total": len(orders),
#         }
#     except Exception as exc:
#         logger.error("Error al obtener pedidos Full de MeLi: %s", exc)
#         raise HTTPException(
#             status_code=502,
#             detail=f"Error al obtener pedidos Full: {str(exc)}"
#         )


@app.get("/api/orders/{order_id}/zpl", tags=["orders"])
async def download_zpl(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
    _current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    Genera y descarga la etiqueta ZPL como archivo .txt.
    - Para MercadoLibre: obtiene el ZPL nativo de la API
    - Para WooCommerce: genera el ZPL localmente
    Vía de contingencia cuando la impresora no está disponible.
    """
    provider = get_provider(source)
    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    # Para MercadoLibre: usar ZPL nativo; para otros: generar localmente
    if source == OrderSource.MERCADOLIBRE.value:
        meli: MeliProvider = provider  # type: ignore[assignment]
        try:
            zpl_str = await meli.get_native_zpl(order_id)
        except Exception as exc:
            logger.error("Error obteniendo ZPL nativo de MeLi para pedido %s: %s", order_id, exc)
            raise HTTPException(
                status_code=502,
                detail=f"No se pudo obtener la etiqueta nativa de MercadoLibre: {exc}"
            )
    else:
        # Generar etiqueta principal
        zpl_str = build_zpl_main(order, dpi=settings.ZEBRA_DPI)

        # Si hay customer_note, agregar etiqueta adicional
        if order.customer_note and order.customer_note.strip():
            zpl_note = build_zpl_note(order, dpi=settings.ZEBRA_DPI)
            if zpl_note:
                zpl_str = zpl_str + "\n\n" + zpl_note

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
    _current_user: dict[str, Any] = Depends(get_current_user),
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
    _current_user: dict[str, Any] = Depends(get_current_user),
):
    """Retorna el detalle normalizado de un pedido por ID."""
    provider = get_provider(source)
    meli: Optional[MeliProvider] = None
    try:
        order = await provider.get_order(order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Fallback Mercado Libre para scanner:
    # 1) lookup directo por order_id en MeLi
    # 2) lookup por shipping_id/display_id en pendientes MeLi
    if not order:
        meli_provider = _providers.get(OrderSource.MERCADOLIBRE.value)
        if isinstance(meli_provider, MeliProvider):
            meli = meli_provider

    if not order and meli:
        try:
            order = await meli.get_order(order_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    if not order and meli:
        try:
            pending_orders = await meli.get_pending_orders()
            matched = next(
                (
                    o for o in pending_orders
                    if o.display_id == str(order_id)
                ),
                None,
            )
            if matched:
                order = await meli.get_order(matched.id)
                if not order:
                    order = matched
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    if not order:
        raise HTTPException(status_code=404, detail=f"Pedido {order_id} no encontrado")

    await upsert_order(order)
    # Enriquecer con información de desglose de Packs
    order = enrich_order_with_pack_info(order)
    return order.model_dump(mode="json")


# ── Pedidos — Acciones de bodega ─────────────────────────────────
@app.post("/api/orders/{order_id}/prepare", tags=["orders"])
async def prepare_order(
    order_id: str,
    source: str = Query(default=OrderSource.WOOCOMMERCE.value),
    _current_user: dict[str, Any] = Depends(get_current_user),
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

    # Enriquecer pedido con información de Packs antes de generar PDF
    order = enrich_order_with_pack_info(order)

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
    _current_user: dict[str, Any] = Depends(get_current_user),
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

    if source == OrderSource.MERCADOLIBRE.value:
        meli: MeliProvider = provider  # type: ignore[assignment]
        try:
            native_zpl = await meli.get_native_zpl(order_id)
        except RuntimeError as exc:
            await log_event(order_id, source, "label_error", str(exc))
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "No se pudo obtener la etiqueta nativa desde Mercado Libre",
                    "reason": str(exc),
                },
            )
        except Exception as exc:
            logger.exception("Error inesperado obteniendo ZPL nativo de MeLi para pedido %s", order_id)
            await log_event(order_id, source, "label_error", str(exc))
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Error inesperado obteniendo etiqueta nativa desde Mercado Libre",
                    "reason": str(exc),
                },
            )

        print_ok, print_msg = await zpl_svc._send(native_zpl)
    else:
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

    # Registrar timestamp exacto de impresión y marcar como completado
    # Solo asignar label_printed_at si es la primera vez (no está en BD como completado)
    existing_status = await get_local_status(order_id, source)
    if not existing_status or existing_status != OrderStatus.COMPLETED:
        order.label_printed_at = datetime.utcnow()
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
