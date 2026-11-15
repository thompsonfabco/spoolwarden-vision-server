"""One-click training update pipeline: ingest Drive feedback zips -> check batch size -> retrain -> benchmark."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..xml_io import DATA_DIR, PROJECT_ROOT, load_dataset_xml, parse_xml_file, write_xml_atomic
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
            if d.resolve() == DEFAULT_INCOMING_DIR.resolve():
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
        dirs = [DEFAULT_INCOMING_DIR]
    else:
        dirs = [Path(p) for p in source_dirs]

    fb_xml_path = Path(feedback_xml)
    before_count = 0
    last_trained_count = 0
    if fb_xml_path.is_file():
        pre_root = parse_xml_file(fb_xml_path)
        before_count = len(pre_root.findall("sample"))
        raw_lts = pre_root.attrib.get("last-trained-samples")
        if raw_lts is not None and str(raw_lts).strip().lstrip("-").isdigit():
            last_trained_count = max(0, int(str(raw_lts).strip()))
        else:
            last_trained_count = 0

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
                dup_idx = 1
                while True:
                    cand = arch_dir / f"{zp.stem}_dup{dup_idx}{zp.suffix}"
                    if not cand.exists():
                        dest = cand
                        break
                    dup_idx += 1
            shutil.move(str(zp), str(dest))
            archived_files.append(dest.name)

    after_count = before_count
    if fb_xml_path.is_file():
        post_root = parse_xml_file(fb_xml_path)
        after_count = len(post_root.findall("sample"))
        if zips or "last-trained-samples" not in post_root.attrib:
            post_root.attrib["last-trained-samples"] = str(last_trained_count)
            write_xml_atomic(fb_xml_path, post_root)

    new_samples = max(0, after_count - last_trained_count)

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
        if fb_xml_path.is_file():
            trained_root = parse_xml_file(fb_xml_path)
            trained_root.attrib["last-trained-samples"] = str(after_count)
            write_xml_atomic(fb_xml_path, trained_root)

    if not skip_benchmark:
        from ..evaluation.benchmark import run_benchmark

        summary["benchmark"] = run_benchmark(include_gpu=(tier in ("gpu", "all")))

    if release_version:
        import subprocess
        import sys

        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "build_standalone_bundle.py"),
            "--version",
            release_version,
            "--build-release",
        ]
        if key_file:
            cmd.extend(["--key-file", str(key_file)])
        subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)
        summary["release_built"] = release_version

    return summary
