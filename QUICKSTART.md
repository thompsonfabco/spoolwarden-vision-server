# Filament Vision Server — User QuickStart Guide (`v0.2.1`)

Welcome to **Filament Vision Server**! This guide takes you from downloading a release archive to recognizing your first filament spools, submitting training corrections, and exporting your inventory.

---

## 1. Choose & Download Your Package

Download the archive for your operating system from the [GitHub Releases page](https://github.com/thompsonfabco/filament-vision-server/releases/latest) (current release: [`v0.2.1`](https://github.com/thompsonfabco/filament-vision-server/releases/tag/v0.2.1)):

| Platform | Package File | Size | Includes | Best For |
| :--- | :--- | :---: | :--- | :--- |
| **Windows (x64)** | `filament-vision-server-0.2.1-windows.zip` | ~316 MB | CPU ONNX tier + GPU VLM LoRA adapter | Windows 10/11 PCs with an NVIDIA CUDA GPU (`>= 8 GB` VRAM) |
| **Windows (x64, CPU-Only)** | `filament-vision-server-0.2.1-windows-cpu.zip` | ~146 MB | CPU ONNX tier only | Any Windows 10/11 PC, laptop, or mini-PC without an NVIDIA GPU |
| **Linux (x86_64)** | `filament-vision-server-0.2.1-linux.tar.gz` | ~316 MB | CPU ONNX tier + GPU VLM LoRA adapter | Linux desktops/servers with an NVIDIA CUDA GPU (`>= 8 GB` VRAM) |
| **Linux (x86_64, CPU-Only)** | `filament-vision-server-0.2.1-linux-cpu.tar.gz` | ~146 MB | CPU ONNX tier only | Headless servers, containers, WSL, or PCs without a discrete GPU |

> [!TIP]
> **Which tier should you pick?**
> - **Choose CPU-Only** for everyday scanning on any modern CPU — it uses lightweight ONNX models (`detector_rfdetr_n.onnx` + `visual_multihead.onnx`) and RapidOCR with zero large model downloads.
> - **Choose Full (CPU + GPU)** if you have an NVIDIA GPU and want automatic fallback to **Qwen2.5-VL-7B-Instruct** (4-bit NF4 + LoRA adapter) when a label is worn, handwritten, or falls below the `0.78` confidence threshold. Note that on the first GPU inference, the server automatically downloads and SHA-256-verifies `Qwen/Qwen2.5-VL-7B-Instruct` from Hugging Face (~15 GB disk cache).

*(Optional)* Verify your download before extracting using `SHA256SUMS` and `SHA256SUMS.sig` — see [Verifying Release Checksums & Signatures](#6-troubleshooting--faq).

---

## 2. Install & Start the Server

### Windows (One-Click Installer)

1. Right-click the downloaded `.zip` file and click **Extract All...** to a folder such as `C:\filament-vision-server`.
2. Open the extracted folder and **double-click `install.bat`**.

What the Windows installer handles for you automatically:
- Checks for **Microsoft Visual C++ 2015–2022 Redistributable (x64)** and installs it via `winget` or `vc_redist.x64.exe` if missing.
- Checks for **Python 3.10–3.14** (with `venv` and `pip`) and automatically installs **Python 3.12** via `winget`, `uv`, or Python.org if missing.
- Creates an isolated `.venv` virtual environment and installs all CPU dependencies (plus CUDA PyTorch `cu124` wheels if an NVIDIA GPU is present and `-Tier gpu` or `-WithGpu` is passed).
- Verifies model files and Python package imports, starts the background server at `http://127.0.0.1:7920/`, and opens your default browser.

*(If a dependency cannot be installed silently, `install.bat` will pause, offer to open the official download link, and let you press Enter to re-check.)*

#### Common Windows Options & Launchers

- **Everyday Start:** Double-click **`start_server.bat`** (starts the server in the background and opens `http://127.0.0.1:7920/`).
- **Everyday Stop:** Double-click **`stop_server.bat`**.
- **Allow Phones/Tablets on Wi-Fi (`0.0.0.0`) & Auto-Start at Logon:**
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Bind 0.0.0.0 -RegisterStartup -Start -OpenBrowser
  ```
- **Force CPU-Only Mode (Even on a GPU Machine):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Tier cpu -Start -OpenBrowser
  ```
- **Uninstall:**
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Uninstall
  ```

---

### Linux (Automated Installer)

1. Extract the `.tar.gz` archive and enter the extracted directory:
   ```bash
   tar -xzf filament-vision-server-0.2.1-linux-cpu.tar.gz
   cd filament-vision-server-0.2.1-linux-cpu
   ```
   *(Replace `-linux-cpu` with `-linux` if you downloaded the Full bundle.)*

2. Run the installer:
   - **Recommended — System Service (`systemd` under `/opt/filament-vision-server`):**
     ```bash
     sudo bash install/install.sh
     ```
   - **No-`root` / WSL / Container Install (under `$HOME/filament-vision-server`):**
     ```bash
     bash install/install.sh --no-systemd --prefix "$HOME/filament-vision-server"
     "$HOME/filament-vision-server/start_server.sh"
     ```

What `install/install.sh` handles automatically:
- Installs any missing OS libraries (`python3-venv`, `python3-pip`, `curl`, `ca-certificates`, `libglib-2.0`, `libgomp1`, `libGL`, `libheif`) via `apt-get`, `dnf`, `yum`, `pacman`, `zypper`, or `apk`.
- Bootstraps standalone **`uv` + Python 3.12** automatically if your distro does not ship Python 3.10–3.14.
- Installs Python packages into `.venv`, verifies `models/cpu/` (and `models/gpu/` if applicable), registers `filament-vision-server.service` (when `systemd` is active), starts the server, and polls `http://127.0.0.1:7920/v1/health` until ready.

#### Useful Linux Options & Commands

- **Expose Web UI on LAN (`0.0.0.0`) for mobile scanning:**
  ```bash
  sudo bash install/install.sh --bind 0.0.0.0 --port 7920
  ```
- **Manage the `systemd` service:**
  ```bash
  sudo systemctl status filament-vision-server
  sudo systemctl restart filament-vision-server
  sudo systemctl stop filament-vision-server
  journalctl -u filament-vision-server -f
  ```
- **Stop user-space (`--no-systemd`) server:**
  ```bash
  "$HOME/filament-vision-server/stop_server.sh"
  ```

---

## 3. Scan & Recognize Spools in the Web UI

1. Open **`http://127.0.0.1:7920/`** in any browser (or `http://<your-server-ip>:7920/` from your phone when bound to `0.0.0.0`).
2. Confirm the top-right status pill turns green (`OK v0.2.1 (CPU mode)` or `OK v0.2.1 (GPU ready)`).
3. Under **1. Spool / Box Photo**:
   - Drag and drop up to **8 photos**, click **Choose Files**, or tap **Take Photo** on a phone/tablet.
   - Supported formats: `.JPG`, `.JPEG`, `.PNG`, `.WEBP`, `.HEIC`, `.HEIF` (native iPhone/Android HEIC photos work directly without converting first).
4. *(Optional)* Adjust **Inference Tier** (`auto`, `cpu`, or `gpu`) or enter an **Optional Hint** such as `"bare spool"`, `"PETG"`, or `"Bambu Lab"`.
5. Click **Recognize Spool(s)**.

> [!TIP]
> **Photo Tips for Highest Accuracy**
> - **Photo 1:** Take a clear, well-lit shot of the side sticker/label (brand, material, print temperatures, weight). Curved spool rim labels are unwarped automatically.
> - **Photo 2 (optional):** Take a slightly angled shot showing both the exposed filament windings and the spool flange/hub — this helps confirm the filament color in LAB color space and distinguish sealed boxes/bags from bare spools.

In **2. Recognized Spool Details**, review the extracted fields:
- **Vendor**, **Material**, **Product Line**, **Color Name**, **Color Family**, and **Color Hex** (with an interactive color picker)
- **Net Weight (g)**, **Tare Weight (g)**, **Diameter (mm)**, **Density (g/cm3)**
- **Nozzle Range (°C)**, **Bed Range (°C)**, and **Factory Sealed** checkbox
- Per-field confidence pills (`Confidence: XX%`) and an expandable **Inspect Raw JSON / XML Response** section

*(If multiple spools/photos produce multiple items, tabs appear at the top of Panel 2 so you can review each item independently.)*

---

## 4. Correct Results & Submit Training Feedback

Spools from niche brands, custom colors, or worn labels can occasionally need a quick tweak — and your verified corrections can directly help improve the recognition models for everyone.

### Editing or Revising a Prediction

- **Direct Edit:** Change any text box, number, checkbox, or color swatch in **2. Recognized Spool Details**.
- **Natural-Language Revise:** Type a quick correction in the **Revise** input box — for example:
  ```text
  brand is Prusament, PETG Jet Black, bare spool
  ```
  and click **Revise** (`POST /v1/revise`). The server applies your note and re-enriches specs (tare weight, density, nozzle/bed temperature range, and official hex code) against its offline catalog of **46 vendors, 1,115 product lines, and 3,979 colors**.

### Submitting Corrections to the Training Queue (Opt-In)

Training feedback is **strictly opt-in** (disabled by default) and privacy-safe:
- Every queued photo has **all EXIF, GPS, camera, and ICC metadata stripped** by decoding pixels into a clean RGB buffer and resizing to at most `1024 px` on the longest side before saving as JPEG.
- Only the sanitized photos + the model's prediction + your corrected fields + optional revise note are stored.

To submit a correction:
1. Enable **"Opt in to correction training feedback"** in the **Training Feedback Queue** card.
2. Adjust the fields in **2. Recognized Spool Details** until they accurately describe your spool.
3. Click **Queue Training Feedback**. The header counter (`Feedback Queue: N`) updates immediately.
4. After scanning/correcting one or more spools, choose either:
   - **Upload Feedback to Cloud** — sends your queued corrections directly to the pre-configured project feedback inbox (`POST /v1/feedback/submit`). Large queues are automatically split into `<= 25 MB` `.zip` batches and uploaded sequentially, clearing each batch from the queue as it succeeds.
   - **Download Feedback .zip** — saves `filament-feedback-bundle.zip` locally (`GET /v1/feedback/bundle.zip`, no size cap) for your own fine-tuning or offline archive.

---

## 5. Build & Export Your Spool Inventory

1. After reviewing (or correcting) each spool in Panel 2, click **Add to Session List**.
2. Your scanned spools accumulate in **3. Spool Inventory Session List** with color swatches, weights, seal state, and confidence.
3. Export your session (or the currently displayed spool if the list is empty) at any time:
   - **Export Spoolman JSON** (`spoolman-import.json`) — structured with `vendors`, `filaments`, and `spools` (including `spool_weight`, `density`, `color_hex`, `settings_extruder_temp`, and `settings_bed_temp`).
   - **Export CSV** (`spools.csv`) — opens cleanly in Excel, LibreOffice Calc, or Google Sheets.
   - **Export JSON** (`spools.json`) — machine-readable JSON array.
   - **Export XML** (`spools.xml`) — `<spool-inventory>` XML format.

---

## 6. Troubleshooting & FAQ

### How do I scan directly from my phone?
Start or configure the server to listen on `0.0.0.0` (all network interfaces) instead of `127.0.0.1`, then allow TCP port `7920` through your firewall:
- **Windows (run PowerShell as Administrator):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Bind 0.0.0.0 -Start
  New-NetFirewallRule -DisplayName "Filament Vision Server" -Direction Inbound -Protocol TCP -LocalPort 7920 -Action Allow
  ```
- **Linux (`ufw`):**
  ```bash
  sudo bash install/install.sh --bind 0.0.0.0
  sudo ufw allow 7920/tcp
  ```
Then open `http://<desktop-lan-ip>:7920/` on your phone and tap **Take Photo**.

### Port `7920` is already in use — how do I change it?
Pass a custom port to the installer or CLI:
- **Windows:** `powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -Port 8080 -Start -OpenBrowser`
- **Linux:** `sudo bash install/install.sh --port 8080`
- **CLI:** `filament-vision-server serve --port 8080`

### My GPU doesn't have enough VRAM or CUDA is failing — how do I force CPU mode?
Switch to CPU mode from the **Inference Tier** dropdown in the Web UI (`cpu (Fast ONNX + OCR)`), or lock the server default:
```bash
filament-vision-server config --tier cpu
```
Or set `FILAMENT_VISION_SERVER_CPU_ONLY=1` before starting the server.

### How do I verify the integrity and authenticity of a downloaded release?
Every release ships with `SHA256SUMS`, `SHA256SUMS.sig`, and `manifest.json`. From an unpacked bundle directory (or repo root):
```bash
# 1. Verify Ed25519 signature over SHA256SUMS against the project public key:
python tools/sign_release.py verify SHA256SUMS --sig SHA256SUMS.sig

# 2. Verify all extracted file SHA-256 digests:
sha256sum -c SHA256SUMS
```
The release signing public key (`Ed25519`, base64 raw) is:
```text
adkFwUZin7sdhZJ1fdr9sOn8BtFIFuiFHrm2d5O0TPU=
```

### Where are logs, configs, and feedback stored?
- **Config:** `configs/service_config.xml`
- **Logs (Windows `install.ps1` / `start_server.bat`):** `data/logs/server.out.log` and `data/logs/server.err.log`
- **Logs (Linux `systemd`):** `journalctl -u filament-vision-server`
- **Logs (Linux `--no-systemd`):** `<prefix>/data/server.log`
- **Queued Feedback (before upload/clear):** `data/feedback_queue/`
