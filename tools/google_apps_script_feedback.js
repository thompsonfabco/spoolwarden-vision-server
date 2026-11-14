/**
 * Write-only Google Apps Script Web App for filament-kit-vision user feedback bundles.
 *
 * Setup:
 *   1. Create a private folder in Google Drive (shared with NOBODY). Copy its folder ID from the URL.
 *   2. Go to https://script.google.com -> New Project -> paste this file.
 *   3. Set FOLDER_ID below to your private folder ID.
 *   4. Deploy -> New deployment -> Web app:
 *      - Execute as: Me
 *      - Who has access: Anyone
 *   5. Configure the deployed /exec URL in service_config.xml (feedback-endpoint) or FILAMENT_KIT_VISION_FEEDBACK_ENDPOINT.
 *
 * Security:
 *   - Only doPost(e) is exposed; there is no doGet() listing or reading files.
 *   - Uploaders cannot view, list, or download anyone else's submissions.
 *   - Enforces a 25 MB decoded ZIP cap and requires a valid ZIP header (PK\x03\x04).
 */

const FOLDER_ID = "REPLACE_WITH_PRIVATE_DRIVE_FOLDER_ID";
const MAX_ZIP_BYTES = 25 * 1024 * 1024;

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
    const rawBytes = Utilities.base64Decode(b64);
    if (rawBytes.length === 0 || rawBytes.length > MAX_ZIP_BYTES) {
      return jsonResp({ ok: false, error: "Invalid or oversized ZIP payload" });
    }
    // Verify PKZIP magic bytes (0x50, 0x4B, 0x03, 0x04)
    if (rawBytes[0] !== 0x50 || rawBytes[1] !== 0x4b || rawBytes[2] !== 0x03 || rawBytes[3] !== 0x04) {
      return jsonResp({ ok: false, error: "Payload is not a valid ZIP archive" });
    }

    const safeName = String(body.filename || "filament-feedback.zip").replace(/[^A-Za-z0-9._-]/g, "_");
    const blob = Utilities.newBlob(rawBytes, "application/zip", safeName);
    const folderId = FOLDER_ID.replace(/.*\/folders\//, "").split(/[?#/]/)[0];
    const folder = DriveApp.getFolderById(folderId);
    const file = folder.createFile(blob);

    return jsonResp({ ok: true, file_id: file.getId(), bytes: rawBytes.length });
  } catch (err) {
    return jsonResp({ ok: false, error: String(err) });
  }
}

function jsonResp(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
