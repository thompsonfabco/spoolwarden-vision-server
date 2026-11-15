"""Unified FilamentRecognizer SDK and cross-platform localhost HTTP daemon (127.0.0.1:7920).

Implements HTTP API contract v1 (see docs/api-contract.md).
"""
from __future__ import annotations

import base64
import datetime
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as StdET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import UnidentifiedImageError

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.pipeline import CPUHybridPipeline, apply_note_to_item
from ..gpu_tier.pipeline import GPUVLMPipeline
from ..xml_io import (
    CONFIGS_DIR,
    SERVICE_CONFIG_PATH,
    STATE_DIR,
    FilamentItem,
    IntakeReadResult,
    parse_xml_file,
    parse_xml_string,
    to_xml_string,
    write_xml_atomic,
)
from .feedback import (
    DEFAULT_FEEDBACK_DIR,
    DEFAULT_POW_DIFFICULTY,
    FeedbackQueue,
    _strip_data_uri,
    _validate_upload_url,
    ensure_feedback_keypair,
)
from .web_ui import STANDALONE_WEB_UI_HTML

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7920
DEFAULT_SERVICE_CONFIG_XML = SERVICE_CONFIG_PATH
DEFAULT_FEEDBACK_ENDPOINT = (
    "https://script.google.com/macros/s/"
    "AKfycbzfxi1uUckO6BXKNdTZi9R-BfQyLpSa8cTWyCTBL-o1MGXyL89V1qLS-88ouOPbcep1/exec"
)

MODULE_VERSION = "0.3.0"
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


_CONFIG_LOCK = threading.RLock()


def _is_cpu_only_env() -> bool:
    raw = (
        os.environ.get("FILAMENT_VISION_SERVER_CPU_ONLY")
        or os.environ.get("FILAMENT_KIT_VISION_CPU_ONLY")
        or ""
    )
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _format_url_host(host: str) -> str:
    h = (host or "").strip()
    if ":" in h and not h.startswith("["):
        return f"[{h}]"
    return h


def _is_trusted_origin(origin: str, host_header: str = "") -> bool:
    o = (origin or "").strip()
    if not o:
        return True
    parsed = urlparse(o)
    if parsed.scheme in ("http", "https"):
        if (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}:
            return True
        if host_header and parsed.netloc.lower() == host_header.strip().lower():
            return True
    allowed_env = os.environ.get("FILAMENT_VISION_SERVER_ALLOWED_ORIGINS", "")
    if allowed_env:
        allowed_set = {a.strip().rstrip("/").lower() for a in allowed_env.split(",") if a.strip()}
        if o.rstrip("/").lower() in allowed_set:
            return True
    return False


def _is_loopback_client(client_ip: str) -> bool:
    raw = (client_ip or "").strip().split("%")[0]
    if not raw:
        return False
    try:
        ip = ipaddress.ip_address(raw)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return mapped.is_loopback
        return ip.is_loopback
    except ValueError:
        return raw.lower() in {"127.0.0.1", "localhost", "::1"}


