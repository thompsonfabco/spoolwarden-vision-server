"""Polar-to-Cartesian unwarping for circular filament spool hub labels."""
from __future__ import annotations

import cv2
import numpy as np


def unwarp_circular_spool_label(
    crop_bgr: np.ndarray,
    inner_radius_ratio: float = 0.22,
    outer_radius_ratio: float = 0.96,
    out_height: int = 220,
    out_width: int = 900,
) -> list[np.ndarray]:
    """Unwarp a circular spool hub label into two upright rectangular strips (0-deg and 180-deg phase).

    Text on circular spool stickers often runs either along the top arc (clockwise) or bottom arc
    (counter-clockwise). Returning both 0-deg and 180-deg unwarped strips ensures horizontal OCR
    reads curved text regardless of spool rotation.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return []
    h, w = crop_bgr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    max_r = min(cx, cy) * outer_radius_ratio
    min_r = min(cx, cy) * inner_radius_ratio
    if max_r <= min_r + 8:
        return [crop_bgr]

    strips: list[np.ndarray] = []
    for phase_deg in (0.0, 180.0):
        theta = np.linspace(0.0, 2.0 * np.pi, out_width, dtype=np.float32) + np.deg2rad(phase_deg)
        radii = np.linspace(min_r, max_r, out_height, dtype=np.float32)
        theta_grid, r_grid = np.meshgrid(theta, radii)

        map_x = (cx + r_grid * np.cos(theta_grid)).astype(np.float32)
        map_y = (cy + r_grid * np.sin(theta_grid)).astype(np.float32)

        strip = cv2.remap(
            crop_bgr,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        strips.append(strip)
        # Also include vertically flipped strip for text printed radially inward vs outward
        strips.append(cv2.flip(strip, 0))
    return strips
