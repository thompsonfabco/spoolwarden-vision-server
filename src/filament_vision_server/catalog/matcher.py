"""Catalog-constrained fuzzy matcher for resolving vendor, product line, material, and color."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as StdET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from ..xml_io import CONFIGS_DIR, FilamentItem, parse_xml_file
from .library_sync import MASTER_CATALOG_XML, infer_color_hex_and_family


@dataclass
class CatalogColor:
    name: str
    hex_code: str
    family: str


@dataclass
class CatalogProduct:
    vendor: str
    line: str
    material: str
    density: float
    diameter: float
    nozzle_range: str
    bed_range: str
    tare_grams: int
    colors: list[CatalogColor] = field(default_factory=list)


def _hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    h = hex_code.lstrip("#").strip()
    if len(h) != 6:
        return (128, 128, 128)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _rgb_distance(hex1: str, hex2: str) -> float:
    r1, g1, b1 = _hex_to_rgb(hex1)
    r2, g2, b2 = _hex_to_rgb(hex2)
    # Redmean perceptual RGB distance
    rmean = (r1 + r2) / 2.0
    dr = r1 - r2
    dg = g1 - g2
    db = b1 - b2
    return math.sqrt((((512 + rmean) * dr * dr) / 256.0) + 4 * dg * dg + (((767 - rmean) * db * db) / 256.0))


class CatalogMatcher:
    """Fast lookup and fuzzy matcher backed by data/catalog/filament_catalog.xml and configs/taxonomy.xml."""

    def __init__(
        self,
        catalog_xml: str | Path = MASTER_CATALOG_XML,
        taxonomy_xml: str | Path = CONFIGS_DIR / "taxonomy.xml",
    ):
        self.taxonomy_root = parse_xml_file(taxonomy_xml)
        self.catalog_path = Path(catalog_xml)
        self.vendors: dict[str, str] = {}  # lower -> canonical
        self.vendor_aliases: dict[str, str] = {}  # lower alias -> canonical
        self.vendor_tares: dict[str, int] = {}
        self.material_aliases: dict[str, str] = {}  # lower alias -> canonical Spoolman material
        self.material_defaults: dict[str, dict[str, Any]] = {}
        self.color_families: list[str] = []
        self.products_by_vendor: dict[str, list[CatalogProduct]] = {}
        self.all_products: list[CatalogProduct] = []

        self._load_taxonomy()
        if self.catalog_path.is_file():
            self._load_catalog(self.catalog_path)

        self._sorted_vendor_aliases: list[str] = [
            a for a in sorted(self.vendor_aliases.keys(), key=len, reverse=True) if len(a) >= 3
        ]
        self._vendor_alias_patterns: list[tuple[str, re.Pattern[str]]] = [
            (a, re.compile(rf"\b{re.escape(a)}\b")) for a in self._sorted_vendor_aliases
        ]
        self._sorted_material_aliases: list[str] = sorted(
            self.material_aliases.keys(), key=len, reverse=True
        )
        self._material_alias_patterns: list[tuple[str, re.Pattern[str]]] = [
            (a, re.compile(rf"\b{re.escape(a)}(?:\b|(?=\+|\s|$))"))
            for a in self._sorted_material_aliases
        ]

    def _load_taxonomy(self) -> None:
        for mat_el in self.taxonomy_root.findall("./materials/material"):
            code = mat_el.attrib["code"]
            self.material_aliases[code.lower()] = code
            self.material_defaults[code] = {
                "density": float(mat_el.attrib.get("density", "1.24")),
                "nozzle_range": mat_el.attrib.get("default-nozzle", "190-230"),
                "bed_range": mat_el.attrib.get("default-bed", "45-60"),
            }
            for al in mat_el.findall("alias"):
                if al.text:
                    self.material_aliases[al.text.strip().lower()] = code

        for v_el in self.taxonomy_root.findall("./vendor-defaults/vendor"):
            vname = v_el.attrib["name"]
            tare = int(v_el.attrib.get("tare", "200"))
            self.vendors[vname.lower()] = vname
            self.vendor_aliases[vname.lower()] = vname
            self.vendor_tares[vname] = tare
            for al in v_el.findall("alias"):
                if al.text:
                    self.vendor_aliases[al.text.strip().lower()] = vname

        for fam_el in self.taxonomy_root.findall("./color-families/family"):
            self.color_families.append(fam_el.attrib["name"])

    def _load_catalog(self, path: Path) -> None:
        root = parse_xml_file(path)
        for v_el in root.findall("vendor"):
            vname = v_el.attrib["name"]
            tare = int(v_el.attrib.get("tare-grams", str(self.vendor_tares.get(vname, 200))))
            self.vendors[vname.lower()] = vname
            self.vendor_aliases[vname.lower()] = vname
            self.vendor_tares[vname] = tare
            for al in v_el.findall("alias"):
                if al.text:
                    self.vendor_aliases[al.text.strip().lower()] = vname

            prods: list[CatalogProduct] = []
            for p_el in v_el.findall("product"):
                colors = [
                    CatalogColor(
                        name=c_el.attrib["name"],
                        hex_code=c_el.attrib.get("hex", "808487"),
                        family=c_el.attrib.get("family", "Gray/Silver"),
                    )
                    for c_el in p_el.findall("color")
                ]
                prod = CatalogProduct(
                    vendor=vname,
                    line=p_el.attrib["line"],
                    material=p_el.attrib["material"],
                    density=float(p_el.attrib.get("density", "1.24")),
                    diameter=float(p_el.attrib.get("diameter", "1.75")),
                    nozzle_range=p_el.attrib.get("nozzle-range", "190-230"),
                    bed_range=p_el.attrib.get("bed-range", "45-60"),
                    tare_grams=tare,
                    colors=colors,
                )
                prods.append(prod)
                self.all_products.append(prod)
            self.products_by_vendor[vname] = prods

    def find_vendor_candidates(self, text: str) -> dict[str, int]:
        """Return canonical vendors found via word-boundary alias matches in text mapped to match counts."""
        clean = (text or "").strip().lower()
        if not clean:
            return {}
        counts: dict[str, int] = {}
        for alias_low, pat in self._vendor_alias_patterns:
            hits = pat.findall(clean)
            if hits:
                canon = self.vendor_aliases[alias_low]
                counts[canon] = counts.get(canon, 0) + len(hits)
        return counts

    def resolve_vendor(self, text: str, min_score: float = 84.0) -> tuple[str, float]:
        """Resolve an OCR vendor string to a canonical vendor name and confidence (0..100)."""
        clean = (text or "").strip()
        if not clean or clean.lower() == "unknown":
            return "Unknown", 0.0

        low = clean.lower()
        if low in self.vendor_aliases:
            return self.vendor_aliases[low], 100.0

        # Word-boundary scan for vendor aliases inside longer OCR strings, ranked by frequency then alias length
        best_canon = "Unknown"
        best_key = (0, 0)
        counts: dict[str, int] = {}
        max_len: dict[str, int] = {}
        for alias_low, pat in self._vendor_alias_patterns:
            hits = pat.findall(low)
            if hits:
                canon = self.vendor_aliases[alias_low]
                counts[canon] = counts.get(canon, 0) + len(hits)
                if len(alias_low) > max_len.get(canon, 0):
                    max_len[canon] = len(alias_low)
        for canon, cnt in counts.items():
            key = (cnt, max_len.get(canon, 0))
            if key > best_key:
                best_key = key
                best_canon = canon
        if best_canon != "Unknown":
            return best_canon, 95.0

        match = process.extractOne(
            low,
            list(self.vendor_aliases.keys()),
            scorer=fuzz.WRatio,
        )
        if match and match[1] >= min_score:
            return self.vendor_aliases[match[0]], float(match[1])

        return "Unknown", 0.0

    def normalize_material(self, text: str, line_hint: str = "") -> str:
        """Map any material or subtype string to Spoolman's canonical base material."""
        combined = f"{text} {line_hint}".strip().lower()
        if not combined:
            return "PLA"

        # Check explicit CF/GF engineering materials first
        for code in (
            "PAHT-CF", "PA12-CF", "PA6-CF", "PA6-GF", "PA-CF", "PET-CF",
            "PPA-CF", "PPS-CF", "PLA-CF", "PETG-CF", "ASA-CF", "ABS-GF",
        ):
            c_low = code.lower()
            if c_low in combined or c_low.replace("-", " ") in combined:
                return code

        # Check line_hint before defaulted "PLA" so updated product lines (e.g. "Rapid PETG") update base material
        if line_hint.strip() and text.strip().upper() == "PLA" and line_hint.strip().lower() != "pla":
            hint_low = line_hint.strip().lower()
            for alias_low, pat in self._material_alias_patterns:
                if pat.search(hint_low):
                    return self.material_aliases[alias_low]

        if text.strip().lower() in self.material_aliases:
            return self.material_aliases[text.strip().lower()]

        for alias_low, pat in self._material_alias_patterns:
            if pat.search(combined):
                return self.material_aliases[alias_low]

        if "petg" in combined:
            return "PETG"
        if "pla" in combined:
            return "PLA"
        if "asa" in combined:
            return "ASA"
        if "abs" in combined:
            return "ABS"
        if "tpu" in combined or "flex" in combined:
            return "TPU"
        if "pva" in combined:
            return "PVA"
        if "hips" in combined:
            return "HIPS"
        if "nylon" in combined or re.search(r"\bpa\d*\b", combined):
            return "PA"
        if re.search(r"\bpc\b", combined) or "polycarbonate" in combined:
            return "PC"
        return "PLA"

    def match_product_line(
        self,
        vendor: str,
        line_query: str,
        material_hint: str = "",
    ) -> CatalogProduct | None:
        """Find the best CatalogProduct for a given vendor and line/subtype hint."""
        candidates = self.products_by_vendor.get(vendor, [])
        if not candidates:
            return None

        q = (line_query or material_hint or "PLA").strip()
        q_low = q.lower()

        # Exact match on line
        for prod in candidates:
            if prod.line.lower() == q_low:
                return prod

        # Filter by base material if known
        norm_mat = self.normalize_material(material_hint, line_query)
        mat_candidates = [p for p in candidates if p.material.upper() == norm_mat.upper()] or candidates

        choices = {i: p.line.lower() for i, p in enumerate(mat_candidates)}
        best = process.extractOne(q_low, choices, scorer=fuzz.token_set_ratio)
        if best and best[1] >= 72.0:
            return mat_candidates[best[2]]

        return mat_candidates[0] if mat_candidates else None

    def resolve_color(
        self,
        color_query: str,
        product: CatalogProduct | None = None,
        visual_hex: str | None = None,
        visual_family: str | None = None,
    ) -> tuple[str, str, str, str]:
        """Return (color_name, color_hex, color_family, hex_source)."""
        clean_q = (color_query or "").strip()
        if product and product.colors:
            if clean_q:
                for c in product.colors:
                    if c.name.lower() == clean_q.lower():
                        return c.name, c.hex_code, c.family, "catalog"
                choices = {i: c.name.lower() for i, c in enumerate(product.colors)}
                best = process.extractOne(clean_q.lower(), choices, scorer=fuzz.WRatio)
                if best and best[1] >= 82.0:
                    matched = product.colors[best[2]]
                    return matched.name, matched.hex_code, matched.family, "catalog"

        if clean_q and clean_q.lower() not in ("unknown", "none", ""):
            hex_code, fam = infer_color_hex_and_family(clean_q, self.taxonomy_root)
            return clean_q, hex_code or visual_hex, fam, "catalog"

        # Fall back to visual color family / hex match against catalog product colors
        if visual_hex and product and product.colors:
            fam_colors = [c for c in product.colors if not visual_family or c.family == visual_family]
            if fam_colors:
                best_c = min(fam_colors, key=lambda c: _rgb_distance(visual_hex, c.hex_code))
                if _rgb_distance(visual_hex, best_c.hex_code) < 65.0:
                    return best_c.name, visual_hex, best_c.family, "estimate"

        fam = visual_family or "Black"
        hex_code, inferred_fam = infer_color_hex_and_family(fam, self.taxonomy_root)
        # Colour names the library does not have must come out as "unknown" plus measured hex and family
        return "unknown", (visual_hex or hex_code).lstrip("#").upper(), fam or inferred_fam, "estimate"

    def enrich_item(self, item: FilamentItem) -> FilamentItem:
        """Snap a draft FilamentItem to canonical catalog vendor, line, material, specs, and color."""
        canon_vendor, v_score = self.resolve_vendor(item.vendor)
        if canon_vendor != "Unknown":
            item.vendor = canon_vendor

        item.material = self.normalize_material(item.material, item.line)
        prod = self.match_product_line(item.vendor, item.line, item.material)

        if prod is not None:
            item.line = prod.line
            item.material = prod.material
            item.density = prod.density
            item.diameter = prod.diameter
            item.tare_grams = prod.tare_grams
            if not item.nozzle_range:
                item.nozzle_range = prod.nozzle_range
            if not item.bed_range:
                item.bed_range = prod.bed_range
        else:
            item.tare_grams = self.vendor_tares.get(item.vendor, 200)
            if "tare" not in item.estimates:
                item.estimates.append("tare")
            mat_def = self.material_defaults.get(item.material, self.material_defaults.get("PLA", {}))
            item.density = float(mat_def.get("density", 1.24))
            if not item.nozzle_range:
                item.nozzle_range = str(mat_def.get("nozzle_range", "190-230"))
            if not item.bed_range:
                item.bed_range = str(mat_def.get("bed_range", "45-60"))

        cname, chex, cfam, hsource = self.resolve_color(
            item.color_name,
            product=prod,
            visual_hex=item.color_hex if item.color_hex_source == "estimate" else None,
            visual_family=item.color_family,
        )
        item.color_name = cname
        item.color_hex = chex
        item.color_family = cfam
        item.color_hex_source = hsource
        if hsource == "estimate" and "color_hex" not in item.estimates:
            item.estimates.append("color_hex")

        if item.nozzle_range and "-" in item.nozzle_range:
            try:
                lo, hi = item.nozzle_range.split("-", 1)
                item.nozzle = (int(lo) + int(hi)) // 2
            except ValueError:
                pass
        if item.bed_range and "-" in item.bed_range:
            try:
                lo, hi = item.bed_range.split("-", 1)
                item.bed = (int(lo) + int(hi)) // 2
            except ValueError:
                pass

        return item
