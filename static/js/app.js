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

let auth0Client = null;
let currentUser = null;

window.auth0Client = null;

function getAuthConfig() {
  return {
    domain: String(window.AUTH0_DOMAIN || '').trim(),
    clientId: String(window.AUTH0_CLIENT_ID || '').trim(),
    audience: String(window.AUTH0_AUDIENCE || '').trim(),
  };
}

function updateAuthUI(isAuthenticated, user = null) {
  const appShell = document.getElementById('app-shell');
  const appNav = document.getElementById('app-nav');
  const printerStatus = document.getElementById('printer-status');
  const authLoading = document.getElementById('auth-loading');

  const loginBtn = document.getElementById('auth-login-btn');
  const logoutBtn = document.getElementById('auth-logout-btn');
  const userBox = document.getElementById('auth-user');
  const userName = document.getElementById('auth-user-name');
  const userEmail = document.getElementById('auth-user-email');

  if (authLoading) authLoading.classList.add('hidden');

  if (isAuthenticated) {
    appShell?.classList.remove('hidden');
    appNav?.classList.remove('hidden');
    appNav?.classList.add('flex');
    printerStatus?.classList.remove('hidden');
    printerStatus?.classList.add('flex');

    loginBtn?.classList.add('hidden');
    logoutBtn?.classList.remove('hidden');

    userBox?.classList.remove('hidden');
    if (userName) userName.textContent = user?.name || user?.nickname || 'Usuario autenticado';
    if (userEmail) userEmail.textContent = user?.email || '';
    return;
  }

  appShell?.classList.add('hidden');
  appNav?.classList.add('hidden');
  appNav?.classList.remove('flex');
  printerStatus?.classList.add('hidden');
  printerStatus?.classList.remove('flex');

  logoutBtn?.classList.add('hidden');
  userBox?.classList.add('hidden');
  loginBtn?.classList.remove('hidden');
}

async function login() {
  if (!auth0Client) return;
  const { audience } = getAuthConfig();
  await auth0Client.loginWithRedirect({
    authorizationParams: {
      audience,
      redirect_uri: `${window.location.origin}${window.location.pathname}`,
    },
  });
}

async function logout() {
  if (!auth0Client) return;
  await auth0Client.logout({
    logoutParams: {
      returnTo: `${window.location.origin}${window.location.pathname}`,
    },
  });
}

function bindAuthButtons() {
  const loginBtn = document.getElementById('auth-login-btn');
  const logoutBtn = document.getElementById('auth-logout-btn');

  loginBtn?.addEventListener('click', () => void login());
  logoutBtn?.addEventListener('click', () => void logout());
}

async function bootAuthenticatedApp() {
  setFilter('all');
  await loadOrders();
  await loadManifestInfo();
  await pingPrinter();
  setInterval(pingPrinter, 30_000);
}

async function initializeAuth() {
  bindAuthButtons();

  const { domain, clientId, audience } = getAuthConfig();
  if (!domain || !clientId || !audience) {
    updateAuthUI(false, null);
    toast('Falta configuración de Auth0 en la página', 'error');
    return;
  }

  if (!window.auth0 || typeof window.auth0.createAuth0Client !== 'function') {
    updateAuthUI(false, null);
    toast('No se pudo cargar Auth0 SPA JS', 'error');
    return;
  }

  try {
    auth0Client = await window.auth0.createAuth0Client({
      domain,
      clientId,
      cacheLocation: 'localstorage',
      useRefreshTokens: true,
      authorizationParams: {
        audience,
        redirect_uri: `${window.location.origin}${window.location.pathname}`,
      },
    });

    window.auth0Client = auth0Client;

    const query = window.location.search;
    if (query.includes('code=') && query.includes('state=')) {
      await auth0Client.handleRedirectCallback();
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    const isAuthenticated = await auth0Client.isAuthenticated();
    if (!isAuthenticated) {
      updateAuthUI(false, null);
      return;
    }

    currentUser = await auth0Client.getUser();
    updateAuthUI(true, currentUser);
    await bootAuthenticatedApp();
  } catch (error) {
    console.error('Error inicializando Auth0:', error);
    updateAuthUI(false, null);
    toast('No se pudo inicializar la autenticación', 'error');
  }
}

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
  void initializeAuth();
});
