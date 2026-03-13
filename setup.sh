#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# Upper Coffee Logistics — setup.sh
#
# Uso:
#   chmod +x setup.sh
#   ./setup.sh          → Configura entorno LOCAL para testeo
#   ./setup.sh --azure  → Muestra comandos de despliegue en Azure
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; }

# ── Modo Azure ────────────────────────────────────────────────────
if [[ "${1:-}" == "--azure" ]]; then
  echo ""
  echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
  echo -e "${CYAN}  Comandos para despliegue en Azure App Service${NC}"
  echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
  echo ""
  cat <<'AZURE'
# 1. Variables de entorno Azure (edita según tu suscripción)
RESOURCE_GROUP="rg-upperapp"
LOCATION="eastus"
ACR_NAME="uppercoffeeacr"          # debe ser único globalmente
APP_PLAN="plan-upperapp"
APP_NAME="upperapp-logistics"       # debe ser único globalmente
IMAGE_TAG="latest"

# 2. Resource Group
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION"

# 3. Azure Container Registry
az acr create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACR_NAME" \
    --sku Basic \
    --admin-enabled true

# 4. Build y push de la imagen
az acr build \
    --registry "$ACR_NAME" \
    --image "${APP_NAME}:${IMAGE_TAG}" .

# 5. App Service Plan (Linux, B1 mínimo)
az appservice plan create \
    --name "$APP_PLAN" \
    --resource-group "$RESOURCE_GROUP" \
    --is-linux \
    --sku B1

# 6. Web App
ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query passwords[0].value -o tsv)

az webapp create \
    --resource-group "$RESOURCE_GROUP" \
    --plan "$APP_PLAN" \
    --name "$APP_NAME" \
    --deployment-container-image-name "${ACR_LOGIN_SERVER}/${APP_NAME}:${IMAGE_TAG}"

az webapp config container set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --docker-registry-server-url "https://${ACR_LOGIN_SERVER}" \
    --docker-registry-server-user "$ACR_NAME" \
    --docker-registry-server-password "$ACR_PASSWORD"

# 7. Variables de entorno en Azure (reemplaza con valores reales)
az webapp config appsettings set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --settings \
        WOO_URL="https://tu-tienda.ejemplo.cl" \
        WOO_KEY="ck_XXXX" \
        WOO_SECRET="cs_XXXX" \
        ZEBRA_IP="192.168.1.100" \
        ZEBRA_PORT="9100" \
        ZEBRA_DPI="300" \
        DB_PATH="/data/pedidos.db" \
        WEBSITES_PORT="8000"

# 8. Montar storage persistente para SQLite (opcional)
# az webapp config storage-account add ...

# 9. Habilitar logs de contenedor
az webapp log config \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --docker-container-logging filesystem

# 10. Ver URL
az webapp show \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query defaultHostName -o tsv
AZURE
  echo ""
  exit 0
fi

# ── Modo LOCAL ────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Upper Coffee — Configuración de entorno local${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
echo ""

# 1. Verificar Python
if ! command -v python3 &>/dev/null; then
    error "Python 3 no encontrado. Instala Python 3.10+"
    exit 1
fi
PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python $PYTHON_VER detectado"

# 2. Crear entorno virtual si no existe
if [ ! -d ".venv" ]; then
    info "Creando entorno virtual..."
    python3 -m venv .venv
    success "Entorno virtual creado en .venv/"
else
    info "Entorno virtual existente detectado"
fi

# Activar
# shellcheck disable=SC1091
source .venv/bin/activate

# 3. Instalar dependencias
info "Instalando dependencias Python..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
success "Dependencias instaladas"

# 4. Crear .env si no existe
if [ ! -f ".env" ]; then
    cp .env.example .env
    warn ".env creado desde .env.example — edítalo con tus credenciales reales"
else
    info ".env ya existe — no se sobreescribió"
fi

# 5. Crear directorio data/ para la BD
mkdir -p data
success "Directorio data/ listo"

# 6. Crear .gitignore si no existe
if [ ! -f ".gitignore" ]; then
cat > .gitignore <<'GITIGNORE'
.env
.venv/
__pycache__/
*.pyc
*.pyo
*.db
data/
*.log
.DS_Store
GITIGNORE
    success ".gitignore creado"
fi

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ¡Listo! Comandos disponibles:${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}Desarrollo local (con recarga):${NC}"
echo -e "    source .venv/bin/activate"
echo -e "    DEBUG=true uvicorn main:app --reload --port 8000"
echo ""
echo -e "  ${CYAN}Con Docker (red host para impresora):${NC}"
echo -e "    docker compose up --build"
echo ""
echo -e "  ${CYAN}Despliegue Azure (ver comandos):${NC}"
echo -e "    ./setup.sh --azure"
echo ""
echo -e "  ${CYAN}Abrir en navegador:${NC}"
echo -e "    http://localhost:8000"
echo ""
