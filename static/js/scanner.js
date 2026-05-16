/**
 * scanner.js
 * Lógica de verificación y escáner de pedidos
 * - Manejo de input de escaneo (Enter, doble escaneo)
 * - Renderizado de resultados del scanner
 * - Impresión automática y reset
 */

/**
 * Maneja el input del scanner (detecta Enter y doble escaneo)
 * @param {KeyboardEvent} e - Evento del teclado
 */
function getOrderScanCode(order) {
  if (!order) return '';
  if (order.source === 'mercadolibre' && order.platform_meta && order.platform_meta.shipping_id) {
    return String(order.platform_meta.shipping_id);
  }
  return String(order.id || '');
}

function handleScanInput(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    const input = document.getElementById('scan-input');
    const id    = input.value.trim().replace(/^#/, '');
    if (!id) { input.focus(); return; }

    // Doble escaneo: mismo código cuando ya hay un pedido cargado
    const currentOrder = AppState.get('currentOrder');
    if (currentOrder && (id === String(currentOrder.id) || id === getOrderScanCode(currentOrder))) {
      input.value = '';
      autoPrintAndComplete();
    } else {
      searchOrder();
    }
  }
}

/**
 * Imprime automáticamente y completa el pedido (para doble escaneo)
 */
async function autoPrintAndComplete() {
  await printLabel(/*autoReset=*/true);
}

/**
 * Renderiza el resultado del pedido escaneado en la UI
 * @param {object} order - Objeto del pedido encontrado
 */
function renderScanResult(order) {
  const ship = order.shipping || {};
  const name = [ship.first_name, ship.last_name].filter(Boolean).join(' ') || '—';
  const addr = [ship.address_1, ship.address_2].filter(Boolean).join(', ') || '—';

  const visibleId = getOrderScanCode(order);
  document.getElementById('result-id').textContent      = `#${visibleId}`;
  document.getElementById('result-name').textContent    = name;
  document.getElementById('result-phone').textContent   = ship.phone || '—';
  document.getElementById('result-address').textContent = addr;
  document.getElementById('result-city').textContent    = ship.city || '—';

  document.getElementById('result-status-badge').innerHTML = orderStatusBadge(order.status);

  const src = sourceInfo(order.source);
  document.getElementById('result-source-badge').innerHTML = `
    <div class="flex flex-col items-center gap-1 ${src.color} rounded-xl p-2">
      ${src.icon}
      <span class="text-xs font-bold">${src.label}</span>
    </div>`;

  const itemsHtml = (order.items || []).map((item, idx) => {
    const isPack = item.is_pack;
    const hasBreakdown = isPack && item.pack_breakdown && item.pack_breakdown.length > 0;

    // Color de fondo según si es pack o no
    const bgColor = isPack ? 'bg-orange-50 border-2 border-orange-200' : 'bg-coffee-50 border border-coffee-100';

    let html = `
    <div class="mb-2 rounded-xl overflow-hidden ${bgColor}">
      <div class="flex items-center justify-between py-2 px-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1">
            <p class="text-sm font-semibold text-coffee-900">${item.name}</p>
            ${isPack ? '<span class="text-[10px] bg-orange-500 text-white px-1.5 py-0.5 rounded font-bold">PACK</span>' : ''}
          </div>
          <p class="text-xs text-coffee-400 font-mono">SKU: ${item.sku}</p>
        </div>
        <div class="ml-3 flex-shrink-0">
          <span class="bg-coffee-900 text-white text-sm font-black w-7 h-7 rounded-full flex items-center justify-center">
            ${item.quantity}
          </span>
        </div>
      </div>`;

    // Si es un Pack y tiene desglose, mostrarlo en un panel destacado
    if (hasBreakdown) {
      html += `
      <div class="bg-orange-100 border-t-2 border-orange-300 px-3 py-2">
        <p class="text-xs font-bold text-orange-800 mb-1.5 uppercase tracking-wide">📦 Contenido del pack:</p>
        <div class="space-y-1">`;

      item.pack_breakdown.forEach(pack => {
        html += `
          <div class="flex items-center gap-2 bg-white rounded-lg px-2.5 py-1.5">
            <span class="bg-orange-500 text-white text-xs font-black px-2 py-0.5 rounded-full min-w-[2rem] text-center">
              ${pack.cajas_de_10}x
            </span>
            <span class="text-sm font-semibold text-orange-900">${pack.sabor}</span>
          </div>`;
      });

      html += `
        </div>
      </div>`;
    }

    html += `</div>`;
    return html;
  }).join('');
  document.getElementById('result-items').innerHTML = itemsHtml || '<p class="text-coffee-400 text-sm">Sin ítemes</p>';

  const noteSection = document.getElementById('result-note-section');
  if (order.customer_note) {
    document.getElementById('result-note').textContent = order.customer_note;
    noteSection.classList.remove('hidden');
  } else {
    noteSection.classList.add('hidden');
  }

  AppState.set({ lastScannedId: visibleId });
  document.getElementById('zpl-confirm-section').classList.add('hidden');
  document.getElementById('scan-result').classList.remove('hidden');
}

/**
 * Resetea el estado del scanner (limpia input y resultados)
 */
function resetScanner() {
  AppState.set({ currentOrder: null });
  document.getElementById('scan-input').value = '';
  document.getElementById('scan-result').classList.add('hidden');
  document.getElementById('scan-error').classList.add('hidden');
  document.getElementById('scan-input').focus();
}
