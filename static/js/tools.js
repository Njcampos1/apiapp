/**
 * tools.js
 * Lógica de la pestaña Herramientas
 * - Calculador DUN-14 (individual y por lotes)
 * - Validación de códigos EAN-13
 * - Exportación de resultados (CSV, clipboard)
 */

// Estado del calculador DUN-14
let activeDunTab = 'single'; // 'single' | 'batch' | 'pack-editor'
let batchResults = []; // Resultados del cálculo por lotes
let skusLoaded = false;
let skusSnapshot = '';

function hasUnsavedSkusChanges() {
  const textarea = document.getElementById('pack-editor-textarea');
  if (!textarea) return false;
  return textarea.value !== skusSnapshot;
}

function shouldWarnOnBeforeUnload() {
  return activeDunTab === 'pack-editor' && hasUnsavedSkusChanges();
}

function updatePackEditorStatus(message, type = 'info') {
  const statusEl = document.getElementById('pack-editor-status');
  if (!statusEl) return;

  const colorByType = {
    info: 'text-coffee-500',
    success: 'text-green-600',
    error: 'text-red-600',
  };

  statusEl.className = `text-sm ${colorByType[type] || colorByType.info}`;
  statusEl.textContent = message;
}

function updateToolsAdminVisibility() {
  const packEditorTab = document.getElementById('pack-tab-editor');
  if (!packEditorTab) return;

  const isAdmin = localStorage.getItem('is_admin') === 'true';
  packEditorTab.classList.toggle('hidden', !isAdmin);

  if (!isAdmin && activeDunTab === 'pack-editor') {
    setDunTab('single');
  }
}

/**
 * Cambia la pestaña activa del calculador DUN-14
 * @param {string} tab - 'single' o 'batch'
 */
function setDunTab(tab) {
  if (tab === 'pack-editor' && localStorage.getItem('is_admin') !== 'true') {
    toast('Solo administradores pueden editar packs', 'warn');
    return;
  }

  if (activeDunTab === 'pack-editor' && tab !== 'pack-editor' && hasUnsavedSkusChanges()) {
    const shouldLeave = window.confirm('Tienes cambios sin guardar en Gestión de Packs. ¿Deseas salir sin guardar?');
    if (!shouldLeave) {
      return;
    }
  }

  activeDunTab = tab;

  // Actualizar estilos de tabs
  const singleTab = document.getElementById('dun-tab-single');
  const batchTab = document.getElementById('dun-tab-batch');
  const packEditorTab = document.getElementById('pack-tab-editor');

  const activeClass = 'px-4 py-2 font-semibold text-sm transition-all border-b-2 border-coffee-400 text-coffee-900';
  const inactiveClass = 'px-4 py-2 font-semibold text-sm transition-all border-b-2 border-transparent text-coffee-400 hover:text-coffee-600';

  singleTab.className = tab === 'single' ? activeClass : inactiveClass;
  batchTab.className = tab === 'batch' ? activeClass : inactiveClass;
  if (packEditorTab && !packEditorTab.classList.contains('hidden')) {
    packEditorTab.className = tab === 'pack-editor' ? activeClass : inactiveClass;
  }

  // Mostrar/ocultar vistas
  document.getElementById('dun-single-view').classList.toggle('hidden', tab !== 'single');
  document.getElementById('dun-batch-view').classList.toggle('hidden', tab !== 'batch');
  document.getElementById('tools-pack-editor-view').classList.toggle('hidden', tab !== 'pack-editor');

  if (tab === 'pack-editor') {
    loadSkus();
  }
}

async function loadSkus(forceReload = false) {
  const textarea = document.getElementById('pack-editor-textarea');
  if (!textarea) return;

  if (skusLoaded && !forceReload) return;

  updatePackEditorStatus('Cargando SKUs...', 'info');

  try {
    const response = await fetch('/api/skus');
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || response.statusText || 'No se pudo cargar SKUs');
    }

    textarea.value = JSON.stringify(payload, null, 2);
    skusSnapshot = textarea.value;
    skusLoaded = true;
    updatePackEditorStatus('SKUs cargados correctamente', 'success');
  } catch (error) {
    updatePackEditorStatus('Error al cargar SKUs', 'error');
    toast(`Error al cargar SKUs: ${error.message}`, 'error');
  }
}

async function saveSkus() {
  const textarea = document.getElementById('pack-editor-textarea');
  const saveBtn = document.getElementById('pack-editor-save-btn');
  if (!textarea || !saveBtn) return;

  let parsedJson;
  try {
    parsedJson = JSON.parse(textarea.value);
  } catch (error) {
    updatePackEditorStatus('JSON inválido. Revisa el formato.', 'error');
    toast(`JSON inválido: ${error.message}`, 'error');
    return;
  }

  saveBtn.disabled = true;
  saveBtn.textContent = 'Guardando...';
  updatePackEditorStatus('Guardando cambios...', 'info');

  try {
    const response = await fetch('/api/skus', {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(parsedJson),
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || response.statusText || 'No se pudo guardar SKUs');
    }

    textarea.value = JSON.stringify(parsedJson, null, 2);
    skusSnapshot = textarea.value;
    skusLoaded = true;
    updatePackEditorStatus('Cambios guardados correctamente', 'success');
    toast(payload.message || 'SKUs actualizados correctamente', 'success');
  } catch (error) {
    updatePackEditorStatus('Error al guardar cambios', 'error');
    toast(`Error al guardar SKUs: ${error.message}`, 'error');
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Guardar Cambios';
  }
}

async function reloadSkus() {
  const reloadBtn = document.getElementById('pack-editor-reload-btn');

  if (hasUnsavedSkusChanges()) {
    const shouldReload = window.confirm('Hay cambios sin guardar. ¿Deseas recargar y descartar los cambios actuales?');
    if (!shouldReload) {
      return;
    }
  }

  if (reloadBtn) {
    reloadBtn.disabled = true;
    reloadBtn.textContent = 'Recargando...';
  }

  try {
    await loadSkus(true);
    toast('JSON recargado desde el servidor', 'success');
  } finally {
    if (reloadBtn) {
      reloadBtn.disabled = false;
      reloadBtn.textContent = 'Recargar JSON';
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

window.setDunTab = setDunTab;
window.loadSkus = loadSkus;
window.saveSkus = saveSkus;
window.reloadSkus = reloadSkus;
window.updateToolsAdminVisibility = updateToolsAdminVisibility;

document.addEventListener('DOMContentLoaded', () => {
  updateToolsAdminVisibility();

  const textarea = document.getElementById('pack-editor-textarea');
  if (textarea) {
    textarea.addEventListener('input', () => {
      if (!skusLoaded) return;
      if (hasUnsavedSkusChanges()) {
        updatePackEditorStatus('Tienes cambios sin guardar', 'info');
      } else {
        updatePackEditorStatus('Sin cambios pendientes', 'success');
      }
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
