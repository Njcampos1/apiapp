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

const AppState = (() => {
  const state = {
    currentOrder: null,
    activeView: 'dashboard',
    lastScannedId: null,
    allOrders: [],
    currentFilter: 'all',
    _orderCache: {},
    appBootstrapped: false,
  };
  const listeners = new Set();

  return {
    get(key) {
      return key ? state[key] : { ...state };
    },
    set(patch) {
      if (!patch || typeof patch !== 'object') return;
      Object.assign(state, patch);
      listeners.forEach((listener) => listener(state, patch));
    },
    subscribe(listener) {
      if (typeof listener !== 'function') return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  };
})();

window.AppState = AppState;

const DEFAULT_SOURCE = 'woocommerce';


function updateAdminNavigationVisibility() {
  const navUsersBtn = document.getElementById('nav-users-btn');
  const isAdmin = localStorage.getItem('is_admin') === 'true';
  if (navUsersBtn) {
    navUsersBtn.classList.toggle('hidden', !isAdmin);
  }

  if (typeof window.updateToolsAdminVisibility === 'function') {
    window.updateToolsAdminVisibility();
  }
}

// ═══════════════════════════════════════════════════════════════
// AUTENTICACIÓN (JWT)
// ═══════════════════════════════════════════════════════════════

function showLoginError(message) {
  const errorEl = document.getElementById('login-error');
  if (!errorEl) return;
  errorEl.textContent = message;
  errorEl.classList.remove('hidden');
}

function clearLoginError() {
  const errorEl = document.getElementById('login-error');
  if (!errorEl) return;
  errorEl.textContent = '';
  errorEl.classList.add('hidden');
}

function showLoginView() {
  const loginContainer = document.getElementById('login-container');
  const appContainer = document.getElementById('app-container');
  if (loginContainer) loginContainer.style.display = 'flex';
  if (appContainer) appContainer.style.display = 'none';
}

function showAppView() {
  const loginContainer = document.getElementById('login-container');
  const appContainer = document.getElementById('app-container');
  if (loginContainer) loginContainer.style.display = 'none';
  if (appContainer) appContainer.style.display = 'block';
}

function bootstrapAuthenticatedApp() {
  if (AppState.get('appBootstrapped')) return;

  setFilter('all');
  loadOrders();
  loadManifestInfo();
  pingPrinter();
  setInterval(pingPrinter, 30_000);
  AppState.set({ appBootstrapped: true });
}

function checkAuth() {
  const token = localStorage.getItem('jwt_token');
  if (token) {
    updateAdminNavigationVisibility();
    showAppView();
    bootstrapAuthenticatedApp();
    return true;
  }

  localStorage.removeItem('is_admin');
  updateAdminNavigationVisibility();
  showLoginView();
  return false;
}

function logout(options = {}) {
  const { reload = true } = options;
  localStorage.removeItem('jwt_token');
  localStorage.removeItem('is_admin');
  updateAdminNavigationVisibility();
  showLoginView();
  if (reload) {
    window.location.reload();
  }
}

async function handleLoginSubmit(e) {
  e.preventDefault();

  const usernameInput = document.getElementById('login-username');
  const passwordInput = document.getElementById('login-password');
  const submitBtn = document.getElementById('login-submit-btn');

  const username = usernameInput?.value?.trim() || '';
  const password = passwordInput?.value || '';

  clearLoginError();

  if (!username || !password) {
    showLoginError('Debes ingresar usuario y contraseña.');
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = 'Validando...';

  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ username, password }),
    });

    if (!response.ok) {
      if (response.status === 401) {
        showLoginError('Credenciales inválidas');
        return;
      }
      const payload = await response.json().catch(() => ({}));
      showLoginError(payload.detail || 'No se pudo iniciar sesión.');
      return;
    }

    const data = await response.json();
    if (!data.access_token) {
      showLoginError('Respuesta inválida del servidor.');
      return;
    }

    localStorage.setItem('jwt_token', data.access_token);
    localStorage.setItem('is_admin', String(Boolean(data.is_admin)));
    updateAdminNavigationVisibility();
    showAppView();
    bootstrapAuthenticatedApp();
    if (passwordInput) passwordInput.value = '';
  } catch {
    showLoginError('Error de conexión. Intenta nuevamente.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Iniciar sesión';
  }
}

window.logout = logout;
window.checkAuth = checkAuth;
window.handleUnauthorized = () => {
  logout({ reload: false });
  showLoginError('Tu sesión expiró. Inicia sesión nuevamente.');
};

// ═══════════════════════════════════════════════════════════════
// NAVEGACIÓN ENTRE VISTAS
// ═══════════════════════════════════════════════════════════════

/**
 * Cambia la vista activa de la aplicación
 * @param {string} view - 'dashboard', 'scanner', 'tools'
 */
function showView(view) {
  AppState.set({ activeView: view });
  document.getElementById('view-dashboard').classList.toggle('hidden', view !== 'dashboard');
  document.getElementById('view-scanner').classList.toggle('hidden', view !== 'scanner');
  document.getElementById('view-tools').classList.toggle('hidden', view !== 'tools');
  document.getElementById('users-container').classList.toggle('hidden', view !== 'users');

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

  const navUsersBtn = document.getElementById('nav-users-btn');
  if (navUsersBtn && !navUsersBtn.classList.contains('hidden')) {
    navUsersBtn.className =
      view === 'users'
        ? 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-400 text-coffee-900'
        : 'tab-btn px-4 py-2 rounded-lg font-semibold text-sm transition-all bg-coffee-900 text-coffee-200 hover:bg-coffee-800 border border-coffee-700';
  }

  if (view === 'users' && typeof window.loadUsers === 'function') {
    window.loadUsers();
  }

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
  if (AppState.get('activeView') === 'scanner' && !e.target.closest('button') && !e.target.closest('input')) {
    document.getElementById('scan-input').focus();
  }
});

// ═══════════════════════════════════════════════════════════════
// INICIALIZACIÓN
// ═══════════════════════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', () => {
  const loginForm = document.getElementById('login-form');
  const logoutBtn = document.getElementById('logout-btn');
  const navUsersBtn = document.getElementById('nav-users-btn');

  if (loginForm) {
    loginForm.addEventListener('submit', handleLoginSubmit);
  }
  if (logoutBtn) {
    logoutBtn.addEventListener('click', () => logout());
  }
  if (navUsersBtn) {
    navUsersBtn.addEventListener('click', () => showView('users'));
  }

  checkAuth();
});
