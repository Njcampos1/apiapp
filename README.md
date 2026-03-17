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


