"""White-balanced CIE LAB color hex & 14-class color family extractor for exposed filament coils."""
from __future__ import annotations

import math
import cv2
import numpy as np

from ..xml_io import CONFIGS_DIR, parse_xml_file

# 14 canonical color families and their reference RGB centroids
FAMILY_CENTROID_RGB: list[tuple[str, tuple[int, int, int], str]] = [
    ("Black", (26, 26, 26), "1A1A1A"),
    ("White", (242, 242, 240), "F5F5F5"),
    ("Gray/Silver", (128, 132, 135), "808487"),
    ("Red", (205, 34, 41), "D12229"),
    ("Orange", (242, 120, 15), "F57C00"),
    ("Yellow", (245, 200, 35), "FBC02D"),
    ("Green", (46, 135, 55), "2E7D32"),
    ("Blue", (25, 115, 205), "1976D2"),
    ("Purple", (120, 35, 165), "7B1FA2"),
    ("Pink", (235, 75, 135), "EC407A"),
    ("Brown/Beige", (140, 105, 85), "8D6E63"),
    ("Gold/Bronze", (205, 165, 55), "D4AF37"),
    ("Transparent/Natural", (224, 232, 234), "E0E8EA"),
    ("Multi-color/Rainbow", (156, 39, 176), "9C27B0"),
]


def _load_color_extractor_config() -> tuple[float, float, int]:
    """Read specular/shadow L* bounds and k-clusters from configs/cpu_pipeline.xml."""
    spec_l_max = 94.0
    shad_l_min = 8.0
    k_clusters = 3
    cfg_path = CONFIGS_DIR / "cpu_pipeline.xml"
    if cfg_path.is_file():
        try:
            root = parse_xml_file(cfg_path)
            ce = root.find("color-extractor")
            if ce is not None:
                spec_l_max = float(ce.attrib.get("specular-l-max", str(spec_l_max)))
                shad_l_min = float(ce.attrib.get("shadow-l-min", str(shad_l_min)))
                k_clusters = max(2, int(ce.attrib.get("k-clusters", str(k_clusters))))
        except Exception:
            pass
    return spec_l_max, shad_l_min, k_clusters


def gray_world_white_balance(img_bgr: np.ndarray) -> np.ndarray:
    """Apply damped gray-world white balance in LAB space to reduce warm/cool indoor lighting bias."""
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    avg_a = float(np.mean(lab[:, :, 1]))
    avg_b = float(np.mean(lab[:, :, 2]))
    # Damped shift (0.45) so saturated mono-color spools aren't neutralized to gray
    lab[:, :, 1] = np.clip(lab[:, :, 1] - ((avg_a - 128.0) * 0.35 * (lab[:, :, 0] / 255.0)), 0, 255)
    lab[:, :, 2] = np.clip(lab[:, :, 2] - ((avg_b - 128.0) * 0.35 * (lab[:, :, 0] / 255.0)), 0, 255)
    return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)


def classify_rgb_to_family(r: int, g: int, b: int) -> str:
    """Map an (R, G, B) tuple to one of the 14 canonical color families using HSV + perceptual distance."""
    arr = np.uint8([[[b, g, r]]])
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)[0, 0]
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])  # h in [0, 179], s,v in [0, 255]

    if v < 48:
        return "Black"
    if s < 28:
        if v > 205:
            return "White"
        if v < 62:
            return "Black"
        return "Gray/Silver"

    # Chromatic classification by Hue (OpenCV H is 0..179 = 0..358 deg)
    if h < 8 or h >= 170:
        if s < 110 and v > 165:
            return "Pink"
        return "Red"
    if 8 <= h < 22:
        if v < 155 and s < 150:
            return "Brown/Beige"
        return "Orange"
    if 22 <= h < 34:
        if s < 140 and 130 <= v <= 215:
            return "Gold/Bronze"
        if v < 150:
            return "Brown/Beige"
        return "Yellow"
    if 34 <= h < 85:
        return "Green"
    if 85 <= h < 132:
        return "Blue"
    if 132 <= h < 154:
        return "Purple"
    if 154 <= h < 170:
        return "Pink"

    # Fallback nearest centroid
    best_fam = "Gray/Silver"
    best_d = 1e9
    for fam, (cr, cg, cb), _ in FAMILY_CENTROID_RGB:
        d = math.hypot(r - cr, g - cg, b - cb)
        if d < best_d:
            best_d = d
            best_fam = fam
    return best_fam


