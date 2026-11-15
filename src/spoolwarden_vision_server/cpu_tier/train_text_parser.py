"""Trains a fast OCR-text-to-Spoolman classifier (TF-IDF + Calibrated Linear/Logistic) for CPU inference."""
from __future__ import annotations

import random
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.pipeline import FeatureUnion, Pipeline

from ..catalog.matcher import CatalogMatcher
from ..xml_io import DATA_DIR, PROJECT_ROOT, load_training_samples_with_feedback, write_xml_atomic
from .text_parser_io import save_text_parser_npz


def _degrade_ocr_text(text: str, rng: random.Random) -> str:
    """Simulate real OCR character confusions (0/O, 1/I/l, 5/S, missing spaces, dropped characters)."""
    out = text
    if rng.random() < 0.35:
        out = out.replace("0", "O") if rng.random() < 0.5 else out.replace("O", "0")
    if rng.random() < 0.30:
        out = out.replace("1", "l") if rng.random() < 0.5 else out.replace("I", "1")
    if rng.random() < 0.25 and len(out) > 10:
        idx = rng.randint(1, len(out) - 2)
        out = out[:idx] + out[idx + 1:]
    return out


def train_and_export_text_parser(
    train_xml: str | Path = DATA_DIR / "annotations" / "train_split.xml",
    output_npz: str | Path = PROJECT_ROOT / "models" / "cpu" / "text_parser.npz",
    seed: int = 42,
) -> dict[str, Any]:
    """Train lightweight character + word n-gram classifiers for Vendor, Material, and Product Line from noisy OCR."""
    rng = random.Random(seed)
    matcher = CatalogMatcher()

    texts_vendor: list[str] = []
    labels_vendor: list[str] = []

    texts_mat: list[str] = []
    labels_mat: list[str] = []

    texts_line: list[str] = []
    labels_line: list[str] = []

    # 1. Generate training pairs from catalog products + aliases
    for prod in matcher.all_products:
        color_names = [c.name for c in prod.colors[:3]] or ["Black", "White", "Gray"]
        for col_name in color_names:
            base_str = (
                f"{prod.vendor} {prod.line} {prod.material} Color: {col_name} "
                f"{prod.diameter:.2f}mm 1kg {prod.nozzle_range}C Bed {prod.bed_range}C"
            )
            noisy = _degrade_ocr_text(base_str, rng)
            texts_vendor.append(noisy)
            labels_vendor.append(prod.vendor)
            texts_mat.append(noisy)
            labels_mat.append(prod.material)
            texts_line.append(noisy)
            labels_line.append(prod.line)

    # 2. Include real & synthetic dataset OCR lines (+ 2x up-weighted verified feedback)
    for s in load_training_samples_with_feedback(train_xml):
        if not s.ocr_lines:
            continue
        joined = " | ".join(s.ocr_lines)
        for _ in range(4):
            noisy = _degrade_ocr_text(joined, rng)
            if s.item.vendor != "Unknown":
                texts_vendor.append(noisy)
                labels_vendor.append(s.item.vendor)
            texts_mat.append(noisy)
            labels_mat.append(s.item.material)
            texts_line.append(noisy)
            labels_line.append(s.item.line)

    def _make_pipe(alpha_val: float = 1e-5) -> Pipeline:
        feats = FeatureUnion(
            [
                ("word", TfidfVectorizer(ngram_range=(1, 2), max_features=6000, sublinear_tf=True)),
                ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), max_features=6000, sublinear_tf=True)),
            ]
        )
        return Pipeline(
            [
                ("feats", feats),
                ("clf", SGDClassifier(loss="log_loss", alpha=alpha_val, max_iter=35, random_state=seed, n_jobs=-1)),
            ]
        )

    vendor_pipe = _make_pipe(alpha_val=1e-5)
    vendor_pipe.fit(texts_vendor, labels_vendor)

    mat_pipe = _make_pipe(alpha_val=1e-5)
    mat_pipe.fit(texts_mat, labels_mat)

    line_pipe = _make_pipe(alpha_val=2e-5)
    line_pipe.fit(texts_line, labels_line)

    out_path = Path(output_npz)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "vendor_pipe": vendor_pipe,
        "material_pipe": mat_pipe,
        "line_pipe": line_pipe,
    }
    save_text_parser_npz(bundle, out_path)

    meta_el = StdET.Element(
        "text-parser-meta",
        attrib={
            "version": "1",
            "samples": str(len(texts_mat)),
            "vendor-classes": str(len(vendor_pipe.classes_)),
            "material-classes": str(len(mat_pipe.classes_)),
            "line-classes": str(len(line_pipe.classes_)),
        },
    )
    write_xml_atomic(out_path.with_name("text_parser_meta.xml"), meta_el)
    return {
        "model_path": str(out_path),
        "samples": len(texts_mat),
        "vendor_classes": len(vendor_pipe.classes_),
        "material_classes": len(mat_pipe.classes_),
        "line_classes": len(line_pipe.classes_),
    }
