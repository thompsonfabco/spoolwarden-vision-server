"""Opt-in user correction feedback queue, EXIF-stripping bundler, and write-only Drive upload."""
from __future__ import annotations

import base64
import binascii
import datetime
import io
import json
import re
import threading
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as StdET
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import defusedxml.ElementTree as DefusedET
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

from ..xml_io import DATA_DIR, AnnotatedSample, FilamentItem, parse_xml_file, write_xml_atomic

MAX_FEEDBACK_PHOTOS = 8
MAX_FEEDBACK_IMAGE_SIDE = 1024
MAX_FEEDBACK_ZIP_BYTES = 25 * 1024 * 1024  # 25 MB cap for Apps Script doPost
DEFAULT_FEEDBACK_DIR = DATA_DIR / "feedback_queue"

_ITEM_FIELDS = (
    "vendor",
    "name",
    "material",
    "line",
    "color_name",
    "color_family",
    "color_hex",
    "net",
    "tare",
    "density",
    "diameter",
    "nozzle",
    "bed",
    "nozzle_range",
    "bed_range",
    "sealed",
    "existing_filament_id",
)


def decode_image_b64(b64_str: str) -> bytes:
    """Strip optional data-URI prefix and strictly decode base64 image bytes."""
    if not isinstance(b64_str, str):
        raise ValueError("Image payload must be a base64 string")
    s = b64_str.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = re.sub(r"\s+", "", s)
    if not s:
        raise ValueError("Empty image payload")
    try:
        raw = base64.b64decode(s, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Invalid base64 image payload: {exc}") from exc
    if not raw:
        raise ValueError("Empty image payload")
    return raw


def _strip_data_uri(b64_str: str) -> bytes:
    return decode_image_b64(b64_str)


def _validate_upload_url(url: str, *, allow_loopback_http: bool = True) -> tuple[str, str]:
    """Validate that an upload endpoint uses HTTPS or explicit loopback HTTP."""
    s = (url or "").strip()
    parsed = urlparse(s)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https" and host:
        return s, scheme
    if allow_loopback_http and scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return s, scheme
    raise ValueError("A valid HTTPS feedback upload endpoint must be configured")


class _SafeUploadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ensure HTTP redirects during feedback upload cannot downgrade or SSRF off-policy hosts."""

    def __init__(self, initial_scheme: str) -> None:
        super().__init__()
        self._allow_loopback_http = initial_scheme == "http"

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_upload_url(newurl, allow_loopback_http=self._allow_loopback_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _safe_float(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def sanitize_image_b64(b64_str: str, max_side: int = MAX_FEEDBACK_IMAGE_SIDE) -> bytes:
    """Decode base64 image, bake EXIF orientation, resize, and re-encode as metadata-free JPEG."""
    raw = _strip_data_uri(b64_str)
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        # Re-create from raw pixel buffer so zero EXIF/ICC/APP markers survive
        clean = Image.frombytes("RGB", img.size, img.tobytes())
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()


def _item_to_el(tag: str, data: dict[str, Any]) -> StdET.Element:
    el = StdET.Element(tag)
    for key in _ITEM_FIELDS:
        if key in data and data[key] is not None:
            child = StdET.SubElement(el, key.replace("_", "-"))
            val = data[key]
            if isinstance(val, bool):
                child.text = "true" if val else "false"
            else:
                child.text = str(val)
    return el


def _el_to_item_dict(el: StdET.Element | None) -> dict[str, Any]:
    if el is None:
        return {}
    out: dict[str, Any] = {}
    for child in el:
        key = child.tag.replace("-", "_")
        txt = (child.text or "").strip()
        if not txt:
            continue
        if key in ("net", "tare", "nozzle", "bed", "existing_filament_id", "net_grams", "tare_grams"):
            try:
                out[key] = int(txt)
                continue
            except ValueError:
                pass
        elif key in ("density", "diameter"):
            try:
                out[key] = float(txt)
                continue
            except ValueError:
                pass
        elif key == "sealed":
            out[key] = txt.lower() == "true"
            continue
        out[key] = txt
    return out


class FeedbackQueue:
    """Manages locally queued user corrections in data/feedback_queue/pending_feedback.xml."""

    def __init__(self, queue_dir: str | Path = DEFAULT_FEEDBACK_DIR) -> None:
        self.queue_dir = Path(queue_dir)
        self.xml_path = self.queue_dir / "pending_feedback.xml"
        self._lock = threading.RLock()

    def _load_root(self) -> StdET.Element:
        if self.xml_path.is_file():
            return parse_xml_file(self.xml_path)
        return StdET.Element("feedback-queue", attrib={"version": "1"})

    def _save_root(self, root: StdET.Element) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        write_xml_atomic(self.xml_path, root)

    def enqueue(
        self,
        images_b64: list[str],
        predicted: dict[str, Any],
        corrected: dict[str, Any],
        note: str = "",
    ) -> dict[str, Any]:
        if not images_b64:
            raise ValueError("At least one image is required in images_b64")
        if len(images_b64) > MAX_FEEDBACK_PHOTOS:
            raise ValueError(f"Too many images ({len(images_b64)} > {MAX_FEEDBACK_PHOTOS})")
        if not isinstance(predicted, dict) or not isinstance(corrected, dict):
            raise ValueError("'predicted' and 'corrected' must be JSON objects")

        self.queue_dir.mkdir(parents=True, exist_ok=True)
        entry_id = f"fb_{uuid.uuid4().hex[:12]}"
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        saved_files: list[str] = []
        try:
            for idx, b64 in enumerate(images_b64):
                jpg_bytes = sanitize_image_b64(b64)
                fname = f"{entry_id}_{idx}.jpg"
                (self.queue_dir / fname).write_bytes(jpg_bytes)
                saved_files.append(fname)
        except Exception:
            for fname in saved_files:
                (self.queue_dir / fname).unlink(missing_ok=True)
            raise

        with self._lock:
            root = self._load_root()
            entry_el = StdET.SubElement(
                root,
                "entry",
                attrib={
                    "id": entry_id,
                    "created-at": created_at,
                },
            )
            if note.strip():
                note_el = StdET.SubElement(entry_el, "note")
                note_el.text = note.strip()[:500]
            imgs_el = StdET.SubElement(entry_el, "images")
            for fname in saved_files:
                img_el = StdET.SubElement(imgs_el, "image")
                img_el.text = fname
            entry_el.append(_item_to_el("predicted", predicted))
            entry_el.append(_item_to_el("corrected", corrected))
            self._save_root(root)

            st = self.status()
        return {
            "ok": True,
            "id": entry_id,
            "queued_count": st["count"],
            "bundle_mb": st["bundle_mb"],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            root = self._load_root()
            entries: list[dict[str, Any]] = []
            total_bytes = self.xml_path.stat().st_size if self.xml_path.is_file() else 0

            for entry_el in root.findall("entry"):
                eid = entry_el.attrib.get("id", "")
                imgs = [
                    (im.text or "").strip()
                    for im in entry_el.findall("./images/image")
                    if (im.text or "").strip()
                ]
                for fname in imgs:
                    fpath = self.queue_dir / fname
                    if fpath.is_file():
                        total_bytes += fpath.stat().st_size
                note_el = entry_el.find("note")
                entries.append(
                    {
                        "id": eid,
                        "created_at": entry_el.attrib.get("created-at", ""),
                        "note": (note_el.text or "").strip() if note_el is not None else "",
                        "images": imgs,
                        "predicted": _el_to_item_dict(entry_el.find("predicted")),
                        "corrected": _el_to_item_dict(entry_el.find("corrected")),
                    }
                )

            return {
                "count": len(entries),
                "bundle_mb": round(total_bytes / 1_000_000, 3),
                "entries": entries,
            }

    def delete(self, entry_id: str) -> bool:
        return self.delete_many([entry_id]) > 0

    def delete_many(self, entry_ids: list[str]) -> int:
        """Delete multiple queued entries in a single locked XML update."""
        target_ids = {eid for eid in entry_ids if eid}
        if not target_ids:
            return 0
        with self._lock:
            if not self.xml_path.is_file():
                return 0
            root = self._load_root()
            removed = 0
            for entry_el in list(root.findall("entry")):
                if entry_el.attrib.get("id") in target_ids:
                    for im in entry_el.findall("./images/image"):
                        fname = (im.text or "").strip()
                        if fname:
                            (self.queue_dir / fname).unlink(missing_ok=True)
                    root.remove(entry_el)
                    removed += 1
            if removed:
                self._save_root(root)
            return removed

    def clear(self) -> None:
        with self._lock:
            if self.xml_path.is_file():
                root = self._load_root()
                for entry_el in root.findall("entry"):
                    for im in entry_el.findall("./images/image"):
                        fname = (im.text or "").strip()
                        if fname:
                            (self.queue_dir / fname).unlink(missing_ok=True)
                self.xml_path.unlink(missing_ok=True)

    def _build_entries_zip(self, entry_els: list[StdET.Element]) -> bytes:
        """Package a subset of <entry> elements and their image files into an in-memory ZIP."""
        sub_root = StdET.Element("feedback-queue", attrib={"version": "1"})
        for el in entry_els:
            sub_root.append(el)
        StdET.indent(sub_root, space="  ")
        xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + StdET.tostring(sub_root, encoding="utf-8") + b"\n"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("pending_feedback.xml", xml_bytes)
            for entry_el in entry_els:
                for im in entry_el.findall("./images/image"):
                    fname = (im.text or "").strip()
                    if not fname:
                        continue
                    fpath = self.queue_dir / fname
                    if fpath.is_file():
                        zf.write(fpath, arcname=fname)
        return buf.getvalue()

    def build_bundle_zip(self, max_bytes: int | None = None) -> bytes:
        """Build a ZIP containing all queued feedback entries (uncapped by default for local download)."""
        with self._lock:
            if not self.xml_path.is_file():
                raise ValueError("Feedback queue is empty")
            root = self._load_root()
            entry_els = root.findall("entry")
            if not entry_els:
                raise ValueError("Feedback queue is empty")

            raw = self._build_entries_zip(entry_els)
            if max_bytes is not None and len(raw) > max_bytes:
                raise ValueError(
                    f"Feedback bundle ({len(raw)} bytes) exceeds {max_bytes} byte limit; remove some entries"
                )
            return raw

    def _entry_raw_bytes(self, entry_el: StdET.Element) -> int:
        total = len(StdET.tostring(entry_el, encoding="utf-8")) + 256
        for im in entry_el.findall("./images/image"):
            fname = (im.text or "").strip()
            if not fname:
                continue
            fpath = self.queue_dir / fname
            if fpath.is_file():
                total += fpath.stat().st_size + 128
        return total

    def build_bundle_batches(self, max_zip_bytes: int | None = None) -> list[tuple[list[str], bytes]]:
        """Split queued feedback entries into ZIP batches each <= max_zip_bytes."""
        with self._lock:
            if not self.xml_path.is_file():
                raise ValueError("Feedback queue is empty")
            root = self._load_root()
            entry_els = root.findall("entry")
            if not entry_els:
                raise ValueError("Feedback queue is empty")

            limit = MAX_FEEDBACK_ZIP_BYTES if max_zip_bytes is None else int(max_zip_bytes)
            soft_check_floor = int(limit * 0.85)
            batches: list[tuple[list[str], bytes]] = []
            current_els: list[StdET.Element] = []
            current_bytes: bytes = b""
            current_est_bytes = 0

            for entry_el in entry_els:
                eid = entry_el.attrib.get("id", "")
                entry_est = self._entry_raw_bytes(entry_el)
                candidate_els = current_els + [entry_el]
                candidate_est = current_est_bytes + entry_est

                if current_els and candidate_est < soft_check_floor:
                    current_els = candidate_els
                    current_bytes = b""
                    current_est_bytes = candidate_est
                    continue

                candidate_bytes = self._build_entries_zip(candidate_els)
                if len(candidate_bytes) <= limit:
                    current_els = candidate_els
                    current_bytes = candidate_bytes
                    current_est_bytes = candidate_est
                    continue

                if current_els:
                    flush_bytes = current_bytes or self._build_entries_zip(current_els)
                    batches.append(([e.attrib.get("id", "") for e in current_els], flush_bytes))
                    single_bytes = self._build_entries_zip([entry_el])
                    if len(single_bytes) > limit:
                        raise ValueError(
                            f"Feedback entry {eid or '<unknown>'} ({len(single_bytes)} bytes) exceeds single-batch limit ({limit} bytes)"
                        )
                    current_els = [entry_el]
                    current_bytes = single_bytes
                    current_est_bytes = entry_est
                else:
                    raise ValueError(
                        f"Feedback entry {eid or '<unknown>'} ({len(candidate_bytes)} bytes) exceeds single-batch limit ({limit} bytes)"
                    )

            if current_els:
                final_bytes = current_bytes or self._build_entries_zip(current_els)
                if len(final_bytes) > limit and len(current_els) > 1:
                    # Rare incompressible edge case where estimated size stayed under 85% but ZIP exceeded limit
                    batches.extend(self._split_elements_exact(current_els, limit))
                elif len(final_bytes) > limit:
                    eid = current_els[0].attrib.get("id", "")
                    raise ValueError(
                        f"Feedback entry {eid or '<unknown>'} ({len(final_bytes)} bytes) exceeds single-batch limit ({limit} bytes)"
                    )
                else:
                    batches.append(([e.attrib.get("id", "") for e in current_els], final_bytes))
            return batches

    def _split_elements_exact(
        self, entry_els: list[StdET.Element], limit: int
    ) -> list[tuple[list[str], bytes]]:
        batches: list[tuple[list[str], bytes]] = []
        current_els: list[StdET.Element] = []
        current_bytes: bytes = b""
        for entry_el in entry_els:
            candidate_els = current_els + [entry_el]
            candidate_bytes = self._build_entries_zip(candidate_els)
            if len(candidate_bytes) <= limit:
                current_els = candidate_els
                current_bytes = candidate_bytes
            elif current_els:
                batches.append(([e.attrib.get("id", "") for e in current_els], current_bytes))
                single_bytes = self._build_entries_zip([entry_el])
                if len(single_bytes) > limit:
                    eid = entry_el.attrib.get("id", "")
                    raise ValueError(
                        f"Feedback entry {eid or '<unknown>'} ({len(single_bytes)} bytes) exceeds single-batch limit ({limit} bytes)"
                    )
                current_els = [entry_el]
                current_bytes = single_bytes
            else:
                eid = entry_el.attrib.get("id", "")
                raise ValueError(
                    f"Feedback entry {eid or '<unknown>'} ({len(candidate_bytes)} bytes) exceeds single-batch limit ({limit} bytes)"
                )
        if current_els:
            batches.append(([e.attrib.get("id", "") for e in current_els], current_bytes))
        return batches

    def _post_zip_batch(
        self,
        url: str,
        filename: str,
        entry_count: int,
        zip_bytes: bytes,
        timeout: float,
    ) -> dict[str, Any]:
        clean_url, initial_scheme = _validate_upload_url(url)
        payload = json.dumps(
            {
                "filename": filename,
                "mime_type": "application/zip",
                "entry_count": entry_count,
                "zip_b64": base64.b64encode(zip_bytes).decode("ascii"),
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            clean_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_SafeUploadRedirectHandler(initial_scheme))
        try:
            with opener.open(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                status_code = resp.status
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Upload failed with HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upload failed: {exc.reason}") from exc

        if status_code >= 400:
            raise RuntimeError(f"Upload failed with HTTP {status_code}: {resp_body}")

        if not resp_body.strip():
            raise RuntimeError("Upload failed: server returned empty response body")
        try:
            parsed = json.loads(resp_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Upload failed: server returned non-JSON response: {resp_body[:200]}") from exc

        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            err = parsed.get("error", resp_body[:200]) if isinstance(parsed, dict) else resp_body[:200]
            raise RuntimeError(f"Upload rejected by server: {err}")

        return parsed

    def submit(
        self,
        endpoint_url: str,
        timeout: float = 45.0,
        max_zip_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Upload queued feedback entries in <= max_zip_bytes ZIP batches, pruning each uploaded batch."""
        url, _ = _validate_upload_url(endpoint_url)

        batches = self.build_bundle_batches(max_zip_bytes=max_zip_bytes)
        total_batches = len(batches)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        total_submitted = 0
        total_bytes = 0
        responses: list[dict[str, Any]] = []

        for batch_idx, (entry_ids, zip_bytes) in enumerate(batches, start=1):
            filename = (
                f"filament-feedback-{stamp}.zip"
                if total_batches == 1
                else f"filament-feedback-{stamp}-part{batch_idx}of{total_batches}.zip"
            )
            parsed = self._post_zip_batch(
                url=url,
                filename=filename,
                entry_count=len(entry_ids),
                zip_bytes=zip_bytes,
                timeout=timeout,
            )
            responses.append(parsed)
            total_submitted += len(entry_ids)
            total_bytes += len(zip_bytes)
            self.delete_many(entry_ids)

        if not self.status()["count"]:
            self.clear()

        return {
            "ok": True,
            "submitted_count": total_submitted,
            "batches_submitted": total_batches,
            "bundle_bytes": total_bytes,
            "server_response": responses[0] if len(responses) == 1 else responses,
        }


def ingest_feedback_bundles(
    source: str | Path,
    dest_xml: str | Path = DATA_DIR / "annotations" / "feedback_verified.xml",
    dest_images_dir: str | Path = DATA_DIR / "feedback_verified",
) -> dict[str, Any]:
    """Import downloaded feedback .zip bundles into a verified XML dataset (2x weight for training)."""
    src = Path(source)
    zips = [src] if src.is_file() and src.suffix.lower() == ".zip" else sorted(src.glob("*.zip"))
    if not zips:
        raise FileNotFoundError(f"No .zip feedback bundles found at {src}")

    dest_img_root = Path(dest_images_dir)
    dest_img_root.mkdir(parents=True, exist_ok=True)
    dest_xml_path = Path(dest_xml)

    if dest_xml_path.is_file():
        out_root = parse_xml_file(dest_xml_path)
    else:
        out_root = StdET.Element("dataset", attrib={"version": "1", "source": "user_feedback"})

    existing_ids = {s.attrib.get("id", "") for s in out_root.findall("sample")}
    imported_entries = 0
    imported_images = 0

    ocr_engine: Any = None
    cv2_mod: Any = None
    np_mod: Any = None
    extract_fn: Any = None
    try:
        import cv2 as cv2_mod  # type: ignore[no-redef]
        import numpy as np_mod  # type: ignore[no-redef]
        from ..cpu_tier.ocr_helper import create_rapidocr
        from ..data_engine.auto_labeler import extract_multi_angle_ocr_and_boxes as extract_fn

        ocr_engine = create_rapidocr()
    except Exception:
        ocr_engine = None

    for zpath in zips:
        with zipfile.ZipFile(zpath, "r") as zf:
            names = set(zf.namelist())
            if "pending_feedback.xml" not in names:
                continue
            xml_bytes = zf.read("pending_feedback.xml")
            fb_root = DefusedET.fromstring(xml_bytes)

            for entry_el in fb_root.findall("entry"):
                eid = entry_el.attrib.get("id", "") or uuid.uuid4().hex[:12]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", eid) or eid in existing_ids:
                    continue

                corr = _el_to_item_dict(entry_el.find("corrected"))
                if not corr.get("vendor") or not corr.get("material"):
                    continue

                saved_paths: list[tuple[str, int, int, list[str], list[Any]]] = []
                for im in entry_el.findall("./images/image"):
                    fname = Path((im.text or "").strip()).name
                    if not fname or fname not in names:
                        continue
                    img_bytes = zf.read(fname)
                    try:
                        with Image.open(io.BytesIO(img_bytes)) as pil_im:
                            pil_im.verify()
                        with Image.open(io.BytesIO(img_bytes)) as pil_im2:
                            w, h = pil_im2.size
                    except Exception:
                        continue
                    out_img = dest_img_root / fname
                    if out_img.exists():
                        out_img = dest_img_root / f"{out_img.stem}_{uuid.uuid4().hex[:6]}{out_img.suffix}"
                    out_img.write_bytes(img_bytes)
                    dest_parents = dest_xml_path.resolve().parents
                    base_dir = dest_parents[1] if len(dest_parents) >= 2 else DATA_DIR.parent
                    out_img_res = out_img.resolve()
                    if out_img_res.is_relative_to(base_dir):
                        rel_or_abs = out_img_res.relative_to(base_dir)
                    elif out_img.is_relative_to(DATA_DIR.parent):
                        rel_or_abs = out_img.relative_to(DATA_DIR.parent)
                    else:
                        rel_or_abs = out_img

                    ocr_lines: list[str] = []
                    boxes: list[Any] = []
                    if ocr_engine is not None and cv2_mod is not None and np_mod is not None and extract_fn is not None:
                        arr = np_mod.frombuffer(img_bytes, dtype=np_mod.uint8)
                        img_bgr = cv2_mod.imdecode(arr, cv2_mod.IMREAD_COLOR)
                        if img_bgr is not None and img_bgr.size > 0:
                            ocr_lines, boxes = extract_fn(img_bgr, ocr_engine)

                    saved_paths.append((str(rel_or_abs).replace("\\", "/"), w, h, ocr_lines, boxes))
                    imported_images += 1

                if not saved_paths:
                    continue

                item = FilamentItem(
                    vendor=str(corr.get("vendor") or "Unknown"),
                    material=str(corr.get("material") or "PLA"),
                    line=str(corr.get("line") or ""),
                    color_name=str(corr.get("color_name") or ""),
                    color_family=str(corr.get("color_family") or ""),
                    color_hex=str(corr.get("color_hex") or ""),
                    net_grams=_safe_int(corr.get("net_grams", corr.get("net")), 1000),
                    tare_grams=_safe_int(corr.get("tare_grams", corr.get("tare")), 170),
                    density=_safe_float(corr.get("density"), 1.24),
                    diameter=_safe_float(corr.get("diameter"), 1.75),
                    nozzle=_safe_int(corr.get("nozzle"), 210),
                    bed=_safe_int(corr.get("bed"), 60),
                    nozzle_range=str(corr.get("nozzle_range") or "190-220"),
                    bed_range=str(corr.get("bed_range") or "50-60"),
                    sealed=bool(corr.get("sealed", False)),
                )
                synth_ocr = f"{item.vendor} {item.line} {item.material} {item.color_name}".strip()
                for idx_p, (spath, w, h, ocr_lines, boxes) in enumerate(saved_paths):
                    sid = eid if idx_p == 0 else f"{eid}_{idx_p}"
                    if sid in existing_ids:
                        continue
                    sample = AnnotatedSample(
                        sample_id=sid,
                        image_path=spath,
                        width=w,
                        height=h,
                        split="train",
                        item=item,
                        boxes=boxes,
                        ocr_lines=ocr_lines if ocr_lines else ([synth_ocr] if synth_ocr else []),
                    )
                    sample_el = sample.to_element()
                    sample_el.attrib["source"] = "user_feedback"
                    sample_el.attrib["weight"] = "2.0"
                    out_root.append(sample_el)
                    existing_ids.add(sid)
                imported_entries += 1

    write_xml_atomic(dest_xml_path, out_root)
    return {
        "zips_processed": len(zips),
        "imported_entries": imported_entries,
        "imported_images": imported_images,
        "dest_xml": str(dest_xml_path),
    }
