/**
 * tools.js
 * Lógica de la pestaña Herramientas
 * - Módulo Calculador DUN-14 (individual y por lotes)
 * - Módulo Gestión de Packs (selección guiada)
 */

const PACK_EDITABLE_CATEGORIES = ['packs_prearmados'];

let activeToolModule = 'dun14';
let activeDunTab = 'single';
let batchResults = [];

let skusLoaded = false;
let skusAuditLoaded = false;
let skusSnapshot = '';
let skusData = null;

let packEditorState = {
  category: 'packs_prearmados',
  packSku: '',
  selectedItems: [],
  search: '',
};

function normalizeText(value) {
  return String(value || '').trim().toLowerCase();
}

function isAdminUser() {
  return localStorage.getItem('is_admin') === 'true';
}

function getSkusCatalog() {
  if (!skusData || typeof skusData !== 'object') return null;
  if (!skusData.catalogo || typeof skusData.catalogo !== 'object') return null;
  return skusData.catalogo;
}

function getPackListByCategory(category) {
  const catalog = getSkusCatalog();
  if (!catalog) return [];
  const packs = catalog[category];
  return Array.isArray(packs) ? packs : [];
}

function getCurrentPackObject() {
  if (!packEditorState.packSku) return null;
  const packs = getPackListByCategory(packEditorState.category);
  return packs.find((pack) => pack.sku_principal === packEditorState.packSku) || null;
}

function createOptionKey(item) {
  return `${item.sku_unitario}__${item.sabor}`;
}

function parseOptionKey(key) {
  const [skuUnitario, sabor] = String(key).split('__');
  return { sku_unitario: skuUnitario, sabor };
}

function buildAvailablePackOptions() {
  const optionMap = new Map();

  PACK_EDITABLE_CATEGORIES.forEach((category) => {
    const packs = getPackListByCategory(category);
    packs.forEach((pack) => {
      const contenido = Array.isArray(pack.contenido) ? pack.contenido : [];
      contenido.forEach((item) => {
        if (!item || typeof item !== 'object') return;
        const skuUnitario = String(item.sku_unitario || '').trim();
        const sabor = String(item.sabor || '').trim();
        if (!skuUnitario || !sabor) return;
        const key = createOptionKey({ sku_unitario: skuUnitario, sabor });
        if (!optionMap.has(key)) {
          optionMap.set(key, { sku_unitario: skuUnitario, sabor });
        }
      });
    });
  });

  const selectedMap = new Map(
    packEditorState.selectedItems.map((item) => [createOptionKey(item), item]),
  );
  selectedMap.forEach((item, key) => {
    if (!optionMap.has(key)) {
      optionMap.set(key, { sku_unitario: item.sku_unitario, sabor: item.sabor });
    }
  });

  return Array.from(optionMap.values()).sort((a, b) => {
    const flavorDiff = a.sabor.localeCompare(b.sabor, 'es', { sensitivity: 'base' });
    if (flavorDiff !== 0) return flavorDiff;
    return a.sku_unitario.localeCompare(b.sku_unitario, 'es', { sensitivity: 'base' });
  });
}

function hasUnsavedSkusChanges() {
  if (!skusData || !skusSnapshot) return false;
  try {
    return JSON.stringify(skusData) !== skusSnapshot;
  } catch {
    return false;
  }
}

function shouldWarnOnBeforeUnload() {
  return activeToolModule === 'packs' && hasUnsavedSkusChanges();
}

function updatePackEditorStatus(message, type = 'info') {
  const statusEl = document.getElementById('pack-editor-status');
  if (!statusEl) return;

  const colorByType = {
    info: 'text-coffee-500',
    success: 'text-green-600',
    error: 'text-red-600',
  };

  statusEl.className = `text-sm ${colorByType[type] || colorByType.info} mb-4`;
  statusEl.textContent = message;
}

function updatePackAuditStatus(message, type = 'info') {
  const statusEl = document.getElementById('pack-audit-status');
  if (!statusEl) return;

  const colorByType = {
    info: 'text-coffee-500',
    success: 'text-green-600',
    error: 'text-red-600',
  };

  statusEl.className = `text-xs ${colorByType[type] || colorByType.info}`;
  statusEl.textContent = message;
}

function formatAuditDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('es-CL', {
    dateStyle: 'short',
    timeStyle: 'medium',
  });
}

