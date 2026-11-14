"""Strict verification and harvesting utilities for real filament spool and box photos.

Ensures every harvested web image is a genuine photo of a 3D printer filament spool
or filament box with a readable label, rejecting:
- Generic search engine fallback/trending images
- Bambu Lab 3D-printed spiral swatch spheres (RFID badge only)
- 3D-printed sample models (benchies, vases, figurines, arches)
- Marketing infographics, temperature/speed comparison charts, and spec tables
- Printer hardware, spare parts, build plates, glue, and resin bottles
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.ocr_helper import create_rapidocr, run_rapidocr
from ..xml_io import (
    DATA_DIR,
    AnnotatedSample,
    BoundingBox,
    save_dataset_xml,
)
from .auto_labeler import parse_ocr_heuristic

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Physical specification tokens that appear on real filament spool/box labels
LABEL_SPEC_RE = re.compile(
    r"(1\.75|2\.85|1\s*kg|0\.5\s*kg|500\s*g|250\s*g|750\s*g|800\s*g|600\s*g\s*400\s*g|"
    r"0\.02|0\.03|0\.05|3d\s*printer\s*filament|3d\s*printing\s*filament|fdm\s*filament|"
    r"net\s*weight|n\.w\.?|g\.w\.?|spool\s*weight|filament\s*with\s*spool|"
    r"print(?:ing)?\s*temp|nozzle\s*temp|ext\.\s*temp|bed\s*temp|diameter|tolerance|"
    r"elegoo\.com|sunlu\.com|bambulab\.com|eryone3d|prusament\.com|esun3d|"
    r"直径|打印温度|净重)",
    re.IGNORECASE,
)

# Known filament brand tokens on labels
BRAND_ALIASES: dict[str, list[str]] = {
    "Bambu Lab": ["bambu", "bambulab"],
    "SUNLU": ["sunlu", "jayo"],
    "ELEGOO": ["elegoo"],
    "eSUN": ["esun", "epla", "epetg", "easa", "etpu", "epa-cf"],
    "Prusament": ["prusament", "prusa"],
    "Creality": ["creality", "hyper pla", "cr-pla", "cr-petg", "cr-silk", "cr-abs", "ender"],
    "Polymaker": ["polymaker", "polyterra", "polylite", "polymax", "polysonic", "fiberon", "panchroma"],
    "OVERTURE": ["overture"],
    "HATCHBOX": ["hatchbox"],
    "Inland": ["inland", "micro center"],
    "MatterHackers": ["matterhackers", "mh build", "pro series"],
    "IIID Max": ["iiid max", "iiidmax", "3d max"],
    "AMOLEN": ["amolen"],
    "Eryone": ["eryone"],
    "Anycubic": ["anycubic", "pantone"],
    "Flashforge": ["flashforge"],
    "Printed Solid": ["printed solid", "jessie"],
    "ProtoPasta": ["protopasta", "proto-pasta"],
    "3DXTECH": ["3dxtech"],
    "ColorFabb": ["colorfabb"],
    "FormFutura": ["formfutura"],
}

POLYMER_LINE_RE = re.compile(
    r"\b(pla\+?|petg|abs|asa|tpu|pa\d*|paht|pc|pva|hips|pps|ppa|"
    r"basic|matte|silk|rapid|meta|plus|pro|hf|wood|marble|sparkle|galaxy|glow|"
    r"translucent|cf|gf|refill|high\s*speed|hyper)\b",
    re.IGNORECASE,
)

# Marketing infographic / chart / non-spool phrases to reject immediately
INFOGRAPHIC_REJECT_RE = re.compile(
    r"(appearance\s*with|transformations|controllable\s*gloss|volumetric|mm3/s|"
    r"comparison|reliable\s*extrusion|compatible\s*with|openprinttag|tensile|"
    r"modulus|charpy|elongation|flexural|step\s*1|step\s*2|how\s*to\s*use|"
    r"s-series|glow\s*in\s*the\s*dark-rainbow|dimensional\s*accuracy\s*99|"
    r"clog-free|tangle-free|bubble-free|mechanical\s*properties|print\s*settings|"
    r"drying\s*settings|recommended\s*settings|why\s*choose|wide\s*compatibility|"
    r"world\s*of\s*printables|radio\s*times|satta\s*king|botanychic|greenguard)",
    re.IGNORECASE,
)

# Non-filament hardware/parts/resin keywords to reject at product title/URL level
NON_FILAMENT_TITLE_RE = re.compile(
    r"\b(printer|resin|wash|cure|nozzle|hotend|build\s*plate|pei|cool\s*plate|"
    r"engineering\s*plate|dryer|dry\s*box|extruder|sensor|cutter|lever|buffer|"
    r"hub|ptfe|tube|wiper|blade|ams\s*2|ams\s*lite|ams\s*internal|camera|filter|"
    r"carbon|pellet|glue|liquid\s*glue|tape|scraper|enclosure|laser|engraver|"
    r"scanner|cable|fpc|board|screen|display|adapter|fan|motor|belt|bearing|"
    r"heater|thermistor|silicone|sock|tool|kit|shirt|merch|gift\s*card|plywood|"
    r"bamboo\s*board|walnut|window|panel|ring|assist\s*module|marker|swatch|"
    r"a1\s*mini|p1s|p1p|x1c|x1e|h2d|p2s|k1\s*max|ender-3|neptune|mars|saturn|centauri)\b",
    re.IGNORECASE,
)

FILAMENT_TITLE_RE = re.compile(
    r"\b(filament|pla\+?|petg|abs|asa|tpu|paht|pa6|pc|pva|hips|prusament|polyterra|polylite|fiberon|refill)\b",
    re.IGNORECASE,
)

BAD_IMAGE_DOMAINS = (
    "bing.net",
    "microsoft.com",
    "schemas.org",
    "w3.org",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "ytimg.com",
    "pinterest.com",
    "pinimg.com",
    "wikimedia.org",
    "wikipedia.org",
    "worldofprintables",
    "radiotimes",
    "freepik.com",
    "shutterstock.com",
    "istockphoto.com",
    "vecteezy.com",
    "clipart",
)


def http_get_bytes(url: str, timeout: float = 10.0) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception:
        return None
    return None


def is_valid_filament_product_title(title: str, product_type: str = "") -> bool:
    combined = f"{title} {product_type}".strip()
    if not combined:
        return False
    if NON_FILAMENT_TITLE_RE.search(combined):
        return False
    if not FILAMENT_TITLE_RE.search(combined):
        return False
    return True


def fetch_bing_async_candidates(query: str, max_results: int = 35) -> list[str]:
    """Query Bing's actual image search results endpoint (/images/async), NOT the HTML shell."""
    q_enc = urllib.parse.quote_plus(query)
    urls: list[str] = []
    seen: set[str] = set()
    for first in (0, 35):
        if len(urls) >= max_results:
            break
        endpoint = f"https://www.bing.com/images/async?q={q_enc}&first={first}&count=35&adlt=off"
        raw = http_get_bytes(endpoint, timeout=10.0)
        if not raw:
            continue
        text = html_lib.unescape(raw.decode("utf-8", errors="ignore"))
        matches = re.findall(r'"murl"\s*:\s*"(https?://[^"]+)"', text)
        if not matches:
            matches = re.findall(r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png)", text, flags=re.IGNORECASE)
        for u in matches:
            u_clean = u.strip()
            low = u_clean.lower()
            if any(bad in low for bad in BAD_IMAGE_DOMAINS):
                continue
            if any(skip in low for skip in ("logo", "icon", "avatar", "banner", "favicon", "sprite")):
                continue
            if u_clean not in seen:
                seen.add(u_clean)
                urls.append(u_clean)
                if len(urls) >= max_results:
                    break
    return urls


