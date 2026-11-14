#!/usr/bin/env bash
set -euo pipefail

# Standalone Linux installer for filament-vision-server (v1.0.0)
# Usage:
#   sudo bash install/install.sh [options]
# Options:
#   --tier <auto|cpu|gpu>     Inference tier (default: auto)
#   --port <port>             HTTP listen port (default: 7920)
#   --bind <ip>               Bind address (default: 127.0.0.1)
#   --user <username>         Service user for systemd unit (default: current sudo user or root)
#   --prefix <dir>            Install prefix (default: /opt/filament-vision-server)
#   --assets <dir>            Models directory (default: ./models)
#   --no-systemd              Skip systemd registration; emit start_server.sh / stop_server.sh
#   --uninstall               Stop service and remove installation under --prefix
#   -h, --help                Show help

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

TIER="auto"
PORT="7920"
BIND="127.0.0.1"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
PREFIX="/opt/filament-vision-server"
ASSETS=""
NO_SYSTEMD=0
UNINSTALL=0

usage() {
  sed -n '4,17p' "$0" | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier)
      TIER="$2"; shift 2 ;;
    --port)
      PORT="$2"; shift 2 ;;
    --bind)
      BIND="$2"; shift 2 ;;
    --user)
      SERVICE_USER="$2"; shift 2 ;;
    --prefix)
      PREFIX="$2"; shift 2 ;;
    --assets)
      ASSETS="$2"; shift 2 ;;
    --no-systemd)
      NO_SYSTEMD=1; shift ;;
    --uninstall)
      UNINSTALL=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "[install.sh] Unknown option: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

case "${TIER}" in
  auto|cpu|gpu) ;;
  *)
    echo "[install.sh] Invalid --tier '${TIER}' (expected auto, cpu, or gpu)" >&2
    exit 2 ;;
esac

SERVICE_NAME="filament-vision-server"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${UNINSTALL}" -eq 1 ]]; then
  echo "[install.sh] Uninstalling filament-vision-server from ${PREFIX}..."
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\.service"; then
      systemctl stop "${SERVICE_NAME}.service" || true
      systemctl disable "${SERVICE_NAME}.service" || true
    fi
    if [[ -f "${SYSTEMD_UNIT_PATH}" ]]; then
      rm -f "${SYSTEMD_UNIT_PATH}"
      systemctl daemon-reload || true
    fi
  fi
  if [[ -f "${PREFIX}/stop_server.sh" ]]; then
    bash "${PREFIX}/stop_server.sh" || true
  fi
  for bin_name in filament-vision-server spool-server; do
    link_path="/usr/local/bin/${bin_name}"
    if [[ -L "${link_path}" ]]; then
      target="$(readlink -f "${link_path}" || true)"
      if [[ "${target}" == "${PREFIX}"* ]]; then
        rm -f "${link_path}"
      fi
    fi
  done
  if [[ -d "${PREFIX}" ]]; then
    rm -rf "${PREFIX}"
  fi
  echo "[install.sh] Uninstalled cleanly."
  exit 0
fi

find_python() {
  local cand
  for cand in python3 python3.12 python3.11 python3.13 python3.14 python3.10; do
    if command -v "${cand}" >/dev/null 2>&1; then
      if "${cand}" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
        command -v "${cand}"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[install.sh] ERROR: Python 3.10 - 3.14 is required." >&2
  exit 1
fi
echo "[install.sh] Using Python: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version))"

if [[ -z "${ASSETS}" && -d "${PROJECT_DIR}/models" ]]; then
  ASSETS="${PROJECT_DIR}/models"
fi

MARKER="${PREFIX}/.installed"
HAD_COMPLETE_INSTALL=0
if [[ -f "${MARKER}" ]]; then
  HAD_COMPLETE_INSTALL=1
fi
CREATED_PREFIX=0

cleanup_on_exit() {
  local rc=$?
  if [[ "${rc}" -ne 0 && "${HAD_COMPLETE_INSTALL}" -eq 0 && "${CREATED_PREFIX}" -eq 1 ]]; then
    echo "[install.sh] Install failed (exit ${rc}); cleaning incomplete ${PREFIX}..." >&2
    rm -rf "${PREFIX}" || true
  fi
}
trap cleanup_on_exit EXIT

if [[ ! -d "${PREFIX}" ]]; then
  mkdir -p "${PREFIX}"
  CREATED_PREFIX=1
fi

mkdir -p "${PREFIX}/configs" "${PREFIX}/data/catalog" "${PREFIX}/data/cache" "${PREFIX}/models/cpu" "${PREFIX}/models/gpu"

if [[ -d "${PROJECT_DIR}/configs" ]]; then
  cp -af "${PROJECT_DIR}/configs/." "${PREFIX}/configs/"
fi
if [[ -d "${PROJECT_DIR}/data/catalog" ]]; then
  cp -af "${PROJECT_DIR}/data/catalog/." "${PREFIX}/data/catalog/"
fi

copy_model_assets() {
  local src="$1"
  if [[ ! -d "${src}" ]]; then
    return 0
  fi
  if [[ -d "${src}/cpu" ]]; then
    shopt -s nullglob
    for f in "${src}/cpu/"*; do
      case "${f}" in
        *.onnx|*.npz|*.xml|*.json)
          cp -af "${f}" "${PREFIX}/models/cpu/" ;;
      esac
    done
    shopt -u nullglob
  fi
  if [[ -d "${src}/gpu" ]]; then
    cp -af "${src}/gpu/." "${PREFIX}/models/gpu/"
  fi
  shopt -s nullglob
  for f in "${src}/"*; do
    if [[ -f "${f}" ]]; then
      case "${f}" in
        *.onnx|*.npz|*.xml)
          cp -af "${f}" "${PREFIX}/models/cpu/" ;;
      esac
    fi
  done
  shopt -u nullglob
}