function renderSkusAudit(entries) {
  const listEl = document.getElementById('pack-audit-list');
  const emptyEl = document.getElementById('pack-audit-empty');
  if (!listEl || !emptyEl) return;

  listEl.innerHTML = '';

  if (!entries || entries.length === 0) {
    emptyEl.classList.remove('hidden');
    return;
  }

  emptyEl.classList.add('hidden');

  entries.forEach((entry) => {
    const totals = entry?.summary?.totals || {};
    const admin = entry?.admin || 'admin';
    const backup = entry?.backup_file || '—';
    const html = `
      <div class="bg-white border border-coffee-200 rounded-lg p-3">
        <div class="flex items-start justify-between gap-3">
          <div>
            <p class="text-sm font-semibold text-coffee-800">${admin}</p>
            <p class="text-xs text-coffee-500">${formatAuditDate(entry?.timestamp)}</p>
          </div>
          <div class="text-right text-xs">
            <p class="text-green-700 font-semibold">+${totals.added || 0}</p>
            <p class="text-red-700 font-semibold">-${totals.removed || 0}</p>
            <p class="text-blue-700 font-semibold">~${totals.modified || 0}</p>
          </div>
        </div>
        <p class="text-xs text-coffee-600 mt-2"><span class="font-semibold">Backup:</span> ${backup}</p>
      </div>
    `;
    listEl.insertAdjacentHTML('beforeend', html);
  });
}

function applyToolModuleStyles() {
  const dunBtn = document.getElementById('tool-module-dun14-btn');
  const packsBtn = document.getElementById('tool-module-packs-btn');
  const dunModule = document.getElementById('tools-module-dun14');
  const packsModule = document.getElementById('tools-module-packs');

  if (!dunBtn || !packsBtn || !dunModule || !packsModule) return;

  const activeClass = 'text-left rounded-xl p-4 border transition-all';
  const active = `${activeClass} bg-coffee-900 text-white border-coffee-900`;
  const inactive = `${activeClass} bg-coffee-50 text-coffee-800 border-coffee-200 hover:bg-coffee-100`;

  dunBtn.className = activeToolModule === 'dun14' ? active : inactive;
  packsBtn.className = activeToolModule === 'packs' ? active : inactive;

  dunModule.classList.toggle('hidden', activeToolModule !== 'dun14');
  packsModule.classList.toggle('hidden', activeToolModule !== 'packs');
}

function setToolModule(module) {
  if (module === 'packs' && !isAdminUser()) {
    toast('Solo administradores pueden usar Gestión de Packs', 'warn');
    return;
  }

  if (activeToolModule === 'packs' && module !== 'packs' && hasUnsavedSkusChanges()) {
    const shouldLeave = window.confirm('Tienes cambios sin guardar en Gestión de Packs. ¿Deseas salir sin guardar?');
    if (!shouldLeave) return;
  }

  activeToolModule = module;
  applyToolModuleStyles();

  if (module === 'packs') {
    loadSkus();
    loadSkusAudit();
  }
}

function updateToolsAdminVisibility() {
  const packsBtn = document.getElementById('tool-module-packs-btn');
  if (!packsBtn) return;

  const isAdmin = isAdminUser();
  packsBtn.classList.toggle('hidden', !isAdmin);

  if (!isAdmin && activeToolModule === 'packs') {
    activeToolModule = 'dun14';
  }

  applyToolModuleStyles();
}

function setDunTab(tab) {
  activeDunTab = tab;

  const singleTab = document.getElementById('dun-tab-single');
  const batchTab = document.getElementById('dun-tab-batch');

  const activeClass = 'px-4 py-2 font-semibold text-sm transition-all border-b-2 border-coffee-400 text-coffee-900';
  const inactiveClass = 'px-4 py-2 font-semibold text-sm transition-all border-b-2 border-transparent text-coffee-400 hover:text-coffee-600';

  if (singleTab) singleTab.className = tab === 'single' ? activeClass : inactiveClass;
  if (batchTab) batchTab.className = tab === 'batch' ? activeClass : inactiveClass;

  document.getElementById('dun-single-view').classList.toggle('hidden', tab !== 'single');
  document.getElementById('dun-batch-view').classList.toggle('hidden', tab !== 'batch');
}

