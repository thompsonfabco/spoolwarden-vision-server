/**
 * Write-only Google Apps Script Web App for spoolwarden-vision-server user feedback bundles.
 *
 * Setup:
 *   1. Create a private quarantine folder in Google Drive (shared with NOBODY). Copy its folder ID.
 *   2. Go to https://script.google.com -> New Project -> paste this file.
 *   3. Set QUARANTINE_FOLDER_ID below to your private quarantine folder ID.
 *   4. Deploy -> New deployment -> Web app:
 *      - Execute as: Me
 *      - Who has access: Anyone
 *
 * Security:
 *   - Only doPost(e) is exposed; there is no doGet() listing or reading files.
 *   - Enforces a 25 MB decoded ZIP cap and requires a valid ZIP header (PK\x03\x04).
 *   - Validates package shape (exactly one pending_feedback.xml + JPEG files only at archive root).
 *   - Enforces a public-key blocklist and daily caps on upload count and total bytes.
 *   - Verifies proof-of-work: SHA-256(package_sha256 + public_key + pow_nonce) >= MIN_POW_DIFFICULTY leading zero bits.
 *   - Stores the ZIP and its <filename>.meta.json sidecar (public_key + Ed25519 signature + PoW)
 *     in the quarantine folder for offline verification by `spoolwarden-vision-server ingest-feedback`.
 */

const QUARANTINE_FOLDER_ID = "REPLACE_WITH_PRIVATE_QUARANTINE_FOLDER_ID";
const MAX_ZIP_BYTES = 25 * 1024 * 1024;
const MIN_POW_DIFFICULTY = 20;
const DAILY_MAX_COUNT = 200;
const DAILY_MAX_BYTES = 500 * 1024 * 1024;
const BLOCKED_PUBLIC_KEYS = [
  // Add base64 Ed25519 public keys here to block abusive installations
];

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResp({ ok: false, error: "Missing POST body" });
    }
    const body = JSON.parse(e.postData.contents);
    const b64 = String(body.zip_b64 || "");
    if (!b64) {
      return jsonResp({ ok: false, error: "Missing zip_b64" });
    }

    const pubKey = String(body.public_key || "").trim();
    const signature = String(body.signature || "").trim();
    const powNonce = String(body.pow_nonce !== undefined && body.pow_nonce !== null ? body.pow_nonce : "");
    const powDifficulty = Number(body.pow_difficulty || 0);
    if (!pubKey || !signature || !powNonce || !Number.isInteger(powDifficulty) || powDifficulty < MIN_POW_DIFFICULTY) {
      return jsonResp({ ok: false, error: "Missing or insufficient key, signature, or proof of work" });
    }

    if (isKeyBlocked(pubKey)) {
      return jsonResp({ ok: false, error: "Public key is blocked" });
    }

    const rawBytes = Utilities.base64Decode(b64);
    if (rawBytes.length === 0 || rawBytes.length > MAX_ZIP_BYTES) {
      return jsonResp({ ok: false, error: "Invalid or oversized ZIP payload" });
    }
    // Verify PKZIP magic bytes (0x50, 0x4B, 0x03, 0x04)
    if (rawBytes[0] !== 0x50 || rawBytes[1] !== 0x4b || rawBytes[2] !== 0x03 || rawBytes[3] !== 0x04) {
      return jsonResp({ ok: false, error: "Payload is not a valid ZIP archive" });
    }

    const pkgSha256 = sha256HexBytes(rawBytes);
    if (body.package_sha256 && String(body.package_sha256).trim().toLowerCase() !== pkgSha256) {
      return jsonResp({ ok: false, error: "package_sha256 mismatch" });
    }

    if (!verifyPow(pkgSha256, pubKey, powNonce, powDifficulty)) {
      return jsonResp({ ok: false, error: "Invalid proof of work" });
    }

    const safeName = String(body.filename || "filament-feedback.zip").replace(/[^A-Za-z0-9._-]/g, "_");
    const blob = Utilities.newBlob(rawBytes, "application/zip", safeName);

    // Validate package shape: one pending_feedback.xml + JPEG files only at archive root
    const entries = Utilities.unzip(blob);
    let xmlCount = 0;
    for (let i = 0; i < entries.length; i++) {
      const name = entries[i].getName();
      if (!name || name.indexOf("/") !== -1 || name.indexOf("\\") !== -1 || name.indexOf("..") !== -1) {
        return jsonResp({ ok: false, error: "Invalid archive entry path: " + name });
      }
      if (name === "pending_feedback.xml") {
        xmlCount++;
      } else if (!/\.(jpg|jpeg)$/i.test(name)) {
        return jsonResp({ ok: false, error: "Unexpected file type in archive: " + name });
      }
    }
    if (xmlCount !== 1) {
      return jsonResp({ ok: false, error: "Archive must contain exactly one pending_feedback.xml" });
    }

    // Enforce daily count and byte caps atomically
    const lock = LockService.getScriptLock();
    lock.waitLock(15000);
    try {
      const props = PropertiesService.getScriptProperties();
      const dayKey = new Date().toISOString().slice(0, 10);
      const countKey = "daily_count_" + dayKey;
      const bytesKey = "daily_bytes_" + dayKey;
      const curCount = Number(props.getProperty(countKey) || "0");
      const curBytes = Number(props.getProperty(bytesKey) || "0");
      if (curCount + 1 > DAILY_MAX_COUNT || curBytes + rawBytes.length > DAILY_MAX_BYTES) {
        return jsonResp({ ok: false, error: "Daily upload cap reached" });
      }

      const folderId = QUARANTINE_FOLDER_ID.replace(/.*\/folders\//, "").split(/[?#/]/)[0];
      const folder = DriveApp.getFolderById(folderId);
      const file = folder.createFile(blob);
      const meta = {
        filename: safeName,
        package_sha256: pkgSha256,
        public_key: pubKey,
        signature: signature,
        pow_nonce: powNonce,
        pow_difficulty: powDifficulty,
        received_at: new Date().toISOString(),
      };
      const metaFile = folder.createFile(safeName + ".meta.json", JSON.stringify(meta, null, 2), MimeType.PLAIN_TEXT);

      props.setProperty(countKey, String(curCount + 1));
      props.setProperty(bytesKey, String(curBytes + rawBytes.length));

      return jsonResp({ ok: true, file_id: file.getId(), meta_file_id: metaFile.getId(), bytes: rawBytes.length });
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return jsonResp({ ok: false, error: String(err) });
  }
}

