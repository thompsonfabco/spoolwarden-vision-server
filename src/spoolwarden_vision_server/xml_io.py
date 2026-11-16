"""Safe XML serialization, parsing, taxonomy loading, and Spoolwarden intake.py adapter."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import xml.etree.ElementTree as StdET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import defusedxml.ElementTree as SafeET
except ImportError as exc:
    raise ImportError(
        "defusedxml is required for safe XML parsing; install defusedxml>=0.7.1"
    ) from exc


def _detect_project_root() -> Path:
    env_home = (
        os.environ.get("SPOOLWARDEN_VISION_SERVER_HOME")
        or os.environ.get("FILAMENT_VISION_SERVER_HOME")
        or os.environ.get("FILAMENT_KIT_VISION_HOME")
    )
    if env_home:
        return Path(env_home).resolve()
    src_root = Path(__file__).resolve().parent.parent.parent
    if (src_root / "configs").is_dir() and (src_root / "data").is_dir():
        return src_root
    cwd = Path.cwd().resolve()
    if (cwd / "configs").is_dir() and (cwd / "data").is_dir():
        return cwd
    for prefix_guess in (
        Path(sys.prefix).parent,
        Path(sys.executable).absolute().parent.parent.parent,
        Path(sys.executable).resolve().parent.parent.parent,
    ):
        if (prefix_guess / "configs").is_dir() and (prefix_guess / "data").is_dir():
            return prefix_guess
    return src_root


PROJECT_ROOT = _detect_project_root()
CONFIGS_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"


def _detect_state_dir() -> Path | None:
    env_state = (
        os.environ.get("SPOOLWARDEN_VISION_SERVER_STATE_DIR")
        or os.environ.get("FILAMENT_VISION_SERVER_STATE_DIR")
        or os.environ.get("FILAMENT_KIT_VISION_STATE_DIR")
        or ""
    ).strip()
    if env_state:
        return Path(env_state).resolve()
    return None


_ENV_STATE_DIR = _detect_state_dir()
STATE_DIR = _ENV_STATE_DIR if _ENV_STATE_DIR is not None else DATA_DIR
SERVICE_CONFIG_PATH = (
    (_ENV_STATE_DIR / "service_config.xml")
    if _ENV_STATE_DIR is not None
    else (CONFIGS_DIR / "service_config.xml")
)


def parse_xml_file(path: str | Path) -> StdET.Element:
    """Safely parse an XML file from disk using defusedxml."""
    raw = Path(path).read_bytes()
    return SafeET.fromstring(raw)


def parse_xml_string(text: str | bytes) -> StdET.Element:
    """Safely parse an XML string/bytes using defusedxml."""
    if isinstance(text, str):
        # Extract <intake-read>...</intake-read> block if surrounded by markdown or prose
        m = re.search(r"(<intake-read\b.*?</intake-read>)", text, flags=re.DOTALL)
        if m:
            text = m.group(1)
        data = text.encode("utf-8")
    else:
        data = text
    return SafeET.fromstring(data)


def to_xml_string(root: StdET.Element, declaration: bool = True) -> str:
    """Format an ElementTree Element with clean 2-space indentation and LF line endings."""
    tree_copy = StdET.fromstring(StdET.tostring(root, encoding="utf-8"))
    StdET.indent(tree_copy, space="  ")
    body = StdET.tostring(tree_copy, encoding="unicode")
    if declaration:
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
    return body + "\n"


def write_xml_atomic(path: str | Path, root: StdET.Element) -> Path:
    """Atomically write an XML Element to disk with UTF-8 encoding and LF line endings."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = to_xml_string(root, declaration=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def parse_temp_range(range_str: str | None) -> tuple[int, int] | None:
    """Parse a nozzle/bed temperature range string into (low, high) integer Celsius bounds."""
    if not range_str:
        return None
    m = re.search(r"(\d+)\s*[-~–]+\s*(\d+)", str(range_str))
    if m:
        return int(m.group(1)), int(m.group(2))
    m_to = re.search(r"(\d+)\s+to\s+(\d+)", str(range_str), flags=re.IGNORECASE)
    if m_to:
        return int(m_to.group(1)), int(m_to.group(2))
    m_single = re.fullmatch(r"\s*(\d+)\s*°?\s*[cC]?\s*", str(range_str))
    if m_single:
        v = int(m_single.group(1))
        return v, v
    return None