function renderPackSelectOptions() {
  const packSelect = document.getElementById('pack-select');
  if (!packSelect) return;

  const packs = getPackListByCategory(packEditorState.category);
  packSelect.innerHTML = '';

  if (packs.length === 0) {
    packSelect.insertAdjacentHTML('beforeend', '<option value="">No hay packs disponibles</option>');
    packEditorState.packSku = '';
    return;
  }

  packs.forEach((pack) => {
    const sku = String(pack.sku_principal || '');
    const nombre = String(pack.nombre || sku);
    const selected = sku === packEditorState.packSku ? 'selected' : '';
    packSelect.insertAdjacentHTML('beforeend', `<option value="${sku}" ${selected}>${nombre} (${sku})</option>`);
  });

  const exists = packs.some((pack) => pack.sku_principal === packEditorState.packSku);
  if (!exists) {
    packEditorState.packSku = String(packs[0].sku_principal || '');
    packSelect.value = packEditorState.packSku;
  }
}

function syncStateToCurrentPack() {
  const pack = getCurrentPackObject();
  if (!pack) return;

  const normalized = packEditorState.selectedItems.map((item) => ({
    sku_unitario: item.sku_unitario,
    sabor: item.sabor,
    cajas_de_10: Math.max(1, Number.parseInt(item.cajas_de_10, 10) || 1),
  }));

  pack.contenido = normalized;
  const totalCajas = normalized.reduce((acc, item) => acc + item.cajas_de_10, 0);
  pack.cantidad_total_capsulas = totalCajas * 10;

  const totalCapsulesEl = document.getElementById('pack-total-capsules');
  if (totalCapsulesEl) {
    totalCapsulesEl.textContent = `Total: ${pack.cantidad_total_capsulas} cápsulas`;
  }
}

function renderPackOptions() {
  const optionsEl = document.getElementById('pack-item-options');
  const countEl = document.getElementById('pack-options-count');
  if (!optionsEl) return;

  const allOptions = buildAvailablePackOptions();
  const search = normalizeText(packEditorState.search);
  const filtered = allOptions.filter((option) => {
    if (!search) return true;
    const haystack = `${option.sku_unitario} ${option.sabor}`.toLowerCase();
    return haystack.includes(search);
  });

  if (countEl) {
    countEl.textContent = `${filtered.length} opción(es)`;
  }

  if (filtered.length === 0) {
    optionsEl.innerHTML = '<p class="text-sm text-coffee-500">No se encontraron ítems.</p>';
    return;
  }

  const selectedKeys = new Set(packEditorState.selectedItems.map((item) => createOptionKey(item)));

  optionsEl.innerHTML = filtered.map((option) => {
    const key = createOptionKey(option);
    const checked = selectedKeys.has(key) ? 'checked' : '';
    return `
      <label class="flex items-center gap-3 bg-white border border-coffee-200 rounded-lg px-3 py-2 cursor-pointer">
        <input type="checkbox" data-option-key="${key}" ${checked}
          class="w-4 h-4 rounded accent-coffee-900" />
        <div class="min-w-0">
          <p class="text-sm font-semibold text-coffee-800 truncate">${option.sabor}</p>
          <p class="text-xs text-coffee-500 font-mono">${option.sku_unitario}</p>
        </div>
      </label>
    `;
  }).join('');

  optionsEl.querySelectorAll('input[data-option-key]').forEach((checkbox) => {
    checkbox.addEventListener('change', (event) => {
      const key = event.currentTarget.dataset.optionKey;
      if (!key) return;
      togglePackOption(key, event.currentTarget.checked);
    });
  });
}

