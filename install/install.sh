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
#   --with-gpu                Install optional PyTorch/Transformers/PEFT GPU extras
#   --no-systemd              Skip systemd registration; emit start_server.sh / stop_server.sh
#   -y, --non-interactive     Do not block waiting for interactive user input
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
WITH_GPU=0
NO_SYSTEMD=0
NON_INTERACTIVE=0
UNINSTALL=0

usage() {
  sed -n '4,19p' "$0" | sed 's/^# \?//'
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
    --with-gpu)
      WITH_GPU=1; shift ;;
    --no-systemd)
      NO_SYSTEMD=1; shift ;;
    -y|--non-interactive)
      NON_INTERACTIVE=1; shift ;;
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

if [[ "${TIER}" == "gpu" ]]; then
  WITH_GPU=1
fi

is_interactive() {
  [[ "${NON_INTERACTIVE}" -eq 0 && -t 0 ]]
}

prompt_yes_no() {
  local prompt_msg="$1"
  local default_ans="${2:-Y}"
  if ! is_interactive; then
    [[ "${default_ans}" =~ ^[Yy]$ ]]
    return $?
  fi
  local reply=""
  read -r -p "${prompt_msg} " reply || true
  if [[ -z "${reply}" ]]; then
    reply="${default_ans}"
  fi
  [[ "${reply}" =~ ^[Yy]$ ]]
}

prompt_manual_dependency() {
  local dep_name="$1"
  local reason="$2"
  local hint="$3"
  echo "" >&2
  echo "============================================================" >&2
  echo "[install.sh] MISSING DEPENDENCY: ${dep_name}" >&2
  echo "  Reason : ${reason}" >&2
  echo "  Install: ${hint}" >&2
  echo "============================================================" >&2
  if ! is_interactive; then
    return 1
  fi
  local ans=""
  read -r -p "Install '${dep_name}' (for example in another terminal) and press [Enter] to re-check, or type 'q' to quit: " ans || return 1
  if [[ "${ans}" =~ ^[Qq]$ ]]; then
    return 1
  fi
  return 0
}

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

detect_pkg_manager() {
  local pm
  for pm in apt-get dnf yum pacman zypper apk; do
    if command -v "${pm}" >/dev/null 2>&1; then
      echo "${pm}"
      return 0
    fi
  done
  echo ""
  return 1
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 1
  fi
}

has_shared_lib() {
  local soname="$1"
  if command -v ldconfig >/dev/null 2>&1; then
    if ldconfig -p 2>/dev/null | grep -q "${soname}"; then
      return 0
    fi
  fi
  local dir
  for dir in /lib /lib64 /usr/lib /usr/lib64 /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu /usr/local/lib; do
    if compgen -G "${dir}/${soname}*" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

find_python() {
  local cand py_path
  for cand in python3.12 python3.11 python3.13 python3.14 python3.10 python3; do
    if command -v "${cand}" >/dev/null 2>&1; then
      if "${cand}" -c 'import sys, venv, ensurepip; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
        command -v "${cand}"
        return 0
      fi
    fi
  done
  if find_uv >/dev/null 2>&1; then
    local uv_bin
    uv_bin="$(find_uv)"
    for ver in 3.12 3.11 3.13 3.10; do
      py_path="$("${uv_bin}" python find "${ver}" 2>/dev/null || true)"
      if [[ -n "${py_path}" && -x "${py_path}" ]]; then
        if "${py_path}" -c 'import sys, venv, ensurepip; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
          echo "${py_path}"
          return 0
        fi
      fi
    done
  fi
  return 1
}

find_uv() {
  local c
  for c in uv "${HOME}/.local/bin/uv" "${HOME}/.cargo/bin/uv" "/usr/local/bin/uv"; do
    if command -v "${c}" >/dev/null 2>&1; then
      command -v "${c}"
      return 0
    elif [[ -x "${c}" ]]; then
      echo "${c}"
      return 0
    fi
  done
  return 1
}

distro_install_hint() {
  local pm="$1"
  case "${pm}" in
    apt-get) echo "sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates libgl1 libglib2.0-0 libgomp1 libheif1" ;;
    dnf)     echo "sudo dnf install -y python3 python3-pip curl ca-certificates mesa-libGL glib2 libgomp" ;;
    yum)     echo "sudo yum install -y python3 python3-pip curl ca-certificates mesa-libGL glib2 libgomp" ;;
    pacman)  echo "sudo pacman -Sy --noconfirm python python-pip curl ca-certificates mesa glib2 gcc-libs libheif" ;;
    zypper)  echo "sudo zypper --non-interactive install python3 python3-pip curl ca-certificates Mesa-libGL1 libglib-2_0-0 libgomp1" ;;
    apk)     echo "sudo apk add --no-cache python3 py3-pip curl ca-certificates mesa-gl glib libgomp libheif" ;;
    *)       echo "Install Python 3.10-3.14 (with venv & pip), curl, libglib2.0, libgomp1, and libGL (https://www.python.org/downloads/)" ;;
  esac
}

