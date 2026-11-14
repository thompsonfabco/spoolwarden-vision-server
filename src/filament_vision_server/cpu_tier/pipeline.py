"""End-to-end CPU Hybrid Inference Pipeline (ONNX Detector + RapidOCR + Unwarping + ONNX MultiHead Visual + Catalog Fusion)."""
from __future__ import annotations

import io
import logging
import os
import time
import warnings
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

from .ocr_helper import create_rapidocr, run_rapidocr
from .text_parser_io import load_text_parser_npz

log = logging.getLogger(__name__)

_ACCEL_PRIORITY = [
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
]


def select_onnx_providers() -> list[str]:
    """Discover available ONNX Runtime hardware accelerators with CPUExecutionProvider fallback."""
    force_val = (
        os.environ.get("FILAMENT_VISION_SERVER_CPU_ONLY")
        or os.environ.get("FILAMENT_KIT_VISION_CPU_ONLY")
        or ""
    )
    force_cpu = force_val.strip().lower() in ("1", "true", "yes")
    if force_cpu:
        return ["CPUExecutionProvider"]
    available = set(ort.get_available_providers())
    chosen = [p for p in _ACCEL_PRIORITY if p in available]
    if "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")
    return chosen


def _create_ort_session(model_path: str | Path, sess_opts: ort.SessionOptions, providers: list[str]) -> ort.InferenceSession:
    """Create an ORT InferenceSession using best available providers, falling back to CPU on init failure."""
    try:
        return ort.InferenceSession(str(model_path), sess_options=sess_opts, providers=providers)
    except Exception as exc:
        if providers != ["CPUExecutionProvider"]:
            log.warning("Accelerated ORT providers %s failed (%s); falling back to CPUExecutionProvider", providers, exc)
            return ort.InferenceSession(str(model_path), sess_options=sess_opts, providers=["CPUExecutionProvider"])
        raise

from ..catalog.matcher import CatalogMatcher
from ..data_engine.auto_labeler import extract_multi_angle_ocr_and_boxes, parse_ocr_heuristic
from ..xml_io import CONFIGS_DIR, PROJECT_ROOT, BoundingBox, FilamentItem, IntakeReadResult, parse_xml_file
from .color_lab import extract_filament_color
from .labels import COLOR_FAMILIES, SEALED_STATES, VISUAL_FINISHES, VISUAL_VENDORS
from .train_detector import DETECTOR_CLASSES
from .unwarp import unwarp_circular_spool_label

MAX_DECODE_PIXELS = 36_000_000
MAX_DECODE_SIDE = 10_000


def _validate_image_dimensions(image_input: str | Path | bytes | np.ndarray) -> tuple[int, int]:
    """Validate image dimensions from header metadata before allocating full pixel buffers."""
    if isinstance(image_input, np.ndarray):
        if image_input.ndim < 2 or image_input.size == 0:
            raise ValueError("Invalid or empty image passed to CPUHybridPipeline")
        h, w = int(image_input.shape[0]), int(image_input.shape[1])
    else:
        if isinstance(image_input, bytes) and not image_input:
            raise ValueError("Invalid or empty image passed to CPUHybridPipeline")
        src: Any = io.BytesIO(image_input) if isinstance(image_input, bytes) else image_input
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(src) as pil_im:
                    w, h = pil_im.size
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(
                "Image dimensions exceed maximum allowed pixel limit (decompression bomb rejected)"
            ) from exc

    if w <= 0 or h <= 0 or w > MAX_DECODE_SIDE or h > MAX_DECODE_SIDE or (w * h) > MAX_DECODE_PIXELS:
        raise ValueError(
            f"Image dimensions {w}x{h} ({w * h} pixels) exceed maximum allowed limit of {MAX_DECODE_PIXELS} pixels"
        )
    return int(w), int(h)


def _box_iou(a: BoundingBox, b: BoundingBox) -> float:
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, a.x2 - a.x1) * max(0.0, a.y2 - a.y1)
    area_b = max(0.0, b.x2 - b.x1) * max(0.0, b.y2 - b.y1)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def _nms_boxes(boxes: list[BoundingBox], iou_thresh: float) -> list[BoundingBox]:
    """Apply class-aware greedy Non-Maximum Suppression to sorted bounding boxes."""
    if not boxes or iou_thresh <= 0.0:
        return boxes
    kept: list[BoundingBox] = []
    for cand in sorted(boxes, key=lambda b: b.confidence, reverse=True):
        if any(cand.label == prev.label and _box_iou(cand, prev) > iou_thresh for prev in kept):
            continue
        kept.append(cand)
    return kept


