#!/usr/bin/env python3
"""Release manifest signing tool for spoolwarden-vision-server (Ed25519 RFC 8032 over SHA256SUMS).

Subcommands:
  keygen --out PATH      Generate an unencrypted PKCS#8 Ed25519 private key outside the repo
  sign SUMS              Sign SHA256SUMS using FVS_RELEASE_KEY / FKV_RELEASE_KEY or --key-file
  pubkey --key-file F    Print the 44-char base64 raw public key for a private key PEM
  verify SUMS --sig S    Verify a SHA256SUMS.sig signature against PUBKEY
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Owner-approved Ed25519 public key (32 bytes raw, 44 chars base64).
PUBKEY = "adkFwUZin7sdhZJ1fdr9sOn8BtFIFuiFHrm2d5O0TPU="

_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class SignError(Exception):
    pass


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise SignError("needs the cryptography package: pip install cryptography") from None
    return serialization, ed25519


def _public_b64(priv) -> str:
    serialization, _ = _crypto()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _load_private(pem: bytes):
    serialization, ed25519 = _crypto()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise SignError("release key is not an unencrypted PEM private key") from None
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise SignError("release key is not an Ed25519 key")
    return key


def _key_pem(key_file: str | Path | None) -> bytes:
    if key_file:
        return Path(key_file).read_bytes()
    for env_name in ("FVS_RELEASE_KEY", "FKV_RELEASE_KEY"):
        env = os.environ.get(env_name, "")
        if env.strip():
            return env.encode("ascii")
    raise SignError("no release key: set FVS_RELEASE_KEY / FKV_RELEASE_KEY or pass --key-file")


def generate_sha256sums(release_dir: Path) -> Path:
    """Write SHA256SUMS for all release assets in release_dir."""
    lines: list[str] = []
    for p in sorted(release_dir.iterdir()):
        if not p.is_file() or p.name in ("SHA256SUMS", "SHA256SUMS.sig"):
            continue
        if not _ASSET_NAME_RE.match(p.name):
            raise SignError(f"invalid asset filename for SHA256SUMS: {p.name}")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{digest}  {p.name}")
    sums_path = release_dir / "SHA256SUMS"
    sums_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return sums_path


def sign(sums: Path, pem: bytes, expect_pub: str = PUBKEY) -> Path:
    priv = _load_private(pem)
    if not expect_pub:
        raise SignError("PUBKEY is not set; obtain owner-approved public key before signing a release")
    if _public_b64(priv) != expect_pub:
        raise SignError("private key does not match configured PUBKEY; refusing to sign")
    data = sums.read_bytes()
    sig = priv.sign(data)
    priv.public_key().verify(sig, data)
    out = sums.with_name(sums.name + ".sig")
    out.write_text(base64.b64encode(sig).decode("ascii") + "\n", encoding="ascii")
    return out


def verify(sums: Path, sig_path: Path, expect_pub: str = PUBKEY) -> None:
    _, ed25519 = _crypto()
    try:
        pub_raw = base64.b64decode(expect_pub.strip(), validate=True)
        pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_raw)
        sig_raw = base64.b64decode(sig_path.read_text(encoding="ascii").strip(), validate=True)
        pub.verify(sig_raw, sums.read_bytes())
    except Exception as exc:
        raise SignError(f"signature verification failed: {exc}") from None


def keygen(out: Path) -> str:
    serialization, ed25519 = _crypto()
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return _public_b64(priv)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sign_release.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen")
    kg.add_argument("--out", required=True, help="where to write the private key (outside the repo)")
    sg = sub.add_parser("sign")
    sg.add_argument("sums")
    sg.add_argument("--key-file")
    sg.add_argument("--pubkey", default=PUBKEY, help="override expected base64 public key")
    pk = sub.add_parser("pubkey")
    pk.add_argument("--key-file", required=True)
    vf = sub.add_parser("verify")
    vf.add_argument("sums")
    vf.add_argument("--sig", required=True)
    vf.add_argument("--pubkey", default=PUBKEY)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            out = Path(args.out).resolve()
            if out.is_relative_to(ROOT.resolve()):
                raise SignError("write the private key outside the repository")
            print(keygen(out))
        elif args.cmd == "sign":
            out = sign(Path(args.sums), _key_pem(args.key_file), args.pubkey)
            print(f"signed: {out}")
        elif args.cmd == "verify":
            verify(Path(args.sums), Path(args.sig), args.pubkey)
            print(f"verified OK: {args.sums}")
        else:
            print(_public_b64(_load_private(Path(args.key_file).read_bytes())))
    except SignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
