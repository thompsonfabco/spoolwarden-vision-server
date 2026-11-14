"""Self-contained dark-mode Web UI served at GET / and GET /ui by filament-vision-server."""
from __future__ import annotations

STANDALONE_WEB_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Filament Vision Server</title>
<style>
  :root {
    --bg: #0b1120;
    --panel: #111827;
    --panel-alt: #1e293b;
    --border: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --accent-hover: #0ea5e9;
    --green: #22c55e;
    --amber: #f59e0b;
    --red: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 20px;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  .container { max-width: 1320px; margin: 0 auto; }
  header {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 20px;
  }
  h1 { margin: 0; font-size: 22px; letter-spacing: -0.02em; }
  .subtitle { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 11px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: var(--panel-alt);
    border: 1px solid var(--border);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.ok { background: var(--green); }
  .dot.warn { background: var(--amber); }
  .dot.err { background: var(--red); }

  .grid {
    display: grid;
    grid-template-columns: minmax(310px, 410px) 1fr;
    gap: 20px;
    align-items: start;
  }
  @media (max-width: 920px) {
    .grid { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 20px;
  }
  .card h2 {
    margin: 0 0 12px 0;
    font-size: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .dropzone {
    border: 2px dashed var(--accent);
    border-radius: 10px;
    padding: 24px 14px;
    text-align: center;
    cursor: pointer;
    background: rgba(56, 189, 248, 0.05);
    transition: background 0.15s, border-color 0.15s;
  }
  .dropzone:hover, .dropzone.dragover {
    background: rgba(56, 189, 248, 0.14);
    border-color: #7dd3fc;
  }
  .thumbs {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .thumb-wrap {
    position: relative;
    width: 78px;
    height: 78px;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
    background: #050811;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    color: var(--muted);
  }
  .thumb-wrap img { width: 100%; height: 100%; object-fit: cover; }
  .thumb-remove {
    position: absolute;
    top: 2px;
    right: 2px;
    background: rgba(15, 23, 42, 0.85);
    color: #fff;
    border: none;
    border-radius: 50%;
    width: 18px;
    height: 18px;
    font-size: 11px;
    cursor: pointer;
    padding: 0;
    line-height: 18px;
  }
  .row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; align-items: center; }
  label.field-lbl { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  input[type="text"], input[type="number"], input[type="password"], select {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 13px;
  }
  input[type="color"] {
    width: 38px;
    height: 34px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    padding: 2px;
    cursor: pointer;
  }
  button {
    background: var(--panel-alt);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 13px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s;
  }
  button:hover:not(:disabled) { background: #334155; }
  button:disabled { opacity: 0.55; cursor: not-allowed; }
  button.primary {
    background: var(--accent);
    color: #0b1120;
    border: none;
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) { background: var(--accent-hover); }
  button.success {
    background: rgba(34, 197, 94, 0.16);
    color: #86efac;
    border: 1px solid rgba(34, 197, 94, 0.4);
  }
  button.warn {
    background: rgba(245, 158, 11, 0.15);
    color: #fcd34d;
    border: 1px solid rgba(245, 158, 11, 0.4);
  }
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
    gap: 12px;
  }
  .conf-badge {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 999px;
    margin-left: 4px;
    background: rgba(56, 189, 248, 0.16);
    color: #7dd3fc;
  }
  .status-line {
    margin-top: 10px;
    font-size: 13px;
    color: var(--accent);
    min-height: 20px;
  }
  .status-line.err { color: #fca5a5; }
  .toggle-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    margin-top: 10px;
  }
  th, td {
    text-align: left;
    padding: 8px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  .swatch-chip {
    display: inline-block;
    width: 14px;
    height: 14px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.35);
    vertical-align: middle;
    margin-right: 6px;
  }
  details pre {
    background: #050811;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px;
    font-size: 11px;
    overflow-x: auto;
    color: #bae6fd;
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Filament Vision Server</h1>
      <div class="subtitle">Standalone dual-tier ONNX + Qwen2.5-VL spool recognition server</div>
    </div>
    <div class="header-right">
      <div id="tokenWrap" style="display:none; width:190px;">
        <input type="password" id="authToken" placeholder="Bearer token (if enabled)">
      </div>
      <span class="pill"><span class="dot" id="healthDot"></span><span id="healthText">Checking server...</span></span>
      <span class="pill" id="feedbackPill">Feedback Queue: 0</span>
    </div>
  </header>

  <div class="grid">
    <!-- Left Column: Upload & Controls -->
    <div>
      <div class="card">
        <h2>1. Spool / Box Photo</h2>
        <div class="dropzone" id="dropzone">
          <strong>Click or Drag &amp; Drop Photo(s)</strong><br>
          <span style="color:var(--muted);font-size:12px">Supports .JPG, .PNG, .WEBP, .HEIC, .HEIF (up to 8 photos)</span>
          <input type="file" id="fileInput" accept=".jpg,.jpeg,.png,.webp,.heic,.heif,image/*" multiple style="display:none">
          <input type="file" id="cameraInput" accept="image/*" capture="environment" style="display:none">
        </div>
        <div class="row">
          <button type="button" onclick="document.getElementById('fileInput').click()">Choose Files</button>
          <button type="button" onclick="document.getElementById('cameraInput').click()">Take Photo</button>
          <button type="button" onclick="clearPhotos()">Clear</button>
        </div>
        <div class="thumbs" id="thumbs"></div>

        <div class="row" style="margin-top:16px;">
          <div style="flex:1; min-width:120px;">
            <label class="field-lbl" for="tierSelect">Inference Tier</label>
            <select id="tierSelect">
              <option value="auto">auto (CPU + GPU Escalation)</option>
              <option value="cpu">cpu (Fast ONNX + OCR)</option>
              <option value="gpu">gpu (Qwen2.5-VL-7B LoRA)</option>
            </select>
          </div>
        </div>
        <div class="row">
          <div style="flex:1;">
            <label class="field-lbl" for="hintInput">Optional Hint</label>
            <input type="text" id="hintInput" placeholder='e.g. "bare spool", "PETG", "Bambu Lab"'>
          </div>
        </div>
        <div class="row">
          <button type="button" class="primary" id="recognizeBtn" style="flex:1;" onclick="runRecognize()">Recognize Spool(s)</button>
        </div>
        <div class="status-line" id="statusLine"></div>
      </div>

      <div class="card">
        <h2>Training Feedback Queue</h2>
        <label class="toggle-row">
          <input type="checkbox" id="optInToggle" onchange="toggleOptIn(this.checked)">
          <span>Opt in to correction training feedback</span>
        </label>
        <div style="font-size:12px; color:var(--muted); margin-top:6px;">
          Saves EXIF-stripped photos + your corrected fields locally so you can download a training bundle or upload it.
        </div>
        <div class="row">
          <button type="button" id="uploadFbBtn" class="warn" onclick="submitFeedback()">Upload Feedback to Cloud</button>
          <button type="button" id="downloadFbBtn" onclick="downloadFeedbackZip()">Download Feedback .zip</button>
        </div>
      </div>
    </div>

    <!-- Right Column: Editable Result + Session Export -->
    <div>
      <div class="card">
        <h2>
          <span>2. Recognized Spool Details</span>
          <span id="readerMeta" style="font-size:12px; color:var(--muted); font-weight:400;"></span>
        </h2>

        <div id="itemTabs" class="row" style="margin-top:0; margin-bottom:10px; display:none;"></div>

        <div class="form-grid">
          <div>
            <label class="field-lbl">Vendor <span class="conf-badge" id="cfVendor"></span></label>
            <input type="text" id="fVendor">
          </div>
          <div>
            <label class="field-lbl">Material <span class="conf-badge" id="cfMaterial"></span></label>
            <input type="text" id="fMaterial">
          </div>
          <div>
            <label class="field-lbl">Product Line <span class="conf-badge" id="cfLine"></span></label>
            <input type="text" id="fLine">
          </div>
          <div>
            <label class="field-lbl">Color Name <span class="conf-badge" id="cfColor"></span></label>
            <input type="text" id="fColorName">
          </div>
          <div>
            <label class="field-lbl">Color Family</label>
            <input type="text" id="fColorFamily">
          </div>
          <div>
            <label class="field-lbl">Color Hex</label>
            <div style="display:flex; gap:6px; align-items:center;">
              <input type="color" id="fColorPicker" value="#1A1A1A" oninput="syncHexFromPicker(this.value)">
              <input type="text" id="fColorHex" placeholder="1A1A1A" maxlength="6" oninput="syncPickerFromHex(this.value)">
            </div>
          </div>
          <div>
            <label class="field-lbl">Net Weight (g)</label>
            <input type="number" id="fNet" value="1000">
          </div>
          <div>
            <label class="field-lbl">Tare Weight (g)</label>
            <input type="number" id="fTare" value="200">
          </div>
          <div>
            <label class="field-lbl">Diameter (mm)</label>
            <input type="number" id="fDiameter" step="0.01" value="1.75">
          </div>
          <div>
            <label class="field-lbl">Density (g/cm3)</label>
            <input type="number" id="fDensity" step="0.01" value="1.24">
          </div>
          <div>
            <label class="field-lbl">Nozzle Range (&deg;C)</label>
            <input type="text" id="fNozzleRange" placeholder="190-230">
          </div>
          <div>
            <label class="field-lbl">Bed Range (&deg;C)</label>
            <input type="text" id="fBedRange" placeholder="45-60">
          </div>
        </div>

        <div class="row">
          <label class="toggle-row">
            <input type="checkbox" id="fSealed">
            <span>Factory Sealed (check for in box / vacuum bag)</span>
            <span class="conf-badge" id="cfSealed"></span>
          </label>
          <span class="pill" id="overallConfPill" style="margin-left:auto;">Confidence: --</span>
        </div>

        <div class="row" style="margin-top:14px; border-top:1px solid var(--border); padding-top:12px;">
          <div style="flex:1; min-width:220px;">
            <input type="text" id="reviseNote" placeholder='Natural-language revise note, e.g. "brand is Polymaker, PolyLite PETG, bare spool"'>
          </div>
          <button type="button" id="reviseBtn" onclick="runRevise()">Revise</button>
        </div>

        <div class="row">
          <button type="button" class="success" onclick="addToSession()">Add to Session List</button>
          <button type="button" onclick="queueFeedback()">Queue Training Feedback</button>
        </div>

        <details style="margin-top:12px;">
          <summary style="cursor:pointer; font-size:12px; color:var(--muted);">Inspect Raw JSON / XML Response</summary>
          <pre id="rawInspect">No recognition run yet.</pre>
        </details>
      </div>

      <div class="card">
        <h2>
          <span>3. Spool Inventory Session List (<span id="sessionCount">0</span>)</span>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button type="button" onclick="exportJSON()">Export JSON</button>
            <button type="button" onclick="exportCSV()">Export CSV</button>
            <button type="button" onclick="exportXML()">Export XML</button>
            <button type="button" class="primary" onclick="exportSpoolman()">Export Spoolman JSON</button>
          </div>
        </h2>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Material</th>
                <th>Line</th>
                <th>Color</th>
                <th>Net / Tare</th>
                <th>State</th>
                <th>Conf</th>
                <th></th>
              </tr>
            </thead>
            <tbody id="sessionBody">
              <tr><td colspan="8" style="color:var(--muted); text-align:center;">No spools added yet. Recognize a photo and click "Add to Session List".</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const state = {
  photosB64: [],      // base64 without data-uri prefix
  photoPreviews: [],  // object URL or data URL for thumbnails
  photoNames: [],
  items: [],
  predictedItems: [],
  activeIdx: 0,
  lastResponse: null,
  sessionList: [],
  authRequired: false,
  endpointConfigured: false,
};

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  const tok = (document.getElementById("authToken").value || "").trim();
  if (tok) h["Authorization"] = "Bearer " + tok;
  return h;
}

function setStatus(msg, isErr = false) {
  const el = document.getElementById("statusLine");
  el.textContent = msg || "";
  el.className = isErr ? "status-line err" : "status-line";
}

function syncHexFromPicker(val) {
  document.getElementById("fColorHex").value = val.replace("#", "").toUpperCase();
}

function syncPickerFromHex(val) {
  const clean = (val || "").replace("#", "").trim();
  if (/^[0-9A-Fa-f]{6}$/.test(clean)) {
    document.getElementById("fColorPicker").value = "#" + clean;
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const idx = dataUrl.indexOf(",");
      resolve({
        dataUrl,
        b64: idx >= 0 ? dataUrl.slice(idx + 1) : dataUrl,
      });
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  for (const f of files) {
    if (state.photosB64.length >= 8) break;
    const { dataUrl, b64 } = await fileToBase64(f);
    state.photosB64.push(b64);
    state.photoNames.push(f.name);
    const isHeic = /\\.(heic|heif)$/i.test(f.name);
    state.photoPreviews.push(isHeic ? "" : dataUrl);
  }
  renderThumbs();
}

function clearPhotos() {
  state.photosB64 = [];
  state.photoPreviews = [];
  state.photoNames = [];
  renderThumbs();
}

function removePhoto(idx) {
  state.photosB64.splice(idx, 1);
  state.photoPreviews.splice(idx, 1);
  state.photoNames.splice(idx, 1);
  renderThumbs();
}

function renderThumbs() {
  const root = document.getElementById("thumbs");
  root.innerHTML = "";
  state.photoNames.forEach((name, idx) => {
    const wrap = document.createElement("div");
    wrap.className = "thumb-wrap";
    if (state.photoPreviews[idx]) {
      const img = document.createElement("img");
      img.src = state.photoPreviews[idx];
      img.alt = name;
      wrap.appendChild(img);
    } else {
      const sp = document.createElement("span");
      sp.textContent = name.slice(0, 12);
      wrap.appendChild(sp);
    }
    const rm = document.createElement("button");
    rm.className = "thumb-remove";
    rm.type = "button";
    rm.textContent = "x";
    rm.onclick = () => removePhoto(idx);
    wrap.appendChild(rm);
    root.appendChild(wrap);
  });
}

const dz = document.getElementById("dropzone");
dz.addEventListener("click", () => document.getElementById("fileInput").click());
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("dragover");
  handleFiles(e.dataTransfer.files);
});
document.getElementById("fileInput").addEventListener("change", (e) => handleFiles(e.target.files));
document.getElementById("cameraInput").addEventListener("change", (e) => handleFiles(e.target.files));

const tokEl = document.getElementById("authToken");
try { localStorage.removeItem("fvs_auth_token"); } catch (_) {}
tokEl.value = sessionStorage.getItem("fvs_auth_token") || "";
tokEl.addEventListener("change", () => {
  sessionStorage.setItem("fvs_auth_token", tokEl.value.trim());
  refreshHealthAndFeedback();
});

async function refreshHealthAndFeedback() {
  try {
    const r = await fetch("/v1/health");
    const h = await r.json();
    const dot = document.getElementById("healthDot");
    dot.className = "dot " + (h.status === "ok" ? "ok" : "warn");
    const gpuStr = h.tiers?.gpu?.usable ? "GPU ready" : "CPU mode";
    document.getElementById("healthText").textContent = `${h.status.toUpperCase()} v${h.module_version} (${gpuStr})`;
    state.authRequired = !!h.auth_required;
    document.getElementById("tokenWrap").style.display = state.authRequired ? "block" : "none";
  } catch (err) {
    document.getElementById("healthDot").className = "dot err";
    document.getElementById("healthText").textContent = "Server offline";
  }

  try {
    const r = await fetch("/v1/feedback", { headers: authHeaders() });
    if (r.ok) {
      const fb = await r.json();
      document.getElementById("optInToggle").checked = !!fb.opt_in;
      state.endpointConfigured = !!fb.endpoint_configured;
      document.getElementById("feedbackPill").textContent = `Feedback Queue: ${fb.count || 0}`;
      document.getElementById("uploadFbBtn").disabled = !(fb.opt_in && fb.endpoint_configured && (fb.count || 0) > 0);
      document.getElementById("downloadFbBtn").disabled = (fb.count || 0) === 0;
    }
  } catch (_) {}
}

function populateForm(item) {
  if (!item) return;
  document.getElementById("fVendor").value = item.vendor || "";
  document.getElementById("fMaterial").value = item.material || "";
  document.getElementById("fLine").value = item.line || "";
  document.getElementById("fColorName").value = item.color_name || "";
  document.getElementById("fColorFamily").value = item.color_family || "";
  const hex = (item.color_hex || "1A1A1A").replace("#", "").toUpperCase();
  document.getElementById("fColorHex").value = hex;
  syncPickerFromHex(hex);
  document.getElementById("fNet").value = item.net ?? 1000;
  document.getElementById("fTare").value = item.tare ?? 200;
  document.getElementById("fDiameter").value = item.diameter ?? 1.75;
  document.getElementById("fDensity").value = item.density ?? 1.24;
  document.getElementById("fNozzleRange").value = item.nozzle_range || "";
  document.getElementById("fBedRange").value = item.bed_range || "";
  document.getElementById("fSealed").checked = !!item.sealed;

  const conf = item.confidence != null ? Math.round(item.confidence * 100) + "%" : "--";
  document.getElementById("overallConfPill").textContent = "Confidence: " + conf;
  const fc = item.field_confidence || {};
  document.getElementById("cfVendor").textContent = fc.vendor != null ? Math.round(fc.vendor * 100) + "%" : "";
  document.getElementById("cfMaterial").textContent = fc.material != null ? Math.round(fc.material * 100) + "%" : "";
  document.getElementById("cfLine").textContent = fc.line != null ? Math.round(fc.line * 100) + "%" : "";
  document.getElementById("cfColor").textContent = fc.color != null ? Math.round(fc.color * 100) + "%" : "";
  document.getElementById("cfSealed").textContent = fc.sealed != null ? Math.round(fc.sealed * 100) + "%" : "";
}

function readFormObject() {
  const line = document.getElementById("fLine").value.trim() || "PLA";
  const colorName = document.getElementById("fColorName").value.trim() || "unknown";
  const colorHex = document.getElementById("fColorHex").value.replace("#", "").trim().toUpperCase();
  const sealed = document.getElementById("fSealed").checked;
  const existing = state.items[state.activeIdx] || {};
  return {
    ...existing,
    vendor: document.getElementById("fVendor").value.trim() || "Unknown",
    material: document.getElementById("fMaterial").value.trim() || "PLA",
    line,
    color_name: colorName,
    name: (colorName && colorName.toLowerCase() !== "unknown") ? `${line} ${colorName}`.trim() : line,
    color_family: document.getElementById("fColorFamily").value.trim() || "Black",
    color_hex: colorHex || "1A1A1A",
    net: parseInt(document.getElementById("fNet").value || "1000", 10),
    tare: parseInt(document.getElementById("fTare").value || "200", 10),
    diameter: parseFloat(document.getElementById("fDiameter").value || "1.75"),
    density: parseFloat(document.getElementById("fDensity").value || "1.24"),
    nozzle_range: document.getElementById("fNozzleRange").value.trim() || null,
    bed_range: document.getElementById("fBedRange").value.trim() || null,
    sealed,
    sealed_reason: sealed ? (existing.sealed_reason === "in vacuum bag" ? "in vacuum bag" : "in box") : "bare spool",
  };
}

function renderItemTabs() {
  const tabs = document.getElementById("itemTabs");
  if (state.items.length <= 1) {
    tabs.style.display = "none";
    tabs.innerHTML = "";
    return;
  }
  tabs.style.display = "flex";
  tabs.innerHTML = "";
  state.items.forEach((it, idx) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = idx === state.activeIdx ? "primary" : "";
    b.textContent = `Item ${idx + 1}: ${it.vendor || "Unknown"} ${it.line || ""}`;
    b.onclick = () => {
      if (state.items[state.activeIdx]) {
        state.items[state.activeIdx] = readFormObject();
      }
      state.activeIdx = idx;
      renderItemTabs();
      populateForm(state.items[idx]);
    };
    tabs.appendChild(b);
  });
}

async function runRecognize() {
  if (!state.photosB64.length) {
    setStatus("Please select or drop at least one photo first.", true);
    return;
  }
  const btn = document.getElementById("recognizeBtn");
  btn.disabled = true;
  setStatus("Running recognition...");
  try {
    const res = await fetch("/v1/read", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        api_version: "1.0",
        tier: document.getElementById("tierSelect").value,
        hint: document.getElementById("hintInput").value.trim(),
        images_b64: state.photosB64,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.message || data.error?.message || `HTTP ${res.status}`);
    }
    state.lastResponse = data;
    state.items = data.items || [];
    state.predictedItems = (data.items || []).map(it => JSON.parse(JSON.stringify(it)));
    state.activeIdx = 0;
    renderItemTabs();
    populateForm(state.items[0]);
    const meta = data.reader || {};
    document.getElementById("readerMeta").textContent = `${meta.tier_used || "cpu"} | ${meta.latency_ms ?? 0} ms`;
    document.getElementById("rawInspect").textContent = JSON.stringify(data, null, 2);
    setStatus(`Recognized ${state.items.length} spool(s) in ${meta.latency_ms ?? 0} ms.`);
    refreshHealthAndFeedback();
  } catch (err) {
    setStatus("Recognition error: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function runRevise() {
  const note = document.getElementById("reviseNote").value.trim();
  if (!note) {
    setStatus("Enter a natural-language revise note first.", true);
    return;
  }
  const prev = readFormObject();
  try {
    const res = await fetch("/v1/revise", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ api_version: "1.0", previous: prev, note }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error?.message || `HTTP ${res.status}`);
    if (data.item) {
      state.items[state.activeIdx] = data.item;
      renderItemTabs();
      populateForm(data.item);
      document.getElementById("rawInspect").textContent = JSON.stringify(data, null, 2);
      setStatus("Item updated from revise note.");
    }
  } catch (err) {
    setStatus("Revise error: " + err.message, true);
  }
}

async function toggleOptIn(enabled) {
  try {
    const res = await fetch("/v1/config", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ api_version: "1.0", feedback_opt_in: !!enabled }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || `HTTP ${res.status}`);
    setStatus(`Training feedback opt-in ${data.feedback_opt_in ? "enabled" : "disabled"}.`);
    refreshHealthAndFeedback();
  } catch (err) {
    setStatus("Config update failed: " + err.message, true);
  }
}

async function queueFeedback() {
  if (!state.photosB64.length) {
    setStatus("No photos loaded to queue feedback with.", true);
    return;
  }
  if (!document.getElementById("optInToggle").checked) {
    setStatus("Please enable 'Opt in to correction training feedback' before queuing feedback.", true);
    return;
  }
  const predicted = state.predictedItems[state.activeIdx] || state.items[state.activeIdx] || {};
  const corrected = readFormObject();
  const note = document.getElementById("reviseNote").value.trim();
  try {
    const res = await fetch("/v1/feedback", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        api_version: "1.0",
        images_b64: state.photosB64,
        predicted,
        corrected,
        note,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error?.message || `HTTP ${res.status}`);
    setStatus(`Queued feedback entry ${data.id} (${data.queued_count} in queue).`);
    refreshHealthAndFeedback();
  } catch (err) {
    setStatus("Failed to queue feedback: " + err.message, true);
  }
}

async function submitFeedback() {
  try {
    setStatus("Uploading feedback bundle...");
    const res = await fetch("/v1/feedback/submit", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ api_version: "1.0" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error?.message || `HTTP ${res.status}`);
    const batchNote = (data.batches_submitted && data.batches_submitted > 1) ? ` in ${data.batches_submitted} batches` : "";
    setStatus(`Uploaded ${data.submitted_count} feedback entries${batchNote}.`);
    refreshHealthAndFeedback();
  } catch (err) {
    setStatus("Upload failed: " + err.message, true);
  }
}

async function downloadFeedbackZip() {
  try {
    const res = await fetch("/v1/feedback/bundle.zip", { headers: authHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    triggerDownload("filament-feedback-bundle.zip", blob);
  } catch (err) {
    setStatus("Download failed: " + err.message, true);
  }
}

function addToSession() {
  const obj = readFormObject();
  state.sessionList.push(obj);
  renderSessionTable();
  setStatus(`Added ${obj.vendor} ${obj.name} to session list.`);
}

function removeFromSession(idx) {
  state.sessionList.splice(idx, 1);
  renderSessionTable();
}

function renderSessionTable() {
  document.getElementById("sessionCount").textContent = String(state.sessionList.length);
  const tbody = document.getElementById("sessionBody");
  if (!state.sessionList.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted); text-align:center;">No spools added yet. Recognize a photo and click "Add to Session List".</td></tr>';
    return;
  }
  tbody.innerHTML = "";
  state.sessionList.forEach((it, idx) => {
    const tr = document.createElement("tr");
    const hex = "#" + (it.color_hex || "1A1A1A").replace("#", "");
    tr.innerHTML = `
      <td>${escapeHtml(it.vendor)}</td>
      <td>${escapeHtml(it.material)}</td>
      <td>${escapeHtml(it.line)}</td>
      <td><span class="swatch-chip" style="background:${escapeHtml(hex)}"></span>${escapeHtml(it.color_name)} (${escapeHtml(it.color_family)})</td>
      <td>${it.net}g / ${it.tare}g</td>
      <td>${it.sealed ? "Sealed" : "Open"}</td>
      <td>${it.confidence != null ? Math.round(it.confidence * 100) + "%" : "--"}</td>
      <td><button type="button" onclick="removeFromSession(${idx})">Remove</button></td>
    `;
    tbody.appendChild(tr);
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function triggerDownload(filename, contentOrBlob, mimeType = "application/octet-stream") {
  const blob = contentOrBlob instanceof Blob ? contentOrBlob : new Blob([contentOrBlob], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function activeExportItems() {
  if (state.sessionList.length) return state.sessionList;
  const cur = readFormObject();
  return (cur.vendor && cur.vendor !== "Unknown") || state.items.length ? [cur] : [];
}

function exportJSON() {
  const list = activeExportItems();
  if (!list.length) return setStatus("Nothing to export yet.", true);
  triggerDownload("spools.json", JSON.stringify(list, null, 2), "application/json");
}

function csvCell(v) {
  const s = String(v ?? "");
  return '"' + s.replace(/"/g, '""') + '"';
}

function exportCSV() {
  const list = activeExportItems();
  if (!list.length) return setStatus("Nothing to export yet.", true);
  const cols = ["vendor", "material", "line", "name", "color_name", "color_hex", "color_family", "net", "tare", "density", "diameter", "nozzle_range", "bed_range", "sealed", "confidence"];
  const lines = [cols.join(",")];
  for (const it of list) {
    lines.push(cols.map((c) => csvCell(it[c])).join(","));
  }
  triggerDownload("spools.csv", lines.join("\n") + "\n", "text/csv;charset=utf-8");
}

function escapeXml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function exportXML() {
  const list = activeExportItems();
  if (!list.length) return setStatus("Nothing to export yet.", true);
  let xml = '<?xml version="1.0" encoding="UTF-8"?>\n<spool-inventory version="1">\n';
  for (const it of list) {
    xml += `  <spool vendor="${escapeXml(it.vendor)}" material="${escapeXml(it.material)}" line="${escapeXml(it.line)}" sealed="${it.sealed ? "true" : "false"}" confidence="${it.confidence ?? 0}">\n`;
    xml += `    <name>${escapeXml(it.name)}</name>\n`;
    xml += `    <color name="${escapeXml(it.color_name)}" family="${escapeXml(it.color_family)}" hex="${escapeXml(it.color_hex)}" />\n`;
    xml += `    <specs net-grams="${it.net}" tare-grams="${it.tare}" density="${it.density}" diameter="${it.diameter || 1.75}" nozzle-range="${escapeXml(it.nozzle_range)}" bed-range="${escapeXml(it.bed_range)}" />\n`;
    xml += `  </spool>\n`;
  }
  xml += "</spool-inventory>\n";
  triggerDownload("spools.xml", xml, "application/xml");
}

function exportSpoolman() {
  const list = activeExportItems();
  if (!list.length) return setStatus("Nothing to export yet.", true);
  const vendorsMap = {};
  const filaments = [];
  const spools = [];
  list.forEach((it, idx) => {
    const vName = it.vendor || "Unknown";
    if (!vendorsMap[vName]) vendorsMap[vName] = { name: vName };
    let nozzleTemp = null;
    if (it.nozzle_range && String(it.nozzle_range).includes("-")) {
      const [a, b] = String(it.nozzle_range).split("-").map(Number);
      if (Number.isFinite(a) && Number.isFinite(b)) nozzleTemp = Math.round((a + b) / 2);
    }
    let bedTemp = null;
    if (it.bed_range && String(it.bed_range).includes("-")) {
      const [a, b] = String(it.bed_range).split("-").map(Number);
      if (Number.isFinite(a) && Number.isFinite(b)) bedTemp = Math.round((a + b) / 2);
    }
    const filament = {
      id: idx + 1,
      name: it.name,
      vendor: { name: vName },
      material: it.material,
      weight: it.net,
      spool_weight: it.tare,
      density: it.density,
      diameter: it.diameter || 1.75,
      color_hex: (it.color_hex || "1A1A1A").replace("#", ""),
      settings_extruder_temp: nozzleTemp,
      settings_bed_temp: bedTemp,
      extra: { line: it.line, color_family: it.color_family },
    };
    filaments.push(filament);
    spools.push({
      filament_id: idx + 1,
      filament,
      remaining_weight: it.net,
      used_weight: 0,
      spool_weight: it.tare,
      archived: false,
      comment: it.sealed ? "Sealed at intake" : "Opened spool",
    });
  });
  const payload = {
    schema: "spoolman-intake-export-v1",
    vendors: Object.values(vendorsMap),
    filaments,
    spools,
  };
  triggerDownload("spoolman-import.json", JSON.stringify(payload, null, 2), "application/json");
}

refreshHealthAndFeedback();
setInterval(refreshHealthAndFeedback, 15000);
</script>
</body>
</html>
"""
