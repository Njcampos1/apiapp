#!/usr/bin/env bash
# Lanzador de tests para entorno NixOS.
#
# Problema que resuelve: los wheels de numpy/pandas instalados con pip en un
# venv NO encuentran las librerías C del sistema (libstdc++.so.6, libz.so.1)
# porque NixOS no las expone en las rutas estándar. La suite del preflight
# importa main.py -> excel_service -> pandas, así que necesita esas libs.
#
# Este script las localiza en el store de Nix, arma LD_LIBRARY_PATH y ejecuta
# pytest dentro de .venv. Pasa cualquier argumento extra a pytest.
#
# Uso:
#   ./run_tests.sh                         # corre tests/
#   ./run_tests.sh tests/test_x.py -v      # corre un archivo concreto
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No existe .venv. Créalo con:"
  echo "  python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt pytest"
  exit 1
fi

# Localizar libs C en el store de Nix (primer match; tolerante a fallos)
libstdcpp_dir="$(dirname "$(find /nix/store -maxdepth 3 -name 'libstdc++.so.6' 2>/dev/null | head -1)")" || true
libz_dir="$(dirname "$(find /nix/store -maxdepth 3 -name 'libz.so.1' 2>/dev/null | head -1)")" || true
export LD_LIBRARY_PATH="${libstdcpp_dir:-}:${libz_dir:-}:${LD_LIBRARY_PATH:-}"

exec .venv/bin/python -m pytest "${@:-tests/}"
