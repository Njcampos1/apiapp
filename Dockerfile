# ─────────────────────────────────────────────────────────────────
# Upper Coffee Logistics — Dockerfile
# Imagen base ligera Python 3.11 slim para producción.
# ─────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Directorio de trabajo
WORKDIR /app

# Dependencias del sistema (necesarias para reportlab/Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python primero (aprovecha caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente
COPY . .

# Crear directorio para la BD persistida (montar como volumen en prod)
RUN mkdir -p /data && chmod 777 /data

# Variables de entorno base (sobreescribibles en docker-compose o Azure)
ENV DB_PATH=/data/pedidos.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# Puerto expuesto
EXPOSE 8000

# Healthcheck que Azure App Service puede utilizar
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

# Arranque en modo producción (sin recarga)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
