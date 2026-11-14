"""Build self-contained distribution bundles and signed GitHub release archives for filament-vision-server.

Stages source code, Linux/Windows installers, static configs, catalog XML,
CPU ONNX models, and optional GPU LoRA weights into dist/ alongside
manifest.json, SHA256SUMS, and optional Ed25519 SHA256SUMS.sig.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from sign_release import generate_sha256sums, sign, verify  # noqa: E402

DEFAULT_VERSION = "0.2.0"

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
        dirs[:] = [d for d in dirs if d not in skip_set and not d.endswith(".egg-info")]
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
    version: str = DEFAULT_VERSION,
    output_dir: str | Path | None = None,
    include_gpu: bool = True,
    key_file: str | Path | None = None,
    bundle_name: str | None = None,
) -> Path:
    base_out = Path(output_dir) if output_dir else (PROJECT_ROOT / "dist")
    if bundle_name is None:
        bundle_name = f"filament-vision-server-{version}" if include_gpu else f"filament-vision-server-{version}-cpu"
    bundle_dir = base_out / bundle_name
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

    signed = False
    if key_file is not None:
        sig_path = sign(sha_path, Path(key_file).read_bytes())
        verify(sha_path, sig_path)
        signed = True

    manifest = {
        "service": "filament-vision-server",
        "version": version,
        "bundle": bundle_name,
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
        "signature_file": "SHA256SUMS.sig" if signed else None,
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return bundle_dir


def _pack_zip_and_tar_gz(bundle_dir: Path, release_dir: Path) -> tuple[Path, Path]:
    top_name = bundle_dir.name
    zip_path = release_dir / f"{top_name}.zip"
    tar_path = release_dir / f"{top_name}.tar.gz"
    files = sorted((p for p in bundle_dir.rglob("*") if p.is_file()), key=lambda p: p.relative_to(bundle_dir).as_posix())

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            rel_posix = f.relative_to(bundle_dir).as_posix()
            arcname = f"{top_name}/{rel_posix}"
            zi = zipfile.ZipInfo(arcname)
            zi.compress_type = zipfile.ZIP_DEFLATED
            perm = 0o755 if f.suffix == ".sh" else 0o644
            zi.external_attr = (stat.S_IFREG | perm) << 16
            zf.writestr(zi, f.read_bytes())

    with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for f in files:
            rel_posix = f.relative_to(bundle_dir).as_posix()
            arcname = f"{top_name}/{rel_posix}"
            data = f.read_bytes()
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            ti.mode = 0o755 if f.suffix == ".sh" else 0o644
            ti.uname = "root"
            ti.gname = "root"
            tf.addfile(ti, io.BytesIO(data))

    return zip_path, tar_path


def build_release_archives(
    version: str = DEFAULT_VERSION,
    output_dir: str | Path | None = None,
    key_file: str | Path | None = None,
) -> Path:
    release_dir = (Path(output_dir) if output_dir else (PROJECT_ROOT / "release")) / f"v{version}"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)

    full_dir = build_bundle(
        version=version,
        include_gpu=True,
        key_file=key_file,
        bundle_name=f"filament-vision-server-{version}",
    )
    cpu_dir = build_bundle(
        version=version,
        include_gpu=False,
        key_file=key_file,
        bundle_name=f"filament-vision-server-{version}-cpu",
    )

    full_zip, full_tar = _pack_zip_and_tar_gz(full_dir, release_dir)
    cpu_zip, cpu_tar = _pack_zip_and_tar_gz(cpu_dir, release_dir)

    assets_info = {}
    for p in (full_zip, full_tar, cpu_zip, cpu_tar):
        assets_info[p.name] = {
            "bytes": p.stat().st_size,
            "mb": round(p.stat().st_size / (1024 * 1024), 2),
        }

    manifest = {
        "service": "filament-vision-server",
        "version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "signed": key_file is not None,
        "bundles": {
            "full": [full_zip.name, full_tar.name],
            "cpu": [cpu_zip.name, cpu_tar.name],
        },
        "assets": assets_info,
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    sums_path = generate_sha256sums(release_dir)
    if key_file is not None:
        sig_path = sign(sums_path, Path(key_file).read_bytes())
        verify(sums_path, sig_path)

    return release_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build standalone filament-vision-server bundles and releases")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-gpu", action="store_true", help="Exclude GPU LoRA weights from single bundle")
    parser.add_argument("--build-release", action="store_true", help="Build both full and CPU-only zip/tar.gz archives under release/v<version>/")
    parser.add_argument("--key-file", default=None, help="Path to Ed25519 private key PEM for signing SHA256SUMS")
    args = parser.parse_args(argv)

    if args.build_release:
        rel_dir = build_release_archives(
            version=args.version,
            output_dir=args.output_dir,
            key_file=args.key_file,
        )
        print(f"Built signed release archives: {rel_dir}")
        return 0

    bundle_dir = build_bundle(
        version=args.version,
        output_dir=args.output_dir,
        include_gpu=not args.no_gpu,
        key_file=args.key_file,
    )
    print(f"Built standalone bundle: {bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