function renderSelectedPackItems() {
  const itemsEl = document.getElementById('pack-selected-items');
  const emptyEl = document.getElementById('pack-manager-empty');
  if (!itemsEl || !emptyEl) return;

  if (packEditorState.selectedItems.length === 0) {
    itemsEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
    syncStateToCurrentPack();
    return;
  }

  emptyEl.classList.add('hidden');
  itemsEl.innerHTML = packEditorState.selectedItems.map((item, index) => `
    <div class="bg-white border border-coffee-200 rounded-lg px-3 py-2 flex items-center gap-3">
      <div class="min-w-0 flex-1">
        <p class="text-sm font-semibold text-coffee-800 truncate">${item.sabor}</p>
        <p class="text-xs text-coffee-500 font-mono">${item.sku_unitario}</p>
      </div>
      <div class="w-28">
        <label class="text-[10px] uppercase tracking-wide text-coffee-500">Cajas de 10</label>
        <input type="number" min="1" value="${item.cajas_de_10}" data-item-index="${index}"
          class="pack-item-qty w-full bg-coffee-50 border border-coffee-200 rounded-md px-2 py-1 text-sm" />
      </div>
      <button type="button" data-remove-index="${index}"
        class="text-xs text-red-700 hover:text-red-900 font-semibold px-2 py-1 rounded-md hover:bg-red-50">
        Quitar
      </button>
    </div>
  `).join('');

  itemsEl.querySelectorAll('.pack-item-qty').forEach((input) => {
    input.addEventListener('input', (event) => {
      const index = Number.parseInt(event.currentTarget.dataset.itemIndex, 10);
      if (!Number.isInteger(index) || !packEditorState.selectedItems[index]) return;
      const value = Number.parseInt(event.currentTarget.value, 10);
      packEditorState.selectedItems[index].cajas_de_10 = Number.isInteger(value) && value > 0 ? value : 1;
      syncStateToCurrentPack();
      updatePackEditorStatus('Tienes cambios sin guardar', 'info');
    });
  });

  itemsEl.querySelectorAll('button[data-remove-index]').forEach((button) => {
    button.addEventListener('click', (event) => {
      const index = Number.parseInt(event.currentTarget.dataset.removeIndex, 10);
      if (!Number.isInteger(index)) return;
      packEditorState.selectedItems.splice(index, 1);
      renderPackManager();
      updatePackEditorStatus('Tienes cambios sin guardar', 'info');
    });
  });

  syncStateToCurrentPack();
}

function renderPackManager() {
  const panel = document.getElementById('pack-manager-panel');
  if (!panel) return;

  const hasPack = Boolean(getCurrentPackObject());
  panel.classList.toggle('hidden', !hasPack);

  if (!hasPack) {
    updatePackEditorStatus('Selecciona un pack para comenzar', 'info');
    return;
  }

  renderPackOptions();
  renderSelectedPackItems();

  if (hasUnsavedSkusChanges()) {
    updatePackEditorStatus('Tienes cambios sin guardar', 'info');
  } else {
    updatePackEditorStatus('Sin cambios pendientes', 'success');
  }
}

function loadSelectedPackIntoState() {
  const pack = getCurrentPackObject();
  if (!pack) {
    packEditorState.selectedItems = [];
    renderPackManager();
    return;
  }

  const contenido = Array.isArray(pack.contenido) ? pack.contenido : [];
  packEditorState.selectedItems = contenido
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      sku_unitario: String(item.sku_unitario || '').trim(),
      sabor: String(item.sabor || '').trim(),
      cajas_de_10: Number.isInteger(item.cajas_de_10) && item.cajas_de_10 > 0 ? item.cajas_de_10 : 1,
    }))
    .filter((item) => item.sku_unitario && item.sabor);

  renderPackManager();
}

function togglePackOption(optionKey, isSelected) {
  const option = parseOptionKey(optionKey);
  const existingIndex = packEditorState.selectedItems.findIndex((item) => createOptionKey(item) === optionKey);

  if (isSelected && existingIndex === -1) {
    packEditorState.selectedItems.push({
      sku_unitario: option.sku_unitario,
      sabor: option.sabor,
      cajas_de_10: 1,
    });
  }

  if (!isSelected && existingIndex >= 0) {
    packEditorState.selectedItems.splice(existingIndex, 1);
  }

  renderPackManager();
  updatePackEditorStatus('Tienes cambios sin guardar', 'info');
}

function onPackCategoryChange() {
  const categorySelect = document.getElementById('pack-category-select');
  if (!categorySelect) return;

  if (hasUnsavedSkusChanges()) {
    const shouldContinue = window.confirm('Tienes cambios sin guardar. ¿Deseas cambiar de categoría?');
    if (!shouldContinue) {
      categorySelect.value = packEditorState.category;
      return;
    }
  }

  packEditorState.category = categorySelect.value;
  packEditorState.packSku = '';
  renderPackSelectOptions();

  const packSelect = document.getElementById('pack-select');
  if (packSelect) {
    packEditorState.packSku = packSelect.value || '';
  }

  loadSelectedPackIntoState();
}