if [[ -n "${ASSETS}" ]]; then
  echo "[install.sh] Copying model assets from ${ASSETS}..."
  copy_model_assets "${ASSETS}"
fi

echo "[install.sh] Creating virtual environment at ${PREFIX}/.venv..."
"${PYTHON_BIN}" -m venv "${PREFIX}/.venv"
VENV_PIP="${PREFIX}/.venv/bin/pip"
VENV_PY="${PREFIX}/.venv/bin/python"

"${VENV_PIP}" install --upgrade pip wheel

INSTALL_TARGET="${PROJECT_DIR}"
HAS_CUDA=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  HAS_CUDA=1
fi

if [[ "${TIER}" == "gpu" ]]; then
  echo "[install.sh] Installing filament-vision-server[gpu]..."
  "${VENV_PIP}" install --no-cache-dir "${INSTALL_TARGET}[gpu]"
else
  echo "[install.sh] Installing filament-vision-server..."
  "${VENV_PIP}" install --no-cache-dir "${INSTALL_TARGET}"
  if [[ "${HAS_CUDA}" -eq 1 && "${TIER}" == "auto" ]]; then
    echo "[install.sh] CUDA GPU detected; attempting onnxruntime-gpu upgrade (optional)..."
    "${VENV_PIP}" install --no-cache-dir --upgrade onnxruntime-gpu || true
  fi
fi

FILAMENT_VISION_SERVER_HOME="${PREFIX}" "${VENV_PY}" -m filament_vision_server.service.cli config \
  --tier "${TIER}" --host "${BIND}" --port "${PORT}" >/dev/null

# Helper start/stop scripts under PREFIX (for --no-systemd or manual launches)
cat > "${PREFIX}/start_server.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export FILAMENT_VISION_SERVER_HOME="${PREFIX}"
PID_FILE="${PREFIX}/server.pid"
if [[ -f "\${PID_FILE}" ]] && kill -0 "\$(cat "\${PID_FILE}")" 2>/dev/null; then
  echo "[filament-vision-server] Already running (PID \$(cat "\${PID_FILE}"))."
  exit 0
fi
nohup "${PREFIX}/.venv/bin/filament-vision-server" serve --host "${BIND}" --port "${PORT}" --tier "${TIER}" \
  >"${PREFIX}/data/server.log" 2>&1 &
echo \$! > "\${PID_FILE}"
echo "[filament-vision-server] Started (PID \$!) on http://${BIND}:${PORT}/"
EOF
chmod +x "${PREFIX}/start_server.sh"

cat > "${PREFIX}/stop_server.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
PID_FILE="${PREFIX}/server.pid"
if [[ -f "\${PID_FILE}" ]]; then
  PID="\$(cat "\${PID_FILE}")"
  if [[ -n "\${PID}" ]] && kill -0 "\${PID}" 2>/dev/null; then
    kill "\${PID}" || true
  fi
  rm -f "\${PID_FILE}"
  echo "[filament-vision-server] Stopped."
else
  echo "[filament-vision-server] No server.pid found under ${PREFIX}."
fi
EOF
chmod +x "${PREFIX}/stop_server.sh"

if [[ "$(id -u)" -eq 0 ]]; then
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}" || true
  fi
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PREFIX}" || true
  if [[ -d /usr/local/bin ]]; then
    ln -sf "${PREFIX}/.venv/bin/filament-vision-server" /usr/local/bin/filament-vision-server
    ln -sf "${PREFIX}/.venv/bin/spool-server" /usr/local/bin/spool-server
  fi
fi

HEALTH_HOST="${BIND}"
if [[ "${HEALTH_HOST}" == "0.0.0.0" || "${HEALTH_HOST}" == "::" ]]; then
  HEALTH_HOST="127.0.0.1"
fi
HEALTH_URL="http://${HEALTH_HOST}:${PORT}/v1/health"

if [[ "${NO_SYSTEMD}" -eq 0 && "$(id -u)" -eq 0 ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  echo "[install.sh] Installing systemd service ${SYSTEMD_UNIT_PATH}..."
  mkdir -p /etc/filament-vision-server
  sed \
    -e "s|__PREFIX__|${PREFIX}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    -e "s|__BIND__|${BIND}|g" \
    -e "s|__PORT__|${PORT}|g" \
    -e "s|__TIER__|${TIER}|g" \
    "${SCRIPT_DIR}/systemd/filament-vision-server.service" > "${SYSTEMD_UNIT_PATH}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  systemctl restart "${SERVICE_NAME}.service"

  echo "[install.sh] Waiting for ${HEALTH_URL}..."
  READY=0
  for _ in $(seq 1 30); do
    if "${VENV_PY}" -c "import urllib.request; urllib.request.urlopen('${HEALTH_URL}', timeout=2).read()" >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 1
  done
  if [[ "${READY}" -ne 1 ]]; then
    echo "[install.sh] ERROR: Service did not become healthy at ${HEALTH_URL} within 30s." >&2
    journalctl -u "${SERVICE_NAME}.service" -n 40 --no-pager >&2 || true
    exit 1
  fi
  echo "[install.sh] Service is healthy at ${HEALTH_URL}"
else
  echo "[install.sh] Systemd not active/requested; start manually with: ${PREFIX}/start_server.sh"
fi

touch "${MARKER}"
HAD_COMPLETE_INSTALL=1
echo "[install.sh] Installation complete! Web UI: http://${HEALTH_HOST}:${PORT}/"
