"""Compatibility wrapper for RapidOCR v3+ (Python 3.10-3.14) and legacy rapidocr-onnxruntime."""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger("filament_vision_server.ocr")

try:
    from rapidocr import RapidOCR as _RapidOCRV3
    _HAS_RAPIDOCR_V3 = True
except ImportError:
    _RapidOCRV3 = None
    _HAS_RAPIDOCR_V3 = False

if not _HAS_RAPIDOCR_V3:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCRLegacy
else:
    _RapidOCRLegacy = None


def create_rapidocr(providers: list[str] | None = None) -> Any:
    """Create a RapidOCR engine using rapidocr v3+ (or legacy rapidocr-onnxruntime)."""
    providers = providers or []
    if _HAS_RAPIDOCR_V3:
        base_params: dict[str, Any] = {"Global.log_level": "warning"}
        accel_params = dict(base_params)
        if "CUDAExecutionProvider" in providers:
            accel_params["EngineConfig.onnxruntime.use_cuda"] = True
        elif "DmlExecutionProvider" in providers:
            accel_params["EngineConfig.onnxruntime.use_dml"] = True
        try:
            return _RapidOCRV3(params=accel_params)
        except Exception as exc:
            if accel_params != base_params:
                log.warning("Accelerated RapidOCR init failed (%s); falling back to CPU", exc)
                return _RapidOCRV3(params=base_params)
            raise

    legacy_kwargs: dict[str, Any] = {}
    if "CUDAExecutionProvider" in providers:
        legacy_kwargs = {"det_use_cuda": True, "cls_use_cuda": True, "rec_use_cuda": True}
    elif "DmlExecutionProvider" in providers:
        legacy_kwargs = {"det_use_dml": True, "cls_use_dml": True, "rec_use_dml": True}
    try:
        return _RapidOCRLegacy(**legacy_kwargs) if legacy_kwargs else _RapidOCRLegacy()
    except Exception as exc:
        log.warning("Accelerated RapidOCR init failed (%s); falling back to CPU", exc)
        return _RapidOCRLegacy()


def run_rapidocr(ocr_engine: Any, img_bgr: np.ndarray) -> list[tuple[Any, str, float]]:
    """Run OCR on an image and return normalized (polygon_box, text, score) tuples."""
    res = ocr_engine(img_bgr)
    if res is None:
        return []
    if isinstance(res, tuple):
        items = res[0]
        return list(items) if items else []
    txts = getattr(res, "txts", None)
    if not txts:
        return []
    boxes = getattr(res, "boxes", None)
    scores = getattr(res, "scores", None)
    if boxes is None:
        boxes = [None] * len(txts)
    if scores is None:
        scores = [1.0] * len(txts)
    return [(box, str(txt), float(score)) for box, txt, score in zip(boxes, txts, scores)]
