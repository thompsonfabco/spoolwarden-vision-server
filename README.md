# Filament Vision Server (`v0.2.0`)

Standalone cross-platform 3D printer filament spool recognition server for **Linux** and **Windows**, featuring:

- **Built-in Web UI** at `http://127.0.0.1:7920/` (`/ui`) — drag & drop or snap mobile photos (`.png`, `.jpg`, `.webp`, `.heic`), inspect/edit spool attributes with a live color swatch picker, refine predictions with natural-language instructions (`/v1/revise`), export results as **JSON**, **CSV**, **XML**, or **Spoolman JSON**, and optionally queue/submit training corrections.
- **Dual-Tier Inference**:
  - **CPU Tier (default / always ready):** RF-DETR-N label & spool detector + ConvNeXt/Visual multi-head ONNX classifier + RapidOCR text parser + verified filament catalog matcher (`models/cpu/`).
  - **GPU Tier (optional, CUDA):** Qwen2.5-VL-7B LoRA adapter (`models/gpu/qwen25_vl_7b_filament_lora/`) with automatic idle VRAM unload and `auto` confidence escalation.
- **CORS-enabled REST API v1** on port `7920` for local scripts, print-farm automation, or external inventory tools.

---

## Quick Start

### Windows (One-Click)

1. Double-click **`install.bat`** (or run PowerShell):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Start -OpenBrowser
   ```
2. Open `http://127.0.0.1:7920/` in your browser.
3. Day-to-day control:
   - Double-click **`start_server.bat`** to start the background server and open the Web UI.
   - Double-click **`stop_server.bat`** to stop the server.

Installer options (`install\install.ps1`):
- `-Tier auto|cpu|gpu` (default: `auto`)
- `-Port 7920` (default: `7920`)
- `-Bind 127.0.0.1` (use `0.0.0.0` to expose on your LAN)
- `-Prefix <dir>` (install to a target directory instead of in-place)
- `-WithGpu` (install optional PyTorch/Transformers/PEFT GPU extras with CUDA wheels)
- `-RegisterStartup` (create Scheduled Task `FilamentVisionServer` at logon)
- `-Start` / `-OpenBrowser`
- `-NonInteractive` (disable interactive dependency prompts for unattended runs)
- `-Uninstall` (stop server, remove Scheduled Task, clean venv)

Automatic Windows Dependency Handling (`install\install.ps1`):
- Checks for **Microsoft Visual C++ 2015–2022 Redistributable (x64)** and automatically installs via `winget` or downloads `vc_redist.x64.exe` if missing.
- Checks for **Python 3.10–3.14** (with `venv` & `ensurepip`), and automatically installs Python 3.12 via `winget`, `uv`, or the official Python.org installer if missing.
- Checks NVIDIA driver (`nvidia-smi`) when `-Tier gpu` or `-WithGpu` is set, installs CUDA PyTorch wheels (`cu124`) if CUDA is present, verifies model weights and Python imports, and prompts interactively (offering browser download + re-check) if any dependency requires manual installation.

---

### Linux (systemd or Standalone)

Run from the unpacked release or repository root:

```bash
sudo bash install/install.sh
```

By default, this installs into `/opt/filament-vision-server`, copies bundled models from `./models` (unless `--assets` is specified), sets up `systemd` service `filament-vision-server.service`, starts the server on `http://127.0.0.1:7920/`, and verifies `/v1/health`.

Installer options (`install/install.sh`):
- `--tier auto|cpu|gpu` (default: `auto`)
- `--port 7920`
- `--bind 127.0.0.1` (use `0.0.0.0` for LAN access)
- `--user <username>`
- `--prefix /opt/filament-vision-server`
- `--assets <models_dir>` (defaults to `./models`)
- `--with-gpu` (install optional PyTorch/Transformers/PEFT GPU extras)
- `--no-systemd` (skip systemd unit creation; creates `start_server.sh` / `stop_server.sh` under `--prefix`)
- `-y`, `--non-interactive` (skip interactive prompts for CI/unattended installs)
- `--uninstall` (stop service, remove unit, remove prefix install)

