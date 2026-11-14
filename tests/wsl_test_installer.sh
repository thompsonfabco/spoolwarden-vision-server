#!/usr/bin/env bash
set -euo pipefail

# End-to-end WSL / Linux test harness for install/install.sh inside an isolated venv prefix.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_PREFIX="/tmp/fvs-wsl-test-$$"

cleanup() {
  if [[ -f "${TEST_PREFIX}/stop_server.sh" ]]; then
    bash "${TEST_PREFIX}/stop_server.sh" >/dev/null 2>&1 || true
  fi
  if [[ -d "${TEST_PREFIX}" ]]; then
    rm -rf "${TEST_PREFIX}" || true
  fi
}
trap cleanup EXIT

echo "[wsl_test] Stage 1: Help / CLI smoke check..."
HELP_OUT="$(bash "${PROJECT_DIR}/install/install.sh" --help)"
if ! grep -q -- "--tier" <<<"${HELP_OUT}" || ! grep -q -- "--non-interactive" <<<"${HELP_OUT}"; then
  echo "[wsl_test] ERROR: install.sh --help missing expected flags" >&2
  exit 1
fi
echo "[wsl_test] Stage 1 PASSED."

# Pick a guaranteed free local TCP port
TEST_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
echo "[wsl_test] Stage 2: Running install.sh into ${TEST_PREFIX} (port=${TEST_PORT}, tier=cpu)..."

bash "${PROJECT_DIR}/install/install.sh" \
  --prefix "${TEST_PREFIX}" \
  --port "${TEST_PORT}" \
  --bind 127.0.0.1 \
  --tier cpu \
  --no-systemd \
  --non-interactive

[[ -f "${TEST_PREFIX}/.installed" ]] || { echo "[wsl_test] Missing .installed marker" >&2; exit 1; }
[[ -x "${TEST_PREFIX}/.venv/bin/python" ]] || { echo "[wsl_test] Missing venv python" >&2; exit 1; }
[[ -x "${TEST_PREFIX}/.venv/bin/filament-vision-server" ]] || { echo "[wsl_test] Missing filament-vision-server CLI" >&2; exit 1; }
[[ -x "${TEST_PREFIX}/start_server.sh" ]] || { echo "[wsl_test] Missing executable start_server.sh" >&2; exit 1; }
[[ -x "${TEST_PREFIX}/stop_server.sh" ]] || { echo "[wsl_test] Missing executable stop_server.sh" >&2; exit 1; }

for req in detector_rfdetr_n.onnx visual_multihead.onnx text_parser.npz text_parser_meta.xml visual_meta.xml; do
  [[ -f "${TEST_PREFIX}/models/cpu/${req}" ]] || { echo "[wsl_test] Missing staged model: ${req}" >&2; exit 1; }
done

CFG_XML="${TEST_PREFIX}/configs/service_config.xml"
[[ -f "${CFG_XML}" ]] || { echo "[wsl_test] Missing service_config.xml" >&2; exit 1; }
grep -q "port=\"${TEST_PORT}\"" "${CFG_XML}" || { echo "[wsl_test] service_config.xml missing port=${TEST_PORT}" >&2; exit 1; }
grep -q 'tier="cpu"' "${CFG_XML}" || { echo "[wsl_test] service_config.xml missing tier=cpu" >&2; exit 1; }
echo "[wsl_test] Stage 2 PASSED."

echo "[wsl_test] Stage 3: Starting server daemon from WSL .venv and testing endpoints..."
bash "${TEST_PREFIX}/start_server.sh"

VENV_PY="${TEST_PREFIX}/.venv/bin/python"
"${VENV_PY}" - "${TEST_PORT}" <<'PYEOF'
import base64
import io
import json
import sys
import time
import urllib.request
from PIL import Image, ImageDraw

port = int(sys.argv[1])
base = f"http://127.0.0.1:{port}"

health = None
for _ in range(30):
    try:
        with urllib.request.urlopen(f"{base}/v1/health", timeout=2.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            if health.get("status") == "ok":
                break
    except Exception:
        time.sleep(1.0)

assert health is not None and health.get("status") == "ok", f"Health check failed: {health}"
assert health.get("module_version") == "0.2.0", f"Unexpected module_version: {health}"
print("[wsl_test] /v1/health OK:", health["module_version"], health["status"])

with urllib.request.urlopen(f"{base}/", timeout=5.0) as resp:
    assert resp.status == 200
    html = resp.read().decode("utf-8")
    assert 'id="dropzone"' in html, "Web UI HTML missing dropzone"
print("[wsl_test] GET / Web UI OK")

img = Image.new("RGB", (640, 480), color=(240, 240, 240))
draw = ImageDraw.Draw(img)
draw.ellipse((80, 40, 560, 440), fill=(35, 35, 38))
draw.ellipse((150, 95, 490, 385), fill=(255, 102, 0))
draw.rectangle((190, 200, 450, 290), fill=(250, 250, 250))
draw.text((205, 220), "HATCHBOX PLA 1.75mm 1kg", fill=(20, 20, 20))
buf = io.BytesIO()
img.save(buf, format="JPEG", quality=92)
jpg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

req = urllib.request.Request(
    f"{base}/v1/read",
    data=json.dumps({
        "api_version": "1.0",
        "tier": "cpu",
        "images_b64": [jpg_b64],
        "hint": "Hatchbox PLA Orange 1kg",
    }).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(req, timeout=30.0) as resp:
    assert resp.status == 200
    read_data = json.loads(resp.read().decode("utf-8"))
assert read_data.get("items"), f"Empty items from /v1/read: {read_data}"
first = read_data["items"][0]
print("[wsl_test] POST /v1/read OK:", first.get("vendor"), first.get("material"), first.get("color_name"))

rev_req = urllib.request.Request(
    f"{base}/v1/revise",
    data=json.dumps({
        "api_version": "1.0",
        "previous": first,
        "note": "Actually Prusament PETG Jet Black",
    }).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(rev_req, timeout=15.0) as resp:
    assert resp.status == 200
    rev_data = json.loads(resp.read().decode("utf-8"))
assert rev_data["item"]["material"] == "PETG", f"Unexpected revise material: {rev_data}"
print("[wsl_test] POST /v1/revise OK:", rev_data["item"].get("vendor"), rev_data["item"].get("material"))
PYEOF

bash "${TEST_PREFIX}/stop_server.sh"
[[ ! -f "${TEST_PREFIX}/server.pid" ]] || { echo "[wsl_test] server.pid was not removed by stop_server.sh" >&2; exit 1; }
echo "[wsl_test] Stage 3 PASSED."

echo "[wsl_test] Stage 4: Testing install.sh --uninstall..."
bash "${PROJECT_DIR}/install/install.sh" --prefix "${TEST_PREFIX}" --uninstall --non-interactive
[[ ! -e "${TEST_PREFIX}" ]] || { echo "[wsl_test] Uninstall left ${TEST_PREFIX} behind" >&2; exit 1; }
echo "[wsl_test] Stage 4 PASSED."
echo "[wsl_test_installer.sh] ALL WSL INSTALLER TESTS PASSED"
