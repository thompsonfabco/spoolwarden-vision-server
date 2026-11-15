"""Lightweight label constants shared between CPU inference and GPU/CPU training."""
from __future__ import annotations

COLOR_FAMILIES = [
    "Black", "White", "Gray/Silver", "Red", "Orange", "Yellow", "Green",
    "Blue", "Purple", "Pink", "Brown/Beige", "Gold/Bronze",
    "Transparent/Natural", "Multi-color/Rainbow",
]

VISUAL_FINISHES = [
    "Basic", "Matte", "Silk", "Galaxy", "Translucent", "CF", "Wood", "Glow",
]

VISUAL_VENDORS = [
    "Bambu Lab", "SUNLU", "ELEGOO", "Polymaker", "eSUN", "OVERTURE",
    "Hatchbox", "Prusament", "Creality", "Anycubic", "Eryone", "Inland",
    "JAYO", "GEEETECH", "DEEPLEE", "Push Plastic", "Kingroon", "Amolen",
    "Flashforge", "3DXTech", "Other",
]

SEALED_STATES = ["bare_spool", "in_vacuum_bag", "in_box"]
