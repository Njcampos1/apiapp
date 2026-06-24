# Guía — Trabajar con ramas y datos sin perder información

> Esta guía es de referencia operativa. Puedes borrarla de la rama cuando ya la
> tengas descargada; no es parte de la aplicación.

## Regla de oro

**El código viaja con Git y las ramas. Los datos del cliente viven en un volumen
fuera del repositorio (`~/appdata`) y NUNCA cambian al cambiar de rama o
reconstruir la imagen.**

Una vez entendido esto, `git checkout`, `git merge` y `docker compose up --build`
son 100% seguros.

---

## Dónde vive cada cosa

| Cosa | Ubicación | ¿Se toca al cambiar de rama / rebuild? |
|---|---|---|
| Código (`main.py`, servicios, etc.) | Repo Git / working tree | Sí (es lo que quieres) |
| Catálogo semilla `data/skus.json` | Repo Git (horneado en imagen) | Cambia, pero solo siembra si NO hay catálogo vivo |
| **Catálogo vivo del cliente** (`skus.json`) | `~/appdata/skus.json` | **NO — nunca** |
| **Base de datos** (`pedidos.db`) | `~/appdata/pedidos.db` | **NO — nunca** |
| Auditoría y backups de SKUs | `~/appdata/` | **NO — nunca** |

El acoplamiento se controla con tres piezas:
- **`.dockerignore`** → evita hornear `.env`, bases de datos y backups en la imagen.
- **`SKUS_PATH=/data/skus.json` + `DB_PATH=/data/pedidos.db`** (en `docker-compose.yml`) → rutas absolutas dentro del volumen.
- **`DATA_DIR=/home/azureuser/appdata`** (en `.env`) → el volumen apunta fuera del repo.

---

## Flujo: probar una funcionalidad nueva en otra rama

```bash
cd ~/apiapp
git checkout main && git pull            # parte siempre desde main actualizado
git checkout -b feat/mi-funcionalidad    # una rama por funcionalidad

# ... programas / haces cambios ...

docker compose up -d --build             # los datos en ~/appdata NO se tocan
# ... pruebas 1-2 días con datos reales ...
```

Durante la prueba, todo lo que el cliente edite (packs, pedidos) se guarda en
`~/appdata` y sobrevive a cualquier rebuild o cambio de rama.

## Flujo: cuando la funcionalidad te convence → a `main`

```bash
git checkout main && git pull
git merge feat/mi-funcionalidad
git push
docker compose up -d --build             # despliegas main; datos intactos
```

## Flujo: solo cambiar de rama (sin tocar dependencias ni Dockerfile)

```bash
git checkout otra-rama && git pull
docker compose up -d                     # sin --build → más rápido
```

Usa `--build` solo cuando cambian `requirements.txt`, el `Dockerfile` o el código.

---

## Comprobaciones rápidas

```bash
# ¿La app está sana?
docker compose ps                                  # debe decir "Up ... (healthy)"

# ¿Dónde están los datos vivos?
ls -la ~/appdata                                   # pedidos.db, skus.json, auditoría, backups

# ¿Quién hizo el último cambio de catálogo?
docker exec upperapp-logistics cat /data/skus_audit.jsonl | tail -1
```

---

## Respaldos (importante)

El activo crítico es **`~/appdata`**, no la carpeta del repo. Respáldalo seguido:

```bash
cp -r ~/appdata ~/backups/appdata_$(date +%F_%H%M)
```

Antes de cualquier operación grande (migración, limpieza), haz una copia primero.

---

## Qué NO hacer

- ❌ No borres `~/appdata` al limpiar el repo o cambiar de rama.
- ❌ No pongas rutas relativas en `DB_PATH` / `SKUS_PATH` (deben ser `/data/...`).
- ❌ No edites a mano `skus_audit.jsonl` ni los backups: son el registro real de
  quién cambió qué. Para cambiar una receta, hazlo desde la web (queda auditado).
- ❌ No guardes datos vivos dentro del working tree de Git: `git checkout` los pisa.

## Cambiar la receta "de fábrica" (poco común)

Si alguna vez quieres cambiar el catálogo por defecto para **instalaciones nuevas**
(no para el cliente actual), edita `data/skus.json` en Git a propósito. Eso NO pisa
el catálogo vivo: el seed solo actúa si `~/appdata/skus.json` todavía no existe.
