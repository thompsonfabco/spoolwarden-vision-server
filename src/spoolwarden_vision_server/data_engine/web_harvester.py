"""Harvests real-world photos of 3D printer filament spools and boxes from public web sources & manufacturer CDNs."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.ocr_helper import create_rapidocr
from ..xml_io import (
    DATA_DIR,
    AnnotatedSample,
    FilamentItem,
    load_dataset_xml,
    save_dataset_xml,
)
from .auto_labeler import extract_multi_angle_ocr_and_boxes, parse_ocr_heuristic

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Public Shopify / WooCommerce JSON endpoints & Reddit search queries for real filament spool & box photos
SHOPIFY_BRAND_STORES: list[tuple[str, str]] = [
    ("ELEGOO", "https://us.elegoo.com/collections/filament/products.json?limit=250"),
    ("SUNLU", "https://www.sunlu.com/collections/fdm-printer-filament/products.json?limit=250"),
    ("Polymaker", "https://us.polymaker.com/collections/all/products.json?limit=250"),
    ("OVERTURE", "https://overture3d.com/collections/all/products.json?limit=250"),
    ("eSUN", "https://esun3dstore.com/collections/all/products.json?limit=250"),
    ("Amolen", "https://www.amolen.com/collections/all/products.json?limit=250"),
    ("Eryone", "https://eryone3d.com/collections/all/products.json?limit=250"),
    ("Flashforge", "https://www.flashforgeshop.com/collections/filament/products.json?limit=250"),
    ("Anycubic", "https://store.anycubic.com/collections/materials/products.json?limit=250"),
    ("Voxelab", "https://www.voxelpla.com/collections/all/products.json?limit=250"),
]

REDDIT_SEARCH_QUERIES: list[tuple[str, str]] = [
    ("Bambu Lab", "https://www.reddit.com/r/BambuLab/search.json?q=filament+spool+OR+box&restrict_sr=1&limit=100&sort=relevance"),
    ("Bambu Lab", "https://www.reddit.com/r/BambuLab/search.json?q=PLA+Matte+OR+PETG+HF+spool&restrict_sr=1&limit=100&sort=relevance"),
    ("SUNLU", "https://www.reddit.com/r/3Dprinting/search.json?q=sunlu+spool+OR+filament+label&restrict_sr=1&limit=100&sort=relevance"),
    ("ELEGOO", "https://www.reddit.com/r/3Dprinting/search.json?q=elegoo+pla+OR+petg+spool&restrict_sr=1&limit=100&sort=relevance"),
    ("Prusament", "https://www.reddit.com/r/prusa3d/search.json?q=prusament+spool+label&restrict_sr=1&limit=100&sort=relevance"),
    ("Polymaker", "https://www.reddit.com/r/3Dprinting/search.json?q=polymaker+OR+polyterra+spool&restrict_sr=1&limit=100&sort=relevance"),
    ("eSUN", "https://www.reddit.com/r/3Dprinting/search.json?q=esun+pla+plus+spool&restrict_sr=1&limit=100&sort=relevance"),
    ("OVERTURE", "https://www.reddit.com/r/3Dprinting/search.json?q=overture+filament+spool&restrict_sr=1&limit=100&sort=relevance"),
    ("Hatchbox", "https://www.reddit.com/r/3Dprinting/search.json?q=hatchbox+spool+filament&restrict_sr=1&limit=100&sort=relevance"),
]

BING_QUERIES: list[tuple[str, str, str]] = [
    ("Bambu Lab", "PLA Basic", "Bambu Lab PLA Basic filament spool label photo"),
    ("Bambu Lab", "PLA Matte", "Bambu Lab PLA Matte filament spool box label"),
    ("Bambu Lab", "PETG HF", "Bambu Lab PETG HF filament spool sticker"),
    ("Bambu Lab", "ABS", "Bambu Lab ABS filament spool box label"),
    ("SUNLU", "PLA+", "SUNLU PLA+ 1.75mm filament spool label photo"),
    ("SUNLU", "PETG", "SUNLU PETG filament spool sticker box"),
    ("SUNLU", "High Speed PLA", "SUNLU High Speed PLA filament spool box"),
    ("ELEGOO", "Rapid PLA+", "ELEGOO Rapid PLA+ cardboard spool label photo"),
    ("ELEGOO", "PLA", "ELEGOO PLA 1.75mm filament spool box label"),
    ("ELEGOO", "Rapid PETG", "ELEGOO Rapid PETG filament spool label"),
    ("Polymaker", "PolyTerra PLA", "Polymaker PolyTerra PLA spool label photo"),
    ("Polymaker", "PolyLite PETG", "Polymaker PolyLite PETG spool label photo"),
    ("eSUN", "PLA+", "eSUN PLA+ 1.75mm filament spool label photo"),
    ("OVERTURE", "PLA", "Overture PLA cardboard spool label photo"),
    ("OVERTURE", "PETG", "Overture PETG filament spool label photo"),
    ("Prusament", "PLA", "Prusament PLA Galaxy Black spool label photo"),
    ("Hatchbox", "PLA", "Hatchbox PLA 1.75mm spool label sticker"),
    ("Creality", "Hyper PLA", "Creality Hyper PLA filament spool box label"),
    ("Anycubic", "PLA", "Anycubic PLA 1.75mm filament spool label"),
    ("Inland", "PLA+", "Inland Microcenter PLA+ filament spool box label"),
    ("JAYO", "PLA+", "JAYO PLA+ filament spool label box"),
]


def _http_get(url: str, timeout: float = 12.0) -> bytes | None:
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


def _parse_product_title(vendor: str, title: str, variant_title: str, matcher: CatalogMatcher) -> tuple[str, str, str]:
    """Extract (line, material, color_name) from a product listing title and variant title."""
    combined = f"{title} {variant_title}".strip()
    line = "PLA"
    line_pats = [
        r"\b(Rapid\s*PLA\+|Rapid\s*PETG|High\s*Speed\s*PLA\+?\s*2\.0|High\s*Speed\s*PLA\+?|High\s*Speed\s*PETG|High\s*Speed\s*Matte\s*PLA)\b",
        r"\b(PolyTerra\s*PLA|PolyLite\s*PLA|PolyLite\s*PETG|PolyLite\s*ABS|PolyLite\s*ASA|PolyMax\s*PLA|PolySonic\s*PLA)\b",
        r"\b(PLA\s*Basic|PLA\s*Matte|PLA\s*Silk|Silk\s*PLA\+?|Matte\s*PLA|PLA\s*Pro|PLA\+\s*2\.0|PLA\+|PLA\s*Plus|PLA\s*Marble|PLA\s*Galaxy|PLA\s*Sparkle|PLA\s*Wood|PLA\s*Glow|PLA\s*CF|PLA-CF)\b",
        r"\b(PETG\s*HF|PETG\s*Basic|PETG\s*Pro|Elite\s*PETG|PETG-CF|PETG)\b",
        r"\b(Hyper\s*PLA|Hyper\s*ABS|CR-ABS|CR-PETG|ABS|ASA|TPU\s*95A|TPU|PC|PAHT-CF|PA6-CF)\b",
    ]
    for pat in line_pats:
        m = re.search(pat, combined, flags=re.IGNORECASE)
        if m:
            line = re.sub(r"\s+", " ", m.group(1)).strip()
            break

    material = matcher.normalize_material(line, combined)
    color_name = ""
    if variant_title and variant_title.lower() not in ("default title", "1.75mm", "1kg"):
        # Clean out weight/diameter tokens from variant_title
        c_clean = re.sub(r"\b(1\.75\s*mm|2\.85\s*mm|\d+(\.\d+)?\s*kg|\d+\s*g|spool|refill|with\s*spool)\b", "", variant_title, flags=re.I)
        c_clean = re.sub(r"[/|-]+", " ", c_clean).strip()
        if len(c_clean) >= 3:
            color_name = c_clean

    if not color_name:
        cm = re.search(
            r"\b(Jade\s*White|Ivory\s*White|Bone\s*White|Charcoal|Ash\s*Gr[ae]y|Space\s*Gr[ae]y|Silk\s*Gold|Silk\s*Silver|Silk\s*Copper|Silk\s*Rainbow|Galaxy\s*Black|Olive\s*Green|Bambu\s*Green|Fire\s*Engine\s*Red|Burgundy\s*Red|Scarlet\s*Red|Mandarin\s*Orange|Lemon\s*Yellow|Grass\s*Green|Marine\s*Blue|Cobalt\s*Blue|Ice\s*Blue|Sakura\s*Pink|Black|White|Gr[ae]y|Silver|Red|Orange|Yellow|Green|Blue|Purple|Pink|Brown|Beige|Gold|Bronze|Copper|Transparent|Clear|Natural|Wood|Marble|Rainbow)\b",
            combined,
            flags=re.IGNORECASE,
        )
        if cm:
            color_name = cm.group(1).strip()

    return line, material, color_name or "Black"


def _collect_shopify_candidates(matcher: CatalogMatcher, max_per_store: int = 45) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for vendor, url in SHOPIFY_BRAND_STORES:
        raw = _http_get(url, timeout=12.0)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            products = data.get("products") or []
        except Exception:
            continue

        store_count = 0
        for prod in products:
            if store_count >= max_per_store:
                break
            title = str(prod.get("title") or "")
            if not any(k in title.lower() for k in ("pla", "petg", "abs", "asa", "tpu", "filament", "pa", "pc")):
                continue
            if any(bad in title.lower() for bad in ("printer", "dryer", "nozzle", "plate", "resin", "wash")):
                continue

            variants_by_img: dict[int, str] = {}
            for v in prod.get("variants") or []:
                vid_img = v.get("image_id")
                vtitle = str(v.get("title") or "")
                if vid_img and vtitle:
                    variants_by_img[int(vid_img)] = vtitle

            images = prod.get("images") or []
            for img_obj in images[:4]:
                if store_count >= max_per_store:
                    break
                src_url = str(img_obj.get("src") or "").strip()
                if not src_url.startswith("http"):
                    continue
                img_id = img_obj.get("id")
                vtitle = variants_by_img.get(int(img_id), "") if img_id else ""
                line, mat, col = _parse_product_title(vendor, title, vtitle, matcher)
                candidates.append(
                    {
                        "url": src_url,
                        "vendor": vendor,
                        "line": line,
                        "material": mat,
                        "color_name": col,
                        "source": "shopify_cdn",
                    }
                )
                store_count += 1
        print(f"[WebHarvester] Collected {store_count} candidate spool/box image URLs from {vendor} store.")
    return candidates


def _collect_bing_image_candidates(max_per_query: int = 18) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for vendor, line, query in BING_QUERIES:
        q_enc = urllib.parse.quote_plus(query)
        search_url = f"https://www.bing.com/images/search?q={q_enc}&form=HDRSC2&first=1"
        raw = _http_get(search_url, timeout=10.0)
        if not raw:
            continue
        html = raw.decode("utf-8", errors="replace")
        # Extract murl (media URL) from Bing image cards
        murls = re.findall(r"murl&quot;:&quot;(https?://[^&]+?\.(?:jpg|jpeg|png))&quot;", html, flags=re.IGNORECASE)
        count = 0
        for murl in murls:
            if count >= max_per_query:
                break
            candidates.append(
                {
                    "url": murl,
                    "vendor": vendor,
                    "line": line,
                    "material": "",
                    "color_name": "",
                    "source": "bing_search",
                }
            )
            count += 1
        print(f"[WebHarvester] Found {count} image URLs for query: '{query}'")
    return candidates


def _collect_reddit_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for vendor, url in REDDIT_SEARCH_QUERIES:
        raw = _http_get(url, timeout=10.0)
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            children = ((payload.get("data") or {}).get("children")) or []
        except Exception:
            continue
        count = 0
        for child in children:
            post = (child or {}).get("data") or {}
            img_url = str(post.get("url_overridden_by_dest") or post.get("url") or "")
            title = str(post.get("title") or "")
            if img_url.lower().endswith((".jpg", ".jpeg", ".png")) and "i.redd.it" in img_url:
                candidates.append(
                    {
                        "url": img_url,
                        "vendor": vendor,
                        "line": "",
                        "material": "",
                        "color_name": "",
                        "title_hint": title,
                        "source": "reddit_community",
                    }
                )
                count += 1
        print(f"[WebHarvester] Found {count} Reddit community photos for {vendor}.")
    return candidates


def harvest_web_dataset(
    max_images: int = 350,
    merge_into_splits: bool = True,
) -> dict[str, int]:
    """Download real filament spool & box photos from the web, auto-annotate with RapidOCR + CatalogMatcher, and merge into XML splits."""
    matcher = CatalogMatcher()
    ocr_engine = create_rapidocr()
    harvest_dir = DATA_DIR / "harvested"
    harvest_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[dict[str, Any]] = []
    candidates.extend(_collect_shopify_candidates(matcher, max_per_store=40))
    candidates.extend(_collect_bing_image_candidates(max_per_query=16))
    candidates.extend(_collect_reddit_candidates())

    # Deduplicate URLs
    seen_urls: set[str] = set()
    unique_cands: list[dict[str, Any]] = []
    for c in candidates:
        u = c["url"].split("?")[0]
        if u not in seen_urls:
            seen_urls.add(u)
            unique_cands.append(c)

    print(f"[WebHarvester] Total unique candidate image URLs: {len(unique_cands)}. Downloading & annotating up to {max_images}...")

    samples: list[AnnotatedSample] = []
    seen_hashes: set[str] = set()

    for cand in unique_cands:
        if len(samples) >= max_images:
            break
        raw_bytes = _http_get(cand["url"], timeout=8.0)
        if not raw_bytes or len(raw_bytes) < 8000:
            continue

        img_hash = hashlib.sha1(raw_bytes).hexdigest()[:12]
        if img_hash in seen_hashes:
            continue
        seen_hashes.add(img_hash)

        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is None or img_bgr.size == 0:
            continue
        h, w = img_bgr.shape[:2]
        if min(h, w) < 180:
            continue

        # Resize to max 1024px side
        if max(h, w) > 1024:
            scale = 1024.0 / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            h, w = img_bgr.shape[:2]

        ocr_lines, boxes = extract_multi_angle_ocr_and_boxes(img_bgr, ocr_engine)
        is_spool = any(b.label == "spool_flange" for b in boxes)
        item, vis_finish = parse_ocr_heuristic(ocr_lines, img_bgr, matcher, is_spool=is_spool)

        # Merge known metadata from product listing / search context when OCR didn't catch it
        if cand.get("vendor") and item.vendor == "Unknown":
            item.vendor = cand["vendor"]
        if cand.get("line") and item.line in ("PLA", "PETG", "ABS", "ASA"):
            item.line = cand["line"]
        if cand.get("material"):
            item.material = matcher.normalize_material(cand["material"], item.line)
        if cand.get("color_name") and (not item.color_name or item.color_name in matcher.color_families):
            item.color_name = cand["color_name"]

        item = matcher.enrich_item(item)

        sid = f"web_{len(samples):04d}_{img_hash[:6]}"
        out_path = harvest_dir / f"{sid}.jpg"
        cv2.imwrite(str(out_path), img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

        split = "val" if (len(samples) % 7 == 0) else "train"
        sample = AnnotatedSample(
            sample_id=sid,
            image_path=f"data/harvested/{out_path.name}",
            original_path=cand["url"],
            width=w,
            height=h,
            split=split,
            item=item,
            boxes=boxes,
            ocr_lines=ocr_lines,
            visual_finish=vis_finish,
        )
        samples.append(sample)
        if len(samples) % 25 == 0:
            print(f"[WebHarvester] Downloaded & annotated {len(samples)}/{max_images} real web photos...")

    ann_dir = DATA_DIR / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    save_dataset_xml(ann_dir / "harvested_labels.xml", samples, split_name="harvested")

    if merge_into_splits and samples:
        train_path = ann_dir / "train_split.xml"
        val_path = ann_dir / "val_split.xml"
        existing_train = [s for s in load_dataset_xml(train_path) if not s.sample_id.startswith("web_")] if train_path.is_file() else []
        existing_val = [s for s in load_dataset_xml(val_path) if not s.sample_id.startswith("web_")] if val_path.is_file() else []

        web_train = [s for s in samples if s.split == "train"]
        web_val = [s for s in samples if s.split == "val"]

        save_dataset_xml(train_path, existing_train + web_train, split_name="train")
        save_dataset_xml(val_path, existing_val + web_val, split_name="val")

    return {
        "harvested_total": len(samples),
        "harvested_train": len([s for s in samples if s.split == "train"]),
        "harvested_val": len([s for s in samples if s.split == "val"]),
    }