def extract_filament_color(
    img_bgr: np.ndarray,
    is_spool_crop: bool = True,
) -> tuple[str, str, str]:
    """Extract (hex_6char, color_family, visual_finish_hint) in CIE LAB space from an image or spool crop."""
    if img_bgr is None or img_bgr.size == 0:
        return "1A1A1A", "Black", "Basic"

    spec_l_max, shad_l_min, k_clusters = _load_color_extractor_config()
    small = cv2.resize(img_bgr, (160, 160), interpolation=cv2.INTER_AREA)
    wb = gray_world_white_balance(small)

    # Create annulus mask if full spool crop (avoids center hole/hub sticker and outer corners)
    h, w = wb.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    cx, cy = w / 2.0, h / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (min(w, h) / 2.0)
    if is_spool_crop:
        geom_mask = (dist >= 0.36) & (dist <= 0.90)
    else:
        geom_mask = np.ones((h, w), dtype=bool)

    lab = cv2.cvtColor(wb, cv2.COLOR_BGR2LAB)
    l_pct = lab[:, :, 0].astype(np.float32) * (100.0 / 255.0)
    hsv = cv2.cvtColor(wb, cv2.COLOR_BGR2HSV)
    val = hsv[:, :, 2]
    sat = hsv[:, :, 1]

    # Filter out specular glare and deep shadow pixels using CIE L* bounds
    valid_mask = geom_mask & (l_pct >= shad_l_min) & ~((l_pct >= spec_l_max) & (sat < 35))
    pixels_lab = lab[valid_mask]
    if len(pixels_lab) < 50:
        pixels_lab = lab.reshape(-1, 3)

    # Estimate surface finish from luminance variance / specular tail
    v_valid = val[geom_mask]
    p95 = float(np.percentile(v_valid, 95)) if len(v_valid) > 10 else 128.0
    p50 = float(np.percentile(v_valid, 50)) if len(v_valid) > 10 else 100.0
    finish_hint = "Silk" if (p95 - p50 > 72.0 and p95 > 215.0) else ("Matte" if (p95 - p50 < 28.0) else "Basic")

    # K-means clustering in CIE LAB space (default k=3 per cpu_pipeline.xml)
    pts = pixels_lab.astype(np.float32)
    is_multicolor = False
    if len(pts) >= 20:
        eff_k = max(2, min(k_clusters, len(pts)))
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 15, 1.0)
        _, labels, centers_lab = cv2.kmeans(pts, eff_k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
        counts = np.bincount(labels.flatten(), minlength=eff_k)
        centers_u8 = np.clip(centers_lab, 0, 255).astype(np.uint8).reshape(1, eff_k, 3)
        centers_bgr = cv2.cvtColor(centers_u8, cv2.COLOR_LAB2BGR)[0].astype(np.float32)
        centers_hsv = cv2.cvtColor(centers_bgr.astype(np.uint8).reshape(1, eff_k, 3), cv2.COLOR_BGR2HSV)[0]

        # Detect balanced vivid multi-color / rainbow windings across distinct hues
        chromatic_hues: list[float] = []
        for idx_c in range(eff_k):
            share = counts[idx_c] / float(len(pts))
            rgb_spread = float(np.max(centers_bgr[idx_c]) - np.min(centers_bgr[idx_c]))
            if share >= 0.22 and rgb_spread >= 45.0 and int(centers_hsv[idx_c, 1]) >= 65:
                chromatic_hues.append(float(centers_hsv[idx_c, 0]) * 2.0)
        for i in range(len(chromatic_hues)):
            for j in range(i + 1, len(chromatic_hues)):
                diff = abs(chromatic_hues[i] - chromatic_hues[j])
                hue_gap = min(diff, 360.0 - diff)
                if hue_gap >= 35.0:
                    is_multicolor = True
                    break
            if is_multicolor:
                break

        dom_idx = int(np.argmax(counts))
        dom = centers_bgr[dom_idx]
        dom_spread = float(np.max(dom) - np.min(dom))
        best_chrom_idx = dom_idx
        best_chrom_spread = dom_spread
        for idx_c in range(eff_k):
            if counts[idx_c] >= 0.22 * len(pts):
                spread = float(np.max(centers_bgr[idx_c]) - np.min(centers_bgr[idx_c]))
                if spread > best_chrom_spread:
                    best_chrom_spread = spread
                    best_chrom_idx = idx_c
        if best_chrom_spread > dom_spread + 35.0:
            dom = centers_bgr[best_chrom_idx]
    else:
        dom_lab = np.clip(np.median(pts, axis=0), 0, 255).astype(np.uint8).reshape(1, 1, 3)
        dom = cv2.cvtColor(dom_lab, cv2.COLOR_LAB2BGR)[0, 0].astype(np.float32)

    b, g, r = int(np.clip(dom[0], 0, 255)), int(np.clip(dom[1], 0, 255)), int(np.clip(dom[2], 0, 255))
    hex_code = f"{r:02X}{g:02X}{b:02X}"
    family = "Multi-color/Rainbow" if is_multicolor else classify_rgb_to_family(r, g, b)
    return hex_code, family, finish_hint
