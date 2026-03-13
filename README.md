# Upper Coffee — Sistema de Gestión Logística de Bodega

Sistema modular para gestión de pedidos, picking y etiquetado en bodega, integrado con WooCommerce (y preparado para Mercado Libre y otras plataformas).

## Arquitectura

```
upperapp/
├── main.py                  # FastAPI + orquestación de proveedores
├── config.py                # Variables de entorno (pydantic-settings)
├── database.py              # SQLite async (persistencia de estados)
├── models/
│   └── order.py             # Modelo normalizado NormalizedOrder
├── providers/
│   ├── base_provider.py     # Interfaz abstracta BaseOrderProvider
│   └── woo_client.py        # Implementación WooCommerce REST API v3
├── services/
│   ├── pdf_service.py       # PDF de picking A4 con QR (reportlab)
│   └── zpl_service.py       # Etiquetas ZPL 10×5cm + socket TCP/IP
├── templates/
│   └── index.html           # SPA Tailwind CSS + JS vanilla
├── Dockerfile
├── docker-compose.yml       # network_mode: host (visibilidad impresora LAN)
├── .env.example
└── setup.sh                 # Configuración local + comandos Azure CLI
```

### Patrón de extensibilidad

Para integrar **Mercado Libre** (u otra plataforma):
1. Crear `providers/meli_client.py` implementando `BaseOrderProvider`
2. Agregar la instancia en `build_providers()` dentro de `main.py`
3. **Nada más** — la UI y los servicios de impresión no necesitan cambios

---

## Requisitos

| Componente | Versión mínima |
|------------|---------------|
| Python     | 3.10+         |
| Docker     | 24+           |
| Docker Compose | v2+      |

---

## Ejecución local

### 1. Clonar y configurar

```bash
git clone <repo-url>
cd upperapp

# Configura entorno virtual + .env automáticamente
chmod +x setup.sh
./setup.sh
```

### 2. Editar credenciales

```bash
# Abre .env y completa:
WOO_URL=https://tu-tienda.cl
WOO_KEY=ck_...
WOO_SECRET=cs_...
ZEBRA_IP=192.168.1.100  # IP estática de la Zebra en bodega
```

### 3. Iniciar servidor de desarrollo

```bash
source .venv/bin/activate
DEBUG=true uvicorn main:app --reload --port 8000
```

Abrir: `http://localhost:8000`

---

## Ejecución con Docker

```bash
# Construir e iniciar (red host = impresora visible)
docker compose up --build

# En background
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f app

# Detener
docker compose down
```

> **Nota `network_mode: host`**: el contenedor comparte la interfaz de red del host Linux, por lo que la impresora Zebra en la LAN es accesible directamente desde la IP configurada en `.env`. En macOS/Windows Docker Desktop esta opción no está disponible — conectar la impresora manualmente o usar testeo en Linux.

---

## Flujo de trabajo en bodega

### Vista Dashboard
1. Abrir `http://IP-DEL-SERVIDOR:8000` en el navegador de bodega
2. Los pedidos en estado `processing` aparecen en la grilla
3. Clic en **"Preparar pedido"** → se genera un PDF A4 de picking que abre en nueva pestaña:
   - Incluye tabla de productos con espacios para marcar manualmente
   - QR del ID del pedido en la esquina para escáner RF
   - Datos de envío del destinatario

### Vista Verificación (pistola RF)
1. Clic en pestaña **"Verificar"** → el campo de texto recibe foco automáticamente
2. Apuntar la pistola RF al QR del papel de picking → el ID se ingresa y busca automáticamente
3. La pantalla muestra:
   - Nombre, dirección, ciudad y teléfono del destinatario (tipografía grande)
   - Lista de ítemes con cantidad destacada
4. Clic en **"Confirmar y Etiquetar"** →
   - Se genera la etiqueta ZPL 10×5 cm
   - Se envía por socket TCP al puerto 9100 de la impresora Zebra
   - El pedido se marca como `completed` en WooCommerce automáticamente

