"""Community and real-world spool harvester targeting Reddit community photos,
user forum setup photos, and real-world brand spools to improve brand detection.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.ocr_helper import create_rapidocr
from ..xml_io import (
    DATA_DIR,
    AnnotatedSample,
    save_dataset_xml,
)
from .auto_labeler import parse_ocr_heuristic
from .spool_verifier import (
    fetch_bing_async_candidates,
    http_get_bytes,
    verify_real_spool_or_box_image,
)

COMMUNITY_QUERIES: list[tuple[str, str, str, str]] = [
    # Bambu Lab
    ("Bambu Lab", "PLA Basic", "PLA", "Reddit r/BambuLab Bambu Lab PLA Basic spool photo"),
    ("Bambu Lab", "PLA Matte", "PLA", "Reddit r/BambuLab Bambu Lab PLA Matte spool label"),
    ("Bambu Lab", "PETG HF", "PETG", "Reddit r/BambuLab Bambu Lab PETG HF spool"),
    ("Bambu Lab", "PLA Basic", "PLA", "Reddit r/3Dprinting Bambu Lab filament reusable spool photo"),
    ("Bambu Lab", "PLA-CF", "PLA-CF", "Reddit Bambu Lab carbon fiber spool label"),
    ("Bambu Lab", "ABS", "ABS", "Reddit Bambu Lab ABS filament spool box"),
    ("Bambu Lab", "PETG Translucent", "PETG", "Bambu Lab spool in AMS photo Reddit"),
    ("Bambu Lab", "PLA Silk", "PLA", "Reddit Bambu Lab PLA Silk spool label photo"),

    # ELEGOO
    ("ELEGOO", "Rapid PLA+", "PLA", "Reddit r/elegoo ELEGOO Rapid PLA+ spool photo"),
    ("ELEGOO", "Rapid PETG", "PETG", "Reddit r/elegoo ELEGOO Rapid PETG spool cardboard"),
    ("ELEGOO", "PLA", "PLA", "Reddit r/3Dprinting ELEGOO PLA filament cardboard spool"),
    ("ELEGOO", "Matte PLA", "PLA", "Reddit ELEGOO Matte PLA spool sticker photo"),
    ("ELEGOO", "Rapid PLA+", "PLA", "ELEGOO cardboard spool label photo Reddit"),
    ("ELEGOO", "PETG Pro", "PETG", "Reddit r/elegoo PETG Pro filament spool photo"),
    ("ELEGOO", "PLA+", "PLA", "Reddit ELEGOO PLA+ 1.75mm spool label"),

    # SUNLU & JAYO
    ("SUNLU", "PLA+", "PLA", "Reddit r/3Dprinting SUNLU PLA+ filament spool label"),
    ("SUNLU", "PETG", "PETG", "Reddit r/3Dprinting SUNLU PETG spool photo"),
    ("SUNLU", "PLA Meta", "PLA", "Reddit SUNLU PLA Meta spool label"),
    ("SUNLU", "High Speed PLA", "PLA", "Reddit SUNLU High Speed PLA spool"),
    ("SUNLU", "Silk PLA+", "PLA", "Reddit SUNLU Silk PLA spool photo"),
    ("SUNLU", "PLA+", "PLA", "JAYO PLA+ filament spool photo Reddit"),
    ("SUNLU", "PLA Matte", "PLA", "Reddit SUNLU Matte PLA spool label"),
    ("SUNLU", "ASA", "ASA", "Reddit SUNLU ASA filament spool"),

    # eSUN
    ("eSUN", "PLA+", "PLA", "Reddit r/3Dprinting eSUN PLA+ spool photo"),
    ("eSUN", "ePLA-Matte", "PLA", "Reddit eSUN ePLA Matte spool label"),
    ("eSUN", "PETG", "PETG", "Reddit eSUN PETG filament cardboard spool photo"),
    ("eSUN", "ePLA-HS", "PLA", "Reddit eSUN High Speed PLA spool"),
    ("eSUN", "ePA-CF", "PA-CF", "Reddit eSUN carbon fiber nylon spool label"),
    ("eSUN", "PLA+", "PLA", "eSUN PLA+ 1.75mm spool sticker label Reddit"),

    # Prusament & Prusa
    ("Prusament", "PLA", "PLA", "Reddit r/prusa3d Prusament PLA Galaxy Black spool photo"),
    ("Prusament", "PETG", "PETG", "Reddit r/prusa3d Prusament PETG spool label photo"),
    ("Prusament", "ASA", "ASA", "Reddit r/prusa3d Prusament ASA spool photo"),
    ("Prusament", "PC Blend", "PC", "Reddit Prusament PC Blend spool label"),
    ("Prusament", "PLA", "PLA", "Reddit Prusament honeycomb spool cardboard core"),

    # Creality
    ("Creality", "Hyper PLA", "PLA", "Reddit r/Creality Creality Hyper PLA spool photo"),
    ("Creality", "Ender PLA", "PLA", "Reddit Creality Ender PLA filament spool label"),
    ("Creality", "CR-PETG", "PETG", "Reddit Creality CR-PETG spool photo"),
    ("Creality", "Hyper PLA", "PLA", "Reddit K1 Creality Hyper Series spool label"),

    # Polymaker
    ("Polymaker", "PolyTerra PLA", "PLA", "Reddit r/3Dprinting PolyTerra PLA cardboard spool photo"),
    ("Polymaker", "PolyLite PLA", "PLA", "Reddit Polymaker PolyLite PLA spool label"),
    ("Polymaker", "PolyLite PETG", "PETG", "Reddit PolyLite PETG spool cardboard photo"),
    ("Polymaker", "Fiberon", "PETG-CF", "Reddit Polymaker Fiberon spool label"),

    # OVERTURE
    ("OVERTURE", "PLA", "PLA", "Reddit r/3Dprinting Overture PLA cardboard spool photo"),
    ("OVERTURE", "PETG", "PETG", "Reddit Overture PETG spool label photo"),
    ("OVERTURE", "Matte PLA", "PLA", "Reddit Overture Matte PLA spool photo"),
    ("OVERTURE", "Super Cardboard PLA", "PLA", "Reddit Overture spool weight ruler label"),

    # HATCHBOX & Inland & US Brands
    ("Hatchbox", "PLA", "PLA", "Reddit r/3Dprinting Hatchbox PLA spool label photo"),
    ("Hatchbox", "PETG", "PETG", "Reddit Hatchbox PETG spool photo"),
    ("Inland", "PLA+", "PLA", "Reddit r/3Dprinting Inland PLA+ spool Micro Center photo"),
    ("Inland", "PETG", "PETG", "Reddit Inland spool cardboard label Micro Center"),
    ("Printed Solid", "Jessie PLA", "PLA", "Reddit Printed Solid Jessie PLA spool photo"),
    ("MatterHackers", "Build Series PLA", "PLA", "Reddit MatterHackers Build Series PLA spool"),
    ("Anycubic", "PLA", "PLA", "Reddit Anycubic High Speed PLA spool photo"),
    ("Flashforge", "Burnt Titanium PLA", "PLA", "Reddit Flashforge PLA spool photo")
]


def harvest_community_real_world(target_count: int = 150) -> Path:
    """Harvest real-world user photos from 3D printing community setups and verify them."""
    out_dir = DATA_DIR / "harvested"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_xml = DATA_DIR / "annotations" / "brand_community_realworld.xml"

    ocr = create_rapidocr()
    matcher = CatalogMatcher()
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()
    samples: list[AnnotatedSample] = []

    # Queue candidate image URLs from Bing async Reddit queries
    candidates: list[tuple[str, str, str, str, str]] = []
    for v_hint, l_hint, m_hint, q_str in COMMUNITY_QUERIES:
        for u in fetch_bing_async_candidates(q_str, max_results=25):
            u_clean = u.split("?")[0]
            if u_clean in seen_urls:
                continue
            seen_urls.add(u_clean)
            candidates.append((v_hint, l_hint, m_hint, f"{v_hint} {l_hint}", u))

    print(f"[CommunityHarvester] Queued {len(candidates)} candidate URLs from community queries.")

    for v_hint, l_hint, m_hint, title_hint, img_url in candidates:
        if len(samples) >= target_count:
            break

        raw_img = http_get_bytes(img_url, timeout=9.0)
        if not raw_img or len(raw_img) < 6000:
            continue
        sha1 = hashlib.sha1(raw_img).hexdigest()
        if sha1 in seen_hashes:
            continue
        arr = np.frombuffer(raw_img, dtype=np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            continue

        ok, ocr_lines, bboxes, _ = verify_real_spool_or_box_image(
            img_bgr,
            ocr,
            required_vendor=v_hint,
            allow_trusted_brand_Tokens=[v_hint],
        )
        if not ok:
            continue

        seen_hashes.add(sha1)
        idx = len(samples)
        sid = f"community_{idx:04d}"
        rel_path = f"data/harvested/{sid}.jpg"
        abs_path = out_dir / f"{sid}.jpg"

        h, w = img_bgr.shape[:2]
        if max(h, w) > 1024:
            sc = 1024.0 / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
            h, w = img_bgr.shape[:2]
        cv2.imwrite(str(abs_path), img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

        item, vis_finish = parse_ocr_heuristic(ocr_lines + [title_hint], img_bgr, matcher, is_spool=True)
        if item.vendor == "Unknown":
            item.vendor = v_hint
        if l_hint and item.line in ("PLA", "PETG", "ABS", "ASA", "TPU"):
            item.line = l_hint
        if m_hint:
            item.material = matcher.normalize_material(m_hint, item.line)
        item = matcher.enrich_item(item)

        split_assign = "val" if (idx % 7 == 0) else "train"
        sample = AnnotatedSample(
            sample_id=sid,
            image_path=rel_path,
            original_path=img_url,
            width=w,
            height=h,
            split=split_assign,
            item=item,
            boxes=bboxes,
            ocr_lines=ocr_lines,
            visual_finish=vis_finish,
        )
        samples.append(sample)
        if len(samples) % 10 == 0 or len(samples) == 1:
            print(
                f"[CommunityHarvester] Verified {len(samples)}/{target_count}: "
                f"{rel_path} -> {item.vendor} | {item.line} | {item.color_name} | OCR: {' '.join(ocr_lines)[:60]}"
            )

    save_dataset_xml(out_xml, samples, split_name="harvested-community")
    print(f"[CommunityHarvester] Successfully saved {len(samples)} strictly verified real-world spool images to {out_xml}")
    return out_xml


if __name__ == "__main__":
    harvest_community_real_world(target_count=120)
