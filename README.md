# Filament Vision Server (`v0.3.0`)

Standalone, cross-platform 3D printer filament spool recognition server for **Windows** and **Linux**, with a built-in dark-mode Web UI, dual-tier **ONNX CPU + Qwen2.5-VL GPU** inference, catalog enrichment, opt-in training feedback collection, and an HTTP REST API (`v1`).

> [!TIP]
> **New user?** Jump straight to the step-by-step **[User QuickStart Guide (`QUICKSTART.md`)](QUICKSTART.md)** or download a ready-to-run package from the **[`v0.3.0` Release](https://github.com/thompsonfabco/filament-vision-server/releases/tag/v0.3.0)**.

---

## Key Highlights

- **Built-In Web UI (`GET /`, `GET /ui`):**
  - Drag-and-drop or snap mobile camera photos (`.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.heif`, up to 8 photos per request).
  - Edit every recognized attribute inline with a synchronized **Color Hex** swatch picker and per-field confidence badges.
  - Refine predictions in plain English via **Revise** (`POST /v1/revise`), automatically re-enriching specifications from the offline catalog.
  - Maintain a multi-spool session list and export as **Spoolman JSON** (`spoolman-import.json`), **CSV**, **JSON**, or **XML**.
- **Two-Tier Hybrid Recognition:**
  - **CPU Tier (always available):** RF-DETR-N ONNX object detector (`detector_rfdetr_n.onnx`) + circular spool-label unwarper + RapidOCR + MobileNetV3-Large ONNX multi-head classifier (`visual_multihead.onnx`) + learned text parser (`text_parser.npz`) + LAB-space filament color clustering (`color_lab.py`) + fuzzy catalog matcher (`46` vendors, `1,115` product lines, `3,979` colors).
  - **GPU Tier (optional CUDA):** `Qwen/Qwen2.5-VL-7B-Instruct` (4-bit NF4, SHA-256 verified pinned revision in `configs/base-model.json`) with LoRA adapter (`models/gpu/qwen25_vl_7b_filament_lora/`), automatic escalation in `auto` mode when `vendor == "Unknown"` or `confidence < 0.78`, and automatic idle VRAM unloading (`idle_unload_seconds`, default `300s`).
- **Opt-In Correction Training Feedback Loop:**
  - Disabled by default (`feedback_opt_in=false`). When enabled, strips all EXIF/GPS/ICC metadata, resizes images to `<= 1024 px`, and queues clean `(images, predicted, corrected, note)` pairs.
  - Supports **uncapped local `.zip` downloads** (`GET /v1/feedback/bundle.zip`) and **automatic multi-batch `<= 25 MB` splitting** for large queues during cloud upload (`POST /v1/feedback/submit`), removing each batch cleanly as it succeeds.
- **Automated Cross-Platform Installers:**
  - `install\install.ps1` / `install.bat` (Windows) and `install/install.sh` (Linux) automatically check, download, install, and verify OS libraries, Python `3.10–3.14`, Microsoft VC++ Redistributable (Windows), `uv` Python runtime fallback (Linux), CUDA PyTorch wheels, and Python dependencies — falling back to interactive prompts if manual intervention is required.

---

## Release Packages (`v0.3.0`)

Platform-specific archives (`.zip` for Windows, `.tar.gz` for Linux) are published on the [GitHub Releases page](https://github.com/thompsonfabco/filament-vision-server/releases/tag/v0.3.0):

| Archive | Platform | Size | Tier Bundle |
| :--- | :--- | :---: | :--- |
| `filament-vision-server-0.3.0-windows.zip` | Windows (x64) | ~316 MB | Full (`models/cpu` + `models/gpu` LoRA adapter) |
| `filament-vision-server-0.3.0-windows-cpu.zip` | Windows (x64) | ~146 MB | CPU-Only (`models/cpu` ONNX + OCR + catalog) |
| `filament-vision-server-0.3.0-linux.tar.gz` | Linux (x86_64) | ~316 MB | Full (`models/cpu` + `models/gpu` LoRA adapter) |
| `filament-vision-server-0.3.0-linux-cpu.tar.gz` | Linux (x86_64) | ~146 MB | CPU-Only (`models/cpu` ONNX + OCR + catalog) |

Every release also includes `manifest.json`, `SHA256SUMS`, and an RFC 8032 Ed25519 signature `SHA256SUMS.sig`.

### Verify Release Signatures & Checksums

```bash
# Verify Ed25519 signature over SHA256SUMS against the project public key:
python tools/sign_release.py verify SHA256SUMS --sig SHA256SUMS.sig

# Verify all SHA-256 checksums:
sha256sum -c SHA256SUMS
```

Release signing public key (`Ed25519`, base64 raw in `tools/sign_release.py`):
```text
adkFwUZin7sdhZJ1fdr9sOn8BtFIFuiFHrm2d5O0TPU=
```

---

## Quick Installation

For full screenshots-free walkthroughs, firewall/LAN setup, and troubleshooting, see **[`QUICKSTART.md`](QUICKSTART.md)**.

### Windows (One-Click)

1. Extract `filament-vision-server-0.3.0-windows*.zip`.
2. Double-click **`install.bat`** (or run PowerShell):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Start -OpenBrowser
   ```
3. Everyday launchers in the root folder: **`start_server.bat`** and **`stop_server.bat`**.

Supported `install\install.ps1` options:
- `-Tier auto|cpu|gpu` (default: `auto`)
- `-Port 7920` (default: `7920`)
- `-Bind 127.0.0.1` (use `0.0.0.0` to allow phones/tablets on your LAN)
- `-Prefix <dir>` (install to a target directory instead of in-place)
- `-WithGpu` (install CUDA PyTorch, Transformers, PEFT, Accelerate, BitsAndBytes, and `qwen-vl-utils`)
- `-RegisterStartup` (create logon Scheduled Task `FilamentVisionServer`)
- `-Start` / `-OpenBrowser` / `-NonInteractive` / `-Uninstall`

### Linux (`systemd` or User-Space)

Extract the `.tar.gz` archive and run `install/install.sh`:

```bash
tar -xzf filament-vision-server-0.3.0-linux-cpu.tar.gz
cd filament-vision-server-0.3.0-linux-cpu

# System service install (/opt/filament-vision-server + systemd unit):
sudo bash install/install.sh

# Or user-space install without root / systemd:
bash install/install.sh --no-systemd --prefix "$HOME/filament-vision-server"
"$HOME/filament-vision-server/start_server.sh"
```

Supported `install/install.sh` options:
- `--tier auto|cpu|gpu` (default: `auto`)
- `--port 7920` (default: `7920`)
- `--bind 127.0.0.1` (use `0.0.0.0` for LAN access)
- `--user <username>` (service user for `systemd` unit)
- `--prefix /opt/filament-vision-server`
- `--assets <models_dir>` (defaults to `./models`)
- `--with-gpu` (install optional PyTorch/Transformers/PEFT GPU extras)
- `--no-systemd` (emit `start_server.sh` and `stop_server.sh` under `--prefix`)
- `-y`, `--non-interactive` / `--uninstall`

### Python Virtual Environment (From Source)

Requires **Python 3.10 – 3.14** and trained model assets under `models/`:

```bash
python -m venv .venv
# Linux / macOS:
.venv/bin/pip install -e .          # or ".[gpu]" for CUDA VLM support
.venv/bin/filament-vision-server serve
# Windows:
.venv\Scripts\pip install -e .      # or ".[gpu]"
.venv\Scripts\filament-vision-server serve
```

---

## Architecture & Two-Tier Recognition Pipeline

```text
  Photo(s) [.jpg/.png/.webp/.heic]
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ Tier 1 — CPU Hybrid Pipeline (configs/cpu_pipeline.xml)          │
  │  1. RF-DETR-N ONNX detector (box_label, spool_label,             │
  │     spool_windings, spool_flange)                                │
  │  2. Circular label unwarper + RapidOCR ONNX multi-angle text     │
  │  3. MobileNetV3-Large multi-head ONNX (color_family, finish,     │
  │     vendor, sealed_state) + text_parser.npz                      │
  │  4. LAB-space k-means filament color clustering (color_lab.py)   │
  │  5. CatalogMatcher (data/catalog/filament_catalog.xml)           │
  └──────────────────────────┬───────────────────────────────────────┘
                             │ confidence >= 0.78 and vendor known
                             ▼
                    Return IntakeReadResult
                             ▲
                             │ confidence < 0.78 or Unknown (tier=auto)
  ┌──────────────────────────┴───────────────────────────────────────┐
  │ Tier 2 — GPU VLM Pipeline (configs/gpu_vlm_lora.xml)             │
  │  • Qwen/Qwen2.5-VL-7B-Instruct (4-bit NF4, SHA-256 verified)     │
  │  • LoRA adapter (models/gpu/qwen25_vl_7b_filament_lora)          │
  │  • Receives CPU priors + catalogs match + idle VRAM auto-unload  │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Built-In Web UI, Corrections & Maintainer Retraining

1. Open `http://127.0.0.1:7920/` in a desktop or mobile browser.
2. Upload 1–8 spool photos (or use **Take Photo** on mobile), optionally add a hint (`"bare spool"`, `"PETG"`), and click **Recognize Spool(s)**.
3. Correct any field manually or via natural-language **Revise** (`POST /v1/revise`).
4. Toggle **"Opt in to correction training feedback"**, click **Queue Training Feedback**, and click **Upload Feedback to Cloud** or **Download Feedback .zip**.
5. Build a session list and export **Spoolman JSON** (`spoolman-import.json`), **CSV**, **JSON**, or **XML**.

### Maintainer One-Click Model Update

Maintainers can ingest collected feedback archives, enforce a batch threshold (`--min-new-samples`, default `10`), retrain CPU and/or GPU models, benchmark against the Gold Test Split, and optionally stage & sign a new release:

```bash
# Linux:
bash tools/update_models.sh --force --tier all

# Windows:
tools\update_models.bat --force --tier all
```

---

## HTTP REST API (`v1`)

Default listen address: `http://127.0.0.1:7920`. CORS allows loopback browser origins (`localhost`, `127.0.0.1`, `::1`), same-origin LAN Web UI requests whose `Origin` host matches the request `Host` header (when bound to `0.0.0.0` or `::`), and any explicit origins configured via `FILAMENT_VISION_SERVER_ALLOWED_ORIGINS`. Browser `OPTIONS`/`POST`/`DELETE` requests carrying an untrusted `Origin` header are rejected (`403 forbidden_origin`). Set `FILAMENT_VISION_SERVER_TOKEN` to require `Authorization: Bearer <token>` on API routes (the Web UI keeps any entered token in browser `sessionStorage`; `GET /`, `/ui`, and `/v1/health` stay unauthenticated).

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/`, `/ui` | Built-in single-page Web UI |
| `GET` | `/v1/health` (`/health`) | Daemon status, version, CPU/GPU tier readiness, request limits |
| `GET` | `/v1/config` (`/config`) | Read active tier, idle unload timeout, feedback opt-in & endpoint state |
| `POST` | `/v1/config` (`/config`) | Update runtime settings and persist `configs/service_config.xml` |
| `POST` | `/v1/read` (`/read`) | Recognize 1–8 base64 images (`images_b64`, optional `tier`, `hint`, `filaments`, `vendors`) |
| `POST` | `/v1/revise` (`/revise`) | Apply natural-language correction (`previous`, `note`) and re-enrich from catalog |
| `GET` | `/v1/feedback` | Check feedback opt-in state and queued entry count |
| `POST` | `/v1/feedback` | Enqueue an EXIF-stripped correction (`images_b64`, `predicted`, `corrected`, `note`) |
| `DELETE` | `/v1/feedback/<id>` | Remove a specific queued feedback entry |
| `POST` | `/v1/feedback/submit` | Upload queued feedback entries in automatic `<= 25 MB` ZIP batches |
| `GET` | `/v1/feedback/bundle.zip` | Download `filament-feedback-bundle.zip` (uncapped local export) |

### `curl` Examples

**Check server health:**
```bash
curl -s http://127.0.0.1:7920/v1/health
```

**Recognize a spool image (`POST /v1/read`):**
```bash
IMG_B64=$(base64 -w0 path/to/spool.jpg)
curl -s -X POST http://127.0.0.1:7920/v1/read \
  -H "Content-Type: application/json" \
  -d "{\"api_version\":\"1.1\",\"tier\":\"auto\",\"images_b64\":[\"$IMG_B64\"],\"hint\":\"PETG\"}"
```

**Revise a prediction with a natural-language note (`POST /v1/revise`):**
```bash
curl -s -X POST http://127.0.0.1:7920/v1/revise \
  -H "Content-Type: application/json" \
  -d '{"api_version":"1.1","previous":{"vendor":"Unknown","material":"PLA","color_name":"Black"},"note":"Actually Prusament PETG Jet Black, bare spool"}'
```

---

## CLI Reference

Both `filament-vision-server` and `spool-server` invoke `filament_vision_server.service.cli:main`:

```bash
filament-vision-server serve [--host 127.0.0.1] [--port 7920] [--tier auto|cpu|gpu]
filament-vision-server predict path/to/spool.jpg [--tier auto|cpu|gpu] [--format xml|filament-kit]
filament-vision-server status
filament-vision-server config [--tier auto|cpu|gpu] [--host 127.0.0.1] [--port 7920] \
    [--idle-unload-seconds 300] [--feedback-opt-in true|false] [--feedback-endpoint URL]
filament-vision-server playground [--host 127.0.0.1] [--port 7861]
filament-vision-server ingest-feedback <zip_or_dir>
filament-vision-server update-models [--source PATH] [--min-new-samples 10] [--force] [--tier cpu|gpu|all]
filament-vision-server train [--tier cpu|gpu|all]
filament-vision-server benchmark [--no-gpu]
filament-vision-server sync-catalog [--library-root PATH]
```

### Environment Variables

| Variable | Description |
| :--- | :--- |
| `FILAMENT_VISION_SERVER_HOME` | Override base directory containing `configs/`, `data/`, and `models/` |
| `FILAMENT_VISION_SERVER_STATE_DIR` | External state folder for `service_config.xml`, bearer token, Ed25519 feedback key, and `feedback_queue/` |
| `FILAMENT_VISION_SERVER_TOKEN` | Require `Authorization: Bearer <token>` on API routes |
| `FILAMENT_VISION_SERVER_ALLOWED_ORIGINS` | Comma-separated extra cross-origin URLs allowed alongside loopback & same-origin LAN UI (`http://...`) |
| `FILAMENT_VISION_SERVER_FEEDBACK_ENDPOINT` | Override the default opt-in training feedback upload endpoint |
| `FILAMENT_VISION_SERVER_CPU_ONLY=1` | Force ONNX Runtime `CPUExecutionProvider` (skip CUDA/DirectML/CoreML) |

---

## Opt-In Correction Feedback

- **Off by default:** No correction or image is ever queued or uploaded unless you explicitly opt in.
- **Nothing sent automatically:** Queued corrections wait locally in your state folder (`feedback_queue/`) until you press **Send** (or call `POST /v1/feedback/submit`).
- **What a package holds:** Photos stripped of all EXIF/GPS metadata and resized to at most `1024 px`, the original reading, your correction, and an optional note.
- **Where it goes:** Packages are uploaded to the project maintainer's write-only intake for review and future model training (`tools/google_apps_script_feedback.js` -> `filament-vision-server ingest-feedback`).
- **Anonymous per-installation key:** On first opt-in, the service generates an anonymous Ed25519 key pair in its state folder (nothing else identifying is included) and signs each package's SHA-256 alongside a short proof-of-work nonce computed in the background at submit time.

> **Note on `src/filament_vision_server/data_engine/`:** Contains offline dataset harvesting and auto-labeling tools used only for model training; it is not imported or used by the runtime service.

---

## Building Bundles & Running Tests

### Run Automated Tests

```bash
# Python end-to-end API, Web UI, installer & bundle tests:
pytest -v

# Windows -> WSL Ubuntu end-to-end Linux installer test harness:
tools\run_wsl_installer_test.bat
```

### Build Platform-Specific Release Archives

Stage bundles in `dist/` and package platform-specific archives (`.zip` for Windows, `.tar.gz` for Linux) with `manifest.json`, `SHA256SUMS`, and Ed25519 `SHA256SUMS.sig` under `release/v0.3.0/`:

```bash
# Single platform bundle:
python tools/build_standalone_bundle.py --platform windows
python tools/build_standalone_bundle.py --platform linux --no-gpu

# Full signed release (windows.zip, windows-cpu.zip, linux.tar.gz, linux-cpu.tar.gz):
python tools/build_standalone_bundle.py --version 0.3.0 --build-release --key-file /path/to/release_key.pem
```

---

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE) and [THIRD-PARTY.md](THIRD-PARTY.md).