install_pkg_manager_deps() {
  local pm="$1"
  echo "[install.sh] Attempting automatic system package installation via ${pm}..."
  case "${pm}" in
    apt-get)
      run_privileged apt-get update -y || return 1
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-venv python3-pip curl ca-certificates tar unzip libglib2.0-0 libgomp1 || return 1
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y libgl1 libheif1 || true
      ;;
    dnf)
      run_privileged dnf install -y python3 python3-pip curl ca-certificates tar unzip mesa-libGL glib2 libgomp || return 1
      run_privileged dnf install -y libheif || true
      ;;
    yum)
      run_privileged yum install -y python3 python3-pip curl ca-certificates tar unzip mesa-libGL glib2 libgomp || return 1
      ;;
    pacman)
      run_privileged pacman -Sy --noconfirm python python-pip curl ca-certificates tar unzip mesa glib2 gcc-libs || return 1
      run_privileged pacman -S --noconfirm libheif || true
      ;;
    zypper)
      run_privileged zypper --non-interactive install python3 python3-pip curl ca-certificates tar unzip Mesa-libGL1 libglib-2_0-0 libgomp1 || return 1
      ;;
    apk)
      run_privileged apk add --no-cache python3 py3-pip curl ca-certificates tar unzip mesa-gl glib libgomp || return 1
      run_privileged apk add --no-cache libheif || true
      ;;
    *)
      return 1 ;;
  esac
}

ensure_linux_system_deps() {
  local pm missing_sys=()
  pm="$(detect_pkg_manager || true)"

  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    missing_sys+=("curl/wget")
  fi
  if ! find_python >/dev/null 2>&1; then
    missing_sys+=("python3(3.10-3.14)+venv")
  fi
  if ! has_shared_lib "libglib-2.0.so.0"; then
    missing_sys+=("libglib-2.0")
  fi
  if ! has_shared_lib "libgomp.so.1"; then
    missing_sys+=("libgomp1")
  fi
  if ! has_shared_lib "libGL.so.1"; then
    missing_sys+=("libGL")
  fi

  if [[ "${#missing_sys[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "[install.sh] Missing or incomplete system dependencies: ${missing_sys[*]}"
  if [[ -n "${pm}" ]] && { [[ "$(id -u)" -eq 0 ]] || command -v sudo >/dev/null 2>&1; }; then
    local proceed=1
    if [[ "$(id -u)" -ne 0 ]] && is_interactive; then
      if ! prompt_yes_no "[install.sh] Install missing packages via '${pm}' using sudo? [Y/n]" "Y"; then
        proceed=0
      fi
    fi
    if [[ "${proceed}" -eq 1 ]]; then
      if ! install_pkg_manager_deps "${pm}"; then
        echo "[install.sh] WARNING: Automated ${pm} install failed or lacked permission." >&2
      fi
    fi
  else
    echo "[install.sh] Note: No supported package manager with root/sudo privilege detected; checking standalone user-space fallbacks..."
  fi
}

bootstrap_python_with_uv() {
  local uv_bin=""
  uv_bin="$(find_uv || true)"
  if [[ -z "${uv_bin}" ]]; then
    echo "[install.sh] Downloading standalone 'uv' tool to bootstrap Python 3.12 (no root required)..."
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh || return 1
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh || return 1
    else
      return 1
    fi
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
    uv_bin="$(find_uv || true)"
  fi
  if [[ -z "${uv_bin}" ]]; then
    return 1
  fi
  echo "[install.sh] Installing Python 3.12 via uv..."
  "${uv_bin}" python install 3.12 || return 1
  local found=""
  found="$("${uv_bin}" python find 3.12 2>/dev/null || true)"
  if [[ -n "${found}" && -x "${found}" ]]; then
    echo "${found}"
    return 0
  fi
  return 1
}

ensure_python_runtime() {
  local pm py=""
  pm="$(detect_pkg_manager || true)"
  while true; do
    py="$(find_python || true)"
    if [[ -n "${py}" ]]; then
      PYTHON_BIN="${py}"
      return 0
    fi
    py="$(bootstrap_python_with_uv || true)"
    if [[ -n "${py}" ]]; then
      PYTHON_BIN="${py}"
      return 0
    fi
    if ! prompt_manual_dependency \
      "Python 3.10 - 3.14 (with venv & ensurepip)" \
      "Automatic package manager / uv bootstrap could not install a supported Python runtime." \
      "$(distro_install_hint "${pm}")"; then
      echo "[install.sh] ERROR: Python 3.10 - 3.14 is required." >&2
      exit 1
    fi
  done
}

ensure_gpu_deps() {
  if [[ "${WITH_GPU}" -eq 0 && "${TIER}" != "gpu" ]]; then
    return 0
  fi
  while true; do
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
      echo "[install.sh] NVIDIA CUDA GPU detected via nvidia-smi."
      return 0
    fi
    echo "" >&2
    echo "[install.sh] WARNING: GPU tier (--tier ${TIER} / --with-gpu) was requested, but 'nvidia-smi' failed or is missing." >&2
    if ! is_interactive; then
      echo "[install.sh] ERROR: Install NVIDIA drivers + CUDA runtime (https://www.nvidia.com/Download/index.aspx) or run with --tier cpu." >&2
      exit 1
    fi
    echo "Choose an option:" >&2
    echo "  [1] Switch to CPU / auto tier and continue installation" >&2
    echo "  [2] Install NVIDIA drivers now (e.g. 'sudo ubuntu-drivers autoinstall' or https://www.nvidia.com/Download/index.aspx) and press Enter to re-check" >&2
    echo "  [3] Quit" >&2
    local choice=""
    read -r -p "Selection [1]: " choice || choice="3"
    case "${choice:-1}" in
      1)
        TIER="auto"
        WITH_GPU=0
        echo "[install.sh] Continuing with tier=auto (CPU ONNX pipeline)."
        return 0 ;;
      2)
        read -r -p "Press [Enter] after installing NVIDIA drivers to re-check nvidia-smi..." _ || exit 1 ;;
      *)
        exit 1 ;;
    esac
  done
}

