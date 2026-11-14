"""White-balanced CIE LAB color hex & 14-class color family extractor for exposed filament coils."""
from __future__ import annotations

import math
import cv2
import numpy as np

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
]


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
    """Extract (hex_6char, color_family, visual_finish_hint) from an image or spool crop."""
    if img_bgr is None or img_bgr.size == 0:
        return "1A1A1A", "Black", "Basic"

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

    hsv = cv2.cvtColor(wb, cv2.COLOR_BGR2HSV)
    val = hsv[:, :, 2]
    sat = hsv[:, :, 1]

    # Filter out specular glare (val > 246 & sat < 30) and deep background shadows (val < 18)
    valid_mask = geom_mask & (val >= 18) & ~((val >= 246) & (sat < 30))
    pixels_bgr = wb[valid_mask]
    if len(pixels_bgr) < 50:
        pixels_bgr = wb.reshape(-1, 3)

    # Estimate surface finish from luminance variance / specular tail
    v_valid = val[geom_mask]
    p95 = float(np.percentile(v_valid, 95)) if len(v_valid) > 10 else 128.0
    p50 = float(np.percentile(v_valid, 50)) if len(v_valid) > 10 else 100.0
    finish_hint = "Silk" if (p95 - p50 > 72.0 and p95 > 215.0) else ("Matte" if (p95 - p50 < 28.0) else "Basic")

    # K-means clustering (k=2) in BGR to separate dominant filament color from black/cardboard flange
    pts = pixels_bgr.astype(np.float32)
    if len(pts) >= 20:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 15, 1.0)
        _, labels, centers = cv2.kmeans(pts, 2, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
        counts = np.bincount(labels.flatten(), minlength=2)
        c0, c1 = centers[0], centers[1]
        # Prefer the more chromatic cluster if counts are reasonably balanced, else largest cluster
        sat0 = float(np.max(c0) - np.min(c0))
        sat1 = float(np.max(c1) - np.min(c1))
        if sat1 > sat0 + 35.0 and counts[1] >= 0.28 * len(pts):
            dom = c1
        elif sat0 > sat1 + 35.0 and counts[0] >= 0.28 * len(pts):
            dom = c0
        else:
            dom = centers[int(np.argmax(counts))]
    else:
        dom = np.median(pts, axis=0)

    b, g, r = int(np.clip(dom[0], 0, 255)), int(np.clip(dom[1], 0, 255)), int(np.clip(dom[2], 0, 255))
    hex_code = f"{r:02X}{g:02X}{b:02X}"
    family = classify_rgb_to_family(r, g, b)
    return hex_code, family, finish_hint
