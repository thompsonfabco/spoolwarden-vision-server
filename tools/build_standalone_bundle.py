"""Build a self-contained distribution bundle for filament-vision-server.

Stages source code, Linux/Windows installers, static configs, catalog XML,
CPU ONNX models, and optional GPU LoRA weights into dist/filament-vision-server-<version>/
alongside manifest.json and SHA256SUMS.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ROOT_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "THIRD-PARTY.md",
    ".gitignore",
    ".gitattributes",
    "install.bat",
    "start_server.bat",
    "stop_server.bat",
)

STATIC_CONFIG_FILES = (
    "cpu_pipeline.xml",
    "gpu_vlm_lora.xml",
    "taxonomy.xml",
    "base-model.json",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_tree_filtered(src_dir: Path, dst_dir: Path, skip_names: Iterable[str] = ("__pycache__",)) -> None:
    if not src_dir.is_dir():
        return
    skip_set = set(skip_names)
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in skip_set]
        rel = Path(root).relative_to(src_dir)
        out_root = dst_dir / rel
        out_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith(".pyc") or name in skip_set:
                continue
            src_file = Path(root) / name
            dst_file = out_root / name
            shutil.copy2(src_file, dst_file)
            if dst_file.suffix in (".sh", ".service"):
                raw = dst_file.read_bytes().replace(b"\r\n", b"\n")
                dst_file.write_bytes(raw)
                if dst_file.suffix == ".sh":
                    mode = dst_file.stat().st_mode
                    dst_file.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_bundle(
    version: str = "1.0.0",
    output_dir: str | Path | None = None,
    include_gpu: bool = True,
) -> Path:
    base_out = Path(output_dir) if output_dir else (PROJECT_ROOT / "dist")
    bundle_dir = base_out / f"filament-vision-server-{version}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for rel in ROOT_FILES:
        src = PROJECT_ROOT / rel
        if src.is_file():
            shutil.copy2(src, bundle_dir / rel)

    for rel_dir in ("src", "install", "tools"):
        _copy_tree_filtered(PROJECT_ROOT / rel_dir, bundle_dir / rel_dir)

    cfg_dst = bundle_dir / "configs"
    cfg_dst.mkdir(parents=True, exist_ok=True)
    for name in STATIC_CONFIG_FILES:
        src = PROJECT_ROOT / "configs" / name
        if src.is_file():
            shutil.copy2(src, cfg_dst / name)

    cat_src = PROJECT_ROOT / "data" / "catalog"
    if cat_src.is_dir():
        _copy_tree_filtered(cat_src, bundle_dir / "data" / "catalog")

    cpu_src = PROJECT_ROOT / "models" / "cpu"
    if not cpu_src.is_dir():
        raise FileNotFoundError(f"CPU models directory missing: {cpu_src}")
    _copy_tree_filtered(cpu_src, bundle_dir / "models" / "cpu")

    gpu_src = PROJECT_ROOT / "models" / "gpu"
    if include_gpu and gpu_src.is_dir():
        _copy_tree_filtered(gpu_src, bundle_dir / "models" / "gpu")

    staged_files: list[Path] = sorted(
        (p for p in bundle_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(bundle_dir).as_posix(),
    )

    cpu_bytes = 0
    cpu_count = 0
    gpu_bytes = 0
    gpu_count = 0
    total_bytes = 0
    sha_lines: list[str] = []

    for f in staged_files:
        rel_posix = f.relative_to(bundle_dir).as_posix()
        size = f.stat().st_size
        total_bytes += size
        if rel_posix.startswith("models/cpu/"):
            cpu_bytes += size
            cpu_count += 1
        elif rel_posix.startswith("models/gpu/"):
            gpu_bytes += size
            gpu_count += 1
        digest = _sha256_file(f)
        sha_lines.append(f"{digest}  {rel_posix}")

    sha_path = bundle_dir / "SHA256SUMS"
    sha_path.write_bytes(("\n".join(sha_lines) + "\n").encode("utf-8"))

    manifest = {
        "service": "filament-vision-server",
        "version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "include_gpu": bool(include_gpu and gpu_count > 0),
        "file_count": len(staged_files),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "tiers": {
            "cpu": {"files": cpu_count, "bytes": cpu_bytes, "mb": round(cpu_bytes / (1024 * 1024), 2)},
            "gpu": {"files": gpu_count, "bytes": gpu_bytes, "mb": round(gpu_bytes / (1024 * 1024), 2)},
        },
        "checksum_file": "SHA256SUMS",
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return bundle_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build standalone filament-vision-server bundle")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-gpu", action="store_true", help="Exclude GPU LoRA weights from bundle")
    args = parser.parse_args(argv)

    bundle_dir = build_bundle(
        version=args.version,
        output_dir=args.output_dir,
        include_gpu=not args.no_gpu,
    )
    print(f"Built standalone bundle: {bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
