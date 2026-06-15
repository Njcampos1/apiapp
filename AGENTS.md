# AGENTS.md — UpperLogistics

> Reglas del sistema, identidad y restricciones para sesiones de auditoría y
> desarrollo asistido por agentes. Este documento se infiere directamente del
> código fuente (`main.py`, `database.py`, `providers/`, `services/`,
> `models/order.py`, `config.py`). **Si el código y la documentación previa
> divergen, el código manda.**

---

## 1. Identidad del Proyecto

- **Nombre:** UpperLogistics (título interno de la app FastAPI:
  `Upper Coffee Logistics`; el footer del PDF de picking referencia
  `Upper Logistics - www.upperlogistics.com`).
- **Propósito:** Sistema de gestión de bodega y despachos para una operación de
  venta de café (y productos asociados: chocolate, detergentes, cobertores).
  Centraliza pedidos de múltiples plataformas, genera hojas de picking (PDF),
  imprime etiquetas térmicas (ZPL) en una impresora Zebra de la LAN, y consolida
  despachos en manifiestos exportables a Excel/CSV.
- **Actores:** un **admin** (gestiona usuarios, edita el catálogo de SKUs) y
  uno o varios **operarios** de bodega (preparan pedidos, imprimen etiquetas,
  cierran manifiestos).

### Stack tecnológico estricto (inmutable)

| Capa | Tecnología | Versión / Detalle |
|------|-----------|-------------------|
| Lenguaje | Python | **3.11+** (imagen base `python:3.11-slim`) |
| Web framework | **FastAPI** | `0.115.5` |
| Servidor ASGI | uvicorn | `0.32.1` (`--workers 2` en producción) |
| Persistencia | **SQLite vía aiosqlite** | `aiosqlite==0.20.0`, modo WAL |
| Cliente HTTP | **httpx.AsyncClient** | `0.27.2` (asíncrono, con timeouts) |
| Validación / modelos | Pydantic + pydantic-settings | modelos en `models/order.py` |
| Auth | PyJWT (`HS256`) + passlib[bcrypt] | tokens Bearer |
| PDF | reportlab | hojas de picking A4 + Code128 |
| Excel / datos | pandas + openpyxl | reporte de despachos `.xlsx` |
| Frontend | SPA estática (Jinja2 + JS vanilla) | `templates/index.html`, `static/js/*` |
| Contenedor | **Docker** | `Dockerfile`, `docker-compose.yml` (`network_mode: host`) |
| Despliegue | **Azure App Service for Containers** | imagen `upperapp-logistics` |
| Túnel impresora | Tailscale | VPN privada Azure ↔ PC puente en bodega |

**Prohibido proponer cambios de stack.** Ver §4.

---

## 2. Vocabulario de Negocio (UI / API)

Términos **obligatorios** al escribir código, comentarios, mensajes de UI,
logs y documentación. Usar el término exacto de la columna izquierda.

### 2.1 Actores y roles

| Término obligatorio | Valor en código | Definición |
|---------------------|-----------------|------------|
| **admin** | `role = "admin"` | Único rol con acceso a gestión de usuarios (`/api/users`) y edición del catálogo (`PUT /api/skus`). Protegido por `get_admin_user`. |
| **operario** | `role = "user"` | Usuario de bodega estándar. Autenticado (`get_current_user`) pero sin privilegios de administración. En la UI se le llama "Usuario". |

> El enum de la tabla `users` solo admite `CHECK (role IN ('admin','user'))`.
> "operario" es el término de negocio para el rol `user`; **nunca** inventar
> roles nuevos sin migración.

### 2.2 Estados logísticos (`OrderStatus`, `models/order.py`)

| Estado (valor) | Término | Significado |
|----------------|---------|-------------|
| `processing` | **processing** | Pedido recibido desde la plataforma, pendiente de preparar. Estado inicial por defecto en la BD. |
| `preparing` | **preparing** | Picking iniciado; la hoja de picking (PDF) fue generada. |
| `labeled` | **labeled** | Etiqueta impresa (estado intermedio del modelo). |
| `completed` | **completed** | Pedido completado / etiqueta despachada; se asigna a un manifiesto. |
| `error` | **error** | Error en algún paso o pedido cancelado/rechazado por la plataforma. |

Reglas de transición relevantes (ver `upsert_order`): un pedido que ya está en
`preparing`/`labeled`/`completed` **no** puede ser degradado a `processing` por
una resincronización; `completed_at` y `label_printed_at` son **inmutables** una
vez establecidos.

### 2.3 Fuentes / plataformas (`OrderSource`)

