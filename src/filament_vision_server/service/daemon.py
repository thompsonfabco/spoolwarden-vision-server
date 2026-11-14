"""Unified FilamentRecognizer SDK and cross-platform localhost HTTP daemon (127.0.0.1:7920).

Implements HTTP API contract v1 (see docs/api-contract.md).
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as StdET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.pipeline import CPUHybridPipeline
from ..gpu_tier.pipeline import GPUVLMPipeline
from ..xml_io import (
    CONFIGS_DIR,
    FilamentItem,
    IntakeReadResult,
    parse_xml_file,
    parse_xml_string,
    to_xml_string,
    write_xml_atomic,
)
from .feedback import DEFAULT_FEEDBACK_DIR, FeedbackQueue
from .web_ui import STANDALONE_WEB_UI_HTML

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7920
DEFAULT_SERVICE_CONFIG_XML = CONFIGS_DIR / "service_config.xml"
DEFAULT_FEEDBACK_ENDPOINT = (
    "https://script.google.com/macros/s/"
    "AKfycbzfxi1uUckO6BXKNdTZi9R-BfQyLpSa8cTWyCTBL-o1MGXyL89V1qLS-88ouOPbcep1/exec"
)

MODULE_VERSION = "0.2.1"
API_VERSION = "1.1"
MAX_PHOTOS = 8
MAX_PHOTO_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_BODY_BYTES = 64 * 1024 * 1024   # 64 MB


class TierUnavailableError(Exception):
    """Raised when an explicit tier (such as GPU) is requested but unavailable."""
    pass


class BusyError(Exception):
    """Raised when the GPU model is busy or queue capacity is reached."""
    pass


def load_service_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(DEFAULT_SERVICE_CONFIG_XML if path is None else path)
    default_endpoint = (
        os.environ.get("FILAMENT_VISION_SERVER_FEEDBACK_ENDPOINT")
        or os.environ.get("FILAMENT_KIT_VISION_FEEDBACK_ENDPOINT")
        or DEFAULT_FEEDBACK_ENDPOINT
    )
    env_token = (
        os.environ.get("FILAMENT_VISION_SERVER_TOKEN")
        or os.environ.get("FILAMENT_KIT_VISION_TOKEN")
        or ""
    )
    if not p.is_file():
        return {
            "tier": "auto",
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "idle_unload_seconds": 300,
            "token": env_token,
            "feedback_opt_in": False,
            "feedback_endpoint": default_endpoint,
        }
    try:
        root = parse_xml_file(p)
        return {
            "tier": root.attrib.get("tier", "auto"),
            "host": root.attrib.get("host", DEFAULT_HOST),
            "port": int(root.attrib.get("port", str(DEFAULT_PORT))),
            "idle_unload_seconds": int(root.attrib.get("idle-unload-seconds", "300")),
            "token": root.attrib.get("token", env_token),
            "feedback_opt_in": root.attrib.get("feedback-opt-in", "false").lower() == "true",
            "feedback_endpoint": root.attrib.get("feedback-endpoint", default_endpoint),
        }
    except Exception:
        return {
            "tier": "auto",
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "idle_unload_seconds": 300,
            "token": env_token,
            "feedback_opt_in": False,
            "feedback_endpoint": default_endpoint,
        }


def save_service_config(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    target = Path(DEFAULT_SERVICE_CONFIG_XML if path is None else path)
    attrs = {
        "version": "1",
        "tier": str(cfg.get("tier", "auto")),
        "host": str(cfg.get("host", DEFAULT_HOST)),
        "port": str(cfg.get("port", DEFAULT_PORT)),
        "idle-unload-seconds": str(cfg.get("idle_unload_seconds", 300)),
        "feedback-opt-in": "true" if cfg.get("feedback_opt_in") else "false",
    }
    if cfg.get("token"):
        attrs["token"] = str(cfg["token"])
    if cfg.get("feedback_endpoint"):
        attrs["feedback-endpoint"] = str(cfg["feedback_endpoint"])
    root = StdET.Element("service-config", attrib=attrs)
    return write_xml_atomic(target, root)


class FilamentRecognizer:
    """Unified recognition engine supporting 'cpu', 'gpu', and 'auto' tiers."""

    def __init__(
        self,
        default_tier: str = "auto",
        idle_unload_seconds: float = 300.0,
        auth_token: str = "",
        feedback_opt_in: bool = False,
        feedback_endpoint: str = DEFAULT_FEEDBACK_ENDPOINT,
        feedback_dir: str | Path = DEFAULT_FEEDBACK_DIR,
    ):
        self.default_tier = default_tier.lower()
        self.cpu_pipeline = CPUHybridPipeline()
        self.matcher = CatalogMatcher()
        self._gpu_pipeline: GPUVLMPipeline | None = None
        self.idle_unload_seconds = idle_unload_seconds
        self.auth_token = auth_token.strip()
        self.feedback_opt_in = bool(feedback_opt_in)
        self.feedback_endpoint = feedback_endpoint.strip()
        self.feedback_queue = FeedbackQueue(queue_dir=feedback_dir)
        self._gpu_available: bool | None = None
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.last_read_at: str | None = None
        self._lock = threading.Lock()
        self._active_reads = 0

    def gpu_available(self) -> bool:
        if self._gpu_available is None:
            if self.default_tier == "cpu":
                self._gpu_available = False
                return False
            try:
                import torch

                self._gpu_available = bool(torch.cuda.is_available())
            except Exception:
                self._gpu_available = False
        return self._gpu_available

    def gpu_loaded(self) -> bool:
        if self._gpu_pipeline is None:
            return False
        return self._gpu_pipeline._model is not None

    def _get_gpu_pipeline(self) -> GPUVLMPipeline:
        if self._gpu_pipeline is None:
            self._gpu_pipeline = GPUVLMPipeline(idle_unload_seconds=self.idle_unload_seconds)
        return self._gpu_pipeline

    def predict(
        self,
        images: list[str | Path | bytes],
        tier: str | None = None,
        hint: str = "",
        spoolman_filaments: list[dict[str, Any]] | None = None,
        spoolman_vendors: list[dict[str, Any]] | None = None,
    ) -> tuple[IntakeReadResult, bool, str | None]:
        """Run prediction. Returns (IntakeReadResult, escalated: bool, fallback_reason: str | None)."""
        chosen = (tier or self.default_tier or "auto").lower()
        escalated = False
        fallback_reason: str | None = None

        with self._lock:
            if self._active_reads >= 4:
                raise BusyError("Queue limit reached (4 active reads)")
            self._active_reads += 1

        try:
            if chosen == "gpu":
                if not self.gpu_available():
                    raise TierUnavailableError("GPU tier was explicitly requested but no CUDA device is available")
                res = self._get_gpu_pipeline().predict(images)
            elif chosen == "auto":
                res = self.cpu_pipeline.predict(images, hint=hint)
                needs_escalation = any(
                    it.vendor == "Unknown" or it.confidence < 0.78 for it in res.items
                )
                if needs_escalation:
                    if self.gpu_available():
                        try:
                            priors = res.items if len(res.items) == len(images) else None
                            res = self._get_gpu_pipeline().predict(images, cpu_priors=priors)
                            escalated = True
                        except Exception as exc:
                            log.warning("GPU escalation failed, keeping CPU result: %s", exc)
                            fallback_reason = f"GPU escalation failed: {exc}"
                    else:
                        fallback_reason = "GPU unavailable for escalation"
            else:
                res = self.cpu_pipeline.predict(images, hint=hint)

            if hint.strip():
                for it in res.items:
                    self._apply_note_to_item(it, hint.strip())
                res.questions = [
                    f"Photo {it.photos[0] if it.photos else 0}: Please confirm filament brand (detected {it.line} {it.color_name})."
                    for it in res.items
                    if it.vendor == "Unknown"
                ]

            # Match against live Spoolman catalog when provided
            if spoolman_filaments:
                for it in res.items:
                    for f in spoolman_filaments:
                        f_vendor = ((f.get("vendor") or {}).get("name") or "").strip()
                        f_name = (f.get("name") or "").strip()
                        if (
                            f_vendor.lower() == it.vendor.lower()
                            and (
                                f_name.lower() == it.name.lower()
                                or f_name.lower() == f"{it.line} {it.color_name}".lower()
                            )
                        ):
                            it.existing_filament_id = f.get("id")
                            it.vendor = f_vendor or it.vendor
                            if f.get("spool_weight"):
                                it.tare_grams = int(float(f["spool_weight"]))
                            if f.get("density"):
                                it.density = float(f["density"])
                            if f.get("color_hex"):
                                it.color_hex = str(f["color_hex"]).lstrip("#").upper()
                            break

            self.last_read_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return res, escalated, fallback_reason
        finally:
            with self._lock:
                self._active_reads -= 1

    def _apply_note_to_item(self, item: FilamentItem, note_clean: str) -> None:
        """Apply explicit user hint/revision note fields onto item and re-enrich from catalog."""
        if not note_clean:
            return
        for v_name in self.matcher.vendors.values():
            if re.search(rf"\b{re.escape(v_name)}\b", note_clean, re.IGNORECASE):
                item.vendor = v_name
                break

        for mat_code in self.matcher._sorted_material_aliases:
            if re.search(rf"\b{re.escape(mat_code)}\b", note_clean, re.IGNORECASE):
                new_mat = self.matcher.material_aliases[mat_code]
                if new_mat != item.material or new_mat.lower() not in (item.line or "").lower():
                    item.material = new_mat
                    item.line = new_mat
                    item.nozzle_range = None
                    item.bed_range = None
                else:
                    item.material = new_mat
                break

        for sub in ("Matte", "Silk", "Plus", "PLA+", "HF", "Pro", "Rapid", "High Speed", "Galaxy", "Marble", "Wood", "Glow", "Translucent", "Basic"):
            if re.search(rf"\b{re.escape(sub)}\b", note_clean, re.IGNORECASE):
                if sub in ("Plus", "PLA+"):
                    item.line = f"{item.material}+" if item.material == "PLA" else f"{item.material} Plus"
                else:
                    item.line = f"{item.material} {sub}"
                break

        nl = note_clean.lower()
        if any(w in nl for w in ("bare", "opened", "open spool", "unsealed")):
            item.sealed = False
            item.sealed_reason = "bare spool"
        elif "in box" in nl or "boxed" in nl:
            item.sealed = True
            item.sealed_reason = "in box"
        elif "vacuum" in nl or "bag" in nl or "sealed" in nl:
            item.sealed = True
            item.sealed_reason = "in vacuum bag"

        self.matcher.enrich_item(item)

    def revise(
        self,
        previous: dict[str, Any],
        note: str,
        spoolman_filaments: list[dict[str, Any]] | None = None,
        spoolman_vendors: list[dict[str, Any]] | None = None,
    ) -> IntakeReadResult:
        """Revise an intake item based on user notes and catalog constraints."""
        item = FilamentItem(
            vendor=previous.get("vendor", "Unknown"),
            material=previous.get("material", "PLA"),
            line=previous.get("line", previous.get("material", "PLA")),
            color_name=previous.get("color_name", "unknown"),
            color_family=previous.get("color_family", "Black"),
            color_hex=previous.get("color_hex"),
            net_grams=int(previous.get("net", 1000)),
            tare_grams=int(previous.get("tare", 200)),
            density=float(previous.get("density", 1.24)),
            count=int(previous.get("count", 1)),
            nozzle=previous.get("nozzle"),
            bed=previous.get("bed"),
            nozzle_range=previous.get("nozzle_range"),
            bed_range=previous.get("bed_range"),
            lot=previous.get("lot"),
            sealed=bool(previous.get("sealed", False)),
            sealed_reason=str(previous.get("sealed_reason", "bare spool")),
            existing_filament_id=previous.get("existing_filament_id"),
            confidence=0.95,
            note=f"Revised by user note: '{note}'. Previously: {previous.get('vendor')} {previous.get('line')}",
            photos=previous.get("photos", [0]),
        )

        note_clean = (note or "").strip()
        if note_clean:
            self._apply_note_to_item(item, note_clean)

        self.last_read_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return IntakeReadResult(
            items=[item],
            tier="revise",
            model_name="filament-vision-server/revise",
            latency_ms=5.0,
        )


class _DaemonRequestHandler(BaseHTTPRequestHandler):
    recognizer: FilamentRecognizer

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)

    def _send_raw(
        self,
        code: int,
        body: str | bytes,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self._send_raw(204, b"", content_type="text/plain; charset=utf-8")

    def _send_json(self, code: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        self._send_raw(code, json.dumps(payload), "application/json; charset=utf-8", extra_headers=extra_headers)

    def _send_error(self, code: int, error_code: str, message: str, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        body = {
            "error": {
                "code": error_code,
                "message": message,
            },
            "code": error_code,
            "message": message,
        }
        self._send_json(code, body, extra_headers=headers)

    def _check_auth(self) -> bool:
        if not self.recognizer.auth_token:
            return True
        auth_hdr = (self.headers.get("Authorization") or "").strip()
        expected = f"Bearer {self.recognizer.auth_token}"
        return auth_hdr == expected

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/ui"):
            self._send_raw(200, STANDALONE_WEB_UI_HTML, "text/html; charset=utf-8")
            return

        if parsed.path in ("/v1/health", "/health"):
            gpu_ok = self.recognizer.gpu_available()
            default_tier = self.recognizer.default_tier
            is_degraded = (default_tier == "gpu" and not gpu_ok)
            status_str = "degraded" if is_degraded else "ok"

            payload = {
                "service": "filament-vision-server",
                "api_version": API_VERSION,
                "module_version": MODULE_VERSION,
                "status": status_str,
                "default_tier": default_tier,
                "tiers": {
                    "cpu": {"usable": True, "reason": ""},
                    "gpu": {
                        "usable": gpu_ok,
                        "reason": "" if gpu_ok else "no CUDA device",
                        "loaded": self.recognizer.gpu_loaded(),
                    },
                },
                "auth_required": bool(self.recognizer.auth_token),
                "limits": {
                    "max_photos": MAX_PHOTOS,
                    "max_photo_bytes": MAX_PHOTO_BYTES,
                    "max_body_bytes": MAX_BODY_BYTES,
                },
                "started_at": self.recognizer.started_at,
                "last_read_at": self.recognizer.last_read_at,
                "revise": True,
                # Legacy keys for backwards compatibility
                "tier": default_tier,
                "gpu_available": gpu_ok,
            }
            self._send_json(200, payload)
            return

        if parsed.path in ("/v1/config", "/config"):
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            self._send_json(
                200,
                {
                    "api_version": API_VERSION,
                    "tier": self.recognizer.default_tier,
                    "idle_unload_seconds": int(self.recognizer.idle_unload_seconds),
                    "feedback_opt_in": self.recognizer.feedback_opt_in,
                    "endpoint_configured": bool(self.recognizer.feedback_endpoint),
                },
            )
            return

        if parsed.path in ("/v1/feedback", "/feedback"):
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            st = self.recognizer.feedback_queue.status()
            self._send_json(
                200,
                {
                    "api_version": API_VERSION,
                    "opt_in": self.recognizer.feedback_opt_in,
                    "endpoint_configured": bool(self.recognizer.feedback_endpoint),
                    **st,
                },
            )
            return

        if parsed.path in ("/v1/feedback/bundle.zip", "/feedback/bundle.zip"):
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            try:
                zip_bytes = self.recognizer.feedback_queue.build_bundle_zip()
            except ValueError as exc:
                self._send_error(404, "empty_feedback_queue", str(exc))
                return
            self._send_raw(
                200,
                zip_bytes,
                "application/zip",
                extra_headers={"Content-Disposition": 'attachment; filename="filament-feedback-bundle.zip"'},
            )
            return

        self._send_error(404, "not_found", f"Unknown route: {parsed.path}")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/feedback/") or parsed.path.startswith("/feedback/"):
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            entry_id = parsed.path.rsplit("/", 1)[-1].strip()
            if not entry_id:
                self._send_error(400, "bad_request", "Missing feedback entry id")
                return
            if not self.recognizer.feedback_queue.delete(entry_id):
                self._send_error(404, "not_found", f"Feedback entry not found: {entry_id}")
                return
            st = self.recognizer.feedback_queue.status()
            self._send_json(200, {"api_version": API_VERSION, "ok": True, "deleted_id": entry_id, "queued_count": st["count"], "bundle_mb": st["bundle_mb"]})
            return
        self._send_error(404, "not_found", f"Unknown route: {parsed.path}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in (
            "/v1/read",
            "/read",
            "/v1/revise",
            "/revise",
            "/v1/config",
            "/config",
            "/v1/feedback",
            "/feedback",
            "/v1/feedback/submit",
            "/feedback/submit",
        ):
            self._send_error(404, "not_found", f"Unknown route: {parsed.path}")
            return

        # Check Authorization
        if not self._check_auth():
            self._send_error(401, "unauthorized", "Missing or invalid bearer token")
            return

        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            self._send_error(413, "too_large", f"Body size {length} exceeds maximum limit of {MAX_BODY_BYTES} bytes")
            return

        raw = self.rfile.read(length) if length > 0 else b""

        # Check JSON encoding
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_error(400, "bad_request", "Request body must be valid application/json; charset=utf-8")
            return

        if not isinstance(body, dict):
            self._send_error(400, "bad_request", "Request JSON body must be an object")
            return

        # Check API version
        req_api_ver = str(body.get("api_version", "1.0"))
        try:
            major_ver = int(req_api_ver.split(".")[0])
            if major_ver > 1:
                self._send_error(400, "unsupported_api_version", f"Unsupported API major version: {req_api_ver}")
                return
        except ValueError:
            self._send_error(400, "bad_request", f"Invalid api_version format: {req_api_ver}")
            return

        # Handle POST /v1/config
        if parsed.path in ("/v1/config", "/config"):
            cfg = load_service_config()
            if "tier" in body and body["tier"] is not None:
                tier_val = str(body["tier"]).lower()
                if tier_val not in ("auto", "cpu", "gpu"):
                    self._send_error(400, "bad_request", f"Invalid tier: {body['tier']}")
                    return
                self.recognizer.default_tier = tier_val
                self.recognizer._gpu_available = None
                cfg["tier"] = tier_val
            if "idle_unload_seconds" in body and body["idle_unload_seconds"] is not None:
                try:
                    idle_sec = int(body["idle_unload_seconds"])
                    if idle_sec < 0:
                        raise ValueError("must be >= 0")
                except Exception:
                    self._send_error(400, "bad_request", f"Invalid idle_unload_seconds: {body['idle_unload_seconds']}")
                    return
                self.recognizer.idle_unload_seconds = float(idle_sec)
                if self.recognizer._gpu_pipeline is not None:
                    self.recognizer._gpu_pipeline.idle_unload_seconds = float(idle_sec)
                cfg["idle_unload_seconds"] = idle_sec
            if "feedback_opt_in" in body and body["feedback_opt_in"] is not None:
                opt_in = bool(body["feedback_opt_in"])
                self.recognizer.feedback_opt_in = opt_in
                cfg["feedback_opt_in"] = opt_in
            if "feedback_endpoint" in body and body["feedback_endpoint"] is not None:
                ep = str(body["feedback_endpoint"]).strip()
                self.recognizer.feedback_endpoint = ep
                cfg["feedback_endpoint"] = ep
            save_service_config(cfg)
            self._send_json(
                200,
                {
                    "api_version": API_VERSION,
                    "ok": True,
                    "tier": self.recognizer.default_tier,
                    "feedback_opt_in": self.recognizer.feedback_opt_in,
                    "idle_unload_seconds": int(self.recognizer.idle_unload_seconds),
                    "endpoint_configured": bool(self.recognizer.feedback_endpoint),
                },
            )
            return

        # Handle POST /v1/feedback
        if parsed.path in ("/v1/feedback", "/feedback"):
            if not self.recognizer.feedback_opt_in:
                self._send_error(403, "feedback_disabled", "Training feedback opt-in is disabled")
                return
            try:
                res = self.recognizer.feedback_queue.enqueue(
                    images_b64=body.get("images_b64") or [],
                    predicted=body.get("predicted") or {},
                    corrected=body.get("corrected") or {},
                    note=str(body.get("note") or ""),
                )
                self._send_json(200, {"api_version": API_VERSION, **res})
            except ValueError as exc:
                self._send_error(400, "bad_request", str(exc))
            except Exception as exc:
                log.exception("Feedback enqueue failed")
                self._send_error(500, "feedback_failed", str(exc))
            return

        # Handle POST /v1/feedback/submit
        if parsed.path in ("/v1/feedback/submit", "/feedback/submit"):
            if not self.recognizer.feedback_opt_in:
                self._send_error(403, "feedback_disabled", "Training feedback opt-in is disabled")
                return
            endpoint = str(body.get("endpoint") or self.recognizer.feedback_endpoint or "").strip()
            try:
                res = self.recognizer.feedback_queue.submit(endpoint_url=endpoint)
                self._send_json(200, {"api_version": API_VERSION, **res})
            except ValueError as exc:
                self._send_error(400, "bad_request", str(exc))
            except Exception as exc:
                log.exception("Feedback submit failed")
                self._send_error(502, "feedback_upload_failed", str(exc))
            return

        # Handle POST /v1/revise
        if parsed.path in ("/v1/revise", "/revise"):
            previous = body.get("previous")
            if not isinstance(previous, dict):
                self._send_error(400, "bad_request", "POST /v1/revise requires a 'previous' object")
                return
            note = str(body.get("note", ""))
            try:
                res = self.recognizer.revise(
                    previous=previous,
                    note=note,
                    spoolman_filaments=body.get("filaments"),
                    spoolman_vendors=body.get("vendors"),
                )
                fk_dict = res.to_filament_kit_dict()
                item_dict = fk_dict["items"][0] if fk_dict.get("items") else {}
                resp = {
                    "api_version": API_VERSION,
                    "item": item_dict,
                    "items": [item_dict],
                    "reader": fk_dict.get("reader", {}),
                    "usage": fk_dict.get("usage", {}),
                }
                self._send_json(200, resp)
                return
            except Exception as exc:
                log.exception("Revise failed")
                self._send_error(500, "read_failed", f"Revise failed: {exc}")
                return

        # Handle POST /v1/read
        images_b64 = body.get("images_b64")
        if not images_b64 or not isinstance(images_b64, list):
            self._send_error(400, "no_photos", "images_b64 is required and must contain at least one photo")
            return

        if len(images_b64) > MAX_PHOTOS:
            self._send_error(400, "too_many_photos", f"Too many photos: {len(images_b64)} exceeds limit of {MAX_PHOTOS}")
            return

        decoded_images: list[bytes] = []
        for idx, b64_str in enumerate(images_b64):
            if not isinstance(b64_str, str):
                self._send_error(400, "bad_request", f"Photo at index {idx} must be a base64 string")
                return
            try:
                raw_bytes = base64.b64decode(b64_str)
            except Exception as exc:
                self._send_error(415, "bad_image", f"Failed to base64 decode photo index {idx}: {exc}")
                return

            if len(raw_bytes) > MAX_PHOTO_BYTES:
                self._send_error(413, "too_large", f"Photo at index {idx} ({len(raw_bytes)} bytes) exceeds limit of {MAX_PHOTO_BYTES} bytes")
                return

            decoded_images.append(raw_bytes)

        req_tier = body.get("tier")
        hint = str(body.get("hint", ""))
        spoolman_filaments = body.get("filaments")
        spoolman_vendors = body.get("vendors")

        try:
            res, escalated, fallback_reason = self.recognizer.predict(
                decoded_images,
                tier=req_tier,
                hint=hint,
                spoolman_filaments=spoolman_filaments,
                spoolman_vendors=spoolman_vendors,
            )
            fk_dict = res.to_filament_kit_dict(
                escalated=escalated,
                fallback_reason=fallback_reason,
            )
            self._send_json(200, fk_dict)
        except TierUnavailableError as tue:
            self._send_error(503, "tier_unavailable", str(tue))
        except BusyError as be:
            self._send_error(503, "busy", str(be), retry_after=5)
        except Exception as exc:
            log.exception("Read failed")
            self._send_error(500, "read_failed", f"Inference execution failed: {exc}")


def start_daemon(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    tier: str = "auto",
    idle_unload_seconds: float = 300.0,
    auth_token: str = "",
    feedback_opt_in: bool = False,
    feedback_endpoint: str = DEFAULT_FEEDBACK_ENDPOINT,
    feedback_dir: str | Path = DEFAULT_FEEDBACK_DIR,
) -> ThreadingHTTPServer:
    """Create and return a configured ThreadingHTTPServer for filament-vision-server."""
    recognizer = FilamentRecognizer(
        default_tier=tier,
        idle_unload_seconds=idle_unload_seconds,
        auth_token=auth_token,
        feedback_opt_in=feedback_opt_in,
        feedback_endpoint=feedback_endpoint,
        feedback_dir=feedback_dir,
    )

    class Handler(_DaemonRequestHandler):
        pass

    Handler.recognizer = recognizer
    server = ThreadingHTTPServer((host, port), Handler)
    return server


def run_daemon_forever(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    tier: str = "auto",
    idle_unload_seconds: float = 300.0,
    auth_token: str = "",
    feedback_opt_in: bool = False,
    feedback_endpoint: str = DEFAULT_FEEDBACK_ENDPOINT,
    feedback_dir: str | Path = DEFAULT_FEEDBACK_DIR,
) -> None:
    server = start_daemon(
        host=host,
        port=port,
        tier=tier,
        idle_unload_seconds=idle_unload_seconds,
        auth_token=auth_token,
        feedback_opt_in=feedback_opt_in,
        feedback_endpoint=feedback_endpoint,
        feedback_dir=feedback_dir,
    )

    def _sweeper() -> None:
        while True:
            time.sleep(30.0)
            if server.RequestHandlerClass.recognizer._gpu_pipeline is not None:
                server.RequestHandlerClass.recognizer._gpu_pipeline.unload_if_idle()

    t = threading.Thread(target=_sweeper, daemon=True)
    t.start()
    print(f"[filament-vision-server] Listening on http://{host}:{port} (tier={tier})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


class LocalIntakeClient:
    """Client for filament-kit's intake.py to query the local daemon."""

    def __init__(
        self,
        base_url: str = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
        timeout: float = 30.0,
        token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token.strip()
        self._inproc_fallback: FilamentRecognizer | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.base_url}/v1/health", headers=self._headers())
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def read_labels(
        self,
        images: list[bytes],
        tier: str = "auto",
        hint: str = "",
        filaments: list[dict[str, Any]] | None = None,
        vendors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "api_version": API_VERSION,
            "tier": tier,
            "images_b64": [base64.standard_b64encode(img).decode("ascii") for img in images],
            "hint": hint,
            "filaments": filaments,
            "vendors": vendors,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/read",
            data=data,
            method="POST",
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def revise(
        self,
        previous: dict[str, Any],
        note: str,
        filaments: list[dict[str, Any]] | None = None,
        vendors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "api_version": API_VERSION,
            "previous": previous,
            "note": note,
            "filaments": filaments,
            "vendors": vendors,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/revise",
            data=data,
            method="POST",
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
