/**
 * api.js
 * Todas las llamadas HTTP al backend Flask de UpperLogistics
 * - Gestión de pedidos (carga, búsqueda, actualización)
 * - Descargas masivas e individuales (ZPL, PDF)
 * - Operaciones de manifest y cierre de día
 * - APIs de impresora y etiquetado
 */

// ═══════════════════════════════════════════════════════════════
// PRINTER API
// ═══════════════════════════════════════════════════════════════

/**
 * Verifica el estado de conectividad de la impresora Zebra
 */
async function pingPrinter() {
  try {
    const r = await fetch('/api/printer/test');
    const d = await r.json();
    const dot   = document.getElementById('printer-dot');
    const label = document.getElementById('printer-label');
    if (d.reachable) {
      dot.className   = 'w-2.5 h-2.5 rounded-full bg-green-400';
      label.textContent = 'Impresora OK';
    } else {
      dot.className   = 'w-2.5 h-2.5 rounded-full bg-red-400 animate-pulse';
      label.textContent = 'Sin impresora';
    }
  } catch {
    document.getElementById('printer-dot').className = 'w-2.5 h-2.5 rounded-full bg-gray-500';
  }
}

// ═══════════════════════════════════════════════════════════════
// DASHBOARD — PEDIDOS
// ═══════════════════════════════════════════════════════════════

/**
 * Carga todos los pedidos desde el backend y actualiza el cache global
 */
async function loadOrders() {
  const grid    = document.getElementById('orders-grid');
  const loading = document.getElementById('orders-loading');
  const empty   = document.getElementById('empty-state');
  const icon    = document.getElementById('refresh-icon');
  const errBox  = document.getElementById('api-errors');

  grid.classList.add('hidden');
  empty.classList.add('hidden');
  errBox.classList.add('hidden');
  loading.classList.remove('hidden');
  icon.classList.add('spinner');

  try {
    const r = await fetch('/api/orders');
    const d = await r.json();

    loading.classList.add('hidden');
    icon.classList.remove('spinner');

    if (d.errors && d.errors.length > 0) {
      errBox.innerHTML = d.errors.map(e => `
        <div class="flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-xl px-4 py-2 text-sm text-amber-800 mb-2">
          <span>⚠️</span>
          <strong>${e.source}:</strong> ${e.error}
        </div>`).join('');
      errBox.classList.remove('hidden');
    }

    // Guardar todos los pedidos en el array global y delegar al render
    allOrders = d.orders || [];
    renderOrders();

    // Actualizar contador de manifiestos
    loadManifestInfo();

  } catch (err) {
    loading.classList.add('hidden');
    icon.classList.remove('spinner');
    document.getElementById('order-count').textContent = '—';
    toast('Error de conexión al servidor', 'error');
  }
}

/**
 * Descarga un PDF masivo con todas las hojas de picking de pedidos nuevos
 */
async function exportAllOrders() {
  const btn = document.getElementById('export-all-btn');
  btn.disabled = true;
  btn.innerHTML = `<svg class="spinner w-4 h-4" fill="none" viewBox="0 0 24 24">
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
  </svg> Generando...`;

  try {
    const r = await fetch('/api/orders/export-all');

    if (r.status === 404) {
      toast('No hay pedidos nuevos para exportar', 'info');
      return;
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Error: ${err.detail || r.statusText}`, 'error');
      return;
    }

    const count = r.headers.get('X-Orders-Count') || '?';
    const blob  = await r.blob();
    const url   = URL.createObjectURL(blob);
    const a     = document.createElement('a');
    a.href     = url;
    a.download = `picking_masivo.pdf`;
    a.click();
    URL.revokeObjectURL(url);

    toast(`PDF masivo descargado (${count} pedidos)`, 'success');
    setTimeout(loadOrders, 1200);

  } catch (err) {
    toast('Error de conexión', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
        d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
    </svg> Descargar todos (PDF)`;
  }
}

/**
 * Cierra el manifest activo y descarga un ZIP con todos los PDFs del día
 */
async function closeManifestAndDownload() {
  const btn = document.getElementById('close-manifest-btn');
  const originalHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<svg class="spinner w-4 h-4" fill="none" viewBox="0 0 24 24">
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
  </svg> Cerrando día...`;

  try {
    const r = await fetch('/api/manifests/close', {
      method: 'POST'
    });

    if (r.status === 404) {
      toast('No hay manifest abierto para cerrar', 'info');
      return;
    }

    if (r.status === 422) {
      const err = await r.json().catch(() => ({}));
      toast(err.detail || 'El manifest está vacío', 'warning');
      return;
    }

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Error: ${err.detail || r.statusText}`, 'error');
      return;
    }

    // Descargar ZIP
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');

    // Obtener nombre del archivo desde headers
    const contentDisposition = r.headers.get('Content-Disposition');
    let filename = 'despachos.zip';
    if (contentDisposition) {
      const matches = /filename="(.+)"/.exec(contentDisposition);
      if (matches) filename = matches[1];
    }

    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    const orderCount = r.headers.get('X-Orders-Count') || '0';
    toast(`Día cerrado exitosamente. ${orderCount} pedidos procesados.`, 'success');

    // Actualizar info del manifest
    loadManifestInfo();

    // Opcional: recargar pedidos para reflejar el nuevo manifest
    setTimeout(() => loadOrders(), 1000);

  } catch (err) {
    console.error('Error al cerrar manifest:', err);
    toast('Error de red al cerrar el día', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHTML;
  }
}

