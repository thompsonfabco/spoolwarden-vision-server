"""One-click training update pipeline: ingest Drive feedback zips -> check batch size -> retrain -> benchmark."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..xml_io import DATA_DIR, PROJECT_ROOT, load_dataset_xml
from .feedback import ingest_feedback_bundles

DEFAULT_INCOMING_DIR = DATA_DIR / "feedback_incoming"
DEFAULT_ARCHIVE_DIR = DATA_DIR / "feedback_archived"
DEFAULT_FEEDBACK_XML = DATA_DIR / "annotations" / "feedback_verified.xml"


def _collect_feedback_zips(source_dirs: list[Path]) -> list[Path]:
    zips: list[Path] = []
    seen: set[Path] = set()
    for d in source_dirs:
        if d.is_file() and d.suffix.lower() == ".zip":
            rp = d.resolve()
            if rp not in seen:
                seen.add(rp)
                zips.append(d)
        elif d.is_dir():
            for zp in sorted(d.glob("filament-feedback-*.zip")):
                rp = zp.resolve()
                if rp not in seen:
                    seen.add(rp)
                    zips.append(zp)
            if d == DEFAULT_INCOMING_DIR:
                for zp in sorted(d.glob("*.zip")):
                    rp = zp.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        zips.append(zp)
    return zips


def run_one_click_update(
    source_dirs: list[str | Path] | None = None,
    min_new_samples: int = 10,
    force: bool = False,
    tier: str = "all",
    skip_train: bool = False,
    skip_benchmark: bool = False,
    archive_dir: str | Path = DEFAULT_ARCHIVE_DIR,
    feedback_xml: str | Path = DEFAULT_FEEDBACK_XML,
    feedback_images_dir: str | Path = DATA_DIR / "feedback_verified",
    release_version: str | None = None,
    key_file: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest incoming Drive feedback bundles, check batch size, retrain models, and benchmark."""
    if source_dirs is None:
        dirs = [DEFAULT_INCOMING_DIR, Path.home() / "Downloads"]
    else:
        dirs = [Path(p) for p in source_dirs]

    fb_xml_path = Path(feedback_xml)
    before_count = len(load_dataset_xml(fb_xml_path)) if fb_xml_path.is_file() else 0

    zips = _collect_feedback_zips(dirs)
    ingested_entries = 0
    ingested_images = 0
    archived_files: list[str] = []
    arch_dir = Path(archive_dir)

    if zips:
        arch_dir.mkdir(parents=True, exist_ok=True)
        for zp in zips:
            res = ingest_feedback_bundles(
                zp,
                dest_xml=fb_xml_path,
                dest_images_dir=feedback_images_dir,
            )
            ingested_entries += res["imported_entries"]
            ingested_images += res["imported_images"]
            dest = arch_dir / zp.name
            if dest.exists():
                dest = arch_dir / f"{zp.stem}_dup{zp.suffix}"
            shutil.move(str(zp), str(dest))
            archived_files.append(dest.name)

    after_count = len(load_dataset_xml(fb_xml_path)) if fb_xml_path.is_file() else 0
    new_samples = after_count - before_count

    summary: dict[str, Any] = {
        "zips_found": len(zips),
        "archived_zips": archived_files,
        "ingested_entries": ingested_entries,
        "ingested_images": ingested_images,
        "total_feedback_samples": after_count,
        "new_feedback_samples": new_samples,
        "min_new_samples": min_new_samples,
        "trained": False,
    }

    if not force and new_samples < min_new_samples:
        summary["skipped_reason"] = (
            f"Only {new_samples} new feedback samples ingested (threshold={min_new_samples}). "
            "Pass --force to retrain anyway."
        )
        return summary

    if not skip_train:
        train_results: dict[str, Any] = {}
        if tier in ("cpu", "all"):
            from ..cpu_tier.train_detector import train_and_export_detector
            from ..cpu_tier.train_text_parser import train_and_export_text_parser
            from ..cpu_tier.train_visual import train_and_export_visual_classifier

            train_results["text_parser"] = train_and_export_text_parser()
            train_results["visual_classifier"] = train_and_export_visual_classifier()
            train_results["detector"] = train_and_export_detector()
        if tier in ("gpu", "all"):
            from ..gpu_tier.train_vlm import train_gpu_vlm_lora

            train_results["gpu_vlm"] = train_gpu_vlm_lora()
        summary["train_results"] = train_results
        summary["trained"] = True

    if not skip_benchmark:
        from ..evaluation.benchmark import run_benchmark

        summary["benchmark"] = run_benchmark(include_gpu=(tier in ("gpu", "all")))

    if release_version:
        import subprocess
        import sys

        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "build_release.py"),
            "--version",
            release_version,
        ]
        if key_file:
            cmd.extend(["--key-file", str(key_file)])
        subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)
        summary["release_built"] = release_version

    return summary