def _fast_upright_ocr(img_bgr: np.ndarray, ocr_engine: Any) -> tuple[list[str], list[BoundingBox]]:
    """Run a single fast upright RapidOCR pass (downscaled to max_side=850 for speed)."""
    h, w = img_bgr.shape[:2]
    max_side = max(h, w)
    if max_side > 850:
        scale = 850.0 / max_side
        work = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        work = img_bgr
    wh, ww = work.shape[:2]
    res = run_rapidocr(ocr_engine, work)
    ocr_lines: list[str] = []
    text_boxes: list[tuple[float, float, float, float, float]] = []
    if res:
        for poly, text, score in res:
            conf = float(score)
            if conf < 0.42 or not str(text).strip():
                continue
            ocr_lines.append(str(text).strip())
            pts = np.array(poly, dtype=np.float32)
            x1 = float(np.clip(np.min(pts[:, 0]) / ww, 0.0, 1.0))
            y1 = float(np.clip(np.min(pts[:, 1]) / wh, 0.0, 1.0))
            x2 = float(np.clip(np.max(pts[:, 0]) / ww, 0.0, 1.0))
            y2 = float(np.clip(np.max(pts[:, 1]) / wh, 0.0, 1.0))
            text_boxes.append((x1, y1, x2, y2, conf))

    bboxes: list[BoundingBox] = [
        BoundingBox(label="spool_flange", x1=0.04, y1=0.04, x2=0.96, y2=0.96, confidence=0.90)
    ]
    if text_boxes:
        lx1 = max(0.0, min(b[0] for b in text_boxes) - 0.02)
        ly1 = max(0.0, min(b[1] for b in text_boxes) - 0.02)
        lx2 = min(1.0, max(b[2] for b in text_boxes) + 0.02)
        ly2 = min(1.0, max(b[3] for b in text_boxes) + 0.02)
        bboxes.append(BoundingBox(label="spool_label", x1=lx1, y1=ly1, x2=lx2, y2=ly2, confidence=0.92))
    return ocr_lines, bboxes