def load_service_config(path: str | Path | None = None, include_env: bool = True) -> dict[str, Any]:
    p = Path(DEFAULT_SERVICE_CONFIG_XML if path is None else path)
    if include_env:
        env_endpoint = (
            os.environ.get("FILAMENT_VISION_SERVER_FEEDBACK_ENDPOINT")
            or os.environ.get("FILAMENT_KIT_VISION_FEEDBACK_ENDPOINT")
            or ""
        ).strip()
        env_token = (
            os.environ.get("FILAMENT_VISION_SERVER_TOKEN")
            or os.environ.get("FILAMENT_KIT_VISION_TOKEN")
            or ""
        ).strip()
        env_host = os.environ.get("FILAMENT_VISION_SERVER_HOST", "").strip()
        env_port_raw = os.environ.get("FILAMENT_VISION_SERVER_PORT", "").strip()
        env_tier = os.environ.get("FILAMENT_VISION_SERVER_TIER", "").strip().lower()
    else:
        env_endpoint = ""
        env_token = ""
        env_host = ""
        env_port_raw = ""
        env_tier = ""

    default_endpoint = env_endpoint or DEFAULT_FEEDBACK_ENDPOINT

    def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
        if env_host:
            cfg["host"] = env_host
        if env_port_raw:
            try:
                cfg["port"] = int(env_port_raw)
            except ValueError:
                log.warning("Ignoring invalid FILAMENT_VISION_SERVER_PORT=%r", env_port_raw)
        if env_tier in ("auto", "cpu", "gpu"):
            cfg["tier"] = env_tier
        return cfg

    with _CONFIG_LOCK:
        if not p.is_file():
            return _apply_env_overrides(
                {
                    "tier": "auto",
                    "host": DEFAULT_HOST,
                    "port": DEFAULT_PORT,
                    "idle_unload_seconds": 300,
                    "token": env_token,
                    "feedback_opt_in": False,
                    "feedback_endpoint": default_endpoint,
                }
            )
        try:
            root = parse_xml_file(p)
            if env_endpoint:
                ep_val = env_endpoint
            elif "feedback-endpoint" in root.attrib:
                ep_val = root.attrib["feedback-endpoint"].strip()
            else:
                ep_val = default_endpoint
            return _apply_env_overrides(
                {
                    "tier": root.attrib.get("tier", "auto"),
                    "host": root.attrib.get("host", DEFAULT_HOST),
                    "port": int(root.attrib.get("port", str(DEFAULT_PORT))),
                    "idle_unload_seconds": int(root.attrib.get("idle-unload-seconds", "300")),
                    "token": env_token or root.attrib.get("token", ""),
                    "feedback_opt_in": root.attrib.get("feedback-opt-in", "false").lower() == "true",
                    "feedback_endpoint": ep_val,
                }
            )
        except Exception as exc:
            log.warning("Failed to load %s, using defaults: %s", p, exc)
            return _apply_env_overrides(
                {
                    "tier": "auto",
                    "host": DEFAULT_HOST,
                    "port": DEFAULT_PORT,
                    "idle_unload_seconds": 300,
                    "token": env_token,
                    "feedback_opt_in": False,
                    "feedback_endpoint": default_endpoint,
                }
            )


