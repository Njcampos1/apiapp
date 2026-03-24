/**
 * utils.js
 * Funciones utilitarias para UpperLogistics
 * - Sistema de notificaciones toast
 * - Información de fuentes (WooCommerce, Mercado Libre)
 * - Badges: logística, estado de pedido, región
 */

// Iconos y metadata de las fuentes de pedidos
const SOURCE_ICONS = {
  woocommerce: {
    icon: `<svg class="w-8 h-8" viewBox="0 0 24 24" fill="currentColor">
             <path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm0 18c-4.418 0-8-3.582-8-8s3.582-8 8-8 8 3.582 8 8-3.582 8-8 8zm-1-5h2v2h-2zm0-8h2v6h-2z"/>
           </svg>`,
    label: 'WooCommerce',
    color: 'text-purple-300 bg-purple-800',
  },
  mercadolibre: {
    icon: `<svg class="w-8 h-8" viewBox="0 0 24 24" fill="currentColor">
             <circle cx="12" cy="12" r="10"/>
           </svg>`,
    label: 'Mercado Libre',
    color: 'text-yellow-300 bg-yellow-700',
  },
  manual: {
    icon: `<span class="text-2xl">📝</span>`,
    label: 'Manual',
    color: 'text-gray-300 bg-gray-700',
  },
};

/**
 * Muestra una notificación toast temporal
 * @param {string} msg - Mensaje a mostrar
 * @param {string} type - Tipo: 'success', 'error', 'info', 'warn'
 */
function toast(msg, type = 'info') {
  const colors = {
    success: 'bg-green-600 text-white',
    error:   'bg-red-600 text-white',
    info:    'bg-coffee-900 text-white',
    warn:    'bg-amber-500 text-white',
  };
  const el = document.createElement('div');
  el.className = `pointer-events-auto max-w-xs px-4 py-3 rounded-xl shadow-lg text-sm font-medium
                  ${colors[type] || colors.info} animate-slide-in`;
  el.textContent = msg;
  const container = document.getElementById('toast-container');
  container.appendChild(el);
  setTimeout(() => el.classList.add('opacity-0', 'transition-opacity', 'duration-500'), 3000);
  setTimeout(() => el.remove(), 3600);
}

/**
 * Devuelve la información de iconos/labels para una fuente de pedido
 * @param {string} source - 'woocommerce', 'mercadolibre', 'manual'
 * @returns {object} Objeto con icon, label, color
 */
function sourceInfo(source) {
  return SOURCE_ICONS[source] || SOURCE_ICONS['manual'];
}

/**
 * Genera badge HTML para el tipo de logística de MercadoLibre
 * @param {object} order - Objeto del pedido
 * @returns {string} HTML del badge o string vacío
 */
function logisticBadge(order) {
  const logisticType = (order.platform_meta && order.platform_meta.logistic_type) || '';
  if (!logisticType) return '';

  const lt = logisticType.toLowerCase();
  if (lt === 'fulfillment') {
    return `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-emerald-100 text-emerald-700">⚡ FULL</span>`;
  }
  if (lt.includes('cross_docking') || lt.includes('xd_drop_off') || lt.includes('flex')) {
    return `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-blue-100 text-blue-700">⚡ FLEX</span>`;
  }
  return `<span class="text-xs px-2 py-0.5 rounded-full font-bold bg-gray-100 text-gray-600">🚚 Despacho</span>`;
}

/**
 * Genera badge HTML para el estado del pedido
 * @param {string} status - Estado: 'processing', 'preparing', 'labeled', etc
 * @returns {string} HTML del badge
 */
function orderStatusBadge(status) {
  const map = {
    processing:  'bg-blue-100 text-blue-700',
    preparing:   'bg-amber-100 text-amber-700',
    labeled:     'bg-indigo-100 text-indigo-700',
    completed:   'bg-green-100 text-green-700',
    cancelled:   'bg-red-100 text-red-700',
    error:       'bg-red-100 text-red-700',
  };
  const labels = {
    processing: 'En Cola',
    preparing:  'En Preparación',
    labeled:    'Etiquetado',
    completed:  'Completado',
    cancelled:  'Cancelado',
    error:      'Error',
  };
  const cls = map[status] || 'bg-gray-100 text-gray-700';
  return `<span class="text-xs px-2 py-0.5 rounded-full font-semibold ${cls}">${labels[status] || status}</span>`;
}

/**
 * Genera badge HTML para la región de envío
 * @param {object} shipping - Objeto con info de envío
 * @returns {string} HTML del badge o string vacío
 */
function regionBadge(shipping) {
  const state = (shipping?.state || '').trim().toUpperCase();
  if (!state) return '';
  const isRM = state === 'RM' || state.includes('METROPOLITANA');
  if (isRM) {
    return `<span class="text-xs px-2 py-0.5 rounded-full font-semibold bg-sky-100 text-sky-700">RM</span>`;
  }
  return `<span class="text-xs px-2 py-0.5 rounded-full font-semibold bg-red-100 text-red-700">Región</span>`;
}
