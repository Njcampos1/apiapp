# Upper Coffee Logistics

> Sistema modular de orquestación logística para bodega: gestión de pedidos multi-plataforma, generación de hojas de picking en PDF y etiquetado ZPL directo a impresoras Zebra.

El sistema resuelve el problema de centralizar pedidos provenientes de distintas plataformas de e-commerce (WooCommerce, Mercado Libre, etc.) en una única interfaz de bodega, automatizando el flujo completo: **sincronización → picking → etiquetado → despacho**.

---

## Stack Tecnológico

| Capa | Tecnología |
|---|---|
| API / Backend | FastAPI 0.115 + Uvicorn |
| Base de datos | SQLite async (`aiosqlite`) |
| Configuración | Pydantic Settings + python-dotenv |
| HTTP Client | HTTPX (async) |
| Generación PDF | ReportLab 4.2 (picking A4 + código de barras Code128) |
| Etiquetas ZPL | Socket TCP/IP directo → impresora Zebra |
| Exportación | Pandas + openpyxl |
| Frontend | SPA vanilla JS + Tailwind CSS CDN |
| Contenedor | Docker + Docker Compose (`network_mode: host`) |
| Despliegue nube | Azure App Service for Containers + ACR |

---

## Arquitectura y Extensibilidad

```
upperapp/
├── main.py                  # FastAPI + orquestación de proveedores
├── config.py                # Variables de entorno (pydantic-settings)
├── database.py              # SQLite async (persistencia de estados)
├── models/
│   └── order.py             # Modelo normalizado NormalizedOrder
├── providers/
│   ├── base_provider.py     # Interfaz abstracta BaseOrderProvider  ← clave
│   ├── woo_client.py        # Implementación WooCommerce REST API v3
│   └── meli_client.py       # Implementación Mercado Libre (referencia)
├── services/
│   ├── pdf_service.py       # PDF de picking A4 con código de barras
│   ├── zpl_service.py       # Etiquetas ZPL 10×5 cm + socket TCP/IP
│   ├── pack_service.py      # Desglose de packs desde catálogo skus.json
│   ├── excel_service.py     # Exportación de reportes a Excel
│   └── chilexpress_service.py
├── templates/
│   └── index.html           # SPA Tailwind CSS + JS vanilla
├── static/
├── Dockerfile
├── docker-compose.yml       # network_mode: host (visibilidad impresora LAN)
├── .env.example
└── setup.sh                 # Configuración local + comandos Azure CLI
```

### Patrón `BaseOrderProvider`

Toda integración de plataforma implementa la misma interfaz abstracta. **La UI y los servicios de impresión nunca conocen la plataforma concreta.**

```python
# providers/base_provider.py
class BaseOrderProvider(ABC):
    source_name: str  # identificador único de la plataforma

    async def get_pending_orders(self) -> List[NormalizedOrder]: ...
    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]: ...
    async def update_order_status(self, order_id: str, status: OrderStatus) -> bool: ...
    def normalize(self, raw: dict) -> NormalizedOrder: ...
```

**Para integrar una nueva plataforma (ej. Shopify):**

1. Crear `providers/shopify_client.py` implementando `BaseOrderProvider`.
2. Registrar la instancia en `build_providers()` dentro de `main.py`.
3. **Nada más** — picking, ZPL y la interfaz funcionan sin modificaciones.

---

## Requisitos Previos

| Componente | Versión mínima |
|---|---|
| Python | 3.10+ |
| Docker | 24+ |
| Docker Compose | v2+ |
| Bash | 4+ (para `setup.sh`) |

---

## Inicio Rápido — Entorno Local

### Opción A: Script de configuración automática (recomendado)

El script `setup.sh` realiza en un solo paso: verificación de Python, creación del entorno virtual, instalación de dependencias, creación del `.env` desde la plantilla y preparación del directorio `data/`.

```bash
# 1. Clonar y entrar al repositorio
git clone <repo-url> upperapp
cd upperapp

# 2. Dar permisos y ejecutar el script
chmod +x setup.sh
./setup.sh
```

