"""Procedural synthetic generator for 3D printer filament spools, circular hub stickers, and boxed labels."""
from __future__ import annotations

import math
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..catalog.matcher import CatalogMatcher, CatalogProduct, _hex_to_rgb
from ..xml_io import (
    DATA_DIR,
    AnnotatedSample,
    BoundingBox,
    FilamentItem,
    load_dataset_xml,
    save_dataset_xml,
)


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = (
        ["arialbd.ttf", "calibrib.ttf", "segoeuib.ttf", "consolab.ttf"]
        if bold
        else ["arial.ttf", "calibri.ttf", "segoeui.ttf", "consola.ttf"]
    )
    for fname in font_candidates:
        for prefix in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
            fpath = Path(prefix) / fname
            if fpath.is_file():
                try:
                    return ImageFont.truetype(str(fpath), size=size)
                except OSError:
                    pass
    return ImageFont.load_default()


def _draw_barcode(draw: ImageDraw.ImageDraw, x1: int, y1: int, w: int, h: int, rng: random.Random) -> None:
    x = x1
    while x < x1 + w - 3:
        bw = rng.choice([1, 2, 3, 4])
        if rng.random() < 0.58:
            draw.rectangle([x, y1, min(x + bw, x1 + w), y1 + h], fill=(15, 15, 15))
        x += bw + rng.choice([1, 2, 3])


def _apply_finish_texture(
    winding_img: np.ndarray,
    mask: np.ndarray,
    rgb: tuple[int, int, int],
    finish: str,
    rng: random.Random,
) -> None:
    """Render realistic filament coil texture & surface finish (Basic, Matte, Silk, Galaxy, Translucent, CF, Wood, Glow)."""
    h, w = winding_img.shape[:2]
    b, g, r = rgb[2], rgb[1], rgb[0]
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2.0, h / 2.0
    rad = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    angle = np.arctan2(yy - cy, xx - cx)

    # Concentric filament strand ridges
    coil_wave = np.sin(rad * 1.65) * 14.0
    # Directional anisotropic highlight along spool circumference
    light_angle = rng.uniform(-math.pi, math.pi)
    aniso = np.cos(angle - light_angle) ** 2

    if finish == "Silk":
        spec = (aniso ** 3) * 78.0 + coil_wave * 1.2
    elif finish == "Matte":
        spec = aniso * 10.0 + coil_wave * 0.45
    elif finish == "CF":
        noise = np.random.default_rng(rng.randint(0, 999999)).normal(0, 14.0, size=(h, w))
        spec = aniso * 12.0 + coil_wave * 0.5 + noise
    elif finish == "Galaxy":
        rng_np = np.random.default_rng(rng.randint(0, 999999))
        flecks = (rng_np.random((h, w)) > 0.965).astype(np.float32) * 135.0
        spec = aniso * 22.0 + coil_wave * 0.8 + flecks
    elif finish == "Translucent":
        spec = aniso * 42.0 + coil_wave * 1.4 + 18.0
    elif finish == "Glow":
        spec = aniso * 18.0 + coil_wave * 0.6 + 15.0
    else:  # Basic / Wood
        spec = (aniso ** 2) * 34.0 + coil_wave * 0.9

    for ch_idx, base_val in enumerate((b, g, r)):
        channel = np.clip(base_val + spec, 0, 255).astype(np.uint8)
        winding_img[:, :, ch_idx] = np.where(mask, channel, winding_img[:, :, ch_idx])


def _infer_finish_from_line(line: str, material: str, color_name: str) -> str:
    combined = f"{line} {material} {color_name}".lower()
    if "cf" in combined or "gf" in combined or "carbon" in combined:
        return "CF"
    if "silk" in combined or "metallic" in combined:
        return "Silk"
    if "matte" in combined:
        return "Matte"
    if "galaxy" in combined or "sparkle" in combined or "marble" in combined or "glitter" in combined:
        return "Galaxy"
    if "translucent" in combined or "transparent" in combined or "clear" in combined:
        return "Translucent"
    if "wood" in combined:
        return "Wood"
    if "glow" in combined:
        return "Glow"
    return "Basic"


