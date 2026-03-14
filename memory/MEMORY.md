# Upper Coffee — Bodega (apiapp)

## Arquitectura
- **Backend**: FastAPI (`main.py`), proveedor WooCommerce (`providers/woo_client.py`)
- **Frontend**: SPA en una sola página `templates/index.html` con Tailwind CDN y JS puro
- **PDF picking**: `services/pdf_service.py` con ReportLab
- **Etiquetas**: `services/zpl_service.py` → impresora Zebra por TCP
- **BD local**: SQLite vía `database.py` (estado local de pedidos)

## Convenciones clave
- Región Metropolitana detectada por `shipping.state === 'RM'` (código WooCommerce Chile)
- Estado del pedido: processing → preparing → labeled → completed | error
- El `display_id` es el número de pedido legible (#XXXX); `id` es el ID interno

## Decisiones de diseño aplicadas (2026-03)
- **PDF eco-tinta**: sin columna SKU, sin grilla vertical, solo líneas horizontales entre filas
  - Columnas: `#` (1cm), `Producto` (13cm), `Cant.` (1.5cm), `✓` (1.5cm)
- **Doble escaneo en Verificar**: primer scan carga el pedido, segundo scan (mismo ID) dispara
  impresión automática (`autoPrintAndComplete`) y resetea la vista 1.5 s después
- **Fallback ZPL**: botón `#zpl-fallback-btn` aparece solo cuando falla la impresión (503)
- **Badge de región**: `regionBadge(shipping)` → "RM" azul (`sky`) o "Región" rojo, al lado
  de `orderStatusBadge` en cada tarjeta del dashboard

## Flujo de bodega sin mouse (RF gun)
1. Escanear código → carga detalle del pedido (primer scan)
2. Escanear mismo código → imprime ZPL + marca completed + reset automático (segundo scan)
3. Si falla la impresora → aparece botón "Descargar ZPL" como contingencia
