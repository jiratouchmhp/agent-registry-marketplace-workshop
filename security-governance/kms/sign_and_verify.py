#!/usr/bin/env python3
"""Cryptographic Skill Artifact Signing & Verification (Local ECDSA P-256 + Cloud KMS).

Agent Registry's native CMEK (`SkillEncryptionSpec`) provides encryption at rest
ONLY — it does NOT cryptographically sign skill artifacts. This module implements
the custom CI/CD signing + Middleware verification layer using ECDSA P-256 with
SHA-256 (`EC_SIGN_P256_SHA256`), supporting BOTH:

  1. Local ECDSA P-256 PEM keys (workshop / offline demos and unit tests).
  2. Live Google Cloud KMS Asymmetric Signing keys via REST API + ADC
     (`:asymmetricSign` for CI publishers with `roles/cloudkms.signerVerifier`,
      `/publicKey` for middleware with `roles/cloudkms.publicKeyViewer`).

Integrity & provenance checks enforced during verification:
  - Computes downloaded ZIP SHA-256 and verifies constant-time equality with
    `SkillRevision.sha256Hash` (when supplied via `--expected-sha256` or
    `--revision-json`).
  - Verifies the ECDSA P-256 SHA-256 signature (embedded in `SKILL.md`
    `frontmatter.metadata.kms_signature` using the canonical skill payload
    identical to `middleware/app/security_verifier.py`, OR via a detached
    attestation JSON over the ZIP SHA-256 digest).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

CLOUD_KMS_REST_BASE = "https://cloudkms.googleapis.com/v1"
EXPECTED_KMS_ALGORITHM = "EC_SIGN_P256_SHA256"
SIGNATURE_METADATA_KEYS = frozenset({"kms_signature", "kms_key_version"})
DEFAULT_KEY_VERSION_LABEL = (
    "projects/acme-agent-governance/locations/asia-southeast3/"
    "keyRings/agent-registry-skill-signing/"
    "cryptoKeys/skill-artifact-ecdsa-p256/cryptoKeyVersions/1"
)
_KMS_PROJECT_RE = re.compile(r"^/?projects/([^/]+)/")


def _extract_project_from_kms_key_version(kms_key_version: str) -> str | None:
    match = _KMS_PROJECT_RE.match(kms_key_version.strip())
    return match.group(1) if match else None


@dataclass
class VerificationReport:
    """Structured outcome of Skill ZIP SHA-256 and ECDSA P-256 verification."""

    verified: bool
    zip_sha256: str
    expected_sha256: str = ""
    sha256_matched: bool = True
    signature_verified: bool = False
    kms_key_version: str = ""
    verification_mode: str = "embedded"
    error: str | None = None


def normalize_sha256_hex(raw: Any) -> str:
    """Normalize a hex SHA-256 digest (strip whitespace and optional 'sha256:' prefix)."""
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :].strip()
    return cleaned


def compute_zip_sha256(zip_bytes: bytes) -> str:
    """Return lowercase 64-char hex SHA-256 digest matching SkillRevision.sha256Hash."""
    return hashlib.sha256(zip_bytes).hexdigest()


def parse_skill_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and markdown body from a SKILL.md document."""
    normalized = raw_text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter delimiter '---'.")
    closing_idx = normalized.find("\n---", 4)
    if closing_idx == -1:
        raise ValueError("SKILL.md is missing closing YAML frontmatter delimiter '---'.")
    fm_text = normalized[4:closing_idx]
    body_start = closing_idx + len("\n---")
    body = normalized[body_start:]
    if body.startswith("\n"):
        body = body[1:]
    parsed = yaml.safe_load(fm_text)
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md YAML frontmatter must deserialize to a mapping.")
    return parsed, body