function onPackSelectionChange() {
  const packSelect = document.getElementById('pack-select');
  if (!packSelect) return;
  packEditorState.packSku = packSelect.value || '';
  loadSelectedPackIntoState();
}

function onPackItemSearchInput(value) {
  packEditorState.search = value;
  renderPackOptions();
}

function initializePackManager() {
  const categorySelect = document.getElementById('pack-category-select');
  const packSelect = document.getElementById('pack-select');

  if (!categorySelect || !packSelect) return;

  categorySelect.value = packEditorState.category;
  renderPackSelectOptions();

  if (!packEditorState.packSku) {
    packEditorState.packSku = packSelect.value || '';
  } else {
    packSelect.value = packEditorState.packSku;
  }

  loadSelectedPackIntoState();
}

async function loadSkus(forceReload = false) {
  if (skusLoaded && !forceReload) {
    if (activeToolModule === 'packs') {
      initializePackManager();
    }
    return;
  }

  updatePackEditorStatus('Cargando catálogo de packs...', 'info');

  try {
    const response = await fetch('/api/skus');
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || response.statusText || 'No se pudo cargar SKUs');
    }

    skusData = payload;
    skusSnapshot = JSON.stringify(payload);
    skusLoaded = true;
    initializePackManager();
    updatePackEditorStatus('Catálogo cargado correctamente', 'success');
  } catch (error) {
    updatePackEditorStatus('Error al cargar catálogo', 'error');
    toast(`Error al cargar catálogo: ${error.message}`, 'error');
  }
}

async function saveSkus() {
  const saveBtn = document.getElementById('pack-editor-save-btn');
  if (!saveBtn || !skusData) return;

  if (!isAdminUser()) {
    toast('Solo administradores pueden guardar cambios', 'warn');
    return;
  }

  syncStateToCurrentPack();

  saveBtn.disabled = true;
  saveBtn.textContent = 'Guardando...';
  updatePackEditorStatus('Guardando cambios...', 'info');

  try {
    const response = await fetch('/api/skus', {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(skusData),
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || response.statusText || 'No se pudo guardar SKUs');
    }

    skusSnapshot = JSON.stringify(skusData);
    skusLoaded = true;
    const backupFile = payload.backup_file ? ` (backup: ${payload.backup_file})` : '';
    updatePackEditorStatus(`Cambios guardados correctamente${backupFile}`, 'success');
    toast(payload.message || 'Packs actualizados correctamente', 'success');
    await loadSkusAudit(true);
  } catch (error) {
    updatePackEditorStatus('Error al guardar cambios', 'error');
    toast(`Error al guardar packs: ${error.message}`, 'error');
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Guardar Cambios';
  }
}

async function loadSkusAudit(forceReload = false) {
  if (skusAuditLoaded && !forceReload) return;

  const refreshBtn = document.getElementById('pack-audit-refresh-btn');
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Actualizando...';
  }

  updatePackAuditStatus('Cargando historial...', 'info');

  try {
    const response = await fetch('/api/skus/audit?limit=30');
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || response.statusText || 'No se pudo cargar el historial');
    }

    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    renderSkusAudit(entries);
    skusAuditLoaded = true;
    updatePackAuditStatus(`Mostrando ${entries.length} registro(s) recientes`, 'success');
  } catch (error) {
    updatePackAuditStatus('Error al cargar historial', 'error');
    toast(`Error al cargar historial: ${error.message}`, 'error');
  } finally {
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = 'Actualizar Historial';
    }
  }
}

async function reloadSkus() {
  const reloadBtn = document.getElementById('pack-editor-reload-btn');

  if (hasUnsavedSkusChanges()) {
    const shouldReload = window.confirm('Hay cambios sin guardar. ¿Deseas recargar y descartar los cambios actuales?');
    if (!shouldReload) return;
  }

  if (reloadBtn) {
    reloadBtn.disabled = true;
    reloadBtn.textContent = 'Recargando...';
  }

  try {
    skusLoaded = false;
    await loadSkus(true);
    toast('Catálogo recargado desde el servidor', 'success');
  } finally {
    if (reloadBtn) {
      reloadBtn.disabled = false;
      reloadBtn.textContent = 'Recargar Catálogo';
    }
  }
}