| Valor | Término | Notas |
|-------|---------|-------|
| `woocommerce` | **WooCommerce** | REST API v3, auth Basic (consumer key/secret). |
| `mercadolibre` | **Mercado Libre** (MeLi) | OAuth 2.0 con refresh token. Abreviar como "MeLi" en logs. |
| `manual` | **manual** | Pedido creado/forzado manualmente. |

### 2.4 Componentes físicos y artefactos

| Término obligatorio | NO usar | Definición |
|---------------------|---------|------------|
| **ZPL label** / **etiqueta ZPL** | "sticker", "etiqueta de papel" | Texto crudo ZPL II enviado por TCP a la Zebra. |
| **hoja de picking** | "ticket", "comanda" | PDF A4 con ítemes a preparar + código de barras Code128. |
| **manifiesto** (manifest) | "lote" a secas | Agrupación de pedidos `completed` para despacho. Estados `open` / `closed`. |
| **planilla / reporte Excel** | "informe" | `.xlsx` generado por `excel_service`. |
| **CSV Chilexpress** | "archivo courier" | CSV de carga masiva para envíos regionales (solo WooCommerce). |
| **Pack** / **Mix personalizable** | "combo" | Producto compuesto con desglose desde `data/skus.json`. |
| **Zebra** | "impresora genérica" | Impresora térmica en la LAN, puerto TCP **9100** (`9101` en algunas instalaciones). |

### 2.5 Couriers (lógica de `excel_service`)

| Courier | Condición |
|---------|-----------|
| **Rocket** | Comuna presente en `data/rm.json` (Región Metropolitana). |
| **Chilexpress** | WooCommerce + comuna NO RM (regional). |
| **Mercado** | Mercado Libre + comuna NO RM. |

---

## 3. Reglas de Seguridad Zero-Trust

Directrices **estrictas** para toda modificación futura.

### 3.1 SQLite — consultas parametrizadas obligatorias

- **Toda** consulta SQL DEBE usar parámetros `?` y pasar los valores como tupla.
  Está prohibido construir SQL con f-strings, `%`, `.format()` o concatenación
  que incluya datos de entrada (IDs de pedido, usernames, roles, etc.).
- Patrón correcto (ya usado en `database.py`):
  ```python
  await db.execute(
      "SELECT status FROM orders WHERE id = ? AND source = ?",
      (order_id, source),
  )
  ```
- Las migraciones `ALTER TABLE` son DDL estático sin entrada de usuario; se
  permiten dentro del bloque `try/except OperationalError` de `init_db()`.
- No exponer `payload_json` crudo en respuestas sin pasar por
  `NormalizedOrder.model_validate_json` (evita filtrar campos internos).

### 3.2 JWT — validación y expiración

- Algoritmo fijo: **`HS256`** (`JWT_ALGORITHM`). Nunca aceptar `alg=none` ni
  permitir que el cliente elija el algoritmo.
- `SECRET_KEY` DEBE tener **≥ 32 caracteres**; la app aborta el arranque si no
  (validación en `lifespan`). No commitear claves reales; usar `.env`/Azure.
- Todo endpoint protegido depende de `get_current_user` (decodifica el token,
  valida `sub`, y **re-verifica que el usuario aún existe en la BD**). Los
  endpoints de administración añaden `get_admin_user`.
- Expiración: `exp` se fija a `JWT_EXPIRE_MINUTES` (def. 720 min). Un token
  expirado o inválido lanza `401`. No extender la expiración por defecto sin
  justificación; no implementar "refresh infinito" del JWT de sesión.
- **Revocación de tokens:** cada usuario tiene `users.token_version`; el JWT
  incluye el claim `tv`. `get_current_user` rechaza con `401` si `tv` ≠
  `token_version` actual. `POST /api/logout` y todo cambio de rol incrementan
  `token_version`, invalidando de inmediato las sesiones activas. No se usa
  denylist (cero consultas extra: el contador viaja en el SELECT de usuario ya existente).
- `SECRET_KEY` y `DEFAULT_ADMIN_PASSWORD` son campos **requeridos** en `config.py`
  (sin valor por defecto). El `lifespan` aborta el arranque con `RuntimeError` si
  `SECRET_KEY` mide <32 chars o es un placeholder, o si `DEFAULT_ADMIN_PASSWORD`
  mide <8 chars o coincide con un valor débil conocido (`admin123`, placeholders).

### 3.3 Privacidad y datos PII (RUT, direcciones, teléfono, email)

Los pedidos contienen PII proveniente de Mercado Libre y WooCommerce:
**RUT** (`doc_number`), nombre del receptor, dirección, teléfono, email.

- **PII Guard de Mercado Libre (obligatorio respetar):** solo consultar
  `/orders/{id}/billing_info` cuando el envío **no** está en
  `shipped` / `delivered` / `cancelled`. MeLi revoca el acceso a PII una vez
  despachado; forzar la consulta genera `403` esperados que **no** deben tratarse
  como error fatal. Ver `_enrich_order` en `meli_client.py`.