def _softmax(x: np.ndarray) -> np.ndarray:
    e_x = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e_x / np.sum(e_x, axis=-1, keepdims=True)


class CPUHybridPipeline:
    """Fast CPU-only inference pipeline using ONNX Runtime, RapidOCR, and CatalogMatcher."""

    def __init__(
        self,
        detector_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "detector_rfdetr_n.onnx",
        visual_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "visual_multihead.onnx",
        text_parser_path: str | Path = PROJECT_ROOT / "models" / "cpu" / "text_parser.npz",
    ):
        self.det_input_size = 384
        self.det_conf_thresh = 0.35
        self.det_iou_thresh = 0.45
        self.vis_input_size = 224
        cfg_path = CONFIGS_DIR / "cpu_pipeline.xml"
        if cfg_path.is_file():
            try:
                cfg_root = parse_xml_file(cfg_path)
                det_el = cfg_root.find("detector")
                if det_el is not None:
                    self.det_input_size = int(det_el.attrib.get("input-size", str(self.det_input_size)))
                    self.det_conf_thresh = float(det_el.attrib.get("conf-threshold", str(self.det_conf_thresh)))
                    self.det_iou_thresh = float(det_el.attrib.get("iou-threshold", str(self.det_iou_thresh)))
                vis_el = cfg_root.find("visual-classifier")
                if vis_el is not None:
                    self.vis_input_size = int(vis_el.attrib.get("input-size", str(self.vis_input_size)))
            except Exception as exc:
                log.warning("Failed to parse %s, using default thresholds: %s", cfg_path, exc)

        self.matcher = CatalogMatcher()
        self.providers = select_onnx_providers()
        self.ocr = create_rapidocr(self.providers)

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = min(8, max(2, os.cpu_count() or 4))
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.detector_session: ort.InferenceSession | None = None
        if Path(detector_onnx).is_file():
            self.detector_session = _create_ort_session(detector_onnx, sess_opts, self.providers)

        self.visual_session: ort.InferenceSession | None = None
        if Path(visual_onnx).is_file():
            self.visual_session = _create_ort_session(visual_onnx, sess_opts, self.providers)

        self.active_providers: list[str] = (
            self.visual_session.get_providers()
            if self.visual_session is not None
            else (self.detector_session.get_providers() if self.detector_session is not None else ["CPUExecutionProvider"])
        )

        self.text_parser: dict[str, Any] | None = None
        tp_path = Path(text_parser_path)
        if tp_path.is_file():
            self.text_parser = load_text_parser_npz(tp_path)

    def _detect_regions(
        self,
        img_bgr: np.ndarray,
        conf_thresh: float | None = None,
        iou_thresh: float | None = None,
    ) -> list[BoundingBox]:
        """Run RF-DETR-N ONNX detector to locate box_label, spool_label, spool_windings, and spool_flange."""
        if self.detector_session is None:
            return []
        eff_conf = self.det_conf_thresh if conf_thresh is None else float(conf_thresh)
        eff_iou = self.det_iou_thresh if iou_thresh is None else float(iou_thresh)
        in_sz = self.det_input_size
        resized = cv2.resize(img_bgr, (in_sz, in_sz), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = ((rgb - mean) / std).transpose(2, 0, 1)[np.newaxis, ...]

        inp_name = self.detector_session.get_inputs()[0].name
        dets, logits = self.detector_session.run(None, {inp_name: norm})
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits[0], -20.0, 20.0)))

        results: list[BoundingBox] = []
        num_cls = len(DETECTOR_CLASSES)
        for i in range(dets.shape[1]):
            cls_probs = probs[i, :num_cls]
            cid = int(np.argmax(cls_probs))
            score = float(cls_probs[cid])
            if score < eff_conf:
                continue
            cx, cy, bw, bh = [float(v) for v in dets[0, i]]
            x1 = max(0.0, min(1.0, cx - bw / 2.0))
            y1 = max(0.0, min(1.0, cy - bh / 2.0))
            x2 = max(0.0, min(1.0, cx + bw / 2.0))
            y2 = max(0.0, min(1.0, cy + bh / 2.0))
            if (x2 - x1) > 0.02 and (y2 - y1) > 0.02:
                results.append(
                    BoundingBox(
                        label=DETECTOR_CLASSES[cid],
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        confidence=score,
                    )
                )
        results.sort(key=lambda b: b.confidence, reverse=True)
        return _nms_boxes(results, eff_iou)

    def _run_visual_onnx(self, img_bgr: np.ndarray) -> dict[str, tuple[str, float]]:
        if self.visual_session is None:
            return {}
        v_sz = self.vis_input_size
        resized = cv2.resize(img_bgr, (v_sz, v_sz), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = ((rgb - mean) / std).transpose(2, 0, 1)[np.newaxis, ...]

        c_log, f_log, v_log, s_log = self.visual_session.run(None, {"image": norm})
        c_prob = _softmax(c_log[0])
        f_prob = _softmax(f_log[0])
        v_prob = _softmax(v_log[0])
        s_prob = _softmax(s_log[0])

        c_idx = int(np.argmax(c_prob))
        f_idx = int(np.argmax(f_prob))
        v_idx = int(np.argmax(v_prob))
        s_idx = int(np.argmax(s_prob))

        return {
            "color_family": (COLOR_FAMILIES[c_idx], float(c_prob[c_idx])),
            "visual_finish": (VISUAL_FINISHES[f_idx], float(f_prob[f_idx])),
            "visual_vendor": (VISUAL_VENDORS[v_idx], float(v_prob[v_idx])),
            "sealed_state": (SEALED_STATES[s_idx], float(s_prob[s_idx])),
        }

    @staticmethod
    def _load_bgr(image_input: str | Path | bytes | np.ndarray) -> np.ndarray:
        """Decode file path, raw bytes (JPEG/PNG/WEBP/HEIC/HEIF), or ndarray into BGR uint8."""
        _validate_image_dimensions(image_input)
        img_bgr: np.ndarray | None = None
        if isinstance(image_input, (str, Path)):
            img_bgr = cv2.imread(str(image_input))
            if img_bgr is None:
                try:
                    with Image.open(image_input) as pil_im:
                        rgb = np.asarray(ImageOps.exif_transpose(pil_im).convert("RGB"))
                        img_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                except Exception:
                    img_bgr = None
        elif isinstance(image_input, bytes):
            arr = np.frombuffer(image_input, dtype=np.uint8)
            img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img_bgr is None:
                try:
                    with Image.open(io.BytesIO(image_input)) as pil_im:
                        rgb = np.asarray(ImageOps.exif_transpose(pil_im).convert("RGB"))
                        img_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                except Exception:
                    img_bgr = None
        else:
            img_bgr = image_input

        if img_bgr is None or img_bgr.size == 0:
            raise ValueError("Invalid or empty image passed to CPUHybridPipeline")
        return img_bgr

    def predict_image(self, image_input: str | Path | bytes | np.ndarray, photo_index: int = 0) -> FilamentItem:
        img_bgr = self._load_bgr(image_input)

        # Keep up to 1600px side so small barcode/address text remains legible
        h, w = img_bgr.shape[:2]
        if max(h, w) > 1600:
            scale = 1600.0 / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        det_boxes = self._detect_regions(img_bgr)
        ocr_lines: list[str] = []
        seen_ocr: set[str] = set()
        boxes: list[BoundingBox] = list(det_boxes)

        def _append_lines(new_lines: list[str]) -> None:
            for txt in new_lines:
                t = str(txt).strip()
                if t and t not in seen_ocr:
                    ocr_lines.append(t)
                    seen_ocr.add(t)

        def _ocr_is_strong(lines: list[str]) -> bool:
            if not lines:
                return False
            joined = " ".join(lines).lower()
            if len(joined) < 18:
                return False
            kws = ("pla", "petg", "abs", "asa", "tpu", "1.75", "2.85", "temp", "color", "colour", "filament", "1kg", "print")
            if sum(1 for k in kws if k in joined) < 2:
                return False
            v, _ = self.matcher.resolve_vendor(joined, min_score=85.0)
            return v != "Unknown"

        # Crop-first OCR: run multi-angle OCR on high-confidence label detections first to avoid full-image passes
        ih, iw = img_bgr.shape[:2]
        label_crops = [
            (b_idx, b) for b_idx, b in enumerate(det_boxes[:3])
            if b.label in ("box_label", "spool_label") and b.confidence >= 0.55 and (b.x2 - b.x1) * (b.y2 - b.y1) < 0.72
        ]
        cropped_indices: set[int] = set()
        for orig_idx, b in label_crops[:2]:
            pad = 0.08
            px1 = max(0, int((b.x1 - pad) * iw))
            py1 = max(0, int((b.y1 - pad) * ih))
            px2 = min(iw, int((b.x2 + pad) * iw))
            py2 = min(ih, int((b.y2 + pad) * ih))
            crop = img_bgr[py1:py2, px1:px2]
            if crop.size > 0 and min(crop.shape[:2]) >= 24:
                if max(crop.shape[:2]) < 640:
                    s_up = min(2.5, 640.0 / max(crop.shape[:2]))
                    crop = cv2.resize(crop, (int(crop.shape[1] * s_up), int(crop.shape[0] * s_up)), interpolation=cv2.INTER_CUBIC)
                crop_lines, _ = extract_multi_angle_ocr_and_boxes(crop, self.ocr)
                _append_lines(crop_lines)
                cropped_indices.add(orig_idx)
                if _ocr_is_strong(ocr_lines):
                    break

        # Full-image fallback only when crop-first OCR was empty/incomplete
        if not _ocr_is_strong(ocr_lines):
            full_lines, full_boxes = extract_multi_angle_ocr_and_boxes(img_bgr, self.ocr)
            _append_lines(full_lines)
            if full_boxes:
                boxes = det_boxes + full_boxes

            for b_idx, b in enumerate(det_boxes[:3]):
                if b.label in ("box_label", "spool_label") and b_idx not in cropped_indices and (b.x2 - b.x1) * (b.y2 - b.y1) < 0.72:
                    pad = 0.06
                    px1 = max(0, int((b.x1 - pad) * iw))
                    py1 = max(0, int((b.y1 - pad) * ih))
                    px2 = min(iw, int((b.x2 + pad) * iw))
                    py2 = min(ih, int((b.y2 + pad) * ih))
                    crop = img_bgr[py1:py2, px1:px2]
                    if crop.size > 0 and min(crop.shape[:2]) >= 24:
                        if max(crop.shape[:2]) < 480:
                            s_up = 480.0 / max(crop.shape[:2])
                            crop = cv2.resize(crop, (int(crop.shape[1] * s_up), int(crop.shape[0] * s_up)), interpolation=cv2.INTER_CUBIC)
                        res_c = run_rapidocr(self.ocr, crop)
                        if res_c:
                            for _, text, score in res_c:
                                t = str(text).strip()
                                if float(score) >= 0.45 and t and t not in seen_ocr:
                                    ocr_lines.append(t)
                                    seen_ocr.add(t)

        is_spool = any(b.label in ("spool_flange", "spool_windings", "spool_label") for b in boxes)

        # If circular spool with minimal upright OCR text, try polar unwarping on detected spool crop
        if is_spool and len(" ".join(ocr_lines)) < 16:
            spool_box = next(
                (b for b in det_boxes if b.label in ("spool_label", "spool_flange") and (b.x2 - b.x1) > 0.15 and (b.y2 - b.y1) > 0.15),
                None,
            )
            if spool_box is not None:
                px1 = max(0, int((spool_box.x1 - 0.04) * iw))
                py1 = max(0, int((spool_box.y1 - 0.04) * ih))
                px2 = min(iw, int((spool_box.x2 + 0.04) * iw))
                py2 = min(ih, int((spool_box.y2 + 0.04) * ih))
                unwarp_target = img_bgr[py1:py2, px1:px2] if (px2 - px1) >= 64 and (py2 - py1) >= 64 else img_bgr
            else:
                unwarp_target = img_bgr
            strips = unwarp_circular_spool_label(unwarp_target)
            for strip in strips[:2]:
                res_s = run_rapidocr(self.ocr, strip)
                if res_s:
                    for _, text, score in res_s:
                        t = str(text).strip()
                        if float(score) >= 0.48 and t and t not in seen_ocr:
                            ocr_lines.append(t)
                            seen_ocr.add(t)

        item, vis_finish = parse_ocr_heuristic(ocr_lines, img_bgr, self.matcher, is_spool=is_spool)
        item.photos = [photo_index]
        heur_vendor = item.vendor
        joined_ocr = " | ".join(ocr_lines)
        ocr_vendor_cands = self.matcher.find_vendor_candidates(joined_ocr) if ocr_lines else {}

        # Refine visually estimated filament color from detected spool_windings crop
        windings_refined = False
        if item.color_hex_source == "estimate":
            windings = next((b for b in det_boxes if b.label == "spool_windings"), None)
            if windings is not None:
                px1 = max(0, int(windings.x1 * img_bgr.shape[1]))
                py1 = max(0, int(windings.y1 * img_bgr.shape[0]))
                px2 = min(img_bgr.shape[1], int(windings.x2 * img_bgr.shape[1]))
                py2 = min(img_bgr.shape[0], int(windings.y2 * img_bgr.shape[0]))
                w_crop = img_bgr[py1:py2, px1:px2]
                if w_crop.size > 0 and min(w_crop.shape[:2]) >= 16:
                    w_hex, w_fam, _ = extract_filament_color(w_crop)
                    item.color_hex = w_hex
                    item.color_family = w_fam
                    if item.color_name in COLOR_FAMILIES or item.color_name.lower() == "unknown":
                        item.color_name = "unknown"
                    windings_refined = True

        # Run Visual Multi-Head ONNX classifier predictions before fusion
        vis_preds = self._run_visual_onnx(img_bgr)

        # Fuse trained Text Parser predictions when OCR text is present
        tp_ven = "Unknown"
        tp_prob = 0.0
        if self.text_parser is not None and ocr_lines:
            joined_lower = joined_ocr.lower()
            if heur_vendor == "Unknown" or len(ocr_vendor_cands) > 1:
                v_probs = self.text_parser["vendor_pipe"].predict_proba([joined_ocr])[0]
                v_best_idx = int(np.argmax(v_probs))
                tp_prob = float(v_probs[v_best_idx])
                tp_ven = str(self.text_parser["vendor_pipe"].classes_[v_best_idx])

            if item.material == "PLA" and "pla" not in joined_lower and "material_pipe" in self.text_parser:
                m_probs = self.text_parser["material_pipe"].predict_proba([joined_ocr])[0]
                m_best_idx = int(np.argmax(m_probs))
                if float(m_probs[m_best_idx]) >= 0.45:
                    new_mat = str(self.text_parser["material_pipe"].classes_[m_best_idx])
                    if new_mat != item.material:
                        item.material = new_mat
                        item.nozzle_range = None
                        item.bed_range = None

            if item.line in ("PLA", "PETG", "ABS", "ASA"):
                l_probs = self.text_parser["line_pipe"].predict_proba([joined_ocr])[0]
                l_best_idx = int(np.argmax(l_probs))
                if float(l_probs[l_best_idx]) >= 0.42:
                    new_line = str(self.text_parser["line_pipe"].classes_[l_best_idx])
                    if new_line != item.line:
                        item.line = new_line
                        item.material = self.matcher.normalize_material(item.material, item.line)
                        item.nozzle_range = None
                        item.bed_range = None

        v_ven, v_conf = vis_preds.get("visual_vendor", ("Other", 0.0))
        if len(ocr_vendor_cands) > 1:
            if v_ven in ocr_vendor_cands and v_conf >= 0.70:
                item.vendor = v_ven
            elif tp_ven in ocr_vendor_cands and tp_prob >= 0.50:
                item.vendor = tp_ven
        elif heur_vendor == "Unknown":
            if v_ven != "Other" and v_conf >= 0.85 and v_conf > tp_prob:
                item.vendor = v_ven
                item.confidence = max(item.confidence, round(min(0.88, 0.78 + (v_conf - 0.85) * 0.50), 3))
                if "vendor" not in item.estimates:
                    item.estimates.append("vendor")
            elif tp_prob >= 0.35:
                item.vendor = tp_ven
                item.confidence = round(min(0.90, 0.65 + tp_prob * 0.25), 3)
            elif v_ven != "Other" and v_conf >= 0.45:
                item.vendor = v_ven
                item.confidence = round(min(0.76, 0.55 + v_conf * 0.20), 3)
                if "vendor" not in item.estimates:
                    item.estimates.append("vendor")

        if vis_preds:
            v_col, c_conf = vis_preds["color_family"]
            v_fin, f_conf = vis_preds["visual_finish"]

            # If color was only visually estimated (no explicit label color text), refine with ONNX color family when confident
            if item.color_hex_source == "estimate" and c_conf >= 0.50:
                if not windings_refined or c_conf >= 0.75:
                    item.color_family = v_col
                    item.color_name = "unknown"

            # If no label text at all, infer subtype from visual finish
            if not ocr_lines:
                item.source_type = "exposed_spool"
                if v_fin in ("Matte", "Silk", "Galaxy", "Translucent", "Wood", "Glow") and f_conf >= 0.45:
                    item.line = f"{item.material} {v_fin}"
                elif v_fin == "CF" and f_conf >= 0.50:
                    item.material = "PLA-CF"
                    item.line = "PLA-CF"

        # Robust sealed state determination
        joined_ocr_lower = " ".join(ocr_lines).lower()
        has_bag_text = any(
            w in joined_ocr_lower
            for w in (
                "desiccant", "silica", "silica gel", "do not eat", "throw away",
                "vacuum sealed", "vacuum bag", "keep away from children",
            )
        )

        v_state, s_conf = vis_preds.get("sealed_state", ("bare_spool", 0.0))

        if has_bag_text:
            item.sealed = True
            item.sealed_reason = "in vacuum bag"
        elif v_state == "bare_spool" and s_conf >= 0.50:
            item.sealed = False
            item.sealed_reason = "bare spool"
            item.source_type = "spool_label"
        elif v_state == "in_box" and s_conf >= 0.70 and not is_spool:
            item.sealed = True
            item.sealed_reason = "in box"
            item.source_type = "box_label"
        elif v_state == "in_vacuum_bag" and s_conf >= 0.85:
            item.sealed = True
            item.sealed_reason = "in vacuum bag"
        elif is_spool or any(w in joined_ocr_lower for w in ("spool", "reusable spool", "tare", "flange")):
            # Spool images without explicit vacuum bag text or high-confidence bag packaging are opened spools
            item.sealed = False
            item.sealed_reason = "bare spool"
            item.source_type = "spool_label"
        else:
            item.sealed = True
            item.sealed_reason = "in box"

        item = self.matcher.enrich_item(item)
        return item

    def predict(self, images: list[str | Path | bytes | np.ndarray], hint: str = "") -> IntakeReadResult:
        t0 = time.perf_counter()
        items: list[FilamentItem] = []
        questions: list[str] = []

        for idx, img in enumerate(images):
            it = self.predict_image(img, photo_index=idx)
            # Group photos of identical vendor + line + color, or fuse multi-angle photos of one spool
            merged = False
            for existing in items:
                exact_same = (
                    existing.vendor == it.vendor
                    and existing.line == it.line
                    and existing.color_name == it.color_name
                    and existing.sealed == it.sealed
                )
                multi_angle_same = (
                    (existing.vendor == "Unknown" or it.vendor == "Unknown")
                    and not (existing.vendor == "Unknown" and it.vendor == "Unknown")
                    and existing.color_family == it.color_family
                    and existing.material == it.material
                    and existing.sealed == it.sealed
                )
                if exact_same or multi_angle_same:
                    if idx not in existing.photos:
                        existing.photos.append(idx)
                    if existing.vendor == "Unknown" and it.vendor != "Unknown":
                        existing.vendor = it.vendor
                        existing.confidence = max(existing.confidence, it.confidence)
                        if existing.line == existing.material and it.line != it.material:
                            existing.line = it.line
                        if existing.color_name in ("unknown", existing.color_family) and it.color_name not in ("unknown", it.color_family):
                            existing.color_name = it.color_name
                            existing.color_hex = it.color_hex
                        self.matcher.enrich_item(existing)
                    elif it.vendor != "Unknown" and existing.vendor == it.vendor:
                        if existing.line == existing.material and it.line != it.material:
                            existing.line = it.line
                            self.matcher.enrich_item(existing)
                    merged = True
                    break
            if not merged:
                items.append(it)

        for it in items:
            if it.vendor == "Unknown":
                questions.append(f"Photo {it.photos[0]}: Please confirm filament brand (detected {it.line} {it.color_name}).")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return IntakeReadResult(
            items=items,
            questions=questions,
            tier="cpu-hybrid",
            latency_ms=elapsed_ms,
            model_name="filament-vision-server-cpu-v1",
        )