function isKeyBlocked(pubKey) {
  if (BLOCKED_PUBLIC_KEYS.indexOf(pubKey) !== -1) {
    return true;
  }
  const rawProp = PropertiesService.getScriptProperties().getProperty("BLOCKED_PUBLIC_KEYS");
  if (rawProp) {
    try {
      const list = JSON.parse(rawProp);
      if (Array.isArray(list) && list.indexOf(pubKey) !== -1) {
        return true;
      }
    } catch (_e) {}
  }
  return false;
}

function sha256HexBytes(byteArr) {
  const digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, byteArr);
  let hex = "";
  for (let i = 0; i < digest.length; i++) {
    const b = (digest[i] + 256) % 256;
    hex += (b < 16 ? "0" : "") + b.toString(16);
  }
  return hex;
}

function verifyPow(pkgSha256, pubKey, nonce, difficulty) {
  const digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, pkgSha256 + pubKey + nonce);
  let bits = 0;
  for (let i = 0; i < digest.length; i++) {
    const b = (digest[i] + 256) % 256;
    if (b === 0) {
      bits += 8;
    } else {
      for (let mask = 0x80; mask > 0; mask >>= 1) {
        if ((b & mask) !== 0) break;
        bits++;
      }
      break;
    }
    if (bits >= difficulty) return true;
  }
  return bits >= difficulty;
}

function jsonResp(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