### Manejo de errores de impresora
- Si la impresora está **offline**: aparece mensaje de error con botón "Reintentar"
- El pedido **NO se marca como completado** hasta que la impresión sea exitosa
- El indicador de estado en el header muestra si la impresora es alcanzable (verde/rojo)

---

## API REST

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET`  | `/api/health` | Estado de la app y proveedores configurados |
| `GET`  | `/api/printer/test` | Diagnóstico de conexión TCP a la Zebra |
| `GET`  | `/api/orders` | Pedidos pendientes de todos los proveedores |
| `GET`  | `/api/orders/{id}?source=` | Detalle normalizado de un pedido |
| `POST` | `/api/orders/{id}/prepare?source=` | Genera PDF picking (retorna PDF) |
| `POST` | `/api/orders/{id}/label?source=` | Imprime etiqueta + completa pedido |

Documentación interactiva: `http://localhost:8000/docs`

---

## Configuración de etiqueta ZPL

La etiqueta de 10×5 cm (100×50 mm) se genera automáticamente con:
- Nombre del destinatario en tipografía grande
- Dirección, ciudad y teléfono
- Código de barras Code128 del ID del pedido
- Número de pedido y fuente (WooCommerce, etc.)

Para ajustar el layout, editar `services/zpl_service.py` → función `build_zpl()`.

**Compatibilidad DPI**: configura `ZEBRA_DPI=203` o `ZEBRA_DPI=300` en `.env` según tu modelo.

---

## Despliegue en Azure

```bash
# Ver todos los comandos de Azure CLI necesarios
./setup.sh --azure
```

Los pasos incluyen:
1. Crear Resource Group
2. Crear Azure Container Registry (ACR)
3. Build y push de imagen
4. Crear App Service Plan (Linux)
5. Crear Web App con la imagen del ACR
6. Configurar variables de entorno en Azure
7. Habilitar logging del contenedor

> **SQLite en Azure**: para producción, considera migrar a Azure Database for PostgreSQL o usar Azure Files para montar un volumen persistente que sobreviva reinicios.

---

## Base de datos SQLite

La BD `pedidos.db` (ruta configurable con `DB_PATH`) persiste:
- **`orders`**: estado interno de cada pedido (por si el servidor se reinicia entre pasos)
- **`order_events`**: log de auditoría de todas las acciones (prepare, label_printed, completed, etc.)

```sql
-- Consultar historial de un pedido
SELECT * FROM order_events WHERE order_id = '12345' ORDER BY ts;
```

---

## Agregar nueva plataforma (ejemplo: Mercado Libre)

```python
# providers/meli_client.py
from providers.base_provider import BaseOrderProvider
from models.order import NormalizedOrder, OrderStatus, OrderSource

class MeliProvider(BaseOrderProvider):
    source_name = OrderSource.MERCADOLIBRE.value

    def __init__(self, token: str): ...

    async def get_pending_orders(self) -> list[NormalizedOrder]: ...
    async def get_order(self, order_id: str) -> NormalizedOrder | None: ...
    async def update_order_status(self, order_id, status, note="") -> bool: ...
    def normalize(self, raw: dict) -> NormalizedOrder: ...
```

```python
# main.py → build_providers()
if settings.MELI_TOKEN:
    reg[OrderSource.MERCADOLIBRE.value] = MeliProvider(token=settings.MELI_TOKEN)
```

---

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `WOO_URL` | — | URL base de WooCommerce |
| `WOO_KEY` | — | Consumer Key de la API REST |
| `WOO_SECRET` | — | Consumer Secret de la API REST |
| `ZEBRA_IP` | `192.168.1.100` | IP de la impresora Zebra |
| `ZEBRA_PORT` | `9100` | Puerto ZPL (por defecto RAW) |
| `ZEBRA_DPI` | `300` | Resolución 203 o 300 DPI |
| `APP_PORT` | `8000` | Puerto del servidor web |
| `DEBUG` | `false` | Habilita logs detallados y recarga |
| `DB_PATH` | `pedidos.db` | Ruta de la base de datos SQLite |