def verify_real_spool_or_box_image(
    img_bgr: np.ndarray,
    ocr_engine: Any,
    required_vendor: str | None = None,
    allow_trusted_brand_Tokens: list[str] | None = None,
) -> tuple[bool, list[str], list[BoundingBox], int]:
    """Verify that an image is a real filament spool or box photo with a readable label."""
    if img_bgr is None or img_bgr.size == 0:
        return False, [], [], 0
    h, w = img_bgr.shape[:2]
    if min(h, w) < 250:
        return False, [], [], 0
    ar = w / float(max(h, 1))
    if ar < 0.65 or ar > 1.52:
        return False, [], [], 0

    # Reject pure line-art / black-and-white coloring pages
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    bright_white_ratio = float(np.mean((hsv[:, :, 1] < 18) & (hsv[:, :, 2] > 235)))
    dark_black_ratio = float(np.mean(hsv[:, :, 2] < 35))
    if bright_white_ratio + dark_black_ratio > 0.96 and bright_white_ratio > 0.72:
        return False, [], [], 0

    ocr_lines, bboxes = _fast_upright_ocr(img_bgr, ocr_engine)
    clean_text = " ".join(ocr_lines).strip()
    if not clean_text or len(clean_text) < 4:
        return False, ocr_lines, bboxes, len(ocr_lines)

    # Reject dense spec tables / web screenshots
    if len(ocr_lines) > 38:
        return False, ocr_lines, bboxes, len(ocr_lines)

    # Reject Bambu Lab 3D-printed spiral swatch spheres (only "RFID" or "RFID NEW")
    tokens_alpha = re.sub(r"[^a-zA-Z0-9]+", " ", clean_text).strip().upper().split()
    if set(tokens_alpha).issubset({"RFID", "RFIO", "NEW", "UL", "GREENGUARD", "B", "0", "O"}):
        return False, ocr_lines, bboxes, len(ocr_lines)

    # Reject marketing infographics, speed/temperature charts, and non-spool slides
    if INFOGRAPHIC_REJECT_RE.search(clean_text):
        return False, ocr_lines, bboxes, len(ocr_lines)

    low_text = clean_text.lower()

    # Check if any competing brand name appears in OCR when required_vendor is set
    detected_brands: list[str] = []
    for canonical_brand, aliases in BRAND_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", low_text) or (len(alias) >= 5 and alias in low_text):
                detected_brands.append(canonical_brand)
                break

    if required_vendor:
        req_norm = required_vendor.lower()
        for db in detected_brands:
            if db.lower() != req_norm and req_norm not in db.lower() and db.lower() not in req_norm:
                if allow_trusted_brand_Tokens and any(t.lower() in db.lower() for t in allow_trusted_brand_Tokens):
                    continue
                return False, ocr_lines, bboxes, len(ocr_lines)

    has_spec = bool(LABEL_SPEC_RE.search(clean_text))
    has_brand = len(detected_brands) > 0
    has_polymer = bool(POLYMER_LINE_RE.search(clean_text))

    spec_matches = LABEL_SPEC_RE.findall(clean_text)
    if (has_spec and (has_polymer or has_brand or len(spec_matches) >= 2)) or (has_brand and has_polymer):
        return True, ocr_lines, bboxes, len(ocr_lines)

    return False, ocr_lines, bboxes, len(ocr_lines)


