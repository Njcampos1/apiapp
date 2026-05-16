/**
 * orders.js
 * Renderizado y manejo de pedidos
 * - Función principal de renderizado con filtros
 * - Construcción de tarjetas de pedido (HTML)
 * - Cache de pedidos para modal MeLi
 */

/**
 * Renderiza los pedidos filtrados en el dashboard
 * Aplica filtros, ordena, y construye el HTML de todas las tarjetas
 */
function renderOrders() {
  const grid    = document.getElementById('orders-grid');
  const empty   = document.getElementById('empty-state');
  const count   = document.getElementById('order-count');
  const meliBar = document.getElementById('meli-bulk-bar');

  const currentFilter = AppState.get('currentFilter');
  const allOrders = AppState.get('allOrders');

  let filtered;
  if (currentFilter === 'mercadolibre') {
    // La pestaña MeLi muestra pedidos pendientes y listos para enviar (ready_to_ship)
    // pero excluye los que ya fueron despachados (shipped/delivered/dropped_off)
    filtered = allOrders.filter(o => {
      if (o.source !== 'mercadolibre') return false;

      const shippingStatus = (o.platform_meta || {}).shipping_status;
      const blockedStates = ['shipped', 'delivered', 'dropped_off'];

      // Excluir pedidos ya despachados
      return !blockedStates.includes(shippingStatus);
    });
    // Pendientes primero, completados al final
    filtered.sort((a, b) => {
      const aDone = a.status === 'completed' ? 1 : 0;
      const bDone = b.status === 'completed' ? 1 : 0;
      return aDone - bDone;
    });
  } else if (currentFilter === 'woocommerce') {
    filtered = allOrders.filter(o => o.source === 'woocommerce' && o.status !== 'preparing' && o.status !== 'labeled');
  } else if (currentFilter === 'preparing') {
    // Filtro de recuperación: pedidos ya impresos (preparing o labeled) pero no completados
    filtered = allOrders.filter(o => o.source === 'woocommerce' && (o.status === 'preparing' || o.status === 'labeled') && o.status !== 'completed');
  } else {
    // "Todos": excluye completados y pedidos ya impresos (van en pestaña recuperación)
    filtered = allOrders.filter(o => o.status !== 'completed' && o.status !== 'preparing' && o.status !== 'labeled');
  }

  const inMeliFilter     = currentFilter === 'mercadolibre';
  const inPreparingFilter = currentFilter === 'preparing';

  // Mostrar/ocultar barra de selección MeLi
  meliBar.classList.toggle('hidden', !inMeliFilter);

  // Resetear select-all al cambiar de pestaña
  const selectAll = document.getElementById('select-all-meli');
  if (selectAll) {
    selectAll.checked = false;
    selectAll.indeterminate = false;
  }

  // Contar solo los pendientes en la etiqueta del encabezado
  const pendingCount = filtered.filter(o => o.status !== 'completed').length;
  if (inMeliFilter) {
    count.textContent = `${pendingCount} pendiente${pendingCount !== 1 ? 's' : ''} · ${filtered.length - pendingCount} ya generado${filtered.length - pendingCount !== 1 ? 's' : ''}`;
  } else if (inPreparingFilter) {
    count.textContent = `${filtered.length} hoja${filtered.length !== 1 ? 's' : ''} impresa${filtered.length !== 1 ? 's' : ''} (recuperables)`;
  } else {
    count.textContent = filtered.length;
  }

  if (filtered.length === 0) {
    grid.classList.add('hidden');
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    // Poblar cache de pedidos para el modal MeLi (evita pasar JSON como arg onclick)
    const nextCache = {};
    filtered.forEach(o => { nextCache[`${o.source}:${o.id}`] = o; });
    AppState.set({ _orderCache: nextCache });
    grid.innerHTML = filtered.map(o => buildOrderCard(o)).join('');
    grid.classList.remove('hidden');
  }

  updateBulkButton();
}