// ═══════════════════════════════════════════════════════════════
// VALIDACIÓN DE INPUTS
// ═══════════════════════════════════════════════════════════════

/**
 * Valida input EAN-13 (solo números)
 * @param {HTMLInputElement} input - Input a validar
 */
function validateEan13(input) {
  // Permitir solo números
  input.value = input.value.replace(/\D/g, '');
}

/**
 * Valida input de indicador (solo números 1-9)
 * @param {HTMLInputElement} input - Input a validar
 */
function validateIndicator(input) {
  // Permitir solo números del 1-9
  input.value = input.value.replace(/[^1-9]/g, '');
}

// ═══════════════════════════════════════════════════════════════
// CÁLCULO DUN-14
// ═══════════════════════════════════════════════════════════════

/**
 * Calcula el dígito de control para un código de 14 dígitos
 * @param {string} cadena13Digitos - Base de 13 dígitos (indicador + primeros 12 del EAN-13)
 * @returns {string} Dígito de control calculado
 */
function calcularDigitoControl(cadena13Digitos) {
  let suma = 0;
  const reversed = cadena13Digitos.split('').reverse();

  for (let i = 0; i < reversed.length; i++) {
    const n = parseInt(reversed[i]);
    if (i % 2 === 0) {
      suma += n * 3;
    } else {
      suma += n;
    }
  }

  const modulo = suma % 10;
  const digitoControl = modulo === 0 ? 0 : 10 - modulo;
  return digitoControl.toString();
}

/**
 * Genera un código DUN-14 a partir de EAN-13 e indicador
 * @param {string} ean13 - Código EAN-13 (13 dígitos)
 * @param {string} indicador - Dígito del 1 al 9
 * @returns {object} Resultado con {dun14, ean13, indicador} o {error}
 */
function generarDun14(ean13, indicador) {
  // Validaciones
  if (ean13.length !== 13 || !/^\d+$/.test(ean13)) {
    return { error: 'El EAN-13 debe tener exactamente 13 dígitos numéricos.' };
  }
  if (indicador.length !== 1 || !/^[1-9]$/.test(indicador)) {
    return { error: 'El indicador debe ser un solo dígito del 1 al 9.' };
  }

  // Extraer los primeros 12 dígitos del EAN-13 (sin el dígito de control)
  const cuerpoEan = ean13.substring(0, 12);
  const codigoBase = indicador + cuerpoEan;

  // Calcular el nuevo dígito verificador
  const digitoVerificador = calcularDigitoControl(codigoBase);

  const dun14 = codigoBase + digitoVerificador;

  return { dun14, ean13, indicador };
}

// ═══════════════════════════════════════════════════════════════
// CALCULADOR INDIVIDUAL
// ═══════════════════════════════════════════════════════════════

/**
 * Calcula un DUN-14 individual desde el formulario
 */
function calculateDun14Single() {
  const ean13Input = document.getElementById('ean13-input');
  const indicatorInput = document.getElementById('indicator-input');
  const resultDiv = document.getElementById('dun14-result');

  const ean13 = ean13Input.value.trim();
  const indicator = indicatorInput.value.trim();

  if (!ean13 || !indicator) {
    toast('Por favor completa todos los campos', 'warn');
    return;
  }

  const result = generarDun14(ean13, indicator);

  if (result.error) {
    toast(result.error, 'error');
    return;
  }

  // Mostrar resultado
  document.getElementById('dun14-value').textContent = result.dun14;
  document.getElementById('result-ean13').textContent = result.ean13;
  document.getElementById('result-indicator').textContent = result.indicador;
  resultDiv.classList.remove('hidden');

  toast('DUN-14 calculado correctamente', 'success');
}

/**
 * Copia el DUN-14 individual al portapapeles
 */
function copyDun14() {
  const dun14 = document.getElementById('dun14-value').textContent;
  navigator.clipboard.writeText(dun14).then(() => {
    toast('DUN-14 copiado al portapapeles', 'success');
  }).catch(() => {
    toast('Error al copiar', 'error');
  });
}

// ═══════════════════════════════════════════════════════════════
// CALCULADOR POR LOTES
// ═══════════════════════════════════════════════════════════════

/**
 * Calcula múltiples DUN-14 desde una lista de EAN-13
 */