@dataclass
class BoundingBox:
    label: str  # box_label | spool_label | spool_windings | spool_flange
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 1.0

    def to_element(self) -> StdET.Element:
        el = StdET.Element(
            "box",
            attrib={
                "label": self.label,
                "x1": f"{self.x1:.4f}",
                "y1": f"{self.y1:.4f}",
                "x2": f"{self.x2:.4f}",
                "y2": f"{self.y2:.4f}",
                "confidence": f"{self.confidence:.3f}",
            },
        )
        return el

    @classmethod
    def from_element(cls, el: StdET.Element) -> BoundingBox:
        return cls(
            label=el.attrib.get("label", "spool_label"),
            x1=float(el.attrib.get("x1", "0")),
            y1=float(el.attrib.get("y1", "0")),
            x2=float(el.attrib.get("x2", "1")),
            y2=float(el.attrib.get("y2", "1")),
            confidence=float(el.attrib.get("confidence", "1.0")),
        )


@dataclass
class FilamentItem:
    """Represents a single recognized filament spool or boxed spool aligned with Spoolman & Spoolwarden."""

    vendor: str = "Unknown"
    material: str = "PLA"
    line: str = "PLA"
    color_name: str = "Black"
    color_family: str = "Black"
    color_hex: str | None = "1A1A1A"
    color_hex_source: str = "estimate"  # catalog | printed | estimate
    net_grams: int = 1000
    tare_grams: int = 200
    density: float = 1.24
    diameter: float = 1.75
    nozzle_range: str | None = "190-230"
    bed_range: str | None = "35-55"
    nozzle: int | None = None
    bed: int | None = None
    lot: str | None = None
    sealed: bool = False
    sealed_reason: str = "bare spool"
    count: int = 1
    existing_filament_id: int | None = None
    source_type: str = "spool_label"  # box_label | spool_label | exposed_spool
    confidence: float = 0.85
    note: str = ""
    estimates: list[str] = field(default_factory=list)
    photos: list[int] = field(default_factory=lambda: [0])

    def __post_init__(self) -> None:
        if self.nozzle is None:
            parsed_n = parse_temp_range(self.nozzle_range)
            if parsed_n is not None:
                self.nozzle = (parsed_n[0] + parsed_n[1]) // 2
        if self.bed is None:
            parsed_b = parse_temp_range(self.bed_range)
            if parsed_b is not None:
                self.bed = (parsed_b[0] + parsed_b[1]) // 2

    @property
    def name(self) -> str:
        """Spoolman filament name: '<Line> <Colour>' without duplicating the line prefix."""
        clean_line = (self.line or self.material or "PLA").strip()
        clean_color = (self.color_name or self.color_family or "").strip()
        if not clean_color:
            return clean_line
        if clean_color.lower().startswith(clean_line.lower()):
            return clean_color
        return f"{clean_line} {clean_color}".strip()

    def to_element(self, index: int = 0) -> StdET.Element:
        item_el = StdET.Element(
            "item",
            attrib={
                "index": str(index),
                "confidence": f"{self.confidence:.3f}",
                "source-type": self.source_type,
                "count": str(max(1, self.count)),
            },
        )
        if self.existing_filament_id is not None:
            item_el.set("existing-filament-id", str(self.existing_filament_id))

        def _add_text(tag: str, val: str | None, **attrs: str) -> StdET.Element | None:
            if val is None:
                return None
            child = StdET.SubElement(item_el, tag, attrib=attrs)
            child.text = str(val)
            return child

        _add_text("vendor", self.vendor)
        _add_text("material", self.material)
        _add_text("line", self.line)
        _add_text("color-name", self.color_name)
        _add_text("name", self.name)
        _add_text("color-family", self.color_family)
        if self.color_hex:
            clean_hex = self.color_hex.lstrip("#").upper()[:6]
            _add_text("color-hex", clean_hex, source=self.color_hex_source)

        spec_attrs: dict[str, str] = {
            "net-grams": str(int(self.net_grams)),
            "tare-grams": str(int(self.tare_grams)),
            "density": f"{float(self.density):.2f}",
            "diameter": f"{float(self.diameter):.2f}",
        }
        if self.nozzle_range:
            spec_attrs["nozzle-range"] = self.nozzle_range
        if self.bed_range:
            spec_attrs["bed-range"] = self.bed_range
        if self.nozzle is not None:
            spec_attrs["nozzle"] = str(int(self.nozzle))
        if self.bed is not None:
            spec_attrs["bed"] = str(int(self.bed))
        if self.lot:
            spec_attrs["lot"] = self.lot
        StdET.SubElement(item_el, "specs", attrib=spec_attrs)

        StdET.SubElement(
            item_el,
            "state",
            attrib={
                "sealed": "true" if self.sealed else "false",
                "sealed-reason": self.sealed_reason or ("in box" if self.sealed else "bare spool"),
            },
        )
        if self.note:
            _add_text("note", self.note)
        if self.estimates:
            est_el = StdET.SubElement(item_el, "estimates")
            for est in self.estimates:
                f_el = StdET.SubElement(est_el, "field")
                f_el.text = est
        if self.photos:
            ph_el = StdET.SubElement(item_el, "photos")
            ph_el.text = ",".join(str(p) for p in self.photos)
        return item_el

    @classmethod
    def from_element(cls, item_el: StdET.Element) -> FilamentItem:
        def _text(tag: str, default: str = "") -> str:
            node = item_el.find(tag)
            return (node.text or "").strip() if node is not None and node.text else default

        hex_node = item_el.find("color-hex")
        color_hex = (hex_node.text or "").strip().lstrip("#").upper() if hex_node is not None and hex_node.text else None
        hex_source = hex_node.attrib.get("source", "estimate") if hex_node is not None else "estimate"

        specs_node = item_el.find("specs")
        s_attr = specs_node.attrib if specs_node is not None else {}

        def _opt_int(val: str | None) -> int | None:
            if not val:
                return None
            try:
                return int(float(val))
            except ValueError:
                return None

        state_node = item_el.find("state")
        st_attr = state_node.attrib if state_node is not None else {}
        sealed = st_attr.get("sealed", "false").lower() == "true"
        sealed_reason = st_attr.get("sealed-reason", "in box" if sealed else "bare spool")

        estimates: list[str] = []
        est_node = item_el.find("estimates")
        if est_node is not None:
            estimates = [(f.text or "").strip() for f in est_node.findall("field") if f.text and f.text.strip()]

        photos = [0]
        ph_node = item_el.find("photos")
        if ph_node is not None and ph_node.text:
            try:
                photos = [int(x.strip()) for x in ph_node.text.split(",") if x.strip()]
            except ValueError:
                photos = [0]

        ef_id = _opt_int(item_el.attrib.get("existing-filament-id"))

        nozzle_range = s_attr.get("nozzle-range") or None
        bed_range = s_attr.get("bed-range") or None
        nozzle = _opt_int(s_attr.get("nozzle"))
        bed = _opt_int(s_attr.get("bed"))
        if nozzle is None:
            parsed_n = parse_temp_range(nozzle_range)
            if parsed_n is not None:
                nozzle = (parsed_n[0] + parsed_n[1]) // 2
        if bed is None:
            parsed_b = parse_temp_range(bed_range)
            if parsed_b is not None:
                bed = (parsed_b[0] + parsed_b[1]) // 2

        parsed_net = _opt_int(s_attr.get("net-grams"))
        parsed_tare = _opt_int(s_attr.get("tare-grams"))

        return cls(
            vendor=_text("vendor", "Unknown"),
            material=_text("material", "PLA"),
            line=_text("line", "PLA"),
            color_name=_text("color-name", "Black"),
            color_family=_text("color-family", "Black"),
            color_hex=color_hex if color_hex and len(color_hex) == 6 else None,
            color_hex_source=hex_source,
            net_grams=parsed_net if parsed_net is not None else 1000,
            tare_grams=parsed_tare if parsed_tare is not None else 200,
            density=float(s_attr.get("density", "1.24")),
            diameter=float(s_attr.get("diameter", "1.75")),
            nozzle_range=nozzle_range,
            bed_range=bed_range,
            nozzle=nozzle,
            bed=bed,
            lot=s_attr.get("lot") or None,
            sealed=sealed,
            sealed_reason=sealed_reason,
            count=max(1, _opt_int(item_el.attrib.get("count")) or 1),
            existing_filament_id=ef_id,
            source_type=item_el.attrib.get("source-type", "spool_label"),
            confidence=float(item_el.attrib.get("confidence", "0.85")),
            note=_text("note", ""),
            estimates=estimates,
            photos=photos,
        )

    def to_spoolwarden_item(self) -> dict[str, Any]:
        """Convert to the exact per-item dictionary expected by Spoolwarden's intake.py."""
        note = self.note
        if self.vendor == "Unknown" and not note.startswith("BRAND UNKNOWN:"):
            note = f"BRAND UNKNOWN: {note}".strip()
        if not note:
            est_str = f"Estimates: {', '.join(self.estimates)}" if self.estimates else "Read by local model"
            note = f"{self.source_type} ({self.color_family}, conf {self.confidence:.2f}). {est_str}"
        return {
            "photos": self.photos,
            "existing_filament_id": self.existing_filament_id,
            "vendor": self.vendor,
            "name": self.name,
            "material": self.material,
            "color_hex": self.color_hex.lstrip("#").upper() if self.color_hex else None,
            "net": self.net_grams,
            "tare": self.tare_grams,
            "density": self.density,
            "diameter": self.diameter,
            "count": self.count,
            "nozzle": self.nozzle,
            "bed": self.bed,
            "nozzle_range": self.nozzle_range,
            "bed_range": self.bed_range,
            "lot": self.lot,
            "note": note,
            "estimates": list(self.estimates),
            "sealed": self.sealed,
            "sealed_reason": self.sealed_reason,
            "line": self.line,
            "color_name": self.color_name,
            "color_family": self.color_family,
            "confidence": round(self.confidence, 3),
            "field_confidence": {
                "vendor": round(0.95 if self.vendor != "Unknown" else 0.20, 2),
                "material": round(0.98 if self.material else 0.50, 2),
                "line": round(0.90 if self.line else 0.50, 2),
                "color": round(0.90 if self.color_hex_source == "catalog" else 0.75, 2),
                "sealed": 0.93,
            },
            # Reader internal metadata
            "reader_extra": {
                "source_type": self.source_type,
                "hex_source": self.color_hex_source,
            },
        }


