/**
 * dashboard.js
 * Lógica del dashboard principal de pedidos
 * - Filtrado de pedidos por fuente (WooCommerce, MercadoLibre)
 * - Selección múltiple de pedidos MeLi
 * - Modal de detalle MeLi
 */

/**
 * Cambia el filtro activo del dashboard y actualiza la UI
 * @param {string} filter - 'all', 'woocommerce', 'mercadolibre', 'preparing'
 */
function setFilter(filter) {
  AppState.set({ currentFilter: filter });

  // Estilos de pills: activo vs inactivo
  const pills = {
    all:           document.getElementById('pill-all'),
    woocommerce:   document.getElementById('pill-woocommerce'),
    mercadolibre:  document.getElementById('pill-mercadolibre'),
    preparing:     document.getElementById('pill-preparing'),
  };
  const activeClass   = 'bg-coffee-900 text-white shadow-md';
  const inactiveClass = 'bg-coffee-100 text-coffee-700 border border-coffee-200 hover:bg-coffee-200';
  // Estilo especial para el pill de recuperación
  const preparingActive   = 'bg-orange-500 text-white shadow-md';
  const preparingInactive = 'ml-auto px-3 py-1 rounded-full text-xs transition-all text-coffee-500 border border-coffee-200 hover:bg-coffee-100';

  Object.entries(pills).forEach(([key, el]) => {
    if (!el) return;
    if (key === 'preparing') {
      el.className = `filter-pill ${filter === 'preparing' ? preparingActive + ' ml-auto px-3 py-1 rounded-full text-xs transition-all' : preparingInactive}`;
    } else {
      el.className = `filter-pill px-4 py-1.5 rounded-full font-semibold text-sm transition-all ${
        key === filter ? activeClass : inactiveClass
      }`;
    }
  });

  renderOrders();
}

// ═══════════════════════════════════════════════════════════════
// SELECCIÓN MÚLTIPLE MERCADOLIBRE
// ═══════════════════════════════════════════════════════════════

/**
 * Actualiza la visibilidad del botón flotante y el estado del select-all
 */
function updateBulkButton() {
  const checkboxes = document.querySelectorAll('.meli-checkbox:not([disabled])');
  const checked    = document.querySelectorAll('.meli-checkbox:not([disabled]):checked');
  const zplBtn     = document.getElementById('bulk-download-zpl-btn');
  const pdfBtn     = document.getElementById('bulk-download-pdf-btn');
  const badge      = document.getElementById('bulk-count-badge');
  const pdfBadge   = document.getElementById('bulk-pdf-count-badge');
  const selectAll  = document.getElementById('select-all-meli');
  const currentFilter = AppState.get('currentFilter');

  if (currentFilter === 'mercadolibre' && checked.length > 0) {
    zplBtn.classList.remove('hidden');
    pdfBtn.classList.remove('hidden');
    badge.textContent = checked.length;
    pdfBadge.textContent = checked.length;
  } else {
    zplBtn.classList.add('hidden');
    pdfBtn.classList.add('hidden');
  }

  // Estado indeterminado del select-all
  if (selectAll) {
    if (checkboxes.length === 0) {
      selectAll.checked       = false;
      selectAll.indeterminate = false;
    } else if (checked.length === 0) {
      selectAll.checked       = false;
      selectAll.indeterminate = false;
    } else if (checked.length === checkboxes.length) {
      selectAll.checked       = true;
      selectAll.indeterminate = false;
    } else {
      selectAll.checked       = false;
      selectAll.indeterminate = true;
    }
  }

  // Contador de selección en la barra
  const barCount = document.getElementById('meli-selected-count');
  if (barCount) {
    if (currentFilter === 'mercadolibre' && checked.length > 0) {
      barCount.textContent = `${checked.length} seleccionado${checked.length !== 1 ? 's' : ''}`;
    } else {
      barCount.textContent = '';
    }
  }
}

/**
 * Selecciona o deselecciona todos los checkboxes MeLi habilitados
 * @param {boolean} checked - true para seleccionar todo, false para deseleccionar
 */
function toggleAllMeli(checked) {
  document.querySelectorAll('.meli-checkbox:not([disabled])').forEach(cb => {
    cb.checked = checked;
  });
  updateBulkButton();
}