def save_service_config(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    target = Path(DEFAULT_SERVICE_CONFIG_XML if path is None else path)
    ep = str(cfg.get("feedback_endpoint") or "").strip()
    attrs = {
        "version": "1",
        "tier": str(cfg.get("tier", "auto")),
        "host": str(cfg.get("host", DEFAULT_HOST)),
        "port": str(cfg.get("port", DEFAULT_PORT)),
        "idle-unload-seconds": str(cfg.get("idle_unload_seconds", 300)),
        "feedback-opt-in": "true" if cfg.get("feedback_opt_in") else "false",
        "feedback-endpoint": ep,
    }
    if cfg.get("token"):
        attrs["token"] = str(cfg["token"])
    root = StdET.Element("service-config", attrib=attrs)
    with _CONFIG_LOCK:
        written = write_xml_atomic(target, root)
        if cfg.get("token") and os.name != "nt":
            try:
                os.chmod(written, 0o600)
            except OSError:
                pass
        return written


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
        pow_difficulty: int = DEFAULT_POW_DIFFICULTY,
    ):
        self.default_tier = default_tier.lower()
        self.cpu_pipeline = CPUHybridPipeline()
        self.matcher = CatalogMatcher()
        self._gpu_pipeline: GPUVLMPipeline | None = None
        self.idle_unload_seconds = idle_unload_seconds
        self.auth_token = auth_token.strip()
        self.feedback_opt_in = bool(feedback_opt_in)
        self.feedback_endpoint = feedback_endpoint.strip()
        fb_path = Path(feedback_dir)
        state_dir = STATE_DIR if fb_path == Path(DEFAULT_FEEDBACK_DIR) else fb_path.parent
        self.feedback_queue = FeedbackQueue(queue_dir=fb_path, state_dir=state_dir)
        self.pow_difficulty = int(pow_difficulty)
        if self.feedback_opt_in:
            try:
                ensure_feedback_keypair(self.feedback_queue.state_dir)
            except Exception as exc:
                log.warning("Could not initialize feedback Ed25519 keypair on startup: %s", exc)
        self._gpu_available: bool | None = None
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.last_read_at = None
        self._lock = threading.Lock()
        self._gpu_init_lock = threading.Lock()
        self._active_reads = 0

    def cpu_accelerator(self) -> str:
        """Return the active hardware accelerator name for the CPU/ONNX tier."""
        providers: list[str] = []
        for sess in (self.cpu_pipeline.visual_session, self.cpu_pipeline.detector_session):
            if sess is not None:
                try:
                    providers = list(sess.get_providers())
                    if providers:
                        break
                except Exception:
                    pass
        if not providers:
            return "none" if self.cpu_pipeline.ocr is None else "cpu"
        top = providers[0]
        if top == "CUDAExecutionProvider":
            return "cuda"
        if top == "DmlExecutionProvider":
            return "directml"
        return "cpu"

    def gpu_available(self) -> bool:
        if _is_cpu_only_env():
            return False
        if self._gpu_available is None:
            try:
                import torch

                self._gpu_available = bool(torch.cuda.is_available())
            except Exception:
                self._gpu_available = False
        return self._gpu_available

    def gpu_loaded(self) -> bool:
        pipe = self._gpu_pipeline
        return bool(pipe is not None and getattr(pipe, "_model", None) is not None)

    def _get_gpu_pipeline(self) -> GPUVLMPipeline:
        pipe = self._gpu_pipeline
        if pipe is None:
            with self._gpu_init_lock:
                pipe = self._gpu_pipeline
                if pipe is None:
                    pipe = GPUVLMPipeline(
                        idle_unload_seconds=self.idle_unload_seconds,
                        cpu_helper=self.cpu_pipeline,
                        matcher=self.matcher,
                    )
                    self._gpu_pipeline = pipe
        return pipe

    def _apply_hint_to_result(self, res: IntakeReadResult, hint: str) -> None:
        hint_clean = (hint or "").strip()
        if not hint_clean:
            return
        for it in res.items:
            was_unknown = it.vendor == "Unknown"
            self._apply_note_to_item(it, hint_clean)
            if was_unknown and it.vendor != "Unknown":
                it.confidence = max(it.confidence, 0.85)
        non_brand_q = [q for q in res.questions if "confirm filament brand" not in q.lower()]
        brand_q = [
            f"Photo {it.photos[0] if it.photos else 0}: Please confirm filament brand (detected {it.line} {it.color_name})."
            for it in res.items
            if it.vendor == "Unknown"
        ]
        res.questions = non_brand_q + brand_q

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
        if chosen not in ("auto", "cpu", "gpu"):
            raise ValueError(f"Unsupported tier '{chosen}'; must be one of 'auto', 'cpu', 'gpu'")
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
                res = self._get_gpu_pipeline().predict(images, hint=hint)
                self._apply_hint_to_result(res, hint)
            elif chosen == "auto":
                res = self.cpu_pipeline.predict(images, hint=hint)
                self._apply_hint_to_result(res, hint)
                needs_escalation = any(
                    it.vendor == "Unknown" or it.confidence < 0.78 for it in res.items
                )
                if needs_escalation:
                    if self.gpu_available():
                        try:
                            prior_by_photo = {p: it for it in res.items for p in it.photos}
                            priors = (
                                [prior_by_photo.get(i) for i in range(len(images))]
                                if prior_by_photo
                                else None
                            )
                            res = self._get_gpu_pipeline().predict(images, cpu_priors=priors, hint=hint)
                            self._apply_hint_to_result(res, hint)
                            escalated = True
                        except Exception as exc:
                            log.warning("GPU escalation failed, keeping CPU result: %s", exc)
                            fallback_reason = f"GPU escalation failed: {exc}"
                    else:
                        fallback_reason = "GPU unavailable for escalation"
            else:
                res = self.cpu_pipeline.predict(images, hint=hint)
                self._apply_hint_to_result(res, hint)

            # Match against live Spoolman catalog when provided
            self._match_spoolman_catalog(res.items, spoolman_filaments, spoolman_vendors)

            self.last_read_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return res, escalated, fallback_reason
        finally:
            with self._lock:
                self._active_reads -= 1

    def _match_spoolman_catalog(
        self,
        items: list[FilamentItem],
        spoolman_filaments: list[dict[str, Any]] | None,
        spoolman_vendors: list[dict[str, Any]] | None = None,
    ) -> None:
        if spoolman_vendors:
            vendor_map = {
                (v.get("name") or "").strip().lower(): (v.get("name") or "").strip()
                for v in spoolman_vendors
                if isinstance(v, dict) and (v.get("name") or "").strip()
            }
            for it in items:
                canon = vendor_map.get(it.vendor.strip().lower())
                if canon:
                    it.vendor = canon
        if not spoolman_filaments:
            return
        for it in items:
            for f in spoolman_filaments:
                if not isinstance(f, dict):
                    continue
                raw_v = f.get("vendor")
                if isinstance(raw_v, dict):
                    f_vendor = str(raw_v.get("name") or "").strip()
                elif isinstance(raw_v, str):
                    f_vendor = raw_v.strip()
                else:
                    f_vendor = ""
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
                    if f.get("spool_weight") is not None:
                        it.tare_grams = int(float(f["spool_weight"]))
                    if f.get("density") is not None:
                        it.density = float(f["density"])
                    if f.get("color_hex"):
                        it.color_hex = str(f["color_hex"]).lstrip("#").upper()
                    break

    def _apply_note_to_item(self, item: FilamentItem, note_clean: str) -> None:
        """Apply explicit user hint/revision note fields onto item and re-enrich from catalog."""
        apply_note_to_item(item, note_clean, self.matcher)

    def revise(
        self,
        previous: dict[str, Any],
        note: str | None,
        spoolman_filaments: list[dict[str, Any]] | None = None,
        spoolman_vendors: list[dict[str, Any]] | None = None,
    ) -> IntakeReadResult:
        """Revise an intake item based on user notes and catalog constraints."""
        def _coerce_int(val: Any, default: int, field_name: str, min_val: int = 0) -> int:
            if val is None or val == "":
                return default
            if isinstance(val, bool):
                raise ValueError(f"previous.{field_name} must be an integer, got boolean")
            try:
                fval = float(val)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"previous.{field_name} must be numeric: {val!r}") from exc
            if not math.isfinite(fval):
                raise ValueError(f"previous.{field_name} must be finite: {val!r}")
            ival = int(fval)
            if ival < min_val:
                raise ValueError(f"previous.{field_name} must be >= {min_val}, got {ival}")
            return ival

        def _coerce_opt_int(val: Any, field_name: str, min_val: int = 0) -> int | None:
            if val is None or val == "":
                return None
            return _coerce_int(val, 0, field_name, min_val=min_val)

        def _coerce_float(val: Any, default: float, field_name: str, min_val: float = 0.0) -> float:
            if val is None or val == "":
                return default
            if isinstance(val, bool):
                raise ValueError(f"previous.{field_name} must be a number, got boolean")
            try:
                fval = float(val)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"previous.{field_name} must be numeric: {val!r}") from exc
            if not math.isfinite(fval) or fval <= min_val:
                raise ValueError(f"previous.{field_name} must be finite and > {min_val}, got {val!r}")
            return fval

        raw_photos = previous.get("photos")
        if raw_photos is None:
            photos_list = [0]
        elif isinstance(raw_photos, list) and all(isinstance(p, int) and not isinstance(p, bool) for p in raw_photos):
            photos_list = list(raw_photos) if raw_photos else [0]
        else:
            raise ValueError("previous.photos must be a list of integers")

        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string")
        note_clean = (note or "").strip()

        reader_extra = previous.get("reader_extra")
        extra_dict = reader_extra if isinstance(reader_extra, dict) else {}
        source_type = str(extra_dict.get("source_type") or previous.get("source_type") or "spool_label")
        hex_source = str(
            extra_dict.get("hex_source")
            or previous.get("color_hex_source")
            or ("catalog" if previous.get("color_hex") else "estimate")
        )

        item = FilamentItem(
            vendor=str(previous.get("vendor") or "Unknown"),
            material=str(previous.get("material") or "PLA"),
            line=str(previous.get("line") or previous.get("material") or "PLA"),
            color_name=str(previous.get("color_name") or "unknown"),
            color_family=str(previous.get("color_family") or "Black"),
            color_hex=previous.get("color_hex"),
            color_hex_source=hex_source,
            net_grams=_coerce_int(previous.get("net"), 1000, "net", min_val=0),
            tare_grams=_coerce_int(previous.get("tare"), 200, "tare", min_val=0),
            density=_coerce_float(previous.get("density"), 1.24, "density", min_val=0.0),
            diameter=_coerce_float(previous.get("diameter"), 1.75, "diameter", min_val=0.0),
            count=_coerce_int(previous.get("count"), 1, "count", min_val=1),
            nozzle=_coerce_opt_int(previous.get("nozzle"), "nozzle", min_val=1),
            bed=_coerce_opt_int(previous.get("bed"), "bed", min_val=0),
            nozzle_range=previous.get("nozzle_range"),
            bed_range=previous.get("bed_range"),
            lot=previous.get("lot"),
            sealed=bool(previous.get("sealed", False)),
            sealed_reason=str(previous.get("sealed_reason") or "bare spool"),
            existing_filament_id=previous.get("existing_filament_id"),
            source_type=source_type,
            confidence=0.95,
            note=f"Revised by user note: '{note_clean}'. Previously: {previous.get('vendor')} {previous.get('line')}",
            photos=photos_list,
        )

        if note_clean:
            self._apply_note_to_item(item, note_clean)

        self._match_spoolman_catalog([item], spoolman_filaments, spoolman_vendors)

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
        origin = (self.headers.get("Origin") or "").strip() if self.headers else ""
        host_header = (self.headers.get("Host") or "").strip() if self.headers else ""
        if origin and _is_trusted_origin(origin, host_header):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _enforce_origin(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip() if self.headers else ""
        host_header = (self.headers.get("Host") or "").strip() if self.headers else ""
        if origin and not _is_trusted_origin(origin, host_header):
            self._send_error(403, "forbidden_origin", f"Untrusted Origin header: {origin}")
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self._enforce_origin():
            return
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
        return hmac.compare_digest(auth_hdr.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/ui"):
            self._send_raw(200, STANDALONE_WEB_UI_HTML, "text/html; charset=utf-8")
            return

        if parsed.path in ("/v1/health", "/health"):
            gpu_ok = self.recognizer.gpu_available()
            cpu_pipe = self.recognizer.cpu_pipeline
            cpu_ok = bool(
                cpu_pipe.detector_session is not None
                or cpu_pipe.visual_session is not None
                or cpu_pipe.ocr is not None
            )
            cpu_reason = "" if cpu_ok else "no CPU ONNX models or OCR engine loaded"
            gpu_reason = (
                ""
                if gpu_ok
                else (
                    "disabled by FILAMENT_VISION_SERVER_CPU_ONLY"
                    if _is_cpu_only_env()
                    else "no CUDA device"
                )
            )
            default_tier = self.recognizer.default_tier
            is_degraded = (not cpu_ok) or (default_tier == "gpu" and not gpu_ok)
            status_str = "degraded" if is_degraded else "ok"
            fb_st = self.recognizer.feedback_queue.status()

            payload = {
                "service": "filament-vision-server",
                "api_version": API_VERSION,
                "module_version": MODULE_VERSION,
                "status": status_str,
                "default_tier": default_tier,
                "capabilities": {
                    "feedback": True,
                    "revise": True,
                },
                "feedback": {
                    "opt_in": self.recognizer.feedback_opt_in,
                    "endpoint_configured": bool(self.recognizer.feedback_endpoint),
                    "count": fb_st["count"],
                    "bundle_mb": fb_st["bundle_mb"],
                },
                "tiers": {
                    "cpu": {
                        "usable": cpu_ok,
                        "reason": cpu_reason,
                        "accelerator": self.recognizer.cpu_accelerator(),
                    },
                    "gpu": {
                        "usable": gpu_ok,
                        "reason": gpu_reason,
                        "loaded": self.recognizer.gpu_loaded(),
                        "accelerator": "cuda" if gpu_ok else "none",
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

        if parsed.path in ("/v1/feedback/submit", "/feedback/submit"):
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            job = self.recognizer.feedback_queue.submit_status()
            self._send_json(200, {"api_version": API_VERSION, "ok": True, **job, "job": job})
            return

        m_img = re.fullmatch(r"(?:/v1)?/feedback/([A-Za-z0-9_-]+)/image", parsed.path)
        if m_img:
            if not self._check_auth():
                self._send_error(401, "unauthorized", "Missing or invalid bearer token")
                return
            qs = parse_qs(parsed.query)
            raw_idx = (qs.get("index") or ["0"])[0]
            try:
                img_idx = int(raw_idx)
            except ValueError:
                self._send_error(400, "bad_request", f"Invalid image index: {raw_idx!r}")
                return
            img_bytes = self.recognizer.feedback_queue.get_entry_image(m_img.group(1), index=img_idx)
            if img_bytes is None:
                self._send_error(404, "not_found", f"Feedback image not found for entry '{m_img.group(1)}'")
                return
            self._send_raw(200, img_bytes, "image/jpeg")
            return

        self._send_error(404, "not_found", f"Unknown route: {parsed.path}")

    def do_DELETE(self) -> None:
        if not self._enforce_origin():
            return
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
        if not self._enforce_origin():
            return
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

        if (self.headers.get("Transfer-Encoding") or "").strip():
            self._send_error(411, "length_required", "Chunked Transfer-Encoding is not supported; provide Content-Length")
            return

        if "Content-Length" not in self.headers:
            self._send_error(411, "length_required", "Content-Length header is required")
            return

        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send_error(415, "unsupported_media_type", "Content-Type must be application/json")
            return

        raw_len = (self.headers.get("Content-Length") or "").strip()
        try:
            length = int(raw_len)
        except ValueError:
            self._send_error(400, "bad_request", f"Invalid Content-Length header: {raw_len}")
            return
        if length < 0:
            self._send_error(400, "bad_request", f"Negative Content-Length header: {length}")
            return
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
        req_api_ver = body.get("api_version", "1.1")
        if not isinstance(req_api_ver, str) or not re.fullmatch(r"\d+\.\d+", req_api_ver.strip()):
            self._send_error(400, "bad_request", f"Invalid api_version format: {req_api_ver!r}")
            return
        major_ver = int(req_api_ver.strip().split(".", 1)[0])
        if major_ver != 1:
            self._send_error(400, "unsupported_api_version", f"Unsupported API major version: {req_api_ver}")
            return

        # Handle POST /v1/config
        if parsed.path in ("/v1/config", "/config"):
            client_ip = self.client_address[0] if self.client_address else ""
            if not _is_loopback_client(client_ip) and not self.recognizer.auth_token:
                self._send_error(403, "forbidden_config", "Remote configuration changes require authentication")
                return
            with _CONFIG_LOCK:
                if "feedback_endpoint" in body and body["feedback_endpoint"] is not None:
                    if not isinstance(body["feedback_endpoint"], str):
                        self._send_error(400, "bad_request", "feedback_endpoint must be a string")
                        return
                    ep_cand = body["feedback_endpoint"].strip()
                    if ep_cand != self.recognizer.feedback_endpoint:
                        ep_host = (urlparse(ep_cand).hostname or "").lower().strip("[]") if ep_cand else ""
                        is_loopback = ep_host in {"127.0.0.1", "localhost", "::1"}
                        if ep_cand:
                            if not is_loopback and ep_cand != DEFAULT_FEEDBACK_ENDPOINT and not self.recognizer.auth_token:
                                self._send_error(
                                    403,
                                    "forbidden_endpoint",
                                    "Custom feedback endpoint update requires authentication or loopback target",
                                )
                                return
                            try:
                                _validate_upload_url(ep_cand)
                            except ValueError as exc:
                                self._send_error(400, "bad_request", str(exc))
                                return
                        elif not _is_loopback_client(client_ip) and not self.recognizer.auth_token:
                            self._send_error(
                                403,
                                "forbidden_endpoint",
                                "Clearing feedback endpoint requires authentication or loopback client",
                            )
                            return

                cfg = load_service_config(include_env=False)
                if "tier" in body and body["tier"] is not None:
                    if not isinstance(body["tier"], str):
                        self._send_error(400, "bad_request", f"Invalid tier: {body['tier']!r}")
                        return
                    tier_val = body["tier"].strip().lower()
                    if tier_val not in ("auto", "cpu", "gpu"):
                        self._send_error(400, "bad_request", f"Invalid tier: {body['tier']}")
                        return
                    cfg["tier"] = tier_val
                if "idle_unload_seconds" in body and body["idle_unload_seconds"] is not None:
                    raw_idle = body["idle_unload_seconds"]
                    if isinstance(raw_idle, bool) or (isinstance(raw_idle, float) and not raw_idle.is_integer()):
                        self._send_error(400, "bad_request", f"Invalid idle_unload_seconds: {raw_idle!r}")
                        return
                    try:
                        idle_sec = int(raw_idle)
                        if idle_sec < 0:
                            raise ValueError("must be >= 0")
                    except Exception:
                        self._send_error(400, "bad_request", f"Invalid idle_unload_seconds: {raw_idle!r}")
                        return
                    cfg["idle_unload_seconds"] = idle_sec
                if "feedback_opt_in" in body and body["feedback_opt_in"] is not None:
                    raw_opt = body["feedback_opt_in"]
                    if isinstance(raw_opt, bool):
                        opt_in = raw_opt
                    elif isinstance(raw_opt, str) and raw_opt.strip().lower() in ("true", "false"):
                        opt_in = raw_opt.strip().lower() == "true"
                    else:
                        self._send_error(400, "bad_request", f"Invalid feedback_opt_in boolean: {raw_opt!r}")
                        return
                    cfg["feedback_opt_in"] = opt_in
                if "feedback_endpoint" in body and body["feedback_endpoint"] is not None:
                    cfg["feedback_endpoint"] = body["feedback_endpoint"].strip()

                save_service_config(cfg)
                self.recognizer.default_tier = cfg["tier"]
                self.recognizer.idle_unload_seconds = float(cfg["idle_unload_seconds"])
                if self.recognizer._gpu_pipeline is not None:
                    self.recognizer._gpu_pipeline.idle_unload_seconds = float(cfg["idle_unload_seconds"])
                self.recognizer.feedback_opt_in = bool(cfg["feedback_opt_in"])
                self.recognizer.feedback_endpoint = str(cfg["feedback_endpoint"])
                if self.recognizer.feedback_opt_in:
                    ensure_feedback_keypair(self.recognizer.feedback_queue.state_dir)

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
                    source=body.get("source") if body.get("source") is not None else "local",
                    record_id=body.get("record_id"),
                )
                self._send_json(200, {"api_version": API_VERSION, **res})
            except UnidentifiedImageError as exc:
                self._send_error(415, "bad_image", f"Invalid feedback image: {exc}")
            except ValueError as exc:
                msg_low = str(exc).lower()
                if "exceed maximum allowed" in msg_low or "decompression bomb" in msg_low:
                    self._send_error(413, "too_large", str(exc))
                else:
                    self._send_error(400, "bad_request", str(exc))
            except Exception as exc:
                log.exception("Feedback enqueue failed")
                self._send_error(500, "feedback_failed", str(exc))
            return

        # Handle POST /v1/feedback/submit
        if parsed.path in ("/v1/feedback/submit", "/feedback/submit"):
            client_ip = self.client_address[0] if self.client_address else ""
            if not _is_loopback_client(client_ip) and not self.recognizer.auth_token:
                self._send_error(403, "forbidden_submit", "Remote feedback submission requires authentication")
                return
            if not self.recognizer.feedback_opt_in:
                self._send_error(403, "feedback_disabled", "Training feedback opt-in is disabled")
                return
            raw_ep = body.get("endpoint")
            if raw_ep is not None and not isinstance(raw_ep, str):
                self._send_error(400, "bad_request", "endpoint must be a string")
                return
            custom_ep = (raw_ep or "").strip()
            if custom_ep:
                if not _is_loopback_client(client_ip):
                    self._send_error(
                        403,
                        "forbidden_endpoint",
                        "Custom feedback endpoint in request body is only allowed from loopback clients",
                    )
                    return
                if custom_ep != self.recognizer.feedback_endpoint:
                    ep_host = (urlparse(custom_ep).hostname or "").lower().strip("[]")
                    is_loopback = ep_host in {"127.0.0.1", "localhost", "::1"}
                    if not is_loopback and not self.recognizer.auth_token:
                        self._send_error(
                            403,
                            "forbidden_endpoint",
                            "Custom feedback endpoint override requires authentication or loopback target",
                        )
                        return
            endpoint = custom_ep or self.recognizer.feedback_endpoint or ""
            try:
                if body.get("sync") is True:
                    res = self.recognizer.feedback_queue.submit(
                        endpoint_url=endpoint,
                        pow_difficulty=self.recognizer.pow_difficulty,
                    )
                    self._send_json(200, {"api_version": API_VERSION, **res})
                else:
                    job = self.recognizer.feedback_queue.start_submit_async(
                        endpoint_url=endpoint,
                        pow_difficulty=self.recognizer.pow_difficulty,
                    )
                    self._send_json(202, {"api_version": API_VERSION, "ok": True, **job, "job": job})
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
            raw_note = body.get("note")
            if raw_note is not None and not isinstance(raw_note, str):
                self._send_error(400, "bad_request", "note must be a string or null")
                return
            spoolman_filaments = body.get("filaments")
            spoolman_vendors = body.get("vendors")
            if spoolman_filaments is not None and not isinstance(spoolman_filaments, list):
                self._send_error(400, "bad_request", "filaments must be an array or null")
                return
            if spoolman_vendors is not None and not isinstance(spoolman_vendors, list):
                self._send_error(400, "bad_request", "vendors must be an array or null")
                return
            note = raw_note or ""
            try:
                res = self.recognizer.revise(
                    previous=previous,
                    note=note,
                    spoolman_filaments=spoolman_filaments,
                    spoolman_vendors=spoolman_vendors,
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
            except ValueError as ve:
                self._send_error(400, "bad_request", str(ve))
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
                raw_bytes = _strip_data_uri(b64_str)
            except Exception as exc:
                self._send_error(415, "bad_image", f"Failed to base64 decode photo index {idx}: {exc}")
                return

            if len(raw_bytes) > MAX_PHOTO_BYTES:
                self._send_error(413, "too_large", f"Photo at index {idx} ({len(raw_bytes)} bytes) exceeds limit of {MAX_PHOTO_BYTES} bytes")
                return

            decoded_images.append(raw_bytes)

        req_tier = body.get("tier")
        if req_tier is not None:
            if not isinstance(req_tier, str) or req_tier.strip().lower() not in ("auto", "cpu", "gpu"):
                self._send_error(400, "bad_request", f"Invalid tier: {req_tier!r}; must be one of 'auto', 'cpu', 'gpu'")
                return
            req_tier = req_tier.strip().lower()

        raw_hint = body.get("hint")
        if raw_hint is not None and not isinstance(raw_hint, str):
            self._send_error(400, "bad_request", "hint must be a string or null")
            return
        hint = raw_hint or ""

        spoolman_filaments = body.get("filaments")
        if spoolman_filaments is not None and not isinstance(spoolman_filaments, list):
            self._send_error(400, "bad_request", "filaments must be an array or null")
            return

        spoolman_vendors = body.get("vendors")
        if spoolman_vendors is not None and not isinstance(spoolman_vendors, list):
            self._send_error(400, "bad_request", "vendors must be an array or null")
            return

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
        except UnidentifiedImageError as uie:
            self._send_error(415, "bad_image", f"Unsupported or corrupt image: {uie}")
        except ValueError as ve:
            msg_low = str(ve).lower()
            if "exceed maximum allowed" in msg_low or "decompression bomb" in msg_low:
                self._send_error(413, "too_large", str(ve))
            elif "invalid or empty image" in msg_low or "empty image" in msg_low:
                self._send_error(415, "bad_image", str(ve))
            else:
                log.exception("Read failed with ValueError")
                self._send_error(500, "read_failed", f"Inference execution failed: {ve}")
        except Exception as exc:
            log.exception("Read failed")
            self._send_error(500, "read_failed", f"Inference execution failed: {exc}")


class _DualStackThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that binds IPv6 (including '::' dual-stack) cleanly alongside IPv4."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
    ) -> None:
        host = str(server_address[0] or "")
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def start_daemon(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    tier: str = "auto",
    idle_unload_seconds: float = 300.0,
    auth_token: str = "",
    feedback_opt_in: bool = False,
    feedback_endpoint: str = DEFAULT_FEEDBACK_ENDPOINT,
    feedback_dir: str | Path = DEFAULT_FEEDBACK_DIR,
    pow_difficulty: int = DEFAULT_POW_DIFFICULTY,
) -> ThreadingHTTPServer:
    """Create and return a configured ThreadingHTTPServer for filament-vision-server."""
    recognizer = FilamentRecognizer(
        default_tier=tier,
        idle_unload_seconds=idle_unload_seconds,
        auth_token=auth_token,
        feedback_opt_in=feedback_opt_in,
        feedback_endpoint=feedback_endpoint,
        feedback_dir=feedback_dir,
        pow_difficulty=pow_difficulty,
    )

    class Handler(_DaemonRequestHandler):
        pass

    Handler.recognizer = recognizer
    server = _DualStackThreadingHTTPServer((host, port), Handler)
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
    pow_difficulty: int = DEFAULT_POW_DIFFICULTY,
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
        pow_difficulty=pow_difficulty,
    )

    def _sweeper() -> None:
        while True:
            time.sleep(30.0)
            try:
                gpu_pipe = server.RequestHandlerClass.recognizer._gpu_pipeline
                if gpu_pipe is not None:
                    gpu_pipe.unload_if_idle()
            except Exception:
                log.exception("GPU idle sweeper failed")

    t = threading.Thread(target=_sweeper, daemon=True)
    t.start()
    print(f"[filament-vision-server] Listening on http://{_format_url_host(host)}:{port} (tier={tier})")
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
