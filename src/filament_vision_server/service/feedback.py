"""Opt-in user correction feedback queue, EXIF-stripping bundler, and write-only Drive upload."""
from __future__ import annotations

import base64
import datetime
import io
import json
import re
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as StdET
import zipfile
from pathlib import Path
from typing import Any

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


def _strip_data_uri(b64_str: str) -> bytes:
    s = b64_str.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


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
        if key in ("net", "tare", "nozzle", "bed", "existing_filament_id"):
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
        if not self.xml_path.is_file():
            return False
        root = self._load_root()
        removed = False
        for entry_el in list(root.findall("entry")):
            if entry_el.attrib.get("id") == entry_id:
                for im in entry_el.findall("./images/image"):
                    fname = (im.text or "").strip()
                    if fname:
                        (self.queue_dir / fname).unlink(missing_ok=True)
                root.remove(entry_el)
                removed = True
        if removed:
            self._save_root(root)
        return removed

    def clear(self) -> None:
        if self.xml_path.is_file():
            root = self._load_root()
            for entry_el in root.findall("entry"):
                for im in entry_el.findall("./images/image"):
                    fname = (im.text or "").strip()
                    if fname:
                        (self.queue_dir / fname).unlink(missing_ok=True)
            self.xml_path.unlink(missing_ok=True)

    def build_bundle_zip(self) -> bytes:
        st = self.status()
        if st["count"] == 0:
            raise ValueError("Feedback queue is empty")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.xml_path, arcname="pending_feedback.xml")
            for entry in st["entries"]:
                for fname in entry["images"]:
                    fpath = self.queue_dir / fname
                    if fpath.is_file():
                        zf.write(fpath, arcname=fname)

        raw = buf.getvalue()
        if len(raw) > MAX_FEEDBACK_ZIP_BYTES:
            raise ValueError(
                f"Feedback bundle ({len(raw)} bytes) exceeds {MAX_FEEDBACK_ZIP_BYTES} byte limit; remove some entries"
            )
        return raw

    def submit(self, endpoint_url: str, timeout: float = 45.0) -> dict[str, Any]:
        url = endpoint_url.strip()
        if not url or not (url.startswith("https://") or url.startswith("http://127.0.0.1")):
            raise ValueError("A valid HTTPS feedback upload endpoint must be configured")

        st = self.status()
        zip_bytes = self.build_bundle_zip()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = json.dumps(
            {
                "filename": f"filament-feedback-{stamp}.zip",
                "mime_type": "application/zip",
                "entry_count": st["count"],
                "zip_b64": base64.b64encode(zip_bytes).decode("ascii"),
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                status_code = resp.status
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Upload failed with HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upload failed: {exc.reason}") from exc

        if status_code >= 400:
            raise RuntimeError(f"Upload failed with HTTP {status_code}: {resp_body}")

        try:
            parsed = json.loads(resp_body) if resp_body.strip() else {"ok": True}
        except json.JSONDecodeError:
            parsed = {"ok": True}

        if isinstance(parsed, dict) and parsed.get("ok") is False:
            raise RuntimeError(f"Upload rejected by server: {parsed.get('error', resp_body)}")

        submitted_count = st["count"]
        self.clear()
        return {
            "ok": True,
            "submitted_count": submitted_count,
            "bundle_bytes": len(zip_bytes),
            "server_response": parsed,
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
                    out_img.write_bytes(img_bytes)
                    rel_or_abs = out_img.relative_to(DATA_DIR.parent) if out_img.is_relative_to(DATA_DIR.parent) else out_img

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
                    vendor=str(corr.get("vendor", "Unknown")),
                    material=str(corr.get("material", "PLA")),
                    line=str(corr.get("line", "")),
                    color_name=str(corr.get("color_name", "")),
                    color_family=str(corr.get("color_family", "")),
                    color_hex=str(corr.get("color_hex", "")),
                    net_grams=int(corr.get("net_grams", corr.get("net", 1000))),
                    tare_grams=int(corr.get("tare_grams", corr.get("tare", 170))),
                    density=float(corr.get("density", 1.24)),
                    diameter=float(corr.get("diameter", 1.75)),
                    nozzle=int(corr.get("nozzle", 210)),
                    bed=int(corr.get("bed", 60)),
                    nozzle_range=str(corr.get("nozzle_range", "190-220")),
                    bed_range=str(corr.get("bed_range", "50-60")),
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