/**
 * Construye el HTML de una tarjeta individual de pedido
 * @param {object} order - Objeto del pedido con toda su info
 * @returns {string} HTML de la tarjeta
 */
function buildOrderCard(order) {
  const currentFilter = AppState.get('currentFilter');
  const src  = sourceInfo(order.source);
  const ship = order.shipping || {};
  const meta = order.platform_meta || {};
  const name = [ship.first_name, ship.last_name].filter(Boolean).join(' ')
               || meta.buyer_nickname
               || meta.receiver_name
               || 'Sin nombre';
  const city = ship.city || '—';
  const itemCount = (order.items || []).reduce((acc, i) => acc + i.quantity, 0);
  const total = new Intl.NumberFormat('es-CL', { style: 'currency', currency: order.currency || 'CLP' })
                       .format(order.total || 0);

  // Zona horaria Chile (America/Santiago maneja DST automáticamente)
  const dateStr = order.created_at
    ? new Date(order.created_at).toLocaleString('es-CL', {
        timeZone: 'America/Santiago',
        day: '2-digit', month: '2-digit',
        hour: '2-digit', minute: '2-digit',
      })
    : '';

  const isMeli      = order.source === 'mercadolibre';
  const isCompleted = order.status === 'completed';
  const isPreparing = order.status === 'preparing' || order.status === 'labeled';
  const showCheckbox = currentFilter === 'mercadolibre' && isMeli;

  // Lógica de bloqueo inteligente: solo bloquear si realmente está despachado/entregado/dropped_off
  const isMeliLocked = isMeli && (meta.shipping_status === 'shipped' || meta.shipping_status === 'delivered' || meta.shipping_status === 'dropped_off');

  // ── Badge de estado: para MeLi usamos etiquetas específicas ──
  let statusBadge;
  if (isMeli) {
    statusBadge = isCompleted
      ? `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-green-100 text-green-700">Impreso</span>`
      : `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-red-100 text-red-700">Por Imprimir</span>`;
  } else if (isPreparing) {
    statusBadge = `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-orange-100 text-orange-700">Hoja impresa</span>`;
  } else {
    statusBadge = orderStatusBadge(order.status);
  }

  const regionBadgeHtml = regionBadge(order.shipping);

  // Badge logístico solo para MeLi
  const logisticHtml = isMeli ? logisticBadge(order) : '';

  // Estado logístico real del envío (solo MeLi)
  const shippingStatusHtml = (isMeli && meta.shipping_status)
    ? `<div class="flex items-center gap-2 text-sm">
         <svg class="w-4 h-4 text-coffee-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4"/>
         </svg>
         <span class="text-coffee-500 text-xs truncate">${meta.shipping_status}</span>
       </div>`
    : '';

  // Visual para pedidos MeLi completados pero no bloqueados
  const cardOpacity = isMeliLocked ? 'opacity-70' : '';
  const completedBanner = (isMeli && isCompleted)
    ? `<div class="bg-green-50 border-b border-green-200 px-4 py-1.5 flex items-center gap-2">
         <span class="text-green-700 text-xs font-bold">✅ Etiqueta generada ${isMeliLocked ? '' : '(Reimpresión disponible)'}</span>
       </div>`
    : '';

  // Banner naranja para pedidos en recuperación (preparing WooCommerce)
  const preparingBanner = (!isMeli && isPreparing)
    ? `<div class="bg-orange-50 border-b border-orange-200 px-4 py-1.5 flex items-center gap-2">
         <span class="text-orange-700 text-xs font-bold">🖨 Hoja ya impresa — recuperar</span>
       </div>`
    : '';

  // Checkbox en esquina superior derecha solo en pestaña MeLi
  // Solo se deshabilita si isMeliLocked (ya despachado/entregado)
  const checkboxHtml = showCheckbox
    ? `<input
         type="checkbox"
         class="meli-checkbox absolute top-3 right-3 w-5 h-5 rounded cursor-pointer accent-yellow-500 z-10"
         value="${order.id}"
         ${isMeliLocked ? 'disabled title="Ya despachado"' : ''}
         onchange="updateBulkButton()"
       />`
    : '';

  // ── Botón de acción según plataforma y estado ─────────────────
  // Botón "Ver detalle" solo para MercadoLibre
  const detailBtn = isMeli
    ? `<button
         onclick="openMeliDetailModal('${order.source}', '${order.id}')"
         class="flex-1 bg-coffee-100 hover:bg-coffee-200 active:scale-95 text-coffee-900 font-bold py-3.5 rounded-xl transition-all shadow-sm text-base flex items-center justify-center gap-2">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
         </svg>
         Ver detalle
       </button>`
    : '';

  let actionBtn = '';
  let showBothButtons = false;

  if (isMeli && isMeliLocked) {
    // MeLi ya despachado/entregado: solo botón deshabilitado + ver detalle
    showBothButtons = true;
    actionBtn = `<button disabled
         class="flex-1 bg-gray-100 text-gray-500 font-bold py-3.5 rounded-xl text-base flex items-center justify-center gap-2 cursor-not-allowed">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/>
         </svg>
         🚚 Ya despachado
       </button>`;
  } else if (isMeli && isCompleted) {
    // MeLi completado: mostrar "Ver detalle" + "Hoja de Picking (PDF)"
    showBothButtons = true;
    actionBtn = `<button
         onclick="downloadSinglePdf('${order.source}', '${order.id}')"
         class="flex-1 bg-blue-500 hover:bg-blue-400 active:scale-95 text-white font-bold py-3.5 rounded-xl transition-all shadow text-base flex items-center justify-center gap-2">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
         </svg>
         Hoja de Picking
       </button>`;
  } else if (isMeli) {
    // MeLi pendiente: mostrar "Ver detalle" + "Hoja de Picking"
    showBothButtons = true;
    actionBtn = `<button
         onclick="downloadSinglePdf('${order.source}', '${order.id}')"
         class="flex-1 bg-blue-600 hover:bg-blue-500 active:scale-95 text-white font-bold py-3.5 rounded-xl transition-all shadow text-base flex items-center justify-center gap-2">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
         </svg>
         Hoja de Picking
       </button>`;
  } else if (isPreparing) {
    // WooCommerce en preparing: solo "Re-imprimir hoja"
    actionBtn = `<button
         onclick="prepareOrder('${order.id}', '${order.source}')"
         data-id="${order.id}"
         class="prepare-btn w-full bg-orange-500 hover:bg-orange-400 active:scale-95 text-white font-bold py-3.5 rounded-xl transition-all shadow text-base flex items-center justify-center gap-2">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
         </svg>
         Re-imprimir hoja
       </button>`;
  } else {
    // WooCommerce processing: solo "Preparar pedido"
    actionBtn = `<button
         onclick="prepareOrder('${order.id}', '${order.source}')"
         data-id="${order.id}"
         class="prepare-btn w-full bg-coffee-900 hover:bg-coffee-800 active:scale-95 text-white font-bold py-3.5 rounded-xl transition-all shadow text-base flex items-center justify-center gap-2">
         <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
           <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
             d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
         </svg>
         Preparar pedido
       </button>`;
  }

  // Combinar botones si es necesario
  const buttonsHtml = showBothButtons
    ? `<div class="flex gap-2">${detailBtn}${actionBtn}</div>`
    : actionBtn;

  const stripColor = order.source === 'woocommerce'
    ? 'bg-purple-500'
    : order.source === 'mercadolibre'
      ? 'bg-yellow-400'
      : 'bg-gray-400';

  const rawDisplayId = isMeli
    ? (meta.shipping_id || order.display_id || order.id)
    : order.id;
  const displayId = String(rawDisplayId);

  // Desplegable de productos (solo WooCommerce)
  const hasPack = order.items && order.items.some(item => item.is_pack === true);
  const productsDropdown = (!isMeli && order.items && order.items.length > 0)
    ? `<details class="group mb-3">
         <summary class="cursor-pointer list-none flex items-center gap-1.5 text-coffee-500 hover:text-coffee-800 transition-colors select-none py-1">
           <svg class="w-3.5 h-3.5 transition-transform duration-200 group-open:rotate-90 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
             <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"/>
           </svg>
           <span class="text-xs font-semibold">Ver productos (${order.items.length})</span>
         </summary>
         <ul class="mt-1.5 space-y-0.5 border ${hasPack ? 'border-orange-300 bg-orange-50' : 'border-coffee-100'} rounded-xl overflow-hidden">
           ${(order.items || []).map((item, idx) => {
             const bgColor = hasPack
               ? (idx % 2 === 0 ? 'bg-orange-100' : 'bg-orange-50')
               : (idx % 2 === 0 ? 'bg-coffee-50' : 'bg-white');

             let itemHtml = `
               <li class="px-3 py-1.5 text-xs ${bgColor}">
                 <div class="flex items-center gap-2.5">
                   <span class="bg-coffee-900 text-white font-black text-[10px] w-5 h-5 rounded-full inline-flex items-center justify-center flex-shrink-0">${item.quantity}</span>
                   <span class="text-coffee-800 leading-snug flex-1">${item.name}</span>
                   ${item.is_pack ? '<span class="text-[10px] bg-orange-500 text-white px-1.5 py-0.5 rounded font-bold">PACK</span>' : ''}
                 </div>`;

             // Si es un Pack y tiene desglose, mostrarlo
             if (item.is_pack && item.pack_breakdown && item.pack_breakdown.length > 0) {
               itemHtml += `
                 <div class="ml-8 mt-1.5 space-y-0.5 text-[11px] text-coffee-600 border-l-2 border-orange-400 pl-2">
                   ${item.pack_breakdown.map(pack => `
                     <div class="flex items-center gap-1.5">
                       <span class="font-semibold text-orange-700">${pack.cajas_de_10}x</span>
                       <span>${pack.sabor}</span>
                     </div>
                   `).join('')}
                 </div>`;
             }

             itemHtml += '</li>';
             return itemHtml;
           }).join('')}
         </ul>
       </details>`
    : '';

  return `
  <div class="relative bg-white rounded-2xl shadow-md hover:shadow-lg transition-shadow border border-coffee-100 overflow-hidden ${cardOpacity}">
    ${checkboxHtml}
    <div class="h-1.5 ${stripColor}"></div>
    ${completedBanner}
    ${preparingBanner}
    <div class="p-5">
      <div class="flex items-start justify-between mb-3">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-xs px-2 py-0.5 rounded-full font-bold ${src.color}">${src.label}</span>
          ${statusBadge}
          ${regionBadgeHtml}
          ${logisticHtml}
        </div>
        <span class="text-xs text-coffee-400 ${showCheckbox ? 'pr-7' : ''}">${dateStr}</span>
      </div>
      <p class="text-2xl font-black text-coffee-900 mb-3 tracking-tight">#${displayId}</p>
      <div class="space-y-1 mb-4">
        <div class="flex items-center gap-2 text-sm">
          <svg class="w-4 h-4 text-coffee-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/>
          </svg>
          <span class="font-semibold text-coffee-800 truncate">${name}</span>
        </div>
        <div class="flex items-center gap-2 text-sm">
          <svg class="w-4 h-4 text-coffee-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/>
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/>
          </svg>
          <span class="text-coffee-600 truncate">${city}</span>
        </div>
        ${shippingStatusHtml}
      </div>
      <div class="flex items-center justify-between text-sm mb-3 py-2.5 px-3 bg-coffee-50 rounded-xl">
        <span class="text-coffee-600">${itemCount} unidad${itemCount !== 1 ? 'es' : ''} · ${(order.items || []).length} producto${(order.items || []).length !== 1 ? 's' : ''}</span>
        <span class="font-bold text-coffee-900">${total}</span>
      </div>
      ${productsDropdown}
      ${buttonsHtml}
    </div>
  </div>`;
}