def _canonicalize_skill_md(raw_text: str) -> bytes:
    """Canonicalize SKILL.md stripping kms_signature/kms_key_version for non-circular signing.

    Byte-for-byte compatible with `middleware/app/security_verifier.py`.
    """
    frontmatter, body = parse_skill_frontmatter(raw_text)
    cleaned_fm = dict(frontmatter)
    metadata = cleaned_fm.get("metadata")
    if isinstance(metadata, dict):
        cleaned_meta = {
            k: v for k, v in metadata.items() if k not in SIGNATURE_METADATA_KEYS
        }
        if cleaned_meta:
            cleaned_fm["metadata"] = cleaned_meta
        else:
            cleaned_fm.pop("metadata", None)

    canonical_fm = json.dumps(cleaned_fm, sort_keys=True, separators=(",", ":"))
    normalized_body = body.replace("\r\n", "\n").strip()
    return f"{canonical_fm}\n---\n{normalized_body}\n".encode("utf-8")


def compute_canonical_skill_payload(zip_bytes: bytes) -> bytes:
    """Build deterministic canonical payload covering every file inside a Skill ZIP.

    Byte-for-byte compatible with `middleware/app/security_verifier.py`.
    """
    chunks: list[bytes] = [b"agent-registry-skill-v1\x00"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        entries = sorted(
            (info for info in zf.infolist() if not info.is_dir()),
            key=lambda i: i.filename,
        )
        skill_paths = [
            i.filename for i in entries if PurePosixPath(i.filename).name == "SKILL.md"
        ]
        if len(skill_paths) != 1:
            raise ValueError(
                f"Expected strictly 1 SKILL.md in skill archive, found {len(skill_paths)}."
            )
        top_prefix = ""
        parts = PurePosixPath(skill_paths[0]).parts
        if len(parts) == 2:
            top_prefix = f"{parts[0]}/"

        for info in entries:
            rel_name = info.filename
            if top_prefix and rel_name.startswith(top_prefix):
                rel_name = rel_name[len(top_prefix) :]
            raw = zf.read(info)
            if PurePosixPath(rel_name).name == "SKILL.md":
                payload = _canonicalize_skill_md(raw.decode("utf-8"))
            else:
                payload = raw
            chunks.append(rel_name.encode("utf-8"))
            chunks.append(b"\x00")
            chunks.append(str(len(payload)).encode("ascii"))
            chunks.append(b"\x00")
            chunks.append(payload)
            chunks.append(b"\x00")
    return b"".join(chunks)


def compute_canonical_skill_digest(zip_bytes: bytes) -> bytes:
    """Compute the 32-byte SHA-256 digest of the canonical skill payload."""
    return hashlib.sha256(compute_canonical_skill_payload(zip_bytes)).digest()


def extract_skill_frontmatter_from_zip(zip_bytes: bytes) -> tuple[str, dict[str, Any], str]:
    """Extract and parse the single SKILL.md inside a Skill ZIP archive."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        matches = [
            info.filename
            for info in zf.infolist()
            if not info.is_dir() and PurePosixPath(info.filename).name == "SKILL.md"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected strictly 1 SKILL.md in skill archive, found {len(matches)}."
            )
        raw_text = zf.read(matches[0]).decode("utf-8")
    fm, body = parse_skill_frontmatter(raw_text)
    return matches[0], fm, body


# -----------------------------------------------------------------------------
# Local ECDSA P-256 Key Management & Signing
# -----------------------------------------------------------------------------


def generate_ecdsa_p256_keypair() -> tuple[bytes, bytes]:
    """Generate a P-256 (SECP256R1) ECDSA keypair matching Cloud KMS EC_SIGN_P256_SHA256."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def write_keypair(private_out: Path, public_out: Path) -> tuple[Path, Path]:
    """Generate and persist a local ECDSA P-256 keypair (private key mode 0600)."""
    private_pem, public_pem = generate_ecdsa_p256_keypair()
    private_out.parent.mkdir(parents=True, exist_ok=True)
    public_out.parent.mkdir(parents=True, exist_ok=True)
    private_out.write_bytes(private_pem)
    os.chmod(private_out, 0o600)
    public_out.write_bytes(public_pem)
    return private_out, public_out


def load_p256_private_key(private_key_pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ValueError("Private key must be an ECDSA P-256 (SECP256R1) key.")
    return key


def load_p256_public_key(public_key_pem: bytes) -> ec.EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ValueError("Public key must be an ECDSA P-256 (SECP256R1) key.")
    return key


def sign_digest_local(digest_bytes: bytes, private_key_pem: bytes) -> str:
    """Sign a 32-byte SHA-256 digest with a local ECDSA P-256 PEM private key."""
    if len(digest_bytes) != 32:
        raise ValueError(f"Expected 32-byte SHA-256 digest, got {len(digest_bytes)} bytes.")
    private_key = load_p256_private_key(private_key_pem)
    der_sig = private_key.sign(digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    return base64.b64encode(der_sig).decode("ascii")


def verify_digest_local(
    digest_bytes: bytes, signature_b64: str, public_key_pem: bytes
) -> bool:
    """Verify a base64 DER ECDSA P-256 signature against a 32-byte SHA-256 digest."""
    if len(digest_bytes) != 32 or not signature_b64 or not signature_b64.strip():
        return False
    try:
        sig_bytes = base64.b64decode(signature_b64.strip(), validate=True)
        public_key = load_p256_public_key(public_key_pem)
        public_key.verify(sig_bytes, digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except (InvalidSignature, ValueError):
        return False


# -----------------------------------------------------------------------------
# Google Cloud KMS Asymmetric REST API + ADC Integration
# -----------------------------------------------------------------------------


def get_adc_access_token() -> str:
    """Acquire an OAuth 2.0 Bearer token via env var, ADC/gcloud CLI, or GCE metadata server."""
    env_token = (
        os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
        or os.environ.get("AGENT_REGISTRY_AUTH_TOKEN", "").strip()
    )
    if env_token:
        return env_token

    gcloud_bin = shutil.which("gcloud") or str(Path.home() / ".local" / "bin" / "gcloud")
    for cmd in (
        [gcloud_bin, "auth", "application-default", "print-access-token", "--quiet"],
        [gcloud_bin, "auth", "print-access-token", "--quiet"],
    ):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            token = proc.stdout.strip()
            if token:
                return token
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue

    # Cloud Run / GCE metadata server fallback.
    meta_req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(meta_req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token = str(data.get("access_token", "")).strip()
            if token:
                return token
    except (urllib.error.URLError, TimeoutError, ValueError):
        pass

    raise RuntimeError(
        "Unable to obtain GCP OAuth access token. Set GOOGLE_OAUTH_ACCESS_TOKEN, "
        "run `gcloud auth application-default login`, or run inside a GCP workload."
    )


def sign_digest_cloud_kms(
    digest_bytes: bytes,
    kms_key_version: str,
    *,
    access_token: str | None = None,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
) -> str:
    """Sign a 32-byte SHA-256 digest via Cloud KMS `asymmetricSign` REST API."""
    if len(digest_bytes) != 32:
        raise ValueError(f"Expected 32-byte SHA-256 digest, got {len(digest_bytes)} bytes.")
    key_version = kms_key_version.strip().lstrip("/")
    if "/cryptoKeyVersions/" not in key_version:
        raise ValueError(
            "kms_key_version must be a full CryptoKeyVersion resource name: "
            "projects/{p}/locations/{l}/keyRings/{r}/cryptoKeys/{k}/cryptoKeyVersions/{v}"
        )

    token = access_token or get_adc_access_token()
    url = f"{kms_endpoint.rstrip('/')}/{key_version}:asymmetricSign"
    body = json.dumps(
        {"digest": {"sha256": base64.b64encode(digest_bytes).decode("ascii")}}
    ).encode("utf-8")

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    quota_project = _extract_project_from_kms_key_version(key_version)
    if quota_project:
        headers["x-goog-user-project"] = quota_project

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    signature = str(payload.get("signature", "")).strip()
    if not signature:
        raise RuntimeError(f"Cloud KMS asymmetricSign response missing 'signature': {payload}")
    return signature


def fetch_public_key_cloud_kms(
    kms_key_version: str,
    *,
    access_token: str | None = None,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
) -> bytes:
    """Fetch and validate an EC_SIGN_P256_SHA256 PEM public key from Cloud KMS REST API."""
    key_version = kms_key_version.strip().lstrip("/")
    if "/cryptoKeyVersions/" not in key_version:
        raise ValueError(
            "kms_key_version must be a full CryptoKeyVersion resource name: "
            "projects/{p}/locations/{l}/keyRings/{r}/cryptoKeys/{k}/cryptoKeyVersions/{v}"
        )

    token = access_token or get_adc_access_token()
    url = f"{kms_endpoint.rstrip('/')}/{key_version}/publicKey"
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    quota_project = _extract_project_from_kms_key_version(key_version)
    if quota_project:
        headers["x-goog-user-project"] = quota_project
    req = urllib.request.Request(
        url,
        method="GET",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    algorithm = str(payload.get("algorithm", ""))
    if algorithm and algorithm != EXPECTED_KMS_ALGORITHM:
        raise ValueError(
            f"Unsupported Cloud KMS key algorithm '{algorithm}'; expected '{EXPECTED_KMS_ALGORITHM}'."
        )
    pem_text = str(payload.get("pem", "")).strip()
    if not pem_text:
        raise RuntimeError(f"Cloud KMS publicKey response missing 'pem': {payload}")
    pem_bytes = (pem_text + "\n").encode("utf-8")
    load_p256_public_key(pem_bytes)
    return pem_bytes


# -----------------------------------------------------------------------------
# High-Level Skill Archive Signing & Verification
# -----------------------------------------------------------------------------


def sign_skill_zip(
    zip_bytes: bytes,
    *,
    private_key_pem: bytes | None = None,
    kms_key_version: str = DEFAULT_KEY_VERSION_LABEL,
    use_cloud_kms: bool = False,
    access_token: str | None = None,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
) -> tuple[bytes, str, str]:
    """Sign a Skill ZIP canonically and embed `kms_signature` + `kms_key_version` in SKILL.md.

    Returns `(signed_zip_bytes, signature_b64, signed_zip_sha256_hex)`.
    The resulting signed ZIP is 100% verifiable by `middleware/app/security_verifier.py`.
    """
    digest_bytes = compute_canonical_skill_digest(zip_bytes)
    if use_cloud_kms:
        sig_b64 = sign_digest_cloud_kms(
            digest_bytes,
            kms_key_version,
            access_token=access_token,
            kms_endpoint=kms_endpoint,
        )
    else:
        if not private_key_pem:
            raise ValueError("private_key_pem is required when use_cloud_kms is False.")
        sig_b64 = sign_digest_local(digest_bytes, private_key_pem)

    out_buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as src_zf,
        zipfile.ZipFile(out_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as dst_zf,
    ):
        for info in sorted(src_zf.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            raw = src_zf.read(info)
            if PurePosixPath(info.filename).name == "SKILL.md":
                fm, body = parse_skill_frontmatter(raw.decode("utf-8"))
                meta = dict(fm.get("metadata") or {})
                meta["kms_signature"] = sig_b64
                meta["kms_key_version"] = kms_key_version
                fm["metadata"] = meta
                yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                raw = f"---\n{yaml_block}\n---\n{body.lstrip(chr(10))}".encode("utf-8")

            new_info = zipfile.ZipInfo(filename=info.filename, date_time=(2025, 1, 1, 0, 0, 0))
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr or (0o100644 << 16)
            dst_zf.writestr(new_info, raw)

    signed_bytes = out_buf.getvalue()
    return signed_bytes, sig_b64, compute_zip_sha256(signed_bytes)


def sign_detached_archive(
    zip_bytes: bytes,
    *,
    private_key_pem: bytes | None = None,
    kms_key_version: str = DEFAULT_KEY_VERSION_LABEL,
    use_cloud_kms: bool = False,
    access_token: str | None = None,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
) -> dict[str, str]:
    """Create a detached JSON signature attestation over the raw ZIP SHA-256 digest."""
    digest_bytes = hashlib.sha256(zip_bytes).digest()
    if use_cloud_kms:
        sig_b64 = sign_digest_cloud_kms(
            digest_bytes,
            kms_key_version,
            access_token=access_token,
            kms_endpoint=kms_endpoint,
        )
    else:
        if not private_key_pem:
            raise ValueError("private_key_pem is required when use_cloud_kms is False.")
        sig_b64 = sign_digest_local(digest_bytes, private_key_pem)

    return {
        "schema_version": "1.0",
        "algorithm": EXPECTED_KMS_ALGORITHM,
        "sha256": digest_bytes.hex(),
        "kms_key_version": kms_key_version,
        "signature": sig_b64,
    }


def verify_skill_zip(
    zip_bytes: bytes,
    *,
    public_key_pem: bytes | None = None,
    kms_key_version: str | None = None,
    use_cloud_kms: bool = False,
    access_token: str | None = None,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
    expected_sha256: str | None = None,
    detached_signature_b64: str | None = None,
) -> VerificationReport:
    """Verify Skill ZIP SHA-256 (`SkillRevision.sha256Hash`) AND ECDSA P-256 signature."""
    actual_sha256 = compute_zip_sha256(zip_bytes)
    normalized_expected = normalize_sha256_hex(expected_sha256)
    mode = "detached" if detached_signature_b64 else "embedded"

    # 1. Verify raw ZIP SHA-256 against SkillRevision.sha256Hash if supplied.
    if normalized_expected and not hmac.compare_digest(actual_sha256, normalized_expected):
        return VerificationReport(
            verified=False,
            zip_sha256=actual_sha256,
            expected_sha256=normalized_expected,
            sha256_matched=False,
            signature_verified=False,
            verification_mode=mode,
            error=(
                f"ZIP SHA-256 '{actual_sha256}' does not match expected "
                f"SkillRevision.sha256Hash '{normalized_expected}'."
            ),
        )

    # 2. Extract embedded signature metadata when in embedded mode.
    signature_b64 = (detached_signature_b64 or "").strip()
    effective_key_version = (kms_key_version or "").strip()

    if not signature_b64:
        try:
            _path, frontmatter, _body = extract_skill_frontmatter_from_zip(zip_bytes)
        except Exception as exc:
            return VerificationReport(
                verified=False,
                zip_sha256=actual_sha256,
                expected_sha256=normalized_expected,
                sha256_matched=True,
                signature_verified=False,
                verification_mode=mode,
                error=f"Failed to read SKILL.md from archive: {exc}",
            )
        metadata = (
            frontmatter.get("metadata")
            if isinstance(frontmatter.get("metadata"), dict)
            else {}
        )
        signature_b64 = str(metadata.get("kms_signature", "")).strip()
        if not effective_key_version:
            effective_key_version = str(metadata.get("kms_key_version", "")).strip()

        if not signature_b64:
            return VerificationReport(
                verified=False,
                zip_sha256=actual_sha256,
                expected_sha256=normalized_expected,
                sha256_matched=True,
                signature_verified=False,
                kms_key_version=effective_key_version,
                verification_mode=mode,
                error="Missing 'kms_signature' in SKILL.md frontmatter.metadata.",
            )

    # 3. Resolve public key PEM (local PEM or Cloud KMS REST /publicKey).
    resolved_pub_pem = public_key_pem
    if resolved_pub_pem is None:
        if use_cloud_kms and effective_key_version:
            try:
                resolved_pub_pem = fetch_public_key_cloud_kms(
                    effective_key_version,
                    access_token=access_token,
                    kms_endpoint=kms_endpoint,
                )
            except Exception as exc:
                return VerificationReport(
                    verified=False,
                    zip_sha256=actual_sha256,
                    expected_sha256=normalized_expected,
                    sha256_matched=True,
                    signature_verified=False,
                    kms_key_version=effective_key_version,
                    verification_mode=mode,
                    error=f"Failed to fetch Cloud KMS public key: {exc}",
                )
        else:
            return VerificationReport(
                verified=False,
                zip_sha256=actual_sha256,
                expected_sha256=normalized_expected,
                sha256_matched=True,
                signature_verified=False,
                kms_key_version=effective_key_version,
                verification_mode=mode,
                error="Provide public_key_pem or set use_cloud_kms=True with kms_key_version.",
            )

    # 4. Verify ECDSA P-256 SHA-256 signature.
    if mode == "detached":
        digest = hashlib.sha256(zip_bytes).digest()
        sig_ok = verify_digest_local(digest, signature_b64, resolved_pub_pem)
    else:
        try:
            canonical_digest = compute_canonical_skill_digest(zip_bytes)
        except Exception as exc:
            return VerificationReport(
                verified=False,
                zip_sha256=actual_sha256,
                expected_sha256=normalized_expected,
                sha256_matched=True,
                signature_verified=False,
                kms_key_version=effective_key_version,
                verification_mode=mode,
                error=f"Failed to compute canonical skill digest: {exc}",
            )
        sig_ok = verify_digest_local(canonical_digest, signature_b64, resolved_pub_pem)

    if not sig_ok:
        return VerificationReport(
            verified=False,
            zip_sha256=actual_sha256,
            expected_sha256=normalized_expected,
            sha256_matched=True,
            signature_verified=False,
            kms_key_version=effective_key_version,
            verification_mode=mode,
            error=(
                "ECDSA P-256 signature verification FAILED — archive may be tampered with "
                "or signed by an untrusted key."
            ),
        )

    return VerificationReport(
        verified=True,
        zip_sha256=actual_sha256,
        expected_sha256=normalized_expected,
        sha256_matched=True,
        signature_verified=True,
        kms_key_version=effective_key_version,
        verification_mode=mode,
        error=None,
    )


# -----------------------------------------------------------------------------
# Command-Line Interface
# -----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sign and verify Agent Registry Skill ZIP archives using local ECDSA P-256 "
            "PEM keys or Google Cloud KMS Asymmetric Signing keys (EC_SIGN_P256_SHA256)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_keygen = sub.add_parser("keygen", help="Generate a local ECDSA P-256 workshop keypair.")
    p_keygen.add_argument("--private-out", required=True, type=Path, help="Output private PEM path.")
    p_keygen.add_argument("--public-out", required=True, type=Path, help="Output public PEM path.")

    p_sign = sub.add_parser("sign", help="Sign a Skill ZIP archive.")
    p_sign.add_argument("--input", required=True, type=Path, help="Input unsigned Skill ZIP.")
    p_sign.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output signed Skill ZIP (required for embedded signing).",
    )
    p_sign.add_argument(
        "--private-key",
        type=Path,
        default=None,
        help="Local ECDSA P-256 private key PEM path (offline/workshop mode).",
    )
    p_sign.add_argument(
        "--kms-key-version",
        default="",
        help="Full Cloud KMS CryptoKeyVersion resource name (enables live Cloud KMS signing if --private-key is omitted).",
    )
    p_sign.add_argument(
        "--attestation-out",
        type=Path,
        default=None,
        help="Optional path to write a detached JSON signature attestation over the final ZIP SHA-256.",
    )

    p_verify = sub.add_parser("verify", help="Verify a Skill ZIP SHA-256 and ECDSA P-256 signature.")
    p_verify.add_argument("--input", required=True, type=Path, help="Skill ZIP to verify.")
    p_verify.add_argument(
        "--public-key",
        type=Path,
        default=None,
        help="Trusted local ECDSA P-256 public key PEM path.",
    )
    p_verify.add_argument(
        "--kms-key-version",
        default="",
        help="Cloud KMS CryptoKeyVersion resource name to fetch the trusted public key via REST/ADC.",
    )
    p_verify.add_argument(
        "--expected-sha256",
        default="",
        help="Expected SkillRevision.sha256Hash hex digest to enforce against the ZIP bytes.",
    )
    p_verify.add_argument(
        "--revision-json",
        type=Path,
        default=None,
        help="Optional Agent Registry SkillRevision JSON file containing 'sha256Hash'.",
    )
    p_verify.add_argument(
        "--attestation",
        type=Path,
        default=None,
        help="Optional detached JSON signature attestation file to verify instead of embedded SKILL.md metadata.",
    )

    p_pub = sub.add_parser(
        "fetch-public-key",
        help="Download an EC_SIGN_P256_SHA256 public key PEM from Cloud KMS via REST/ADC.",
    )
    p_pub.add_argument("--kms-key-version", required=True, help="Full Cloud KMS CryptoKeyVersion name.")
    p_pub.add_argument("--output", required=True, type=Path, help="Destination PEM file path.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "keygen":
        priv, pub = write_keypair(args.private_out, args.public_out)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "algorithm": EXPECTED_KMS_ALGORITHM,
                    "private_key": str(priv),
                    "public_key": str(pub),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "fetch-public-key":
        pem_bytes = fetch_public_key_cloud_kms(args.kms_key_version)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(pem_bytes)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "kms_key_version": args.kms_key_version,
                    "public_key": str(args.output),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "sign":
        zip_bytes = args.input.read_bytes()
        use_cloud_kms = args.private_key is None and bool(args.kms_key_version.strip())
        private_pem = args.private_key.read_bytes() if args.private_key else None
        key_version = args.kms_key_version.strip() or DEFAULT_KEY_VERSION_LABEL

        if args.output is not None:
            signed_bytes, sig_b64, zip_sha256 = sign_skill_zip(
                zip_bytes,
                private_key_pem=private_pem,
                kms_key_version=key_version,
                use_cloud_kms=use_cloud_kms,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(signed_bytes)
            result: dict[str, Any] = {
                "status": "signed",
                "mode": "cloud_kms" if use_cloud_kms else "local_pem",
                "output": str(args.output),
                "sha256": zip_sha256,
                "kms_key_version": key_version,
                "kms_signature": sig_b64,
            }
            if args.attestation_out is not None:
                att = sign_detached_archive(
                    signed_bytes,
                    private_key_pem=private_pem,
                    kms_key_version=key_version,
                    use_cloud_kms=use_cloud_kms,
                )
                args.attestation_out.parent.mkdir(parents=True, exist_ok=True)
                args.attestation_out.write_text(json.dumps(att, indent=2) + "\n", encoding="utf-8")
                result["attestation"] = str(args.attestation_out)
            print(json.dumps(result, indent=2))
            return 0

        if args.attestation_out is not None:
            att = sign_detached_archive(
                zip_bytes,
                private_key_pem=private_pem,
                kms_key_version=key_version,
                use_cloud_kms=use_cloud_kms,
            )
            args.attestation_out.parent.mkdir(parents=True, exist_ok=True)
            args.attestation_out.write_text(json.dumps(att, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(att, indent=2))
            return 0

        parser.error("Provide --output (for embedded SKILL.md signing) and/or --attestation-out.")

    if args.command == "verify":
        zip_bytes = args.input.read_bytes()
        expected_sha256 = args.expected_sha256.strip()
        if args.revision_json is not None:
            rev_data = json.loads(args.revision_json.read_text(encoding="utf-8"))
            rev_sha = normalize_sha256_hex(rev_data.get("sha256Hash"))
            if not rev_sha:
                raise SystemExit("ERROR: --revision-json does not contain a non-empty 'sha256Hash'.")
            expected_sha256 = rev_sha

        detached_sig = None
        att_key_version = args.kms_key_version.strip()
        if args.attestation is not None:
            att_data = json.loads(args.attestation.read_text(encoding="utf-8"))
            detached_sig = str(att_data.get("signature", "")).strip()
            if not expected_sha256 and att_data.get("sha256"):
                expected_sha256 = str(att_data["sha256"])
            if not att_key_version and att_data.get("kms_key_version"):
                att_key_version = str(att_data["kms_key_version"])

        pub_pem = args.public_key.read_bytes() if args.public_key else None
        use_cloud_kms = pub_pem is None and bool(att_key_version)

        report = verify_skill_zip(
            zip_bytes,
            public_key_pem=pub_pem,
            kms_key_version=att_key_version or None,
            use_cloud_kms=use_cloud_kms,
            expected_sha256=expected_sha256 or None,
            detached_signature_b64=detached_sig,
        )
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.verified else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