models_present_in_dir() {
  local d="$1"
  local req
  for req in detector_rfdetr_n.onnx visual_multihead.onnx text_parser.npz text_parser_meta.xml visual_meta.xml; do
    if [[ ! -f "${d}/cpu/${req}" && ! -f "${d}/${req}" ]]; then
      return 1
    fi
  done
  return 0
}

download_and_extract_assets_url() {
  local url="$1"
  local dest_dir="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  echo "[install.sh] Downloading model assets from ${url}..."
  if command -v curl >/dev/null 2>&1; then
    curl -fL "${url}" -o "${tmp_file}" || { rm -f "${tmp_file}"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${tmp_file}" "${url}" || { rm -f "${tmp_file}"; return 1; }
  else
    rm -f "${tmp_file}"
    return 1
  fi
  mkdir -p "${dest_dir}"
  if [[ "${url}" == *.zip* ]]; then
    "${PYTHON_BIN}" -m zipfile -e "${tmp_file}" "${dest_dir}" || { rm -f "${tmp_file}"; return 1; }
  else
    tar -xf "${tmp_file}" -C "${dest_dir}" || "${PYTHON_BIN}" -m zipfile -e "${tmp_file}" "${dest_dir}" || { rm -f "${tmp_file}"; return 1; }
  fi
  rm -f "${tmp_file}"
  if [[ -d "${dest_dir}/models" ]] && models_present_in_dir "${dest_dir}/models"; then
    cp -af "${dest_dir}/models/." "${dest_dir}/"
  fi
  models_present_in_dir "${dest_dir}"
}

ensure_model_assets() {
  if [[ -z "${ASSETS}" && -d "${PROJECT_DIR}/models" ]]; then
    ASSETS="${PROJECT_DIR}/models"
  fi
  while true; do
    if [[ -n "${ASSETS}" ]] && models_present_in_dir "${ASSETS}"; then
      return 0
    fi
    if [[ -n "${FILAMENT_VISION_SERVER_ASSETS_URL:-}" ]]; then
      local dl_target="${PROJECT_DIR}/models"
      if download_and_extract_assets_url "${FILAMENT_VISION_SERVER_ASSETS_URL}" "${dl_target}"; then
        ASSETS="${dl_target}"
        return 0
      fi
    fi
    if ! is_interactive; then
      echo "[install.sh] ERROR: Required CPU model weights missing (detector_rfdetr_n.onnx, visual_multihead.onnx, text_parser.npz). Pass --assets <dir> or set FILAMENT_VISION_SERVER_ASSETS_URL." >&2
      exit 1
    fi
    echo "" >&2
    echo "[install.sh] Model weights were not found in '${ASSETS:-./models}'." >&2
    local user_src=""
    read -r -p "Enter a local models folder path or archive URL (.zip/.tar.gz), or type 'q' to quit: " user_src || exit 1
    if [[ "${user_src}" =~ ^[Qq]$ || -z "${user_src}" ]]; then
      exit 1
    fi
    if [[ "${user_src}" =~ ^https?:// ]]; then
      local dl_target="${PROJECT_DIR}/models"
      if download_and_extract_assets_url "${user_src}" "${dl_target}"; then
        ASSETS="${dl_target}"
      else
        echo "[install.sh] Download or extraction from '${user_src}' failed." >&2
      fi
    elif [[ -d "${user_src}" ]]; then
      ASSETS="${user_src}"
    else
      echo "[install.sh] Path not found: ${user_src}" >&2
    fi
  done
}

# Step 1: Check and install OS/system dependencies + Python runtime
ensure_linux_system_deps
PYTHON_BIN=""
ensure_python_runtime
echo "[install.sh] Using Python: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version))"

# Step 2: Check GPU driver if GPU tier requested
ensure_gpu_deps

# Step 3: Ensure trained model assets are present (or downloaded/prompted)
ensure_model_assets

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

if [[ "${WITH_GPU}" -eq 1 ]]; then
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

verify_python_deps() {
  local check_gpu="${WITH_GPU}"
  while true; do
    local broken=""
    broken="$("${VENV_PY}" - "${check_gpu}" <<'PYEOF'
import importlib, sys
want_gpu = sys.argv[1] == "1"
checks = [
    ("defusedxml", "defusedxml"),
    ("PIL", "Pillow"),
    ("pillow_heif", "pillow-heif"),
    ("numpy", "numpy"),
    ("cv2", "opencv-python-headless"),
    ("onnxruntime", "onnxruntime"),
    ("rapidfuzz", "rapidfuzz"),
    ("rapidocr", "rapidocr"),
    ("sklearn", "scikit-learn"),
]
if want_gpu:
    checks.extend([
        ("torch", "torch"),
        ("transformers", "transformers"),
        ("peft", "peft"),
        ("accelerate", "accelerate"),
        ("qwen_vl_utils", "qwen-vl-utils"),
    ])
missing = []
for mod, pkg in checks:
    try:
        importlib.import_module(mod)
    except Exception:
        missing.append(pkg)
print(" ".join(dict.fromkeys(missing)))
PYEOF
)"
    if [[ -z "${broken}" ]]; then
      echo "[install.sh] Verified all Python dependencies import cleanly."
      return 0
    fi
    echo "[install.sh] Attempting auto-repair for failed Python packages: ${broken}"
    # shellcheck disable=SC2086
    "${VENV_PIP}" install --no-cache-dir --force-reinstall ${broken} || true

    local still_broken=""
    still_broken="$("${VENV_PY}" - "${check_gpu}" <<'PYEOF'
import importlib, sys
want_gpu = sys.argv[1] == "1"
checks = [
    ("defusedxml", "defusedxml"),
    ("PIL", "Pillow"),
    ("pillow_heif", "pillow-heif"),
    ("numpy", "numpy"),
    ("cv2", "opencv-python-headless"),
    ("onnxruntime", "onnxruntime"),
    ("rapidfuzz", "rapidfuzz"),
    ("rapidocr", "rapidocr"),
    ("sklearn", "scikit-learn"),
]
if want_gpu:
    checks.extend([
        ("torch", "torch"),
        ("transformers", "transformers"),
        ("peft", "peft"),
        ("accelerate", "accelerate"),
        ("qwen_vl_utils", "qwen-vl-utils"),
    ])
missing = []
for mod, pkg in checks:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"{pkg} ({exc})")
print("\n".join(missing))
PYEOF
)"
    if [[ -z "${still_broken}" ]]; then
      echo "[install.sh] Auto-repair succeeded; all Python dependencies verified."
      return 0
    fi
    if ! prompt_manual_dependency \
      "Python runtime dependencies" \
      "Failed to import: ${still_broken}" \
      "Check internet access / missing shared libraries, or install manually inside ${PREFIX}/.venv with: ${VENV_PIP} install --no-cache-dir ${broken}"; then
      echo "[install.sh] ERROR: Python dependencies incomplete." >&2
      exit 1
    fi
  done
}

verify_python_deps

FILAMENT_VISION_SERVER_HOME="${PREFIX}" "${VENV_PY}" -m filament_vision_server.service.cli config \
  --tier "${TIER}" --host "${BIND}" --port "${PORT}" >/dev/null

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
