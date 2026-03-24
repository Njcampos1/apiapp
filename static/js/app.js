/**
 * app.js
 * Inicialización y estado global de UpperLogistics
 * - Variables de estado global
 * - Cambio entre vistas (dashboard, scanner, tools)
 * - Event listeners y setup inicial
 */

// ═══════════════════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

let currentOrder    = null;
let activeView      = 'dashboard';
let lastScannedId   = null;
const DEFAULT_SOURCE = 'woocommerce';

// Estado del dashboard
let allOrders     = [];   // Todos los pedidos recibidos de la API
let currentFilter = 'all'; // 'all' | 'woocommerce' | 'mercadolibre' | 'preparing'
let _orderCache   = {};    // Cache de objetos de pedido para el modal MeLi: "source:id" → order

// ═══════════════════════════════════════════════════════════════
// NAVEGACIÓN ENTRE VISTAS
// ═══════════════════════════════════════════════════════════════

/**
 * Cambia la vista activa de la aplicación
 * @param {string} view - 'dashboard', 'scanner', 'tools'
 */
function showView(view) {
  activeView = view;
  document.getElementById('view-dashboard').classList.toggle('hidden', view !== 'dashboard');
  document.getElementById('view-scanner').classList.toggle('hidden', view !== 'scanner');
  document.getElementById('view-tools').classList.toggle('hidden', view !== 'tools');

  document.getElementById('tab-dashboard').className =
    view === 'dashboard'
      ? 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-400 text-coffee-900'
      : 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-900 text-coffee-200 hover:bg-coffee-800 border border-coffee-700';

  document.getElementById('tab-scanner').className =
    view === 'scanner'
      ? 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-400 text-coffee-900'
      : 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-900 text-coffee-200 hover:bg-coffee-800 border border-coffee-700';

  document.getElementById('tab-tools').className =
    view === 'tools'
      ? 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-400 text-coffee-900'
      : 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-900 text-coffee-200 hover:bg-coffee-800 border border-coffee-700';

  if (view === 'scanner') {
    setTimeout(() => document.getElementById('scan-input').focus(), 100);
  }
}

// ═══════════════════════════════════════════════════════════════
// EVENT LISTENERS GLOBALES
// ═══════════════════════════════════════════════════════════════

// Cerrar modal MeLi con tecla Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeMeliDetailModal();
});

// Auto-focus en scanner cuando se hace click en el fondo
document.addEventListener('click', (e) => {
  if (activeView === 'scanner' && !e.target.closest('button') && !e.target.closest('input')) {
    document.getElementById('scan-input').focus();
  }
});

// ═══════════════════════════════════════════════════════════════
// INICIALIZACIÓN
// ═══════════════════════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', () => {
  setFilter('all'); // Aplica estilos de pills en el estado inicial
  loadOrders();
  loadManifestInfo();
  pingPrinter();
  setInterval(pingPrinter, 30_000);
});
