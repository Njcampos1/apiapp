/**
 * users.js
 * Gestión de usuarios (solo administrador)
 * - Listado de usuarios
 * - Creación de nuevos usuarios
 */

function showUsersFormError(message) {
  const errorEl = document.getElementById('users-form-error');
  if (!errorEl) return;
  errorEl.textContent = message;
  errorEl.classList.remove('hidden');
}

function clearUsersFormError() {
  const errorEl = document.getElementById('users-form-error');
  if (!errorEl) return;
  errorEl.textContent = '';
  errorEl.classList.add('hidden');
}

function extractApiErrorMessage(payload, fallbackMessage) {
  if (!payload) return fallbackMessage;

  if (Array.isArray(payload.detail)) {
    const firstError = payload.detail[0];
    if (firstError?.loc && firstError?.msg) {
      const fieldName = firstError.loc[firstError.loc.length - 1];
      return `${fieldName}: ${firstError.msg}`;
    }
    return payload.detail.map((item) => item?.msg).filter(Boolean).join(' | ') || fallbackMessage;
  }

  if (typeof payload.detail === 'string' && payload.detail.trim()) {
    return payload.detail;
  }

  return fallbackMessage;
}

function notifyUsers(message, type = 'info') {
  if (typeof window.toast === 'function') {
    window.toast(message, type);
    return;
  }
  window.alert(message);
}

function renderUsersTable(users) {
  const tbody = document.getElementById('users-table-body');
  if (!tbody) return;

  if (!Array.isArray(users) || users.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="2" class="px-4 py-4 text-center text-coffee-500">No hay usuarios registrados</td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = users
    .map(
      (user) => `
      <tr class="hover:bg-coffee-50">
        <td class="px-4 py-3 font-mono text-coffee-700">${user.id}</td>
        <td class="px-4 py-3 font-semibold text-coffee-900">${user.username}</td>
      </tr>
    `,
    )
    .join('');
}

async function loadUsers() {
  const isAdmin = localStorage.getItem('is_admin') === 'true';
  if (!isAdmin) return;

  const tbody = document.getElementById('users-table-body');
  if (tbody) {
    tbody.innerHTML = `
      <tr>
        <td colspan="2" class="px-4 py-4 text-center text-coffee-500">Cargando usuarios...</td>
      </tr>
    `;
  }

  try {
    const response = await fetch('/api/users');
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const message = payload.detail || 'No se pudieron cargar los usuarios';
      notifyUsers(message, 'error');
      renderUsersTable([]);
      return;
    }

    const users = await response.json();
    renderUsersTable(users);
  } catch {
    notifyUsers('Error de conexión al cargar usuarios', 'error');
    renderUsersTable([]);
  }
}

async function handleCreateUserSubmit(event) {
  event.preventDefault();

  const usernameInput = document.getElementById('users-username');
  const passwordInput = document.getElementById('users-password');
  const submitBtn = document.getElementById('users-submit-btn');

  const username = usernameInput?.value?.trim() || '';
  const password = passwordInput?.value || '';

  clearUsersFormError();

  if (!username || !password) {
    showUsersFormError('Debes ingresar usuario y contraseña.');
    return;
  }

  if (password.length < 8) {
    showUsersFormError('La contraseña debe tener al menos 8 caracteres.');
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = 'Creando...';

  try {
    const response = await fetch('/api/users', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ username, password }),
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      showUsersFormError(extractApiErrorMessage(payload, 'No se pudo crear el usuario.'));
      return;
    }

    notifyUsers('Usuario creado correctamente', 'success');
    if (usernameInput) usernameInput.value = '';
    if (passwordInput) passwordInput.value = '';
    await loadUsers();
  } catch {
    showUsersFormError('Error de conexión al crear usuario.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Crear Usuario';
  }
}

window.loadUsers = loadUsers;

window.addEventListener('DOMContentLoaded', () => {
  const createForm = document.getElementById('users-create-form');
  if (createForm) {
    createForm.addEventListener('submit', handleCreateUserSubmit);
  }
});
