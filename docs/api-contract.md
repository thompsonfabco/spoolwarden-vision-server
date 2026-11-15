# Filament Vision Server HTTP REST API (`v1`, `api_version: "1.1"`)

Default bind address: `http://127.0.0.1:7920`.

## Security, CORS & Authentication

- **CORS & Origin Gating:** `OPTIONS`, `POST`, and `DELETE` requests carrying an `Origin` header must match loopback (`http(s)://localhost`, `127.0.0.1`, `::1`) or `FILAMENT_VISION_SERVER_ALLOWED_ORIGINS`. Untrusted `Origin` headers receive `403 forbidden_origin`.
- **Bearer Authentication:** When `FILAMENT_VISION_SERVER_TOKEN` (or `token` in `configs/service_config.xml`) is configured, every API route except `GET /`, `GET /ui`, and `GET /v1/health` requires `Authorization: Bearer <token>`, returning `401 unauthorized` otherwise.
- **Request Framing:** All `POST` endpoints require `Content-Length` (`<= 67,108,864` bytes / 64 MB) and JSON body `application/json; charset=utf-8`. `Transfer-Encoding: chunked` is rejected with `411 length_required`.
- **Image Safety Limits:** Individual decoded photos on `/v1/read` are bounded by `MAX_PHOTO_BYTES` (20 MB), `MAX_PHOTOS` (8), `MAX_DECODE_SIDE` (10,000 px), and `MAX_DECODE_PIXELS` (36,000,000 px). Oversized images return `413 too_large` before pixel buffer decompression.

## Endpoints

### `GET /`, `GET /ui`
Serves the built-in dark-mode Web UI HTML page (`text/html; charset=utf-8`). Unauthenticated.

### `GET /v1/health` (alias `/health`)
Returns daemon health, version, tier capabilities, auth requirement, and request limits:
- `service`: `"filament-vision-server"`
- `api_version`: `"1.1"`
- `module_version`: `"0.2.4"`
- `status`: `"ok"` or `"degraded"` (if `default_tier == "gpu"` and CUDA is unavailable)
- `tiers`: `{ "cpu": {...}, "gpu": { "usable": bool, "reason": str, "loaded": bool } }`
- `auth_required`: `bool`
- `limits`: `{ "max_photos": 8, "max_photo_bytes": 20971520, "max_body_bytes": 67108864 }`

### `GET /v1/config` (alias `/config`)
Returns `{ "api_version": "1.1", "tier", "idle_unload_seconds", "feedback_opt_in", "endpoint_configured" }`.

### `POST /v1/config` (alias `/config`)
Updates runtime config and persists `configs/service_config.xml`. Accepts optional keys:
- `tier`: `"auto" | "cpu" | "gpu"`
- `idle_unload_seconds`: non-negative integer
- `feedback_opt_in`: boolean
- `feedback_endpoint`: HTTPS URL (public IP only; rejects RFC1918/link-local/reserved IPs) or explicit loopback URL

Remote clients require Bearer authentication (`403 forbidden_config`), and unauthenticated custom non-loopback `feedback_endpoint` updates are rejected (`403 forbidden_endpoint`).

### `POST /v1/read` (alias `/read`)
Recognizes 1–8 filament spool or box images.
- **Request JSON:**
  ```json
  {
    "api_version": "1.1",
    "tier": "auto",
    "images_b64": ["..."],
    "hint": "",
    "filaments": [],
    "vendors": []
  }
  ```
- **Response (`200`):**
  ```json
  {
    "api_version": "1.1",
    "items": [ { "vendor": "...", "material": "...", "line": "...", "color_name": "...", "color_hex": "..." } ],
    "questions": [],
    "reader": { "service": "filament-vision-server", "module_version": "0.2.4", "tier_used": "cpu", "escalated": false, "fallback_reason": null, "model": "...", "latency_ms": 120.5 },
    "usage": { "model": "filament-vision-server/cpu", "input_tokens": 0, "cached": 0, "output_tokens": 0 }
  }
  ```

### `POST /v1/revise` (alias `/revise`)
Applies natural-language correction `note` to `previous` item and re-enriches from `filament_catalog.xml` and optional Spoolman `filaments` / `vendors`. Returns `{ "api_version": "1.1", "item": {...}, "items": [{...}], "reader": {...}, "usage": {...} }`.

### `GET /v1/feedback` (alias `/feedback`)
Returns `{ "api_version": "1.1", "opt_in": bool, "endpoint_configured": bool, "count": int, "bundle_mb": float, "entries": [...] }`.

### `POST /v1/feedback` (alias `/feedback`)
Requires `feedback_opt_in == true` (`403 feedback_disabled` otherwise). Strips EXIF/ICC/APP markers, resizes images to `<= 1024 px`, and enqueues `(images_b64, predicted, corrected, note)`.

### `DELETE /v1/feedback/<id>`
Deletes queued feedback entry `<id>` and removes its stored images (`404 not_found` if absent).

### `POST /v1/feedback/submit` (alias `/feedback/submit`)
Splits queued corrections into `<= 25 MB` `.zip` batches and POSTs each batch fail-closed (`ok === true` JSON required) to `feedback_endpoint` (or loopback / authenticated `endpoint` override), deleting uploaded entries per succeeded batch.

### `GET /v1/feedback/bundle.zip`
Downloads an uncapped `.zip` archive (`filament-feedback-bundle.zip`) containing all queued entries (`404 empty_feedback_queue` when empty).

## Standard Error Format
```json
{
  "error": { "code": "<error_code>", "message": "<human readable description>" },
  "code": "<error_code>",
  "message": "<human readable description>"
}
```
Status codes: `400`, `401`, `403`, `404`, `411`, `413`, `415`, `500`, `502`, `503`.