Automatic Linux Dependency Handling (`install/install.sh`):
- Detects missing system packages/shared libs (`python3-venv`, `python3-pip`, `curl`, `ca-certificates`, `libglib-2.0`, `libgomp1`, `libGL`, `libheif`) and installs them automatically via `apt-get`, `dnf`, `yum`, `pacman`, `zypper`, or `apk` (with `sudo` prompt if non-root).
- If no distro Python 3.10–3.14 runtime is available, automatically bootstraps standalone `uv` + Python 3.12 without needing root.
- Verifies GPU driver readiness (`nvidia-smi` when `--tier gpu`/`--with-gpu`), model weights (with optional `FILAMENT_VISION_SERVER_ASSETS_URL` download), and Python imports with `--force-reinstall` auto-repair, prompting the user with install instructions + retry if any manual step is needed.

---

### Manual Python Virtual Environment

Requires **Python 3.10 – 3.14**:

```bash
python -m venv .venv
# Linux / macOS:
.venv/bin/pip install -e .        # or ".[gpu]" for CUDA VLM tier
.venv/bin/filament-vision-server serve
# Windows:
.venv\Scripts\pip install -e .    # or ".[gpu]"
.venv\Scripts\filament-vision-server serve
```

---

## CLI Commands

Both `filament-vision-server` and `spool-server` expose the same CLI:

```bash
filament-vision-server serve [--host 127.0.0.1] [--port 7920] [--tier auto|cpu|gpu]
filament-vision-server predict path/to/spool.jpg [--tier auto] [--format xml|filament-kit]
filament-vision-server status
filament-vision-server config [--tier auto|cpu|gpu] [--host 127.0.0.1] [--port 7920] [--feedback-opt-in true|false]
filament-vision-server ingest-feedback <zip_or_dir>
filament-vision-server update-models [--force] [--tier cpu|gpu|all]
filament-vision-server benchmark [--no-gpu]
```

---

## HTTP API v1 (`http://127.0.0.1:7920`)

CORS (`Access-Control-Allow-Origin: *`) is enabled for local web integrations, with optional Bearer auth via `FILAMENT_VISION_SERVER_TOKEN`.

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/`, `/ui` | Built-in single-page Web UI |
| `GET` | `/v1/health` (`/health`) | Service status, version, CPU/GPU tier availability, limits |
| `GET` | `/v1/config` (`/config`) | Read active tier, idle unload timeout, feedback opt-in state |
| `POST` | `/v1/config` (`/config`) | Update runtime config and persist `configs/service_config.xml` |
| `POST` | `/v1/read` (`/read`) | Recognize 1–8 base64 photos (`images_b64`, optional `tier`, `hint`) |
| `POST` | `/v1/revise` (`/revise`) | Apply natural-language correction note to a previous recognition item |
| `GET` | `/v1/feedback` | Queue status and opt-in state |
| `POST` | `/v1/feedback` | Queue an EXIF-stripped verified correction entry (requires opt-in) |
| `DELETE` | `/v1/feedback/<id>` | Delete a queued feedback entry |
| `POST` | `/v1/feedback/submit` | Upload queued corrections bundle (requires opt-in) |
| `GET` | `/v1/feedback/bundle.zip` | Download queued corrections as a zip archive |

---

## Environment Variables

- `FILAMENT_VISION_SERVER_HOME`: Base directory containing `configs/`, `data/`, and `models/` (helpful after non-editable `pip install .`).
- `FILAMENT_VISION_SERVER_TOKEN`: Require `Authorization: Bearer <token>` on API routes (`GET /`, `/ui`, and `/v1/health` remain open).
- `FILAMENT_VISION_SERVER_FEEDBACK_ENDPOINT`: Override the opt-in feedback upload URL.
- `FILAMENT_VISION_SERVER_CPU_ONLY=1`: Force ONNX Runtime CPU execution provider.

---

## Building a Self-Contained Release Bundle

Package source code, installers, launchers, configs, catalog, and trained model weights into `dist/filament-vision-server-0.2.0/` with `manifest.json` and `SHA256SUMS`:

```bash
python tools/build_standalone_bundle.py
# CPU-only bundle (smaller, no 200 MB GPU LoRA adapter):
python tools/build_standalone_bundle.py --no-gpu
```

---

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE) and [THIRD-PARTY.md](THIRD-PARTY.md).