/**
 * Carga información del manifest actual (cantidad de pedidos)
 */
async function loadManifestInfo() {
  try {
    const r = await fetch('/api/manifests/current');
    if (!r.ok) return;

    const data = await r.json();
    const countEl = document.getElementById('manifest-count');

    if (countEl) {
      if (data.exists) {
        countEl.textContent = `Manifest actual: ${data.order_count} pedidos`;
      } else {
        countEl.textContent = 'Sin manifest abierto';
      }
    }
  } catch (err) {
    console.error('Error cargando info de manifest:', err);
  }
}

// ═══════════════════════════════════════════════════════════════
// DESCARGAS MASIVAS — MERCADOLIBRE
// ═══════════════════════════════════════════════════════════════

/**
 * Descarga ZPL masivo para los pedidos MeLi seleccionados
 */
async function downloadBulkMeliZpl() {
  const checked = document.querySelectorAll('.meli-checkbox:not([disabled]):checked');
  const ids = Array.from(checked).map(cb => cb.value);

  if (ids.length === 0) return;

  const bulkBtn = document.getElementById('bulk-download-zpl-btn').querySelector('button');
  bulkBtn.disabled = true;

  try {
    const url = `/api/orders/meli/bulk-zpl?ids=${ids.join(',')}`;
    const r   = await fetch(url);

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      let msg = (err.detail && typeof err.detail === 'object')
        ? err.detail.message
        : (err.detail || r.statusText);
      if (err.detail && Array.isArray(err.detail.failed) && err.detail.failed.length > 0) {
        msg += '\nDetalle: ' + err.detail.failed.map(f => f.error).join(' | ');
      }
      toast(`Error: ${msg}`, 'error');
      return;
    }

    const labelCount  = r.headers.get('X-Labels-Count') || ids.length;
    const failedCount = parseInt(r.headers.get('X-Failed-Count') || '0', 10);

    const blob   = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a      = document.createElement('a');
    a.href       = blobUrl;
    a.download   = 'etiquetas_meli.txt';
    a.click();
    URL.revokeObjectURL(blobUrl);

    toast(
      `${labelCount} etiqueta${labelCount != 1 ? 's' : ''} descargada${labelCount != 1 ? 's' : ''}` +
      (failedCount > 0 ? ` · ${failedCount} fallido${failedCount !== 1 ? 's' : ''}` : ''),
      failedCount > 0 ? 'warn' : 'success'
    );

    // Marcar los pedidos descargados como completados en el cache local
    // para que aparezcan con el banner "Etiqueta ya generada" en esta sesión
    ids.forEach(id => {
      const order = allOrders.find(o => String(o.id) === String(id));
      if (order) order.status = 'completed';
    });

    // Refrescar la vista (los pedidos procesados quedan visibles pero marcados)
    renderOrders();

  } catch {
    toast('Error de conexión', 'error');
  } finally {
    bulkBtn.disabled = false;
  }
}

/**
 * Descarga PDFs masivos para los pedidos MeLi seleccionados
 */
