"""Interactive browser Playground UI for testing spoolwarden-vision-server CPU and GPU models on any image (.HEIC/.JPG/.PNG)."""
from __future__ import annotations

import base64
import hmac
import io
import json
import random
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

from ..cpu_tier.pipeline import _validate_image_dimensions
from .daemon import (
    FilamentRecognizer,
    MAX_BODY_BYTES,
    _DualStackThreadingHTTPServer,
    _is_trusted_origin,
    load_service_config,
)
from .feedback import _strip_data_uri
from ..xml_io import PROJECT_ROOT

PLAYGROUND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>spoolwarden-vision-server — Model Playground</title>
<style>
  :root {
    --bg: #0f172a; --panel: #1e293b; --border: #334155;
    --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --green: #22c55e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
  }
  h1 { margin: 0 0 6px 0; font-size: 22px; }
  .sub { color: var(--muted); font-size: 14px; margin-bottom: 20px; }
  .layout { display: grid; grid-template-columns: 380px 1fr; gap: 20px; align-items: start; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px;
  }
  .dropzone {
    border: 2px dashed var(--accent); border-radius: 10px; padding: 28px 16px;
    text-align: center; cursor: pointer; background: rgba(56, 189, 248, 0.05);
    transition: background 0.15s;
  }
  .dropzone:hover, .dropzone.dragover { background: rgba(56, 189, 248, 0.14); }
  .controls { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  select, button {
    background: #0f172a; color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 14px; font-size: 14px; cursor: pointer;
  }
  button.primary {
    background: var(--accent); color: #0f172a; font-weight: 600; border: none; flex: 1;
  }
  button.secondary { background: #334155; color: var(--text); border: none; }
  #preview {
    width: 100%; max-height: 340px; object-fit: contain;
    border-radius: 8px; margin-top: 14px; display: none; background: #090d16;
  }
  .tiers-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
  .tier-title {
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 12px;
  }
  .badge {
    background: rgba(34, 197, 94, 0.15); color: var(--green);
    padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }
  table.kv { width: 100%; border-collapse: collapse; font-size: 14px; }
  table.kv td { padding: 6px 4px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  table.kv td.k { color: var(--muted); width: 135px; }
  table.kv td.v { font-weight: 600; }
  .swatch {
    display: inline-block; width: 16px; height: 16px; border-radius: 4px;
    vertical-align: middle; margin-right: 6px; border: 1px solid rgba(255,255,255,0.3);
  }
  pre.xml {
    background: #090d16; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font-size: 12px; overflow-x: auto; color: #7dd3fc; margin-top: 12px;
  }
  .status { color: var(--accent); font-size: 14px; margin-top: 10px; min-height: 20px; }
</style>
</head>
<body>
  <h1>spoolwarden-vision-server — Interactive Model Tester</h1>
  <div class="sub">Drop any filament spool or box photo (.HEIC, .JPG, .PNG) or click "Random Seed Photo" to test CPU Hybrid &amp; GPU VLM models live.</div>

  <div class="layout">
    <div class="card">
      <div class="dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
        <strong>Click or Drag &amp; Drop Image Here</strong><br>
        <span style="color:var(--muted);font-size:12px">Supports .HEIC, .JPG, .PNG, .WEBP</span>
        <input type="file" id="fileInput" accept=".heic,.heif,.jpg,.jpeg,.png,.webp" style="display:none">
      </div>
      <div class="controls">
        <input type="password" id="authToken" placeholder="Bearer token (if server requires auth)" style="flex:1;padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:var(--text);font-size:14px">
      </div>
      <div class="controls">
        <select id="tierSelect">
          <option value="both">Compare Both (CPU + GPU)</option>
          <option value="cpu" selected>CPU Hybrid Only (Fast)</option>
          <option value="gpu">GPU VLM Only (Qwen2.5-VL-7B 4-bit)</option>
          <option value="auto">Auto Tier (CPU -> GPU fallback)</option>
        </select>
        <button class="primary" onclick="runSelectedFile()">Analyze Image</button>
      </div>
      <div class="controls">
        <button class="secondary" style="width:100%" onclick="testRandomSeed()">🎲 Load &amp; Test Random Seed Photo (.HEIC)</button>
        <button class="secondary" style="width:100%" onclick="testRandomHarvested()">🌐 Load &amp; Test Random Verified Web Spool</button>
      </div>
      <div class="status" id="statusText">Ready.</div>
      <img id="preview" alt="Preview">
    </div>

    <div id="resultsArea" class="tiers-grid">
      <div class="card" style="color:var(--muted)">Upload an image or click <strong>Load &amp; Test Random Seed Photo</strong> to view predictions and XML output.</div>
    </div>
  </div>

<script>
let currentFile = null;
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const authTokenEl = document.getElementById('authToken');
try { localStorage.removeItem('fvs_auth_token'); } catch (_) {}
authTokenEl.value = sessionStorage.getItem('fvs_auth_token') || '';
authTokenEl.addEventListener('change', () => {
  sessionStorage.setItem('fvs_auth_token', authTokenEl.value.trim());
});

function pgAuthHeaders(extra = {}) {
  const h = { ...extra };
  const tok = (authTokenEl.value || sessionStorage.getItem('fvs_auth_token') || '').trim();
  if (tok) h['Authorization'] = `Bearer ${tok}`;
  return h;
}

dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('dragover');
  if (e.dataTransfer.files.length) {
    currentFile = e.dataTransfer.files[0];
    runSelectedFile();
  }
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    currentFile = fileInput.files[0];
    runSelectedFile();
  }
});

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderTierCard(title, data) {
  if (data.error) {
    return `<div class="card"><div class="tier-title"><strong>${escHtml(title)}</strong></div><div style="color:#f87171">${escHtml(data.error)}</div></div>`;
  }
  const it = (data.dict && data.dict.items && data.dict.items[0]) || {};
  const rawHex = String(it.color_hex || '').replace(/^#/, '');
  const hex = /^[0-9a-fA-F]{6}$/.test(rawHex) ? rawHex : '808487';
  const cName = it.color_name || it._color_name || '-';
  const cFam = it.color_family || it._color_family || '-';
  const srcType = (it.reader_extra && it.reader_extra.source_type) || it._source_type || '-';
  const confVal = it.confidence !== undefined ? it.confidence : (it._confidence || 0);
  const xmlEsc = escHtml(data.xml || '');
  return `
    <div class="card">
      <div class="tier-title">
        <strong>${escHtml(title)}</strong>
        <span class="badge">${escHtml(data.latency_ms)} ms</span>
      </div>
      <table class="kv">
        <tr><td class="k">Brand (vendor)</td><td class="v">${escHtml(it.vendor || '-')}</td></tr>
        <tr><td class="k">Product Line</td><td class="v">${escHtml(it.line || '-')}</td></tr>
        <tr><td class="k">Base Material</td><td class="v">${escHtml(it.material || '-')}</td></tr>
        <tr><td class="k">Color Name</td><td class="v">${escHtml(cName)} (${escHtml(cFam)})</td></tr>
        <tr><td class="k">Spoolman Name</td><td class="v">${escHtml(it.name || '-')}</td></tr>
        <tr><td class="k">Color Hex</td><td class="v"><span class="swatch" style="background:#${hex}"></span>#${hex}</td></tr>
        <tr><td class="k">Nozzle / Bed</td><td class="v">${escHtml(it.nozzle_range || '-')}°C (${escHtml(it.nozzle || '-')}°C) / ${escHtml(it.bed_range || '-')}°C</td></tr>
        <tr><td class="k">Weight / Tare</td><td class="v">Net ${escHtml(it.net ?? 1000)}g / Tare ${escHtml(it.tare ?? 200)}g / Density ${escHtml(it.density ?? 1.24)}</td></tr>
        <tr><td class="k">Sealed State</td><td class="v">${it.sealed ? 'Sealed (' + escHtml(it.sealed_reason || '') + ')' : 'Opened (' + escHtml(it.sealed_reason || '') + ')'}</td></tr>
        <tr><td class="k">Source / Conf</td><td class="v">${escHtml(srcType)} (${Math.round(Number(confVal) * 100)}%)</td></tr>
      </table>
      <pre class="xml">${xmlEsc}</pre>
    </div>
  `;
}

async function runSelectedFile() {
  if (!currentFile) {
    document.getElementById('statusText').textContent = 'Please select an image first.';
    return;
  }
  const tier = document.getElementById('tierSelect').value;
  document.getElementById('statusText').textContent = `Running ${tier.toUpperCase()} model on ${currentFile.name}...`;
  const reader = new FileReader();
  reader.onload = async () => {
    const b64 = reader.result.split(',')[1];
    const resp = await fetch('/api/test', {
      method: 'POST',
      headers: pgAuthHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({filename: currentFile.name, image_b64: b64, tier: tier})
    });
    const res = await resp.json();
    updateUI(res);
  };
  reader.readAsDataURL(currentFile);
}

async function testRandomSeed() {
  const tier = document.getElementById('tierSelect').value;
  document.getElementById('statusText').textContent = `Loading random .HEIC seed photo (${tier.toUpperCase()})...`;
  const resp = await fetch(`/api/random_seed?tier=${encodeURIComponent(tier)}`, { headers: pgAuthHeaders() });
  const res = await resp.json();
  updateUI(res);
}

async function testRandomHarvested() {
  const tier = document.getElementById('tierSelect').value;
  document.getElementById('statusText').textContent = `Loading random verified web spool (${tier.toUpperCase()})...`;
  const resp = await fetch(`/api/random_harvested?tier=${encodeURIComponent(tier)}`, { headers: pgAuthHeaders() });
  const res = await resp.json();
  updateUI(res);
}

function updateUI(res) {
  if (res.error) {
    document.getElementById('statusText').textContent = res.error;
    document.getElementById('resultsArea').innerHTML = '';
    return;
  }
  if (res.preview_b64) {
    const prev = document.getElementById('preview');
    prev.src = 'data:image/jpeg;base64,' + res.preview_b64;
    prev.style.display = 'block';
  }
  document.getElementById('statusText').textContent = `Finished: ${res.filename || 'image'}`;
  let html = '';
  for (const [label, tdata] of Object.entries(res.results || {})) {
    html += renderTierCard(label, tdata);
  }
  document.getElementById('resultsArea').innerHTML = html;
}
</script>
</body>
</html>
"""


def _decode_any_image_to_jpeg_bytes(raw_bytes: bytes) -> bytes:
    _validate_image_dimensions(raw_bytes)
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im = ImageOps.exif_transpose(im)
        rgb = im.convert("RGB")
        if max(rgb.size) > 1600:
            scale = 1600.0 / max(rgb.size)
            rgb = rgb.resize((int(rgb.size[0] * scale), int(rgb.size[1] * scale)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=92)
        return buf.getvalue()


def run_playground_server(host: str = "127.0.0.1", port: int = 7861) -> None:
    recognizer = FilamentRecognizer(default_tier="auto")
    seed_dir = PROJECT_ROOT / "seed images"
    auth_token = str(load_service_config().get("token") or "").strip()

    class PlaygroundHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _enforce_origin(self) -> bool:
            origin = (self.headers.get("Origin") or "").strip() if self.headers else ""
            host_header = (self.headers.get("Host") or "").strip() if self.headers else ""
            if origin and not _is_trusted_origin(origin, host_header):
                self._send_json(403, {"error": f"Untrusted Origin header: {origin}"})
                return False
            return True

        def _check_auth(self) -> bool:
            if not auth_token:
                return True
            auth_hdr = (self.headers.get("Authorization") or "").strip()
            expected = f"Bearer {auth_token}"
            return hmac.compare_digest(auth_hdr.encode("utf-8"), expected.encode("utf-8"))

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            origin = (self.headers.get("Origin") or "").strip() if self.headers else ""
            host_header = (self.headers.get("Host") or "").strip() if self.headers else ""
            if origin and _is_trusted_origin(origin, host_header):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:
            if not self._enforce_origin():
                return
            self.send_response(204)
            origin = (self.headers.get("Origin") or "").strip() if self.headers else ""
            host_header = (self.headers.get("Host") or "").strip() if self.headers else ""
            if origin and _is_trusted_origin(origin, host_header):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _evaluate_tiers(self, jpg_bytes: bytes, tier_mode: str) -> dict[str, Any]:
            out: dict[str, Any] = {}
            tiers_to_run = [("CPU Hybrid (ONNX)", "cpu"), ("GPU VLM (Qwen2.5-VL-7B 4-bit)", "gpu")] if tier_mode == "both" else [(f"{tier_mode.upper()} Mode", tier_mode)]
            for label, t_name in tiers_to_run:
                try:
                    res, escalated, fallback_reason = recognizer.predict([jpg_bytes], tier=t_name)
                    out[label] = {
                        "latency_ms": round(res.latency_ms, 1),
                        "dict": res.to_spoolwarden_dict(
                            escalated=escalated,
                            fallback_reason=fallback_reason,
                        ),
                        "xml": res.to_xml(),
                    }
                except Exception as exc:
                    out[label] = {"error": str(exc)}
            return out

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = PLAYGROUND_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if not self._check_auth():
                self._send_json(401, {"error": "Missing or invalid bearer token"})
                return

            if parsed.path == "/api/random_seed":
                qs = parse_qs(parsed.query)
                tier = (qs.get("tier", ["cpu"])[0]).lower()
                files = sorted(seed_dir.glob("*.HEIC")) if seed_dir.is_dir() else []
                if not files:
                    self._send_json(404, {"error": f"No .HEIC seed images found in {seed_dir}"})
                    return
                chosen = random.choice(files)
                try:
                    jpg_bytes = _decode_any_image_to_jpeg_bytes(chosen.read_bytes())
                    results = self._evaluate_tiers(jpg_bytes, tier)
                except Exception as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(
                    200,
                    {
                        "filename": chosen.name,
                        "preview_b64": base64.standard_b64encode(jpg_bytes).decode("ascii"),
                        "results": results,
                    },
                )
                return

            if parsed.path == "/api/random_harvested":
                qs = parse_qs(parsed.query)
                tier = (qs.get("tier", ["cpu"])[0]).lower()
                files: list[Path] = []
                for candidate_dir in (
                    PROJECT_ROOT / "data" / "harvested",
                    PROJECT_ROOT / "data" / "seed_converted",
                    PROJECT_ROOT / "data" / "feedback_verified",
                ):
                    if candidate_dir.is_dir():
                        files.extend(sorted(candidate_dir.glob("*.jpg")))
                        files.extend(sorted(candidate_dir.glob("*.png")))
                if not files:
                    self._send_json(404, {"error": "No sample images found under data/harvested, seed_converted, or feedback_verified"})
                    return
                chosen = random.choice(files)
                try:
                    jpg_bytes = _decode_any_image_to_jpeg_bytes(chosen.read_bytes())
                    results = self._evaluate_tiers(jpg_bytes, tier)
                except Exception as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(
                    200,
                    {
                        "filename": chosen.name,
                        "preview_b64": base64.standard_b64encode(jpg_bytes).decode("ascii"),
                        "results": results,
                    },
                )
                return

            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if not self._enforce_origin():
                return
            if not self._check_auth():
                self._send_json(401, {"error": "Missing or invalid bearer token"})
                return
            if urlparse(self.path).path != "/api/test":
                self._send_json(404, {"error": "Not found"})
                return
            if (self.headers.get("Transfer-Encoding") or "").strip():
                self._send_json(411, {"error": "Chunked Transfer-Encoding is not supported; provide Content-Length"})
                return
            raw_len = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw_len)
            except ValueError:
                self._send_json(400, {"error": f"Invalid Content-Length: {raw_len}"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(413 if length > MAX_BODY_BYTES else 400, {"error": f"Invalid request size: {length}"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                raw_bytes = _strip_data_uri(payload["image_b64"])
                tier = str(payload.get("tier") or "cpu").lower()
                fname = str(payload.get("filename") or "uploaded_image")
                jpg_bytes = _decode_any_image_to_jpeg_bytes(raw_bytes)
            except Exception as exc:
                self._send_json(400, {"error": f"Invalid test payload or image: {exc}"})
                return
            results = self._evaluate_tiers(jpg_bytes, tier)
            self._send_json(
                200,
                {
                    "filename": fname,
                    "preview_b64": base64.standard_b64encode(jpg_bytes).decode("ascii"),
                    "results": results,
                },
            )

    server = _DualStackThreadingHTTPServer((host, port), PlaygroundHandler)
    print(f"[Playground UI] Running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_playground_server()
