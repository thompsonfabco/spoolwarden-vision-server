#!/usr/bin/env bash
set -euo pipefail

# Clean-host root + default prefix + default user + systemd end-to-end test.
# Usage (as root on a clean Linux host/container/WSL distro with systemd):
#   bash tests/test_linux_root_systemd.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[test_linux_root_systemd] ERROR: must be run as root" >&2
  exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
  echo "[test_linux_root_systemd] ERROR: systemd is not active (/run/systemd/system missing)" >&2
  exit 1
fi

DEFAULT_PREFIX="/opt/spoolwarden-vision-server"
DEFAULT_STATE="/var/lib/spoolwarden-vision-server"
DEFAULT_USER="spoolwarden-vision-server"
UNIT_PATH="/etc/systemd/system/spoolwarden-vision-server.service"

echo "[1/5] Installing with default prefix, default user, and systemd..."
bash "${REPO_DIR}/install/install.sh" --tier cpu --non-interactive

echo "[2/5] Checking service user, group, ownership, and systemd unit..."
id -u "${DEFAULT_USER}" >/dev/null
getent group "${DEFAULT_USER}" >/dev/null
systemctl is-active --quiet spoolwarden-vision-server.service

echo "[3/5] Checking /v1/health and one POST /v1/read..."
"${DEFAULT_PREFIX}/.venv/bin/python" - <<'PY'
import base64
import io
import json
import urllib.request
from PIL import Image, ImageDraw

with urllib.request.urlopen("http://127.0.0.1:7920/v1/health", timeout=5) as r:
    health = json.loads(r.read().decode("utf-8"))
assert health["status"] == "ok", health
assert health["api_version"] == "1.1", health
assert health["capabilities"]["feedback"] is True, health

img = Image.new("RGB", (240, 160), (240, 240, 240))
ImageDraw.Draw(img).text((20, 40), "SUNLU PLA Black", fill=(20, 20, 20))
buf = io.BytesIO()
img.save(buf, format="JPEG")
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

req = urllib.request.Request(
    "http://127.0.0.1:7920/v1/read",
    data=json.dumps({"api_version": "1.1", "tier": "cpu", "images_b64": [b64]}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as r:
    read_res = json.loads(r.read().decode("utf-8"))
assert read_res.get("items"), read_res
print("Health + Read OK:", health["module_version"], read_res["items"][0].get("vendor"))
PY

echo "[4/5] Uninstalling with --uninstall --purge..."
bash "${REPO_DIR}/install/install.sh" --uninstall --purge --non-interactive

echo "[5/5] Verifying user, group, unit, prefix, and state dir are gone..."
if systemctl is-active --quiet spoolwarden-vision-server.service 2>/dev/null; then
  echo "FAIL: systemd service is still active" >&2
  exit 1
fi
[[ ! -e "${UNIT_PATH}" ]] || { echo "FAIL: unit file still exists: ${UNIT_PATH}" >&2; exit 1; }
[[ ! -e "${DEFAULT_PREFIX}" ]] || { echo "FAIL: prefix still exists: ${DEFAULT_PREFIX}" >&2; exit 1; }
[[ ! -e "${DEFAULT_STATE}" ]] || { echo "FAIL: state dir still exists: ${DEFAULT_STATE}" >&2; exit 1; }
if id -u "${DEFAULT_USER}" >/dev/null 2>&1; then
  echo "FAIL: service user still exists: ${DEFAULT_USER}" >&2
  exit 1
fi
if getent group "${DEFAULT_USER}" >/dev/null 2>&1; then
  echo "FAIL: service group still exists: ${DEFAULT_USER}" >&2
  exit 1
fi

echo "[test_linux_root_systemd] PASS: root systemd install, health, read, and full purge verified."