def render_box_label_sample(
    prod: CatalogProduct,
    color_name: str,
    color_hex: str,
    color_family: str,
    rng: random.Random,
    sample_id: str,
    out_dir: Path,
) -> AnnotatedSample:
    """Render a realistic boxed filament photo (cardboard or colored box + thermal/printed spec sticker)."""
    w, h = 640, 640
    # Background shelf / cardboard box color
    is_cyan_sunlu_box = prod.vendor.upper() == "SUNLU" and rng.random() < 0.55
    is_dark_box = prod.vendor in ("Creality", "Phrozen", "Bambu Lab") and rng.random() < 0.40
    if is_cyan_sunlu_box:
        box_rgb = (rng.randint(35, 55), rng.randint(155, 185), rng.randint(175, 205))
    elif is_dark_box:
        box_rgb = (rng.randint(28, 45), rng.randint(30, 48), rng.randint(32, 52))
    else:
        # Kraft cardboard
        box_rgb = (rng.randint(155, 190), rng.randint(135, 168), rng.randint(102, 132))

    pil_img = Image.new("RGB", (w, h), color=box_rgb)
    draw = ImageDraw.Draw(pil_img)

    # Optional cutout window showing sealed filament windings inside the box (like 4853.jpg, 4860.jpg, 4890.jpg)
    boxes: list[BoundingBox] = []
    finish = _infer_finish_from_line(prod.line, prod.material, color_name)
    fil_rgb = _hex_to_rgb(color_hex)
    if rng.random() < 0.65:
        win_x1 = rng.randint(180, 250)
        win_y1 = rng.randint(50, 110)
        win_w = rng.randint(110, 160)
        win_h = rng.randint(170, 230)
        draw.rounded_rectangle([win_x1, win_y1, win_x1 + win_w, win_y1 + win_h], radius=28, fill=fil_rgb)
        for sy in range(win_y1 + 6, win_y1 + win_h - 6, 5):
            shade = tuple(max(0, min(255, c + rng.randint(-22, 22))) for c in fil_rgb)
            draw.line([(win_x1 + 4, sy), (win_x1 + win_w - 4, sy)], fill=shade, width=3)
        boxes.append(
            BoundingBox(
                label="spool_windings",
                x1=win_x1 / w,
                y1=win_y1 / h,
                x2=(win_x1 + win_w) / w,
                y2=(win_y1 + win_h) / h,
                confidence=1.0,
            )
        )

    # Draw white rectangular box sticker
    lx1 = rng.randint(55, 120)
    ly1 = rng.randint(290, 340) if boxes else rng.randint(110, 210)
    lw = rng.randint(390, 490)
    lh = rng.randint(230, 285)
    lx2 = min(w - 30, lx1 + lw)
    ly2 = min(h - 30, ly1 + lh)

    draw.rounded_rectangle([lx1, ly1, lx2, ly2], radius=12, fill=(248, 248, 246), outline=(210, 210, 208), width=2)

    f_vendor = _get_font(rng.randint(22, 28), bold=True)
    f_line = _get_font(rng.randint(26, 34), bold=True)
    f_body = _get_font(rng.randint(16, 20), bold=False)
    f_color = _get_font(rng.randint(18, 23), bold=True)

    tx = lx1 + 22
    ty = ly1 + 18
    ocr_lines: list[str] = []

    # Vendor header
    v_display = prod.vendor if rng.random() < 0.90 else prod.vendor.upper()
    draw.text((tx, ty), v_display, fill=(20, 20, 20), font=f_vendor)
    ocr_lines.append(v_display)
    ty += 34

    # Product line
    draw.text((tx, ty), prod.line, fill=(10, 10, 10), font=f_line)
    ocr_lines.append(prod.line)
    ty += 40

    # Specs lines
    spec1 = f"Diameter: {prod.diameter:.2f}±0.02mm   Net Weight: 1 kg"
    draw.text((tx, ty), spec1, fill=(35, 35, 35), font=f_body)
    ocr_lines.append(spec1)
    ty += 26

    spec2 = f"Printing Temp: {prod.nozzle_range}°C   Bed Temp: {prod.bed_range}°C"
    draw.text((tx, ty), spec2, fill=(35, 35, 35), font=f_body)
    ocr_lines.append(spec2)
    ty += 28

    # Color line + swatch
    col_label = rng.choice(["Color:", "Colour:", "COLOR :"])
    col_line = f"{col_label} {color_name}"
    draw.text((tx, ty), col_line, fill=(12, 12, 12), font=f_color)
    ocr_lines.append(col_line)

    # Small color swatch on label
    sw_x = lx2 - 64
    sw_y = ty - 2
    draw.rectangle([sw_x, sw_y, sw_x + 36, sw_y + 26], fill=fil_rgb, outline=(40, 40, 40), width=1)
    ty += 32

    # Barcode at bottom of sticker
    if ty + 36 < ly2 - 8:
        _draw_barcode(draw, tx, ty, min(260, (lx2 - lx1) - 44), 32, rng)

    boxes.insert(
        0,
        BoundingBox(
            label="box_label",
            x1=lx1 / w,
            y1=ly1 / h,
            x2=lx2 / w,
            y2=ly2 / h,
            confidence=1.0,
        ),
    )

    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    # Slight lighting gradient & noise
    noise = np.random.default_rng(rng.randint(0, 999999)).normal(0, rng.uniform(2.0, 6.0), img_bgr.shape)
    img_bgr = np.clip(img_bgr.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    out_path = out_dir / f"{sample_id}.jpg"
    cv2.imwrite(str(out_path), img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    item = FilamentItem(
        vendor=prod.vendor,
        material=prod.material,
        line=prod.line,
        color_name=color_name,
        color_family=color_family,
        color_hex=color_hex,
        color_hex_source="catalog",
        net_grams=1000,
        tare_grams=prod.tare_grams,
        density=prod.density,
        diameter=prod.diameter,
        nozzle_range=prod.nozzle_range,
        bed_range=prod.bed_range,
        sealed=True,
        sealed_reason="in box",
        source_type="box_label",
        confidence=1.0,
    )
    return AnnotatedSample(
        sample_id=sample_id,
        image_path=f"data/synthetic/{out_path.name}",
        original_path="",
        width=w,
        height=h,
        split="train",
        item=item,
        boxes=boxes,
        ocr_lines=ocr_lines,
        visual_finish=finish,
    )


def render_spool_sample(
    prod: CatalogProduct,
    color_name: str,
    color_hex: str,
    color_family: str,
    rng: random.Random,
    sample_id: str,
    out_dir: Path,
    include_label: bool = True,
) -> AnnotatedSample:
    """Render a realistic 3D printer filament spool (windings, vendor flange cutouts, optional sticker & vacuum bag)."""
    w, h = 640, 640
    bg_val = rng.randint(45, 195)
    img_bgr = np.full((h, w, 3), (bg_val - 8, bg_val, bg_val + 8), dtype=np.uint8)

    cx, cy = w // 2 + rng.randint(-20, 20), h // 2 + rng.randint(-20, 20)
    outer_r = rng.randint(245, 285)
    wind_outer_r = int(outer_r * 0.91)
    hub_outer_r = int(outer_r * 0.54)
    hole_r = int(outer_r * 0.24)

    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)

    # 1. Draw spool flange (cardboard for ELEGOO/Polymaker/OVERTURE, reusable gray/white for Bambu, black plastic for SUNLU/others)
    v_up = prod.vendor.upper()
    if v_up in ("ELEGOO", "POLYMAKER", "OVERTURE", "DEEPLEE"):
        flange_bgr = (108, 148, 178)  # Cardboard brown in BGR
    elif "BAMBU" in v_up:
        flange_bgr = (205, 208, 210) if rng.random() < 0.65 else (55, 58, 60)
    else:
        flange_bgr = (38, 40, 42)

    flange_mask = (dist <= outer_r) & (dist >= hole_r)
    img_bgr[flange_mask] = flange_bgr

    # 2. Draw exposed filament windings (visible through flange windows or full rim)
    fil_rgb = _hex_to_rgb(color_hex)
    finish = _infer_finish_from_line(prod.line, prod.material, color_name)
    winding_mask = (dist <= wind_outer_r) & (dist >= hub_outer_r)
    if include_label:
        # Cutouts / windows in flange showing filament windings
        angles = np.arctan2(yy - cy, xx - cx)
        if "SUNLU" in v_up:
            # Radial perforated slots + large side windows
            window_mask = winding_mask & (np.abs(np.sin(angles * 3.0)) > 0.35)
        elif "BAMBU" in v_up:
            window_mask = winding_mask & (np.abs(np.cos(angles * 4.0)) > 0.28)
        else:
            window_mask = winding_mask & (np.abs(np.sin(angles * 2.0)) > 0.22)
    else:
        window_mask = winding_mask

    _apply_finish_texture(img_bgr, window_mask, fil_rgb, finish, rng)

    boxes: list[BoundingBox] = [
        BoundingBox(
            label="spool_flange",
            x1=max(0.0, (cx - outer_r) / w),
            y1=max(0.0, (cy - outer_r) / h),
            x2=min(1.0, (cx + outer_r) / w),
            y2=min(1.0, (cy + outer_r) / h),
            confidence=1.0,
        ),
        BoundingBox(
            label="spool_windings",
            x1=max(0.0, (cx - wind_outer_r) / w),
            y1=max(0.0, (cy - wind_outer_r) / h),
            x2=min(1.0, (cx + wind_outer_r) / w),
            y2=min(1.0, (cy + wind_outer_r) / h),
            confidence=1.0,
        ),
    ]

    ocr_lines: list[str] = []
    pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)

    if include_label:
        # Rectangular sticker on flange (like 4950.jpg / 4975.jpg) or circular hub sticker
        st_w = rng.randint(230, 290)
        st_h = rng.randint(125, 160)
        sx1 = cx - st_w // 2
        sy1 = cy - outer_r + rng.randint(24, 52)
        sx2 = sx1 + st_w
        sy2 = sy1 + st_h
        draw.rounded_rectangle([sx1, sy1, sx2, sy2], radius=10, fill=(246, 246, 244), outline=(180, 180, 180), width=2)

        f_b = _get_font(19, bold=True)
        f_s = _get_font(15, bold=False)

        tx, ty = sx1 + 14, sy1 + 10
        draw.text((tx, ty), f"{prod.vendor}  {prod.line}", fill=(15, 15, 15), font=f_b)
        ocr_lines.append(f"{prod.vendor} {prod.line}")
        ty += 26

        draw.text((tx, ty), f"Color: {color_name}", fill=(20, 20, 20), font=f_b)
        ocr_lines.append(f"Color: {color_name}")
        ty += 24

        spec_t = f"{prod.diameter:.2f}mm / 1KG   {prod.nozzle_range}°C"
        draw.text((tx, ty), spec_t, fill=(35, 35, 35), font=f_s)
        ocr_lines.append(spec_t)
        ty += 22

        bed_t = f"Bed Temp: {prod.bed_range}°C"
        draw.text((tx, ty), bed_t, fill=(35, 35, 35), font=f_s)
        ocr_lines.append(bed_t)

        boxes.insert(
            0,
            BoundingBox(
                label="spool_label",
                x1=max(0.0, sx1 / w),
                y1=max(0.0, sy1 / h),
                x2=min(1.0, sx2 / w),
                y2=min(1.0, sy2 / h),
                confidence=1.0,
            ),
        )

    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    # Vacuum bag simulation (specular crinkle streaks) on 45% of spools
    sealed = rng.random() < 0.45
    if sealed:
        overlay = img_bgr.copy()
        for _ in range(rng.randint(3, 7)):
            pt1 = (rng.randint(40, w - 40), rng.randint(40, h - 40))
            pt2 = (pt1[0] + rng.randint(-180, 180), pt1[1] + rng.randint(-120, 120))
            cv2.line(overlay, pt1, pt2, (248, 248, 248), thickness=rng.randint(4, 14))
        overlay = cv2.GaussianBlur(overlay, (21, 21), 7.0)
        img_bgr = cv2.addWeighted(img_bgr, 0.78, overlay, 0.22, 0)

    out_path = out_dir / f"{sample_id}.jpg"
    cv2.imwrite(str(out_path), img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    item = FilamentItem(
        vendor=prod.vendor,
        material=prod.material,
        line=prod.line,
        color_name=color_name if include_label else color_family,
        color_family=color_family,
        color_hex=color_hex,
        color_hex_source="catalog" if include_label else "estimate",
        net_grams=1000,
        tare_grams=prod.tare_grams,
        density=prod.density,
        diameter=prod.diameter,
        nozzle_range=prod.nozzle_range,
        bed_range=prod.bed_range,
        sealed=sealed,
        sealed_reason="in vacuum bag" if sealed else "bare spool",
        source_type="spool_label" if include_label else "exposed_spool",
        confidence=0.98 if include_label else 0.85,
    )
    return AnnotatedSample(
        sample_id=sample_id,
        image_path=f"data/synthetic/{out_path.name}",
        original_path="",
        width=w,
        height=h,
        split="train",
        item=item,
        boxes=boxes,
        ocr_lines=ocr_lines,
        visual_finish=finish,
    )


def generate_synthetic_dataset(
    num_samples: int = 800,
    seed: int = 42,
    merge_with_seed_splits: bool = True,
) -> dict[str, int]:
    """Generate procedural box and spool samples weighted toward priority brands and merge into XML splits."""
    rng = random.Random(seed)
    matcher = CatalogMatcher()
    syn_dir = DATA_DIR / "synthetic"
    syn_dir.mkdir(parents=True, exist_ok=True)

    # Weight priority brands (Bambu Lab, SUNLU, ELEGOO) higher while sampling all catalog brands
    priority_vendors = {"Bambu Lab", "SUNLU", "ELEGOO"}
    secondary_vendors = {
        "Polymaker", "eSUN", "OVERTURE", "Hatchbox", "Prusament",
        "Creality", "Anycubic", "Eryone", "Inland", "JAYO", "Push Plastic", "3DXTech",
    }

    weighted_pool: list[CatalogProduct] = []
    for prod in matcher.all_products:
        if not prod.colors:
            continue
        if prod.vendor in priority_vendors:
            weighted_pool.extend([prod] * 6)
        elif prod.vendor in secondary_vendors:
            weighted_pool.extend([prod] * 3)
        else:
            weighted_pool.append(prod)

    train_syn: list[AnnotatedSample] = []
    val_syn: list[AnnotatedSample] = []

    for i in range(num_samples):
        prod = rng.choice(weighted_pool)
        col = rng.choice(prod.colors)
        sid = f"syn_{i:04d}"
        r_mode = rng.random()
        if r_mode < 0.48:
            sample = render_box_label_sample(prod, col.name, col.hex_code, col.family, rng, sid, syn_dir)
        elif r_mode < 0.85:
            sample = render_spool_sample(prod, col.name, col.hex_code, col.family, rng, sid, syn_dir, include_label=True)
        else:
            sample = render_spool_sample(prod, col.name, col.hex_code, col.family, rng, sid, syn_dir, include_label=False)

        if i % 8 == 0:
            sample.split = "val"
            val_syn.append(sample)
        else:
            sample.split = "train"
            train_syn.append(sample)

    ann_dir = DATA_DIR / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    save_dataset_xml(ann_dir / "synthetic_labels.xml", train_syn + val_syn, split_name="synthetic")

    if merge_with_seed_splits:
        seed_all_path = ann_dir / "seed_labels.xml"
        seed_train: list[AnnotatedSample] = []
        seed_val: list[AnnotatedSample] = []
        if seed_all_path.is_file():
            all_seed = load_dataset_xml(seed_all_path)
            # Oversample real seed training images 4x so real camera distribution dominates
            real_tr = [s for s in all_seed if s.split == "train"]
            seed_train = real_tr * 4
            seed_val = [s for s in all_seed if s.split == "val"]

        save_dataset_xml(ann_dir / "train_split.xml", seed_train + train_syn, split_name="train")
        save_dataset_xml(ann_dir / "val_split.xml", seed_val + val_syn, split_name="val")

    return {"train_synthetic": len(train_syn), "val_synthetic": len(val_syn)}
