"""Reads filament-library/library (read-only) and builds data/catalog/filament_catalog.xml."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any

from ..xml_io import CONFIGS_DIR, DATA_DIR, parse_xml_file, write_xml_atomic

DEFAULT_LIBRARY_ROOT = Path(
    os.environ.get("SPOOLWARDEN_VISION_SERVER_LIBRARY_ROOT")
    or os.environ.get("FILAMENT_VISION_SERVER_LIBRARY_ROOT")
    or "filament-library"
)
MASTER_CATALOG_XML = DATA_DIR / "catalog" / "filament_catalog.xml"

# Official manufacturer color palettes with (Color Name, 6-digit Hex, Color Family)
# Used both to enrich deficient/empty colors[] arrays in filament-library and to populate
# hex + color-family ground truth in filament_catalog.xml.
OFFICIAL_COLOR_HEX_DB: dict[str, tuple[str, str]] = {
    # Blacks & Charcoals
    "Black": ("1A1A1A", "Black"),
    "Charcoal": ("1E1E1E", "Black"),
    "Matte Black": ("212121", "Black"),
    "Matte Charcoal": ("1E1E1E", "Black"),
    "Jet Black": ("111111", "Black"),
    "Galaxy Black": ("232528", "Black"),
    "Onyx Black": ("18191A", "Black"),
    "Obsidian Black": ("1C1C1E", "Black"),
    "Carbon Black": ("1F2022", "Black"),
    "Sparkle Black": ("222426", "Black"),
    "Silk Black": ("262626", "Black"),
    "Translucent Black": ("333333", "Black"),
    # Whites & Ivories
    "White": ("F5F5F5", "White"),
    "Jade White": ("F2F4F3", "White"),
    "Ivory White": ("F7F4E9", "White"),
    "Matte Ivory White": ("F6F3E7", "White"),
    "Cold White": ("F4F7FA", "White"),
    "Cool White": ("F3F6FA", "White"),
    "Warm White": ("F6F1E3", "White"),
    "Bone White": ("E8E1CF", "White"),
    "Marble White": ("ECECE8", "White"),
    "Pearl White": ("F0EFEA", "White"),
    "Silk White": ("F4F4F6", "White"),
    "Alabaster White": ("F2F0E6", "White"),
    "Pure White": ("FAFAFA", "White"),
    # Grays & Silvers
    "Gray": ("808487", "Gray/Silver"),
    "Grey": ("808487", "Gray/Silver"),
    "Ash Gray": ("6E7275", "Gray/Silver"),
    "Matte Ash Gray": ("6E7275", "Gray/Silver"),
    "Light Gray": ("B2B6B9", "Gray/Silver"),
    "Dark Gray": ("4B4E52", "Gray/Silver"),
    "Dark Grey": ("4B4E52", "Gray/Silver"),
    "Blue Gray": ("657786", "Gray/Silver"),
    "Blue Grey": ("657786", "Gray/Silver"),
    "Silver": ("A8ADB2", "Gray/Silver"),
    "Silk Silver": ("BCC1C6", "Gray/Silver"),
    "Space Gray": ("5D6166", "Gray/Silver"),
    "Space Grey": ("5D6166", "Gray/Silver"),
    "Slate Gray": ("626D75", "Gray/Silver"),
    "Nardo Gray": ("7A7F82", "Gray/Silver"),
    "Titanium Gray": ("71757A", "Gray/Silver"),
    "Iron Gray": ("4A4D50", "Gray/Silver"),
    "Marble": ("D8D9D6", "Gray/Silver"),
    "Marble Gray": ("C7C9C6", "Gray/Silver"),
    "Rock Gray": ("787A78", "Gray/Silver"),
    # Reds
    "Red": ("D12229", "Red"),
    "Scarlet Red": ("D92121", "Red"),
    "Matte Scarlet Red": ("D12B2B", "Red"),
    "Crimson Red": ("A31621", "Red"),
    "Dark Red": ("8B1A1A", "Red"),
    "Burgundy": ("6B1D2F", "Red"),
    "Burgundy Red": ("6B1D2F", "Red"),
    "Fire Engine Red": ("CE2029", "Red"),
    "Wine Red": ("6E1E2A", "Red"),
    "Cherry Red": ("C41E3A", "Red"),
    "Ruby Red": ("9B111E", "Red"),
    "Silk Red": ("C8232C", "Red"),
    "Translucent Red": ("D9383A", "Red"),
    # Oranges
    "Orange": ("F57C00", "Orange"),
    "Mandarin Orange": ("F26522", "Orange"),
    "Matte Mandarin Orange": ("F06A28", "Orange"),
    "Pumpkin Orange": ("FF7518", "Orange"),
    "Tangerine": ("F28500", "Orange"),
    "Coral": ("FF6F61", "Orange"),
    "Amber": ("FFBF00", "Orange"),
    "Terracotta": ("C66345", "Orange"),
    "Sunrise Orange": ("F76F20", "Orange"),
    "Silk Orange": ("F58220", "Orange"),
    "Translucent Orange": ("F7882F", "Orange"),
    # Yellows
    "Yellow": ("FBC02D", "Yellow"),
    "Lemon Yellow": ("FCE300", "Yellow"),
    "Matte Lemon Yellow": ("F7DC28", "Yellow"),
    "Sunflower Yellow": ("F5B700", "Yellow"),
    "Cyber Yellow": ("FFD300", "Yellow"),
    "Fluorescent Yellow": ("E6FF00", "Yellow"),
    "Silk Yellow": ("F7CE3E", "Yellow"),
    "Translucent Yellow": ("F9DB4A", "Yellow"),
    # Greens
    "Green": ("2E7D32", "Green"),
    "Bambu Green": ("00AE42", "Green"),
    "Mistletoe Green": ("265C3A", "Green"),
    "Grass Green": ("43A047", "Green"),
    "Matte Grass Green": ("469A49", "Green"),
    "Olive Green": ("556B2F", "Green"),
    "Army Green": ("4B5320", "Green"),
    "Dark Green": ("1B4D2E", "Green"),
    "Pine Green": ("1D4E3E", "Green"),
    "Forest Green": ("225529", "Green"),
    "Mint": ("88D8B0", "Green"),
    "Mint Green": ("88D8B0", "Green"),
    "Ice Mint": ("9FE2BF", "Green"),
    "Jade Green": ("00A86B", "Green"),
    "Matcha Green": ("74A12E", "Green"),
    "Lime Green": ("76FF03", "Green"),
    "Fluorescent Green": ("39FF14", "Green"),
    "Avocado Green": ("568203", "Green"),
    "Silk Green": ("28965A", "Green"),
    "Glow Green": ("B2FBA5", "Green"),
    "Translucent Green": ("4CAF50", "Green"),
    # Blues
    "Blue": ("1976D2", "Blue"),
    "Cobalt Blue": ("0047AB", "Blue"),
    "Marine Blue": ("005BBB", "Blue"),
    "Matte Marine Blue": ("0E5A9C", "Blue"),
    "Ice Blue": ("8FD3E8", "Blue"),
    "Matte Ice Blue": ("8ECAE6", "Blue"),
    "Sky Blue": ("38B6FF", "Blue"),
    "Cyan": ("00AEEF", "Blue"),
    "Navy Blue": ("1A2B4C", "Blue"),
    "Dark Blue": ("102A56", "Blue"),
    "Royal Blue": ("2454C5", "Blue"),
    "Teal": ("008080", "Blue"),
    "Turquoise": ("30D5C8", "Blue"),
    "Lake Blue": ("2B8CBE", "Blue"),
    "Sea Blue": ("006994", "Blue"),
    "Silk Blue": ("1E78D6", "Blue"),
    "Glow Blue": ("8CE7FF", "Blue"),
    "Translucent Blue": ("2998E5", "Blue"),
    "Translucent Light Blue": ("6EC4F5", "Blue"),
    # Purples
    "Purple": ("7B1FA2", "Purple"),
    "Indigo Purple": ("4B0082", "Purple"),
    "Lilac Purple": ("B388EB", "Purple"),
    "Matte Lilac Purple": ("B088E3", "Purple"),
    "Lavender": ("967BB6", "Purple"),
    "Violet": ("6A0DAD", "Purple"),
    "Galaxy Purple": ("3F2A56", "Purple"),
    "Plum": ("673147", "Purple"),
    "Taro Purple": ("A58FAA", "Purple"),
    "Silk Purple": ("8A3AB9", "Purple"),
    "Translucent Purple": ("8E44AD", "Purple"),
    # Pinks & Magentas
    "Pink": ("EC407A", "Pink"),
    "Sakura Pink": ("F4B6C2", "Pink"),
    "Matte Sakura Pink": ("F2B1BE", "Pink"),
    "Magenta": ("D81B60", "Pink"),
    "Hot Pink": ("FF1493", "Pink"),
    "Rose Pink": ("E86F88", "Pink"),
    "Peach": ("FFCBA4", "Pink"),
    "Peach Pink": ("F7A89A", "Pink"),
    "Fuchsia": ("C154C1", "Pink"),
    "Silk Pink": ("EE6B9E", "Pink"),
    # Browns, Beiges & Woods
    "Brown": ("6D4C41", "Brown/Beige"),
    "Cocoa Brown": ("5C3A21", "Brown/Beige"),
    "Dark Brown": ("4A2E1B", "Brown/Beige"),
    "Matte Dark Brown": ("4E342E", "Brown/Beige"),
    "Latte Brown": ("B8926A", "Brown/Beige"),
    "Matte Latte Brown": ("B58E65", "Brown/Beige"),
    "Desert Tan": ("C9B28B", "Brown/Beige"),
    "Matte Desert Tan": ("C7B087", "Brown/Beige"),
    "Beige": ("D8C6A5", "Brown/Beige"),
    "Tan": ("C6A678", "Brown/Beige"),
    "Khaki": ("BDB092", "Brown/Beige"),
    "Coffee": ("6F4E37", "Brown/Beige"),
    "Chocolate": ("593722", "Brown/Beige"),
    "Caramel": ("AF6E4D", "Brown/Beige"),
    "Wood": ("B08354", "Brown/Beige"),
    "Light Wood": ("C89D6C", "Brown/Beige"),
    "Dark Wood": ("6E492D", "Brown/Beige"),
    "Rosewood": ("652A2A", "Brown/Beige"),
    "Clay Brown": ("9B6244", "Brown/Beige"),
    "Skin": ("E5B89B", "Brown/Beige"),
    # Golds, Bronzes & Coppers
    "Gold": ("D4AF37", "Gold/Bronze"),
    "Silk Gold": ("DFB73C", "Gold/Bronze"),
    "Bronze": ("8C6239", "Gold/Bronze"),
    "Silk Bronze": ("966A3E", "Gold/Bronze"),
    "Copper": ("B87333", "Gold/Bronze"),
    "Silk Copper": ("C27938", "Gold/Bronze"),
    "Brass": ("B5A642", "Gold/Bronze"),
    "Champagne Gold": ("E5C88E", "Gold/Bronze"),
    "Rose Gold": ("B76E79", "Gold/Bronze"),
    # Transparent & Natural
    "Transparent": ("E0E8EA", "Transparent/Natural"),
    "Clear": ("E4ECEE", "Transparent/Natural"),
    "Natural": ("E3E0D3", "Transparent/Natural"),
    "Translucent": ("DCE6E8", "Transparent/Natural"),
    # Multi-color / Gradient / Rainbow
    "Rainbow": ("9C27B0", "Multi-color/Rainbow"),
    "Silk Rainbow": ("A636B8", "Multi-color/Rainbow"),
    "Gradient": ("5C6BC0", "Multi-color/Rainbow"),
    "Blueberry Bubblegum": ("5B4DA8", "Multi-color/Rainbow"),
    "Candy": ("E94E77", "Multi-color/Rainbow"),
    "Ocean to Meadow": ("1B8A6B", "Multi-color/Rainbow"),
    "Solar Breeze": ("F39C12", "Multi-color/Rainbow"),
    "Dusk to Dawn": ("6C3483", "Multi-color/Rainbow"),
    "Red Gold": ("C84B31", "Multi-color/Rainbow"),
    "Blue Green": ("118AB2", "Multi-color/Rainbow"),
    "Gold Silver Copper": ("C5A03F", "Multi-color/Rainbow"),
}

# Brand + Product Line specific curated color lists for enriching filament-library
BRAND_LINE_COLORS: dict[tuple[str, str], list[str]] = {
    ("Bambu Lab", "PLA Basic"): [
        "Jade White", "Black", "Gray", "Blue Gray", "Silver", "Red", "Magenta",
        "Orange", "Pumpkin Orange", "Sunflower Yellow", "Yellow", "Bambu Green",
        "Mistletoe Green", "Cyan", "Blue", "Cobalt Blue", "Indigo Purple",
        "Pink", "Cocoa Brown", "Beige", "Gold", "Bronze",
    ],
    ("Bambu Lab", "PLA Matte"): [
        "Ivory White", "Bone White", "Charcoal", "Ash Gray", "Nardo Gray",
        "Scarlet Red", "Dark Red", "Mandarin Orange", "Lemon Yellow",
        "Grass Green", "Dark Green", "Ice Blue", "Marine Blue", "Lilac Purple",
        "Sakura Pink", "Latte Brown", "Dark Brown", "Desert Tan",
    ],
    ("Bambu Lab", "PLA Silk"): [
        "Gold", "Silver", "Copper", "Rose Gold", "Red", "Blue", "Green", "Purple", "White", "Pink",
    ],
    ("Bambu Lab", "PLA-CF"): [
        "Black", "Iris Purple", "Matcha Green", "Royal Blue", "Burgundy Red", "Lava Gray", "Jeans Blue",
    ],
    ("Bambu Lab", "PLA Marble"): ["Red Granite", "White Marble"],
    ("Bambu Lab", "PLA Sparkle"): ["Classic Gold Sparkle", "Slate Gray Sparkle", "Crimson Red Sparkle", "Onyx Black Sparkle", "Alpine Green Sparkle", "Royal Purple Sparkle"],
    ("Bambu Lab", "PLA Galaxy"): ["Purple", "Brown", "Green", "Nebula"],
    ("Bambu Lab", "PLA Glow"): ["Glow Green", "Glow Blue", "Glow Orange", "Glow Pink", "Glow Yellow"],
    ("Bambu Lab", "PLA Translucent"): ["Teal", "Light Jade", "Blue", "Orange", "Purple", "Red", "Yellow", "Ice Blue", "Cherry Pink", "Mellow Yellow"],
    ("Bambu Lab", "PETG Basic"): ["Black", "White", "Gray", "Blue Gray", "Red", "Orange", "Yellow", "Green", "Blue", "Violet", "Natural", "Gold"],
    ("Bambu Lab", "PETG HF"): ["Black", "White", "Gray", "Dark Gray", "Red", "Orange", "Yellow", "Green", "Lake Blue", "Blue", "Cream", "Peanut Brown"],
    ("Bambu Lab", "PETG Translucent"): ["Translucent", "Translucent Blue", "Translucent Green", "Translucent Orange", "Translucent Purple", "Translucent Pink", "Translucent Gray", "Translucent Olive", "Translucent Teal"],
    ("Bambu Lab", "PETG-CF"): ["Black", "Titan Gray", "Brick Red", "Indigo Blue", "Malachite Green", "Violet Purple"],
    ("Bambu Lab", "ABS"): ["Black", "White", "Silver", "Red", "Orange", "Yellow", "Bambu Green", "Blue", "Navy Blue", "Azure", "Olive", "Tangerine Yellow"],
    ("Bambu Lab", "ASA"): ["Black", "White", "Gray", "Red", "Green", "Blue"],
    ("Bambu Lab", "TPU 95A"): ["Black", "White", "Red", "Yellow", "Blue", "Transparent"],
    ("Bambu Lab", "TPU 95A HF"): ["Black", "White", "Gray", "Red", "Yellow", "Blue"],
    ("Bambu Lab", "PC"): ["Black", "White", "Clear Black", "Transparent"],
    ("Bambu Lab", "PAHT-CF"): ["Black"],
    ("Bambu Lab", "PA6-CF"): ["Black"],
    ("Bambu Lab", "PA6-GF"): ["Black", "White", "Gray", "Orange", "Yellow", "Green", "Blue", "Brown"],
    ("Bambu Lab", "PET-CF"): ["Black"],
    ("SUNLU", "PLA"): ["Black", "White", "Grey", "Silver", "Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Pink", "Transparent", "Wood", "Skin", "Coffee"],
    ("SUNLU", "PLA+"): ["Black", "White", "Grey", "Silver", "Red", "Fire Engine Red", "Orange", "Sunny Orange", "Yellow", "Green", "Grass Green", "Olive Green", "Blue", "Sky Blue", "Purple", "Pink", "Rose", "Coffee", "Skin", "Bone White", "Transparent"],
    ("SUNLU", "PLA+ 2.0"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Olive Green", "Blue", "Cyan", "Purple", "Pink", "Bone White", "Coffee"],
    ("SUNLU", "High Speed PLA"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Klein Blue"],
    ("SUNLU", "High Speed Matte PLA"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Matcha Green", "Olive Green", "Blue", "Navy Blue", "Purple", "Pink", "Clay", "Coconut"],
    ("SUNLU", "Matte PLA"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Olive Green", "Blue", "Purple", "Pink", "Skin", "Coffee"],
    ("SUNLU", "Silk PLA+"): ["Gold", "Silver", "Copper", "Red Copper", "Bronze", "Black", "White", "Red", "Blue", "Green", "Purple", "Rainbow"],
    ("SUNLU", "PETG"): ["Black", "White", "Grey", "Silver", "Red", "Orange", "Yellow", "Green", "Olive Green", "Blue", "Cyan", "Purple", "Pink", "Transparent", "Transparent Blue", "Transparent Red", "Transparent Green"],
    ("SUNLU", "High Speed PETG"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Transparent"],
    ("SUNLU", "ABS"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue"],
    ("SUNLU", "ASA"): ["Black", "White", "Grey", "Red", "Blue", "Green"],
    ("SUNLU", "TPU 95A"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Transparent"],
    ("ELEGOO", "PLA"): ["Black", "White", "Grey", "Space Grey", "Silver", "Red", "True Red", "Burgundy Red", "Orange", "Yellow", "Green", "Neon Green", "Sea Green", "Blue", "Dark Blue", "Sky Blue", "Purple", "Pink", "Wood", "Beige", "Brown", "Clear"],
    ("ELEGOO", "PLA+"): ["Black", "White", "Grey", "Space Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Dark Blue", "Purple", "Pink", "Beige", "Brown", "Translucent"],
    ("ELEGOO", "Rapid PLA+"): ["Black", "White", "Grey", "Space Grey", "Silver", "Red", "Orange", "Yellow", "Green", "Blue", "Brown", "Beige"],
    ("ELEGOO", "Matte PLA"): ["Matte Black", "Matte White", "Matte Slate Grey", "Matte Ruby Red", "Matte Orange", "Matte Sunshine Yellow", "Matte Mint Green", "Matte Olive Green", "Matte Navy Blue", "Matte Ice Blue", "Matte Lavender Purple", "Matte Sakura Pink", "Matte Beige", "Matte Earth Brown"],
    ("ELEGOO", "Silk PLA"): ["Silk Gold", "Silk Silver", "Silk Copper", "Silk Bronze", "Silk White", "Silk Black", "Silk Red", "Silk Blue", "Silk Green", "Silk Purple", "Silk Rainbow", "Blue Green", "Red Gold"],
    ("ELEGOO", "PLA Pro"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Brown"],
    ("ELEGOO", "Rapid PETG"): ["Black", "White", "Grey", "Space Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Clear", "Translucent Blue"],
    ("ELEGOO", "PETG Pro"): ["Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Clear", "Silver", "Burgundy Red"],
    ("ELEGOO", "ASA"): ["Black", "White", "Grey", "Red", "Green", "Blue"],
    ("ELEGOO", "ABS"): ["Black", "White", "Grey", "Red", "Blue"],
    ("ELEGOO", "TPU 95A"): ["Black", "White", "Grey", "Red", "Yellow", "Blue", "Translucent"],
}


def default_colors_for_line(product: str, material: str) -> list[str]:
    """Return sensible standard colors for a product line based on its subtype/material."""
    p_low = product.lower()
    m_low = material.lower()
    if "cf" in p_low or "cf" in m_low:
        return ["Black", "Dark Gray", "Burgundy Red", "Royal Blue", "Pine Green"]
    if "gf" in p_low or "gf" in m_low:
        return ["Black", "White", "Gray", "Orange", "Blue"]
    if "silk" in p_low:
        return ["Silk Gold", "Silk Silver", "Silk Copper", "Silk Bronze", "Silk Red", "Silk Blue", "Silk Green", "Silk Purple", "Silk White", "Silk Black", "Silk Rainbow"]
    if "matte" in p_low:
        return ["Charcoal", "Ivory White", "Ash Gray", "Scarlet Red", "Mandarin Orange", "Lemon Yellow", "Grass Green", "Marine Blue", "Ice Blue", "Lilac Purple", "Sakura Pink", "Latte Brown", "Desert Tan"]
    if "marble" in p_low:
        return ["Marble White", "Marble Gray", "Red Granite"]
    if "galaxy" in p_low or "sparkle" in p_low or "glitter" in p_low:
        return ["Galaxy Black", "Galaxy Purple", "Slate Gray", "Crimson Red", "Pine Green", "Gold"]
    if "wood" in p_low:
        return ["Wood", "Light Wood", "Dark Wood", "Rosewood"]
    if "glow" in p_low:
        return ["Glow Green", "Glow Blue", "Glow Orange", "Glow Pink"]
    if "translucent" in p_low or "transparent" in p_low or "clear" in p_low:
        return ["Transparent", "Translucent Blue", "Translucent Red", "Translucent Green", "Translucent Yellow", "Translucent Orange", "Translucent Purple"]
    if "rainbow" in p_low or "gradient" in p_low:
        return ["Rainbow", "Gradient", "Blue Green", "Red Gold"]
    if m_low in ("pa", "pc", "pei", "peek", "pps", "ppa", "pva", "hips", "pp"):
        return ["Black", "White", "Natural", "Gray"]
    if m_low in ("abs", "asa", "tpu"):
        return ["Black", "White", "Gray", "Silver", "Red", "Orange", "Yellow", "Green", "Blue", "Transparent"]
    return [
        "Black", "White", "Gray", "Silver", "Red", "Orange", "Yellow",
        "Green", "Olive Green", "Blue", "Cyan", "Purple", "Pink", "Brown", "Beige", "Gold",
    ]


def has_known_color_match(color_name: str, taxonomy_root: StdET.Element | None = None) -> bool:
    """Return True if color_name resolves via OFFICIAL_COLOR_HEX_DB or taxonomy color keywords."""
    clean = color_name.strip()
    if not clean:
        return False
    if clean in OFFICIAL_COLOR_HEX_DB:
        return True
    low = clean.lower()
    for k in OFFICIAL_COLOR_HEX_DB:
        if k.lower() == low or k.lower() in low:
            return True
    if taxonomy_root is not None:
        for fam_el in taxonomy_root.findall("./color-families/family"):
            for kw_el in fam_el.findall("keyword"):
                kw = (kw_el.text or "").strip().lower()
                if kw and kw in low:
                    return True
    return False


def infer_color_hex_and_family(color_name: str, taxonomy_root: StdET.Element | None = None) -> tuple[str, str]:
    """Resolve a color name to (6-char uppercase hex, 14-class color_family)."""
    clean = color_name.strip()
    if clean in OFFICIAL_COLOR_HEX_DB:
        return OFFICIAL_COLOR_HEX_DB[clean]

    # Case-insensitive exact lookup
    low = clean.lower()
    for k, val in OFFICIAL_COLOR_HEX_DB.items():
        if k.lower() == low:
            return val

    # Substring match against official DB (longest key first)
    for k in sorted(OFFICIAL_COLOR_HEX_DB.keys(), key=len, reverse=True):
        if k.lower() in low:
            return OFFICIAL_COLOR_HEX_DB[k]

    # Taxonomy keyword fallback
    if taxonomy_root is not None:
        for fam_el in taxonomy_root.findall("./color-families/family"):
            fam_name = fam_el.attrib.get("name", "Black")
            def_hex = fam_el.attrib.get("default-hex", "1A1A1A")
            for kw_el in fam_el.findall("keyword"):
                kw = (kw_el.text or "").strip().lower()
                if kw and kw in low:
                    return def_hex, fam_name

    return "808487", "Gray/Silver"


def enrich_filament_library(library_root: str | Path = DEFAULT_LIBRARY_ROOT, write_back: bool = True) -> dict[str, int]:
    """Read-only check of filament-library/library (modifications disabled)."""
    lib_root = Path(library_root)
    tools_dir = lib_root / "tools"
    if not (lib_root / "library").is_dir():
        raise FileNotFoundError(f"filament-library not found at {lib_root}")

    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import build_index  # type: ignore[import-not-found]
    import fl  # type: ignore[import-not-found]

    files = fl.library_files()
    stats = {"total": len(files), "already_had_colors": 0, "enriched": 0, "invalid": 0}

    for fpath in files:
        entry = fl.read_entry(fpath)
        vendor = entry.get("vendor", "")
        product = entry.get("product", "")
        material = entry.get("material", "PLA")
        existing_colors = entry.get("colors") or []

        target_colors = BRAND_LINE_COLORS.get((vendor, product))
        if not existing_colors:
            new_colors = list(target_colors) if target_colors else default_colors_for_line(product, material)
            entry["colors"] = new_colors
            if write_back:
                errs = fl.validate(entry)
                if errs:
                    stats["invalid"] += 1
                else:
                    fl.write_entry(entry)
                    stats["enriched"] += 1
            else:
                stats["enriched"] += 1
        elif target_colors and len(existing_colors) < len(target_colors):
            merged = list(existing_colors) + [c for c in target_colors if c not in existing_colors]
            entry["colors"] = merged
            if write_back:
                errs = fl.validate(entry)
                if not errs:
                    fl.write_entry(entry)
                    stats["enriched"] += 1
            stats["already_had_colors"] += 1
        else:
            stats["already_had_colors"] += 1

    if write_back and stats["enriched"] > 0:
        build_index.build()

    return stats


def normalize_bed_range(raw: str | None, default: str = "45-60") -> str:
    """Normalize heterogeneous bed temperature strings to strict '<lo>-<hi>' Celsius bounds."""
    s = (raw or "").strip()
    if not s:
        return default
    m_pm = re.search(r"(\d+)\s*\+/-\s*(\d+)", s)
    if m_pm:
        center, tol = int(m_pm.group(1)), int(m_pm.group(2))
        return f"{max(0, center - tol)}-{center + tol}"
    m_rt = re.search(r"(?:no\s*heating|rt|room\s*temp(?:erature)?)\s*[-~–]+\s*(\d+)", s, flags=re.IGNORECASE)
    if m_rt:
        return f"25-{int(m_rt.group(1))}"
    m_range = re.search(r"(\d+)\s*[-~–]+\s*(\d+)", s)
    if m_range:
        return f"{int(m_range.group(1))}-{int(m_range.group(2))}"
    m_lt = re.match(r"^\s*<\s*(\d+)", s)
    if m_lt:
        return f"25-{int(m_lt.group(1))}"
    m_single = re.match(r"^\s*(\d+)(?:\s*\([^)]*\))?\s*$", s)
    if m_single:
        v = int(m_single.group(1))
        return f"{v}-{v}"
    return default


def build_master_catalog_xml(
    library_root: str | Path = DEFAULT_LIBRARY_ROOT,
    output_xml: str | Path = MASTER_CATALOG_XML,
    enrich_source_library: bool = False,
) -> Path:
    """Build data/catalog/filament_catalog.xml from filament-library/library and taxonomy.xml."""
    # filament-library is treated as strictly read-only.
    if enrich_source_library:
        enrich_filament_library(library_root=library_root, write_back=False)

    taxonomy_root = parse_xml_file(CONFIGS_DIR / "taxonomy.xml")
    vendor_defaults: dict[str, dict[str, Any]] = {}
    for v_el in taxonomy_root.findall("./vendor-defaults/vendor"):
        vname = v_el.attrib["name"]
        vendor_defaults[vname.lower()] = {
            "name": vname,
            "tare": v_el.attrib.get("tare", "200"),
            "priority": v_el.attrib.get("priority", "3"),
            "flange_trait": v_el.attrib.get("flange-trait", "standard-spool"),
            "aliases": [(a.text or "").strip() for a in v_el.findall("alias") if a.text],
        }

    lib_dir = Path(library_root) / "library"
    json_files = sorted(lib_dir.rglob("*.json"))

    by_vendor: dict[str, list[dict[str, Any]]] = {}
    for jf in json_files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        by_vendor.setdefault(data["vendor"], []).append(data)

    root = StdET.Element(
        "filament-catalog",
        attrib={
            "version": "1",
            "source-library": "filament-library",
            "vendor-count": str(len(by_vendor)),
            "product-count": str(len(json_files)),
        },
    )

    for vendor_name in sorted(by_vendor.keys(), key=str.lower):
        v_meta = vendor_defaults.get(
            vendor_name.lower(),
            {"tare": "200", "priority": "3", "flange_trait": "standard-spool", "aliases": []},
        )
        v_el = StdET.SubElement(
            root,
            "vendor",
            attrib={
                "name": vendor_name,
                "slug": re.sub(r"[^a-z0-9]+", "-", vendor_name.lower()).strip("-"),
                "tare-grams": str(v_meta["tare"]),
                "priority": str(v_meta["priority"]),
                "flange-trait": str(v_meta["flange_trait"]),
            },
        )
        for alias in v_meta["aliases"]:
            a_el = StdET.SubElement(v_el, "alias")
            a_el.text = alias

        for entry in sorted(by_vendor[vendor_name], key=lambda x: x["product"].lower()):
            fields = entry.get("fields") or {}
            ranges = entry.get("published_ranges") or {}
            low_t = (fields.get("nozzle_temperature_range_low") or [None])[0]
            high_t = (fields.get("nozzle_temperature_range_high") or [None])[0]
            nozzle_range = f"{low_t}-{high_t}" if low_t and high_t else "190-230"
            raw_bed = (
                ranges.get("bed_all_plates")
                or ranges.get("textured_plate_temp")
                or ranges.get("hot_plate_temp")
                or "45-60"
            )
            bed_range = normalize_bed_range(str(raw_bed))
            density = (fields.get("filament_density") or ["1.24"])[0]
            diameter = (fields.get("filament_diameter") or ["1.75"])[0]

            p_attrs = {
                "line": entry["product"],
                "material": entry["material"],
                "density": str(density),
                "diameter": str(diameter),
                "nozzle-range": str(nozzle_range),
                "bed-range": str(bed_range),
            }
            if entry.get("bambu_parent"):
                p_attrs["bambu-parent"] = entry["bambu_parent"]
            if entry.get("orca_parent"):
                p_attrs["orca-parent"] = entry["orca_parent"]

            p_el = StdET.SubElement(v_el, "product", attrib=p_attrs)
            # Use only colors genuinely present in the committed filament-library entries
            colors = entry.get("colors") or []
            for cname in colors:
                hex_code, fam = infer_color_hex_and_family(cname, taxonomy_root)
                StdET.SubElement(
                    p_el,
                    "color",
                    attrib={
                        "name": cname,
                        "hex": hex_code,
                        "family": fam,
                    },
                )

    return write_xml_atomic(output_xml, root)