async function downloadBulkMeliPdf() {
  const checked = document.querySelectorAll('.meli-checkbox:not([disabled]):checked');
  const ids = Array.from(checked).map(cb => cb.value);

  if (ids.length === 0) return;

  const bulkBtn = document.getElementById('bulk-download-pdf-btn').querySelector('button');
  bulkBtn.disabled = true;

  try {
    const url = `/api/orders/meli/bulk-pdf?ids=${ids.join(',')}`;
    const r   = await fetch(url);

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      let msg = (err.detail && typeof err.detail === 'object')
        ? err.detail.message
        : (err.detail || r.statusText);
      if (err.detail && Array.isArray(err.detail.failed) && err.detail.failed.length > 0) {
        msg += '\nDetalle: ' + err.detail.failed.map(f => f.error).join(' | ');
      }
      toast(`Error: ${msg}`, 'error');
      return;
    }

    const ordersCount = r.headers.get('X-Orders-Count') || ids.length;
    const failedCount = parseInt(r.headers.get('X-Failed-Count') || '0', 10);

    const blob   = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a      = document.createElement('a');
    a.href       = blobUrl;
    a.download   = 'picking_meli_masivo.pdf';
    a.click();
    URL.revokeObjectURL(blobUrl);

    toast(
      `${ordersCount} hoja${ordersCount != 1 ? 's' : ''} de picking descargada${ordersCount != 1 ? 's' : ''}` +
      (failedCount > 0 ? ` · ${failedCount} fallido${failedCount !== 1 ? 's' : ''}` : ''),
      failedCount > 0 ? 'warn' : 'success'
    );

    // Refrescar la vista
    renderOrders();

  } catch {
    toast('Error de conexión', 'error');
  } finally {
    bulkBtn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════
// DESCARGAS INDIVIDUALES
// ═══════════════════════════════════════════════════════════════

/**
 * Descarga ZPL individual para reimpresión
 * @param {string} source - Fuente del pedido
 * @param {string|number} id - ID del pedido
 */
async function downloadSingleZpl(source, id) {
  try {
    const url = `/api/orders/${id}/zpl?source=${source}`;
    const r = await fetch(url);

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      const msg = (err.detail && typeof err.detail === 'object')
        ? err.detail.message
        : (err.detail || r.statusText);
      toast(`Error al descargar ZPL: ${msg}`, 'error');
      return;
    }

    const blob = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = `zpl_${id}.txt`;
    a.click();
    URL.revokeObjectURL(blobUrl);

    toast('Etiqueta ZPL descargada correctamente', 'success');

    // Refrescar pedidos para actualizar el estado si cambió
    await loadOrders();
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

/**
 * Descarga hoja de picking (PDF) de un solo pedido
 * @param {string} source - Fuente del pedido
 * @param {string|number} id - ID del pedido
 */
async function downloadSinglePdf(source, id) {
  try {
    const url = `/api/orders/${id}/prepare?source=${source}`;
    const r = await fetch(url, { method: 'POST' });

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      const msg = (err.detail && typeof err.detail === 'object')
        ? err.detail.message
        : (err.detail || r.statusText);
      toast(`Error al generar PDF: ${msg}`, 'error');
      return;
    }

    const blob = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = `picking_${id}.pdf`;
    a.click();
    URL.revokeObjectURL(blobUrl);

    toast('Hoja de picking descargada correctamente', 'success');

    // Refrescar pedidos para actualizar el estado si cambió
    await loadOrders();
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

/**
 * Genera PDF de picking individual y lo abre en nueva pestaña
 * @param {string|number} orderId - ID del pedido
 * @param {string} source - Fuente del pedido
 */
async function prepareOrder(orderId, source) {
  const btn = document.querySelector(`[data-id="${orderId}"]`);
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<svg class="spinner w-5 h-5 mr-2" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
    </svg> Generando PDF...`;
  }

  try {
    const r = await fetch(`/api/orders/${orderId}/prepare?source=${source}`, { method: 'POST' });

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Error: ${err.detail || r.statusText}`, 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Preparar pedido'; }
      return;
    }

    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    window.open(url, '_blank');

    toast(`Pedido #${orderId} en preparación`, 'success');
    setTimeout(loadOrders, 1200);

  } catch (err) {
    toast('Error de conexión', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Preparar pedido'; }
  }
}

// ═══════════════════════════════════════════════════════════════
// MANIFEST
// ═══════════════════════════════════════════════════════════════

/**
 * Busca un pedido específico por ID
 */
async function searchOrder() {
  const input = document.getElementById('scan-input');
  const id    = input.value.trim().replace(/^#/, '');
  if (!id) {
    input.focus();
    return;
  }
  input.value = '';

  document.getElementById('scan-result').classList.add('hidden');
  document.getElementById('scan-error').classList.add('hidden');
  document.getElementById('scan-loading').classList.remove('hidden');
  currentOrder = null;

  try {
    const r = await fetch(`/api/orders/${id}?source=${DEFAULT_SOURCE}`);

    document.getElementById('scan-loading').classList.add('hidden');

    if (r.status === 404) {
      document.getElementById('scan-error-msg').textContent = `Pedido #${id} no encontrado`;
      document.getElementById('scan-error').classList.remove('hidden');
      input.select();
      return;
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      document.getElementById('scan-error-msg').textContent = err.detail || 'Error al buscar el pedido';
      document.getElementById('scan-error').classList.remove('hidden');
      input.select();
      return;
    }

    const order = await r.json();
    currentOrder = order;
    renderScanResult(order);

  } catch (err) {
    document.getElementById('scan-loading').classList.add('hidden');
    document.getElementById('scan-error-msg').textContent = 'Error de conexión al servidor';
    document.getElementById('scan-error').classList.remove('hidden');
  }
}

// ═══════════════════════════════════════════════════════════════
// SCANNER — DESCARGAS Y ACCIONES
// ═══════════════════════════════════════════════════════════════

/**
 * Descarga PDF de picking del pedido actual en el scanner
 */
async function downloadPickingPdf() {
  if (!currentOrder) return;
  const { id, source } = currentOrder;
  try {
    const r = await fetch(`/api/orders/${id}/prepare?source=${source || DEFAULT_SOURCE}`, { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Error: ${err.detail || r.statusText}`, 'error');
      return;
    }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    window.open(url, '_blank');
    toast(`PDF de picking descargado`, 'success');
  } catch {
    toast('Error de conexión', 'error');
  }
}

/**
 * Descarga archivo ZPL del pedido actual en el scanner
 */
async function downloadZpl() {
  if (!currentOrder) return;
  const { id, source } = currentOrder;
  try {
    const r = await fetch(`/api/orders/${id}/zpl?source=${source || DEFAULT_SOURCE}`);
    if (!r.ok) {
      toast('Error al generar ZPL', 'error');
      return;
    }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `etiqueta_${id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
    document.getElementById('zpl-confirm-section').classList.remove('hidden');
    toast('ZPL descargado — confirma la impresión manual cuando esté lista', 'info');
  } catch {
    toast('Error de conexión', 'error');
  }
}

/**
 * Imprime etiqueta del pedido actual en impresora Zebra
 * @param {boolean} autoReset - Si debe resetear el scanner automáticamente
 */
async function printLabel(autoReset = false) {
  if (!currentOrder) return;

  const { id, source } = currentOrder;

  try {
    const r = await fetch(`/api/orders/${id}/label?source=${source || DEFAULT_SOURCE}`, {
      method: 'POST'
    });

    const body = await r.json().catch(() => ({}));

    if (!r.ok) {
      const detail = body.detail || {};
      const msg = typeof detail === 'string'
        ? detail
        : `${detail.message || 'Error'} — ${detail.reason || ''} (${detail.printer || ''})`;
      toast(`Error de impresión: ${msg} — usa "Descargar ZPL" como respaldo`, 'error');
      return;
    }

    document.getElementById('zpl-confirm-section').classList.add('hidden');
    toast(`Pedido #${id} completado ✅`, 'success');
    currentOrder  = null;
    lastScannedId = null;

    if (activeView === 'dashboard') loadOrders();

    if (autoReset) {
      setTimeout(resetScanner, 1500);
    }

  } catch (err) {
    toast('Error de conexión al servidor', 'error');
  }
}

/**
 * Cambia manualmente el estado de un pedido
 * @param {string} newStatus - Nuevo estado: 'processing', 'completed', 'cancelled'
 */
async function setStatus(newStatus) {
  if (!currentOrder) return;
  const { id, source } = currentOrder;
  const labels = { processing: 'Procesando', completed: 'Completado', cancelled: 'Cancelado' };

  try {
    const r = await fetch(
      `/api/orders/${id}/set-status?source=${source || DEFAULT_SOURCE}&new_status=${newStatus}`,
      { method: 'POST' }
    );
    const body = await r.json().catch(() => ({}));

    if (!r.ok) {
      toast(`Error: ${body.detail || r.statusText}`, 'error');
      return;
    }

    toast(`Pedido #${id} marcado como ${labels[newStatus] || newStatus}`, 'success');

    if (newStatus === 'completed') {
      document.getElementById('zpl-confirm-section').classList.add('hidden');
    }
    resetScanner();

    currentOrder = null;
    if (activeView === 'dashboard') loadOrders();

  } catch {
    toast('Error de conexión', 'error');
  }
}
