#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  exec "${ROOT}/.venv/bin/python" -m filament_vision_server.service.cli update-models "$@"
else
  exec python3 -m filament_vision_server.service.cli update-models "$@"
fi
