"""filament-vision-server: Dual-tier local filament spool & box label recognition."""
from __future__ import annotations

from .xml_io import (
    AnnotatedSample,
    BoundingBox,
    FilamentItem,
    IntakeReadResult,
    load_dataset_xml,
    save_dataset_xml,
)

__version__ = "0.2.1"

__all__ = [
    "AnnotatedSample",
    "BoundingBox",
    "FilamentItem",
    "IntakeReadResult",
    "__version__",
    "load_dataset_xml",
    "save_dataset_xml",
]