function calculateDun14Batch() {
  const batchInput = document.getElementById('ean13-batch-input');
  const indicatorInput = document.getElementById('indicator-batch-input');
  const resultsDiv = document.getElementById('dun14-batch-results');
  const tbody = document.getElementById('dun14-batch-tbody');

  const ean13List = batchInput.value.trim().split('\n').filter(line => line.trim());
  const indicator = indicatorInput.value.trim();

  if (ean13List.length === 0) {
    toast('Por favor ingresa al menos un código EAN-13', 'warn');
    return;
  }

  if (!indicator) {
    toast('Por favor ingresa el indicador', 'warn');
    return;
  }

  // Limpiar resultados previos
  batchResults = [];
  tbody.innerHTML = '';

  let successCount = 0;
  let errorCount = 0;

  ean13List.forEach((ean13, index) => {
    const cleanEan = ean13.trim();
    if (!cleanEan) return;

    const result = generarDun14(cleanEan, indicator);

    if (result.error) {
      errorCount++;
      const row = `
        <tr class="bg-red-50">
          <td class="py-2 px-2 text-red-700">${cleanEan}</td>
          <td class="py-2 px-2 text-red-700">-</td>
          <td class="py-2 px-2 text-red-700 text-xs">Error: ${result.error}</td>
        </tr>
      `;
      tbody.insertAdjacentHTML('beforeend', row);
    } else {
      successCount++;
      batchResults.push(result);
      const row = `
        <tr class="${index % 2 === 0 ? 'bg-white' : 'bg-coffee-50'}">
          <td class="py-2 px-2">${result.ean13}</td>
          <td class="py-2 px-2">${result.indicador}</td>
          <td class="py-2 px-2 font-bold text-coffee-900">${result.dun14}</td>
        </tr>
      `;
      tbody.insertAdjacentHTML('beforeend', row);
    }
  });

  resultsDiv.classList.remove('hidden');

  if (errorCount > 0) {
    toast(`${successCount} códigos calculados, ${errorCount} con errores`, 'warn');
  } else {
    toast(`${successCount} códigos DUN-14 calculados correctamente`, 'success');
  }
}

/**
 * Copia todos los DUN-14 calculados al portapapeles
 */
function copyAllDun14() {
  if (batchResults.length === 0) {
    toast('No hay resultados para copiar', 'warn');
    return;
  }

  const text = batchResults.map(r => r.dun14).join('\n');
  navigator.clipboard.writeText(text).then(() => {
    toast(`${batchResults.length} códigos DUN-14 copiados al portapapeles`, 'success');
  }).catch(() => {
    toast('Error al copiar', 'error');
  });
}

/**
 * Descarga los resultados DUN-14 como archivo CSV
 */
function downloadDun14Csv() {
  if (batchResults.length === 0) {
    toast('No hay resultados para descargar', 'warn');
    return;
  }

  // Crear CSV
  let csv = 'EAN-13,Indicador,DUN-14\n';
  batchResults.forEach(r => {
    csv += `${r.ean13},${r.indicador},${r.dun14}\n`;
  });

  // Descargar
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `dun14_${new Date().toISOString().split('T')[0]}.csv`;
  a.click();
  URL.revokeObjectURL(url);

  toast('CSV descargado correctamente', 'success');
}

window.setToolModule = setToolModule;
window.setDunTab = setDunTab;
window.loadSkus = loadSkus;
window.saveSkus = saveSkus;
window.reloadSkus = reloadSkus;
window.loadSkusAudit = loadSkusAudit;
window.updateToolsAdminVisibility = updateToolsAdminVisibility;

document.addEventListener('DOMContentLoaded', () => {
  applyToolModuleStyles();
  updateToolsAdminVisibility();

  const categorySelect = document.getElementById('pack-category-select');
  const packSelect = document.getElementById('pack-select');
  const searchInput = document.getElementById('pack-item-search');

  if (categorySelect) {
    categorySelect.addEventListener('change', onPackCategoryChange);
  }

  if (packSelect) {
    packSelect.addEventListener('change', onPackSelectionChange);
  }

  if (searchInput) {
    searchInput.addEventListener('input', (event) => {
      onPackItemSearchInput(event.target.value || '');
    });
  }
});

window.addEventListener('beforeunload', (event) => {
  if (!shouldWarnOnBeforeUnload()) {
    return;
  }

  event.preventDefault();
  event.returnValue = '';
});
