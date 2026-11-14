"""Auto-annotator for real seed images using RapidOCR (multi-angle) + Teacher VLM (RTX 3090) + CatalogMatcher."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.color_lab import extract_filament_color
from ..cpu_tier.ocr_helper import create_rapidocr, run_rapidocr
from ..xml_io import (
    DATA_DIR,
    PROJECT_ROOT,
    AnnotatedSample,
    BoundingBox,
    FilamentItem,
    save_dataset_xml,
)


def extract_multi_angle_ocr_and_boxes(
    img_bgr: np.ndarray,
    ocr_engine: Any,
) -> tuple[list[str], list[BoundingBox]]:
    """Run RapidOCR at 0 deg and 90 deg CCW (for vertical box stickers) and locate label & spool boxes."""
    h, w = img_bgr.shape[:2]
    ocr_lines: list[str] = []
    text_boxes_norm: list[tuple[float, float, float, float, float]] = []

    # 1. Standard upright OCR
    res0 = run_rapidocr(ocr_engine, img_bgr)
    if res0:
        for poly, text, score in res0:
            conf = float(score)
            if conf < 0.42 or not str(text).strip():
                continue
            ocr_lines.append(str(text).strip())
            pts = np.array(poly, dtype=np.float32)
            x1 = float(np.clip(np.min(pts[:, 0]) / w, 0.0, 1.0))
            y1 = float(np.clip(np.min(pts[:, 1]) / h, 0.0, 1.0))
            x2 = float(np.clip(np.max(pts[:, 0]) / w, 0.0, 1.0))
            y2 = float(np.clip(np.max(pts[:, 1]) / h, 0.0, 1.0))
            text_boxes_norm.append((x1, y1, x2, y2, conf))

    # 2. If upright OCR lacks clear filament keywords, try 90-deg CW and 90-deg CCW rotations (handles vertical labels like 4910.jpg)
    def _keyword_hits(lines: list[str]) -> int:
        joined = " ".join(lines).lower()
        kws = ("pla", "petg", "abs", "asa", "tpu", "1.75", "temp", "color", "colour", "filament", "1kg", "print")
        return sum(1 for k in kws if k in joined)

    base_hits = _keyword_hits(ocr_lines)
    if base_hits < 2:
        for rot_code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
            rot_img = cv2.rotate(img_bgr, rot_code)
            rh, rw = rot_img.shape[:2]
            res_rot = run_rapidocr(ocr_engine, rot_img)
            if not res_rot:
                continue
            rot_lines = [str(t).strip() for _, t, s in res_rot if float(s) >= 0.42 and str(t).strip()]
            rot_hits = _keyword_hits(rot_lines)
            if rot_hits > base_hits or (rot_hits == base_hits and len(" ".join(rot_lines)) > len(" ".join(ocr_lines)) + 15):
                base_hits = rot_hits
                ocr_lines = rot_lines
                text_boxes_norm.clear()
                for poly, text, score in res_rot:
                    conf = float(score)
                    if conf < 0.42:
                        continue
                    pts = np.array(poly, dtype=np.float32)
                    rx1, ry1 = float(np.min(pts[:, 0])) / rw, float(np.min(pts[:, 1])) / rh
                    rx2, ry2 = float(np.max(pts[:, 0])) / rw, float(np.max(pts[:, 1])) / rh
                    if rot_code == cv2.ROTATE_90_CLOCKWISE:
                        ox1, oy1 = float(np.clip(ry1, 0.0, 1.0)), float(np.clip(1.0 - rx2, 0.0, 1.0))
                        ox2, oy2 = float(np.clip(ry2, 0.0, 1.0)), float(np.clip(1.0 - rx1, 0.0, 1.0))
                    else:
                        ox1, oy1 = float(np.clip(1.0 - ry2, 0.0, 1.0)), float(np.clip(rx1, 0.0, 1.0))
                        ox2, oy2 = float(np.clip(1.0 - ry1, 0.0, 1.0)), float(np.clip(rx2, 0.0, 1.0))
                    text_boxes_norm.append((ox1, oy1, ox2, oy2, conf))
                if base_hits >= 3:
                    break

    boxes: list[BoundingBox] = []
    # Detect whether image shows a large circular spool vs rectangular box(es)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    small_gray = cv2.resize(gray, (320, int(320 * h / max(w, 1))))
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(small_gray, (9, 9), 2.0),
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=120,
        param1=100,
        param2=58,
        minRadius=95,
        maxRadius=160,
    )
    is_spool = circles is not None and len(ocr_lines) < 22

    if text_boxes_norm:
        # Compute bounding envelope of high-density text lines as the primary label region
        xs1 = [b[0] for b in text_boxes_norm]
        ys1 = [b[1] for b in text_boxes_norm]
        xs2 = [b[2] for b in text_boxes_norm]
        ys2 = [b[3] for b in text_boxes_norm]
        pad_x, pad_y = 0.025, 0.025
        lx1 = max(0.0, min(xs1) - pad_x)
        ly1 = max(0.0, min(ys1) - pad_y)
        lx2 = min(1.0, max(xs2) + pad_x)
        ly2 = min(1.0, max(ys2) + pad_y)
        lbl_type = "spool_label" if is_spool else "box_label"
        boxes.append(BoundingBox(label=lbl_type, x1=lx1, y1=ly1, x2=lx2, y2=ly2, confidence=0.95))

    if is_spool:
        boxes.append(BoundingBox(label="spool_flange", x1=0.04, y1=0.04, x2=0.96, y2=0.96, confidence=0.90))
        boxes.append(BoundingBox(label="spool_windings", x1=0.08, y1=0.15, x2=0.92, y2=0.88, confidence=0.88))
    elif not boxes:
        boxes.append(BoundingBox(label="box_label", x1=0.10, y1=0.10, x2=0.90, y2=0.90, confidence=0.75))

    return ocr_lines, boxes


def parse_ocr_heuristic(
    ocr_lines: list[str],
    img_bgr: np.ndarray,
    matcher: CatalogMatcher,
    is_spool: bool = False,
) -> tuple[FilamentItem, str]:
    """Parse OCR lines + visual color into a structured FilamentItem and visual_finish."""
    full_text = " | ".join(ocr_lines)
    low_text = full_text.lower()

    # 1. Vendor resolution
    vendor, v_score = matcher.resolve_vendor(full_text, min_score=78.0)
    if vendor == "Unknown":
        # Check domain names or secondary clues in OCR
        domain_vendors = {
            "phrozen": "Phrozen",
            "overture": "OVERTURE",
            "sunlu": "SUNLU",
            "elegoo": "ELEGOO",
            "bambu": "Bambu Lab",
            "anycubic": "Anycubic",
            "creality": "Creality",
            "cr-abs": "Creality",
            "cr-petg": "Creality",
            "cr-pla": "Creality",
            "comgrow": "Comgrow",
            "jayo": "JAYO",
            "esun": "eSUN",
            "polymaker": "Polymaker",
            "panchroma": "Polymaker",
            "hatchbox": "Hatchbox",
            "prusament": "Prusament",
            "eryone": "Eryone",
            "inland": "Inland",
            "amolen": "Amolen",
            "kingroon": "Kingroon",
            "geeetech": "GEEETECH",
            "flashforge": "Flashforge",
            "push plastic": "Push Plastic",
            "atomic": "Atomic Filament",
            "3dxtech": "3DXTech",
            "colorfabb": "colorFabb",
            "formfutura": "FormFutura",
            "fiberlogy": "Fiberlogy",
            "fillamentum": "Fillamentum",
            "extrudr": "Extrudr",
            "deeplee": "DEEPLEE",
            "cc3d": "CC3D",
            "cctree": "CCTREE",
            "gizmo dorks": "Gizmo Dorks",
            "iemai": "IEMAI",
            "inslogic": "Inslogic",
            "kexcelled": "Kexcelled",
        }
        for kw, vcanon in domain_vendors.items():
            if kw in low_text:
                vendor = vcanon
                break

    # 2. Extract printed temperature ranges
    nozzle_range = None
    bed_range = None
    temp_matches = re.findall(r"\b(1[7-9]\d|2\d\d|3[0-5]\d)\s*[-~to]+\s*(1[8-9]\d|2\d\d|3[0-6]\d)\s*°?\s*[cC]?\b", full_text)
    if temp_matches:
        # Highest nozzle range per intake.py rule
        best_pair = max(temp_matches, key=lambda p: int(p[1]))
        nozzle_range = f"{best_pair[0]}-{best_pair[1]}"

    bed_matches = re.findall(r"(?:bed|plate|hot\s*bed)[^0-9]{0,18}(\d{2,3})\s*[-~to]+\s*(\d{2,3})", low_text)
    if bed_matches:
        bed_range = f"{bed_matches[0][0]}-{bed_matches[0][1]}"

    # 3. Extract diameter & net weight
    diameter = 1.75
    if "2.85" in low_text:
        diameter = 2.85
    net_grams = 1000
    kg_m = re.search(r"\b(\d+(?:\.\d+)?)\s*kg\b", low_text)
    if kg_m:
        try:
            val = float(kg_m.group(1))
            if 0.2 <= val <= 10.0:
                net_grams = int(round(val * 1000))
        except ValueError:
            pass
    else:
        g_m = re.search(r"\b(250|500|750|1000|2000|3000)\s*g\b", low_text)
        if g_m:
            net_grams = int(g_m.group(1))

    # 4. Extract product line & base material
    line = "PLA"
    line_patterns = [
        r"\b(HIGH\s*SPEED\s*PLA\+?\s*2\.0|HS\s*PLA\+?\s*2\.0|High\s*Speed\s*PLA\+?)\b",
        r"\b(RAPID\s*PLA\+|Rapid\s*PLA\+|RAPID\s*PETG|Rapid\s*PETG)\b",
        r"\b(PAHT-CF|PA12-CF|PA6-CF|PA6-GF|PA-CF|PET-CF|PETG-CF|PLA-CF|ASA-CF|ABS-GF|PPA-CF|PPS-CF)\b",
        r"\b(PLA\s*Basic|PLA\s*Matte|PLA\s*Silk|Silk\s*PLA\+?|Matte\s*PLA|PLA\s*Pro|PLA\+\s*2\.0|PLA\+|PLA\s*Plus|PLA\s*High\s*Speed|PLA\s*Marble|PLA\s*Galaxy|PLA\s*Sparkle|PLA\s*Wood|PLA\s*Glow|PLA\s*Translucent|PLA\s*Metal|PLA\s*Aero)\b",
        r"\b(Elite\s*PETG|PETG\s*HF|PETG\s*Basic|PETG\s*Pro|PETG\s*Matte|PETG\s*Translucent|High\s*Speed\s*PETG)\b",
        r"\b(CR-ABS|CR-PETG|CR-PLA|Hyper\s*PLA|Hyper\s*ABS|High\s*Speed\s*ABS|ASA\s*Aero)\b",
        r"\b(TPU\s*95A\s*HF|TPU\s*95A|TPU\s*85A|TPU\s*90A)\b",
        r"\b(PETG|PLA|ABS|ASA|TPU|PC|PVA|HIPS|PEI|PA)\b",
    ]
    for pat in line_patterns:
        matches = re.findall(pat, full_text, flags=re.IGNORECASE)
        if matches:
            counts: dict[str, int] = {}
            for m_str in matches:
                norm_m = re.sub(r"\s+", " ", m_str).strip()
                if norm_m.upper().startswith("HS PLA") and "2.0" in norm_m:
                    norm_m = "High Speed PLA+ 2.0"
                elif norm_m.upper() == "PLA HIGH SPEED":
                    norm_m = "High Speed PLA"
                counts[norm_m] = counts.get(norm_m, 0) + 1
            line = max(counts.items(), key=lambda kv: kv[1])[0]
            break

    material = matcher.normalize_material(line, full_text)

    # 5. Extract explicit color name from OCR
    color_name = ""
    color_patterns = [
        r"(?:colou?r)\s*[:：\-]\s*([A-Za-z][A-Za-z0-9\s\+\*]{1,28}?)(?=\s*\||$|\s+1\.75|\s+2\.85|\s+Print|\s+Net|\s+Made|\s+\d)",
        r"\b(Indigo\s*Blue|Titanium\s*Gr[ae]y|Carbon\s*Fiber\s*Black|Carbon\s*Fiber\s*Green|Silk\s*Copper|Silk\s*Gold|Silk\s*Silver|Silk\s*Black|Silk\s*White|Wood\s*Color|Mango\s*Yellow|Jade\s*White|Ivory\s*White|Bone\s*White|Ash\s*Gr[ae]y|Nardo\s*Gr[ae]y|Charcoal|Scarlet\s*Red|Mandarin\s*Orange|Lemon\s*Yellow|Grass\s*Green|Bambu\s*Green|Mistletoe\s*Green|Marine\s*Blue|Cobalt\s*Blue|Ice\s*Blue|Lilac\s*Purple|Sakura\s*Pink|Latte\s*Brown|Desert\s*Tan|Olive\s*Green|Space\s*Gr[ae]y|Burgundy\s*Red|Fire\s*Engine\s*Red|Metallic\s*Rose\s*Gold|Rose\s*Gold|Galaxy\s*Black|Midnight|Black\*2\+White\*2)\b",
        r"\b(Black|White|Gr[ae]y|Silver|Red|Orange|Yellow|Green|Blue|Purple|Pink|Brown|Beige|Gold|Bronze|Copper|Cyan|Magenta|Transparent|Clear|Natural|Wood|Marble|Rainbow)\b",
    ]
    for cpat in color_patterns:
        cm = re.search(cpat, full_text, flags=re.IGNORECASE)
        if cm:
            cand = re.sub(r"\s+", " ", cm.group(1)).strip(" -:|.,")
            if cand.lower() not in ("transition", "filament", "printer", "speed", "temp", "china", "silk pla", "pla", "petg", "abs", "asa", "tpu"):
                color_name = cand
                break

    # 6. Visual color & finish extraction from image
    vis_hex, vis_family, vis_finish = extract_filament_color(img_bgr, is_spool_crop=is_spool)
    if "matte" in line.lower() or "matte" in color_name.lower():
        vis_finish = "Matte"
    elif "silk" in line.lower() or "silk" in color_name.lower() or "metallic" in color_name.lower():
        vis_finish = "Silk"
    elif "cf" in material.lower() or "cf" in line.lower():
        vis_finish = "CF"
    elif "galaxy" in line.lower() or "sparkle" in line.lower() or "marble" in line.lower():
        vis_finish = "Galaxy"
    elif "translucent" in line.lower() or "transparent" in color_name.lower() or "clear" in color_name.lower():
        vis_finish = "Translucent"
    elif "wood" in line.lower() or "wood" in color_name.lower():
        vis_finish = "Wood"
    elif "glow" in line.lower() or "glow" in color_name.lower():
        vis_finish = "Glow"

    # Determine sealed state accurately
    if is_spool:
        # A spool is only sealed in a vacuum bag if explicit bag/desiccant cues are present
        has_bag_evidence = any(
            w in low_text
            for w in (
                "desiccant", "silica", "silica gel", "do not eat",
                "throw away", "vacuum bag", "vacuum sealed", "keep away from children",
            )
        )
        if has_bag_evidence:
            sealed = True
            sealed_reason = "in vacuum bag"
        else:
            # Standalone, unwrapped spool on printer, desk, or studio shot
            sealed = False
            sealed_reason = "bare spool"
    else:
        sealed = True
        sealed_reason = "in box"

    item = FilamentItem(
        vendor=vendor,
        material=material,
        line=line,
        color_name=color_name or vis_family,
        color_family=vis_family,
        color_hex=vis_hex,
        color_hex_source="estimate",
        net_grams=net_grams,
        diameter=diameter,
        nozzle_range=nozzle_range,
        bed_range=bed_range,
        sealed=sealed,
        sealed_reason=sealed_reason,
        source_type="spool_label" if is_spool else "box_label",
        confidence=0.90 if vendor != "Unknown" else 0.72,
    )
    item = matcher.enrich_item(item)
    return item, vis_finish


def annotate_seed_dataset(
    converted_dir: str | Path = DATA_DIR / "seed_converted",
    use_vlm_teacher: bool = True,
) -> dict[str, Path]:
    """Annotate all 134 converted seed images and write seed_labels.xml, gold_test_split.xml, train_split.xml, val_split.xml."""
    conv_root = Path(converted_dir)
    jpg_files = sorted(conv_root.glob("*.jpg"), key=lambda p: p.name.lower())
    ocr_engine = create_rapidocr()
    matcher = CatalogMatcher()

    vlm_model = None
    vlm_processor = None
    if use_vlm_teacher:
        try:
            import torch
            from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

            if torch.cuda.is_available():
                model_id = "Qwen/Qwen2.5-VL-7B-Instruct"
                print(f"[AutoLabeler] Loading Teacher VLM {model_id} on {torch.cuda.get_device_name(0)}...")
                vlm_processor = AutoProcessor.from_pretrained(
                    model_id, min_pixels=256 * 28 * 28, max_pixels=768 * 28 * 28
                )
                vlm_model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    model_id,
                    torch_dtype=torch.bfloat16,
                    device_map="cuda:0",
                )
                vlm_model.eval()
        except Exception as exc:
            print(f"[AutoLabeler] VLM Teacher unavailable ({exc}); using multi-angle RapidOCR + CatalogMatcher.")
            vlm_model = None

    samples: list[AnnotatedSample] = []
    for idx, jpg_path in enumerate(jpg_files):
        img_bgr = cv2.imread(str(jpg_path))
        if img_bgr is None:
            continue
        h, w = img_bgr.shape[:2]
        ocr_lines, boxes = extract_multi_angle_ocr_and_boxes(img_bgr, ocr_engine)
        is_spool = any(b.label == "spool_flange" for b in boxes)
        item, vis_finish = parse_ocr_heuristic(ocr_lines, img_bgr, matcher, is_spool=is_spool)

        # Refine with Teacher VLM on RTX 3090 when OCR is ambiguous or vendor/color is missing
        if vlm_model is not None and vlm_processor is not None:
            try:
                import torch

                ocr_hint = "; ".join(ocr_lines[:20]) if ocr_lines else "(no text detected)"
                prompt = (
                    "You are annotating a 3D printer filament photo (either filament box(es) or a filament spool). "
                    f"OCR text detected: {ocr_hint}\n"
                    "Identify the primary/centered filament in the image and respond ONLY with a JSON object having keys: "
                    '{"vendor": "<Brand Name or Unknown>", "line": "<Product Line without color, e.g. PLA Basic, PETG-CF, Rapid PLA+, High Speed PLA+ 2.0>", '
                    '"material": "<Base polymer: PLA, PETG, ABS, ASA, TPU, PC, PAHT-CF, PETG-CF, PLA-CF, etc.>", '
                    '"color_name": "<Exact printed color name or visual color>", '
                    '"color_family": "<One of: Black, White, Gray/Silver, Red, Orange, Yellow, Green, Blue, Purple, Pink, Brown/Beige, Gold/Bronze, Transparent/Natural, Multi-color/Rainbow>", '
                    '"source_type": "<box_label | spool_label | exposed_spool>", '
                    '"sealed": true/false}'
                )
                pil_img = Image.open(jpg_path).convert("RGB")
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "image": pil_img},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]
                text_input = vlm_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                inputs = vlm_processor(text=[text_input], images=[pil_img], padding=True, return_tensors="pt").to("cuda:0")
                with torch.no_grad():
                    gen_ids = vlm_model.generate(**inputs, max_new_tokens=180, do_sample=False)
                out_ids = [out[len(inp):] for inp, out in zip(inputs.input_ids, gen_ids)]
                reply = vlm_processor.batch_decode(out_ids, skip_special_tokens=True)[0]
                m_json = re.search(r"\{.*?\}", reply, flags=re.DOTALL)
                if m_json:
                    vdata = json.loads(m_json.group(0))
                    v_vendor = str(vdata.get("vendor") or "").strip()
                    if v_vendor and v_vendor.lower() != "unknown" and item.vendor == "Unknown":
                        item.vendor = v_vendor
                    v_line = str(vdata.get("line") or "").strip()
                    if v_line and (item.line in ("PLA", "PETG", "ABS", "ASA") or len(v_line) > len(item.line)):
                        item.line = v_line
                    v_mat = str(vdata.get("material") or "").strip()
                    if v_mat:
                        item.material = matcher.normalize_material(v_mat, item.line)
                    v_col = str(vdata.get("color_name") or "").strip()
                    if v_col and v_col.lower() not in ("unknown", "none", ""):
                        item.color_name = v_col
                    v_fam = str(vdata.get("color_family") or "").strip()
                    if v_fam in matcher.color_families:
                        item.color_family = v_fam
                    v_src = str(vdata.get("source_type") or "").strip()
                    if v_src in ("box_label", "spool_label", "exposed_spool"):
                        item.source_type = v_src
                    if isinstance(vdata.get("sealed"), bool):
                        item.sealed = vdata["sealed"]
                    item = matcher.enrich_item(item)
            except Exception as exc:
                print(f"[AutoLabeler] VLM refinement warning on {jpg_path.name}: {exc}")

        # Stratified split assignment: every 3rd image -> gold_test (44 images), every 6th -> val (18 images), rest -> train (72 images)
        if idx % 3 == 0 and len([s for s in samples if s.split == "gold_test"]) < 44:
            split = "gold_test"
        elif idx % 6 == 1:
            split = "val"
        else:
            split = "train"

        orig_heic = f"seed images/{jpg_path.stem}.HEIC"
        rel_jpg = f"data/seed_converted/{jpg_path.name}"
        sample = AnnotatedSample(
            sample_id=f"seed_{jpg_path.stem}",
            image_path=rel_jpg,
            original_path=orig_heic,
            width=w,
            height=h,
            split=split,
            item=item,
            boxes=boxes,
            ocr_lines=ocr_lines,
            visual_finish=vis_finish,
        )
        samples.append(sample)
        if (idx + 1) % 20 == 0 or (idx + 1) == len(jpg_files):
            print(f"[AutoLabeler] Annotated {idx + 1}/{len(jpg_files)} seed images...")

    if vlm_model is not None:
        import torch

        del vlm_model
        torch.cuda.empty_cache()

    ann_dir = DATA_DIR / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    seed_xml = save_dataset_xml(ann_dir / "seed_labels.xml", samples, split_name="all_seed")
    gold_xml = save_dataset_xml(
        ann_dir / "gold_test_split.xml",
        [s for s in samples if s.split == "gold_test"],
        split_name="gold_test",
    )
    train_xml = save_dataset_xml(
        ann_dir / "train_split.xml",
        [s for s in samples if s.split == "train"],
        split_name="train",
    )
    val_xml = save_dataset_xml(
        ann_dir / "val_split.xml",
        [s for s in samples if s.split == "val"],
        split_name="val",
    )
    return {
        "seed_labels": seed_xml,
        "gold_test": gold_xml,
        "train": train_xml,
        "val": val_xml,
    }
