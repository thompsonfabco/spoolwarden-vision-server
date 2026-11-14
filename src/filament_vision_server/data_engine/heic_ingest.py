"""Converts HEIC seed images to RGB JPEG with EXIF orientation normalization."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageOps
import pillow_heif

from ..xml_io import DATA_DIR, PROJECT_ROOT

pillow_heif.register_heif_opener()

DEFAULT_SEED_DIR = PROJECT_ROOT / "seed images"
DEFAULT_CONVERTED_DIR = DATA_DIR / "seed_converted"


def convert_seed_heic_images(
    seed_dir: str | Path = DEFAULT_SEED_DIR,
    output_dir: str | Path = DEFAULT_CONVERTED_DIR,
    max_side: int = 1600,
    jpeg_quality: int = 94,
) -> list[dict[str, str | int]]:
    """Convert all .HEIC/.jpg/.png images in seed_dir to standardized RGB JPEGs in output_dir."""
    src_root = Path(seed_dir)
    dst_root = Path(output_dir)
    dst_root.mkdir(parents=True, exist_ok=True)

    heic_files = sorted(
        [p for p in src_root.iterdir() if p.is_file() and p.suffix.lower() in (".heic", ".heif", ".jpg", ".jpeg", ".png")],
        key=lambda p: p.name.lower(),
    )
    results: list[dict[str, str | int]] = []
    for p in heic_files:
        stem = p.stem
        out_path = dst_root / f"{stem}.jpg"
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            rgb = im.convert("RGB")
            w, h = rgb.size
            if max(w, h) > max_side:
                scale = max_side / float(max(w, h))
                new_w, new_h = int(round(w * scale)), int(round(h * scale))
                rgb = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
                w, h = new_w, new_h
            rgb.save(out_path, format="JPEG", quality=jpeg_quality)
        results.append(
            {
                "id": f"seed_{stem}",
                "original_path": str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "image_path": str(out_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "width": w,
                "height": h,
            }
        )
    return results