def harvest_verified_brand_dataset(
    brand_slug: str,
    default_vendor: str,
    allowed_vendors: list[str],
    shopify_sources: list[tuple[str, str | None]],
    bing_queries: list[tuple[str, str, str, str]],
    target_count: int = 55,
    extra_image_candidates: list[tuple[str, str, str, str, str]] | None = None,
) -> Path:
    """Harvest and strictly verify real filament spool/box images for a brand group."""
    out_dir = DATA_DIR / "harvested"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_xml = DATA_DIR / "annotations" / f"brand_{brand_slug}.xml"

    ocr = create_rapidocr()
    matcher = CatalogMatcher()
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()
    samples: list[AnnotatedSample] = []

    candidates: list[tuple[str, str, str, str, str]] = []
    if extra_image_candidates:
        candidates.extend(extra_image_candidates)

    # 1. Collect candidates from Shopify /products.json feeds
    for json_url, vendor_filter in shopify_sources:
        raw = http_get_bytes(json_url, timeout=12.0)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        for prod in data.get("products", []):
            p_vendor = str(prod.get("vendor") or default_vendor).strip()
            p_title = str(prod.get("title") or "").strip()
            p_type = str(prod.get("product_type") or "").strip()
            if vendor_filter and vendor_filter.lower() not in p_vendor.lower() and vendor_filter.lower() not in p_title.lower():
                continue
            if not is_valid_filament_product_title(p_title, p_type):
                continue

            prod_urls: list[tuple[str, str]] = []
            for v in prod.get("variants", []):
                v_title = str(v.get("title") or "").strip()
                fi = v.get("featured_image")
                if isinstance(fi, dict) and fi.get("src"):
                    prod_urls.append((f"{p_title} {v_title}", str(fi["src"])))
            for im in prod.get("images", [])[:3]:
                if isinstance(im, dict) and im.get("src"):
                    prod_urls.append((p_title, str(im["src"])))

            added_for_prod = 0
            for combined_title, u in prod_urls:
                u_clean = u.split("?")[0]
                if u_clean in seen_urls:
                    continue
                seen_urls.add(u_clean)
                candidates.append((p_vendor, "", "", combined_title, u))
                added_for_prod += 1
                if added_for_prod >= 4:
                    break

    # 2. Collect candidates from Bing Async image search (/images/async)
    for v_hint, l_hint, m_hint, q_str in bing_queries:
        for u in fetch_bing_async_candidates(q_str, max_results=30):
            u_clean = u.split("?")[0]
            if u_clean in seen_urls:
                continue
            seen_urls.add(u_clean)
            candidates.append((v_hint, l_hint, m_hint, f"{v_hint} {l_hint} {m_hint}", u))

    print(f"[{brand_slug}] Total candidate URLs queued: {len(candidates)}")

    # 3. Download, strictly verify with OCR + visual gate, and annotate
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
            required_vendor=v_hint or default_vendor,
            allow_trusted_brand_Tokens=allowed_vendors,
        )
        if not ok:
            continue

        seen_hashes.add(sha1)
        idx = len(samples)
        sid = f"{brand_slug}_{idx:04d}"
        rel_path = f"data/harvested/{sid}.jpg"
        abs_path = out_dir / f"{sid}.jpg"

        h, w = img_bgr.shape[:2]
        if max(h, w) > 1024:
            sc = 1024.0 / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
            h, w = img_bgr.shape[:2]
        cv2.imwrite(str(abs_path), img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

        item, vis_finish = parse_ocr_heuristic(ocr_lines + [title_hint], img_bgr, matcher, is_spool=True)
        if item.vendor == "Unknown" or (allowed_vendors and all(av.lower() not in item.vendor.lower() for av in allowed_vendors)):
            item.vendor = v_hint or default_vendor
        if l_hint and item.line in ("PLA", "PETG", "ABS", "ASA", "TPU"):
            item.line = l_hint
        if m_hint:
            item.material = matcher.normalize_material(m_hint, item.line)
        item = matcher.enrich_item(item)

        split_assign = "val" if (idx % 7 == 0) else "train"
        samples.append(
            AnnotatedSample(
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
        )
        if len(samples) % 10 == 0 or len(samples) == 1:
            print(
                f"[{brand_slug}] Verified {len(samples)}/{target_count}: "
                f"{rel_path} -> {item.vendor} | {item.line} | {item.color_name} | OCR: {' '.join(ocr_lines)[:65]}"
            )

    save_dataset_xml(out_xml, samples, split_name=f"harvested-{brand_slug}")
    print(f"[{brand_slug}] DONE! Saved {len(samples)} strictly verified images to {out_xml}")
    return out_xml