@dataclass
class IntakeReadResult:
    items: list[FilamentItem] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    tier: str = "cpu-hybrid"
    latency_ms: float = 0.0
    model_name: str = "spoolwarden-vision-server-cpu"

    def to_element(self) -> StdET.Element:
        root = StdET.Element(
            "intake-read",
            attrib={
                "version": "1",
                "tier": self.tier,
                "model": self.model_name,
                "latency-ms": f"{self.latency_ms:.1f}",
            },
        )
        for idx, item in enumerate(self.items):
            root.append(item.to_element(index=idx))
        if self.questions:
            q_root = StdET.SubElement(root, "questions")
            for q in self.questions:
                q_el = StdET.SubElement(q_root, "question")
                q_el.text = q
        return root

    def to_xml(self, declaration: bool = True) -> str:
        return to_xml_string(self.to_element(), declaration=declaration)

    @classmethod
    def from_element(cls, root: StdET.Element) -> IntakeReadResult:
        items = [FilamentItem.from_element(el) for el in root.findall("item")]
        questions: list[str] = []
        q_root = root.find("questions")
        if q_root is not None:
            questions = [(q.text or "").strip() for q in q_root.findall("question") if q.text and q.text.strip()]
        return cls(
            items=items,
            questions=questions,
            tier=root.attrib.get("tier", "cpu-hybrid"),
            latency_ms=float(root.attrib.get("latency-ms", "0.0")),
            model_name=root.attrib.get("model", "spoolwarden-vision-server"),
        )

    @classmethod
    def from_xml(cls, xml_text: str | bytes) -> IntakeReadResult:
        return cls.from_element(parse_xml_string(xml_text))

    def to_spoolwarden_dict(
        self,
        tag: dict | None = None,
        scale: dict | None = None,
        escalated: bool = False,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        """Format as the complete response dict conforming to HTTP API contract v1."""
        res: dict[str, Any] = {
            "api_version": "1.1",
            "items": [it.to_spoolwarden_item() for it in self.items],
            "questions": list(self.questions),
            "reader": {
                "service": "spoolwarden-vision-server",
                "module_version": "0.2.4",
                "tier_used": "gpu" if "gpu" in self.tier.lower() else "cpu",
                "escalated": escalated,
                "fallback_reason": fallback_reason,
                "model": self.model_name,
                "latency_ms": round(self.latency_ms, 1),
            },
            "usage": {
                "model": f"spoolwarden-vision-server/{'gpu' if 'gpu' in self.tier.lower() else 'cpu'}",
                "input_tokens": 0,
                "cached": 0,
                "output_tokens": 0,
            },
        }
        if tag is not None:
            res["tag"] = tag
        if scale is not None:
            res["scale"] = scale
        return res


@dataclass
class AnnotatedSample:
    """Single dataset image annotation stored in XML."""

    sample_id: str
    image_path: str
    original_path: str = ""
    width: int = 0
    height: int = 0
    split: str = "train"  # train | val | gold_test
    item: FilamentItem = field(default_factory=FilamentItem)
    boxes: list[BoundingBox] = field(default_factory=list)
    ocr_lines: list[str] = field(default_factory=list)
    visual_finish: str = "Basic"

    def to_element(self) -> StdET.Element:
        el = StdET.Element(
            "sample",
            attrib={
                "id": self.sample_id,
                "image-path": self.image_path.replace("\\", "/"),
                "original-path": self.original_path.replace("\\", "/"),
                "width": str(self.width),
                "height": str(self.height),
                "split": self.split,
                "visual-finish": self.visual_finish,
            },
        )
        el.append(self.item.to_element(index=0))
        if self.boxes:
            boxes_el = StdET.SubElement(el, "boxes")
            for b in self.boxes:
                boxes_el.append(b.to_element())
        if self.ocr_lines:
            ocr_el = StdET.SubElement(el, "ocr-lines")
            for line in self.ocr_lines:
                l_el = StdET.SubElement(ocr_el, "line")
                l_el.text = line
        return el

    @classmethod
    def from_element(cls, el: StdET.Element) -> AnnotatedSample:
        item_el = el.find("item")
        item = FilamentItem.from_element(item_el) if item_el is not None else FilamentItem()
        boxes: list[BoundingBox] = []
        boxes_el = el.find("boxes")
        if boxes_el is not None:
            boxes = [BoundingBox.from_element(b) for b in boxes_el.findall("box")]
        ocr_lines: list[str] = []
        ocr_el = el.find("ocr-lines")
        if ocr_el is not None:
            ocr_lines = [(l.text or "").strip() for l in ocr_el.findall("line") if l.text and l.text.strip()]
        return cls(
            sample_id=el.attrib.get("id", ""),
            image_path=el.attrib.get("image-path", ""),
            original_path=el.attrib.get("original-path", ""),
            width=int(el.attrib.get("width", "0")),
            height=int(el.attrib.get("height", "0")),
            split=el.attrib.get("split", "train"),
            item=item,
            boxes=boxes,
            ocr_lines=ocr_lines,
            visual_finish=el.attrib.get("visual-finish", "Basic"),
        )


def save_dataset_xml(path: str | Path, samples: list[AnnotatedSample], split_name: str = "all") -> Path:
    root = StdET.Element(
        "filament-dataset",
        attrib={
            "version": "1",
            "split": split_name,
            "count": str(len(samples)),
        },
    )
    for s in samples:
        root.append(s.to_element())
    return write_xml_atomic(path, root)


def load_dataset_xml(path: str | Path) -> list[AnnotatedSample]:
    root = parse_xml_file(path)
    return [AnnotatedSample.from_element(el) for el in root.findall("sample")]


def load_training_samples_with_feedback(
    train_xml: str | Path,
    feedback_xml: str | Path = DATA_DIR / "annotations" / "feedback_verified.xml",
    feedback_weight: int = 2,
) -> list[AnnotatedSample]:
    """Load train_xml and append verified user-feedback samples up-weighted by feedback_weight."""
    samples = load_dataset_xml(train_xml) if Path(train_xml).is_file() else []
    fb_path = Path(feedback_xml)
    if fb_path.is_file():
        fb_samples = load_dataset_xml(fb_path)
        for _ in range(max(1, feedback_weight)):
            samples.extend(fb_samples)
    return samples