El script produce una salida guiada con el siguiente resultado:

```
[OK]   Python 3.11 detectado
[OK]   Entorno virtual creado en .venv/
[OK]   Dependencias instaladas
[WARN] .env creado desde .env.example — edítalo con tus credenciales reales
[OK]   Directorio data/ listo
```

**Editar credenciales** antes de levantar el servidor:

```bash
# Abrir .env y completar los valores reales
WOO_URL=https://tu-tienda.cl
WOO_KEY=ck_XXXX
WOO_SECRET=cs_XXXX
ZEBRA_IP=192.168.1.100   # IP de la impresora Zebra en la LAN
ZEBRA_PORT=9100
```

**Levantar el servidor en modo desarrollo:**

```bash
source .venv/bin/activate
DEBUG=true uvicorn main:app --reload --port 8000
```

La aplicación queda disponible en `http://localhost:8000`.

---

### Opción B: Docker Compose

Usa `network_mode: host` para que el contenedor tenga visibilidad directa de la impresora Zebra en la LAN sin necesidad de port-forwarding.

```bash
# Construir imagen y levantar
docker compose up --build

# En background
docker compose up --build -d
```

> **Nota:** `network_mode: host` solo funciona en Linux. En macOS/Windows usa la Opción A para desarrollo local.

La base de datos SQLite se persiste en el volumen `./data/pedidos.db`, fuera del contenedor.

---

## Despliegue en Azure

El script incluye un modo `--azure` que imprime en consola la secuencia completa de comandos Azure CLI para desplegar la aplicación como contenedor en **Azure App Service**.

```bash
./setup.sh --azure
```

El flujo generado cubre los siguientes pasos:

1. **Resource Group** — crear el grupo de recursos con `az group create`.
2. **Azure Container Registry (ACR)** — crear el registro y hacer build/push de la imagen con `az acr build`.
3. **App Service Plan** — plan Linux B1 mínimo con `az appservice plan create`.
4. **Web App** — crear la aplicación apuntando a la imagen en ACR con `az webapp create` + `az webapp config container set`.
5. **Variables de entorno** — inyectar `WOO_URL`, `WOO_KEY`, `WOO_SECRET`, `ZEBRA_*`, `DB_PATH` y `WEBSITES_PORT=8000` con `az webapp config appsettings set`.
6. **Logs** — habilitar logging del contenedor con `az webapp log config`.
7. **URL final** — obtener el hostname público con `az webapp show`.

> Para persistencia de SQLite en Azure se recomienda montar un Azure File Share como volumen (`az webapp config storage-account add`). El script incluye este paso comentado como referencia.

---

## Variables de Entorno

| Variable | Descripción | Default |
|---|---|---|
| `WOO_URL` | URL base de la tienda WooCommerce | — |
| `WOO_KEY` | Consumer Key de la API REST | — |
| `WOO_SECRET` | Consumer Secret de la API REST | — |
| `ZEBRA_IP` | IP de la impresora Zebra en la LAN | `192.168.1.100` |
| `ZEBRA_PORT` | Puerto TCP de la impresora | `9100` |
| `ZEBRA_DPI` | Resolución de impresión | `300` |
| `DB_PATH` | Ruta al archivo SQLite | `/data/pedidos.db` |
| `APP_PORT` | Puerto de la aplicación | `8000` |
| `DEBUG` | Modo debug con recarga automática | `false` |

Copia `.env.example` como punto de partida:

```bash
cp .env.example .env
```

---

## Endpoints Principales

```
GET  /                         → Interfaz web (SPA)
GET  /api/orders               → Pedidos pendientes (todos los proveedores)
POST /api/orders/{id}/pick     → Marcar pedido como picking
POST /api/orders/{id}/complete → Completar pedido + actualizar plataforma
GET  /api/orders/{id}/pdf      → Descargar hoja de picking en PDF
POST /api/orders/{id}/zpl      → Enviar etiqueta ZPL a impresora
GET  /api/health               → Health check (Azure / Docker / balanceador)
```

---

## Autor

**Nestor Campos** — Creador y mantenedor del proyecto.