// ═══════════════════════════════════════════════════════════════
// MODAL DE DETALLE MERCADOLIBRE
// ═══════════════════════════════════════════════════════════════

/**
 * Abre el modal con el detalle completo de un pedido MercadoLibre
 * @param {string} source - Fuente del pedido
 * @param {string|number} id - ID del pedido
 */
function openMeliDetailModal(source, id) {
  const orderCache = AppState.get('_orderCache') || {};
  const order = orderCache[`${source}:${id}`];
  if (!order) { toast('No se pudo cargar el detalle del pedido', 'error'); return; }

  const meta = order.platform_meta || {};
  const ship = order.shipping || {};

  // Nombre del comprador
  const name = [ship.first_name, ship.last_name].filter(Boolean).join(' ')
               || meta.receiver_name
               || meta.buyer_nickname
               || '—';

  // Tipo de envío: Flex (xd_drop_off / cross_docking) o despacho normal
  const logisticType = meta.logistic_type || '';
  const lt = logisticType.toLowerCase();
  let logisticLabel;
  if (lt.includes('cross_docking') || lt.includes('xd_drop_off') || lt.includes('flex')) {
    logisticLabel = '<span class="font-bold text-blue-700 bg-blue-100 px-2 py-0.5 rounded-full text-xs">⚡ Flex</span>';
  } else if (lt === 'fulfillment') {
    logisticLabel = '<span class="font-bold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full text-xs">⚡ Full</span>';
  } else if (lt) {
    logisticLabel = '<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-gray-100 text-gray-600">🚚 Despacho</span>';
  } else {
    logisticLabel = '<span class="text-xs text-coffee-400">—</span>';
  }

  document.getElementById('modal-order-id').textContent    = `#${order.id}`;
  document.getElementById('modal-logistic').innerHTML      = logisticLabel;
  document.getElementById('modal-name').textContent        = name;
  document.getElementById('modal-nickname').textContent    = ship.full_address || [ship.address_1, ship.city].filter(Boolean).join(', ') || '—';
  const shippingStatus = (meta.shipping_status || '').toLowerCase();
  const piiBlockedStates = ['shipped', 'delivered', 'dropped_off', 'cancelled'];
  const piiBlocked = piiBlockedStates.includes(shippingStatus);

  const rutText = meta.rut || '';
  const phoneText = ship.phone || '';

  document.getElementById('modal-rut').textContent =
    rutText || (piiBlocked ? 'No disponible (pedido ya despachado)' : 'No informado por el comprador');
  document.getElementById('modal-phone').textContent =
    phoneText || (piiBlocked ? 'No disponible (pedido ya despachado)' : '—');
  document.getElementById('modal-shipping-status').textContent = meta.shipping_status || '—';

  // Mostrar nota del cliente si existe
  const customerNote = (order.customer_note || '').trim();
  const noteSection = document.getElementById('modal-customer-note-section');
  const noteElement = document.getElementById('modal-customer-note');
  if (customerNote) {
    noteElement.textContent = customerNote;
    noteSection.classList.remove('hidden');
  } else {
    noteSection.classList.add('hidden');
  }

  const itemsHtml = (order.items || []).map(item => `
    <tr class="border-b border-coffee-100 last:border-0">
      <td class="py-2 pr-3 font-mono text-xs text-coffee-500">${item.sku || '—'}</td>
      <td class="py-2 pr-3 text-sm text-coffee-900">${item.name}</td>
      <td class="py-2 text-center">
        <span class="bg-coffee-900 text-white text-xs font-black w-6 h-6 rounded-full inline-flex items-center justify-center">${item.quantity}</span>
      </td>
    </tr>`).join('');
  document.getElementById('modal-items').innerHTML = itemsHtml
    || '<tr><td colspan="3" class="py-3 text-coffee-400 text-sm text-center">Sin ítemes</td></tr>';

  document.getElementById('meli-detail-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

/**
 * Cierra el modal de detalle MeLi
 */
function closeMeliDetailModal() {
  document.getElementById('meli-detail-modal').classList.add('hidden');
  document.body.style.overflow = '';
}
