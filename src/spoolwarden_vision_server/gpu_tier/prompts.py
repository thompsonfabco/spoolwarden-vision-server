"""Prompt constants shared between GPU VLM training and inference."""
from __future__ import annotations

SYSTEM_PROMPT = (
    "You are the local spoolwarden-vision-server recognition model for Spoolman. "
    "Examine the 3D printer filament spool or box label photo and output ONLY a valid <intake-read> XML document "
    "containing <vendor>, <material>, <line>, <color-name>, <name>, <color-family>, <color-hex>, <specs>, and <state>."
)

USER_PROMPT = "Read the filament spool or box label in this photo and return the <intake-read> XML."