- No registrar PII completa en logs de nivel INFO. El logging de diagnóstico
  existente usa marcadores `✓ / ✗` para indicar presencia de RUT/teléfono/nota
  **sin** volcar el dato. Mantener ese patrón.
- No persistir PII fuera de `orders.payload_json`. No exportarla a destinos
  externos no autorizados. El CSV de Chilexpress y el Excel son artefactos de
  despacho legítimos; cualquier nuevo export con PII requiere justificación
  explícita.
- Sanitizar siempre el texto que va a ZPL con `_safe()` (elimina `^` y `~`,
  caracteres de control ZPL) antes de enviarlo a la impresora.

### 3.4 Otras reglas

- CORS restringido a `ALLOWED_ORIGINS` (configurable; no usar `*` con
  `allow_credentials=True`).
- Tokens de MeLi (`access_token`/`refresh_token`) viven solo en la tabla
  `meli_tokens` (un único registro, `CHECK (id = 1)`). No loguearlos.
- El handler global de excepciones devuelve `500` genérico; `str(exc)` solo se
  incluye en la respuesta si `DEBUG=true`. Los `except` del callback de MeLi no
  filtran `response.text` ni `str(exc)` al cliente (se loguean en servidor).
- `POST /api/login` aplica rate-limiting en memoria por IP de origen
  (`LOGIN_MAX_ATTEMPTS=5` en `LOGIN_WINDOW_SECONDS=300`); excederlo responde `429`.
  Estado en `_LOGIN_ATTEMPTS` (apto para despliegue de un solo nodo).

---

## 4. Estándares de Código Backend

### 4.1 Tipado

- **Type Hints obligatorios** en toda función y método (`typing`,
  `Optional`, `List`, `Dict`, `TypedDict`, etc.). El código actual está
  totalmente anotado; mantener la consistencia.
- Los datos que cruzan capas viajan como `NormalizedOrder` (Pydantic). Las
  plataformas concretas **nunca** filtran su forma cruda hacia la UI o los
  servicios: cada proveedor implementa `BaseOrderProvider.normalize()`.

### 4.2 Asincronía — I/O 100% async

- Toda operación de I/O (red, BD, sockets) DEBE ser `async`/`await`:
  - HTTP saliente: **`httpx.AsyncClient`** reutilizable por proveedor.
  - BD: **`aiosqlite`** mediante el helper `get_db()`.
  - Impresión ZPL: `asyncio.open_connection` (socket TCP no bloqueante).
- **Prohibido bloquear el event loop.** No usar `requests`, `time.sleep`,
  `socket` bloqueante, ni llamadas sincrónicas de red dentro de corutinas. Para
  paralelizar enriquecimiento de pedidos usar `asyncio.gather` (patrón ya
  presente en `meli_client.get_pending_orders`).
- Las operaciones CPU-bound puntuales y deterministas (generación de PDF con
  reportlab, Excel con pandas, parseo de catálogo cacheado con `lru_cache`) se
  ejecutan inline; si una se vuelve costosa, evaluar `run_in_executor` antes de
  bloquear.

### 4.3 Arquitectura de proveedores (extensibilidad)

- Para integrar una plataforma nueva: implementar `BaseOrderProvider`
  (`get_pending_orders`, `get_order`, `update_order_status`, `normalize`) y
  registrarla **únicamente** en `build_providers()` de `main.py`. No acoplar la
  UI ni los servicios a una plataforma concreta.

### 4.4 Restricción de stack (inmutable)

- **Prohibido sugerir o iniciar migraciones a PostgreSQL** (u otro motor). La
  persistencia es SQLite + aiosqlite por diseño (un solo nodo, WAL, sidecar
  files). Optimizar dentro de SQLite; no proponer ORMs pesados ni cambiar el
  motor.
- **Prohibido cambiar** FastAPI → otro framework, httpx → requests/aiohttp, o el
  modelo de despliegue (Docker + Azure App Service). Mejoras sí; reemplazos de
  stack no.

---

## 5. Comandos y operación rápida

| Acción | Comando |
|--------|---------|
| Arranque local | `python main.py` (o `uvicorn main:app --reload`) |
| Build contenedor | `docker compose build` |
| Levantar (bodega, host network) | `docker compose up -d` |
| Health check | `GET /api/health` |
| Test impresora | `GET /api/printer/test` (auth) |
| Tests | `pytest` (ver `tests/`) |

Variables de entorno: ver `.env.example` y `config.py` (`Settings`).
La BD se ubica en `DB_PATH` (`/data/pedidos.db` en contenedor, volumen persistido).
