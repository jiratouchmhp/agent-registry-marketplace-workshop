"""Security policy, scan gate, SHA-256 integrity, and Cloud KMS ECDSA P-256 verification.

Implements the Middleware security boundary:
1. Skill lifecycle gate: STATE_ACTIVE (plus optional STATE_DEPRECATED with warnings).
2. Scan gate: scanResult.scanState in ("SUCCEEDED", "DISABLED") and len(findings) == 0.
3. Archive integrity: downloaded ZIP SHA-256 matches SkillRevision.sha256Hash.
4. Cryptographic provenance: when REQUIRE_KMS_SIGNATURE=true, verifies ECDSA P-256 SHA-256
   signature (`frontmatter.metadata.kms_signature`) against the trusted KMS public key PEM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from schemas.validators import parse_frontmatter

ALLOWED_SCAN_STATES = frozenset({"SUCCEEDED", "DISABLED"})
SIGNATURE_METADATA_KEYS = frozenset({"kms_signature", "kms_key_version"})
PLUGIN_SYNC_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
CLOUD_KMS_REST_BASE = "https://cloudkms.googleapis.com/v1"
EXPECTED_KMS_ALGORITHM = "EC_SIGN_P256_SHA256"
GCE_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


def normalize_sha256_hex(raw: Any) -> str:
    """Normalize a SHA-256 hex digest string (strip whitespace and optional 'sha256:' prefix)."""
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :].strip()
    return cleaned


@dataclass
class VerificationResult:
    """Outcome of verifying a Skill + SkillRevision + downloaded ZIP archive."""

    approved: bool
    skill_name: str
    revision_name: str
    state: str
    sha256: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    rejection_reason: str | None = None
    kms_verified: bool = False
    kms_key_version: str = ""


def generate_ecdsa_p256_keypair() -> tuple[bytes, bytes]:
    """Generate a P-256 ECDSA key pair (private PEM, public PEM) matching Cloud KMS EC_SIGN_P256_SHA256."""
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


def extract_skill_md_from_zip(zip_bytes: bytes) -> tuple[str, dict[str, Any], str]:
    """Extract and parse the single SKILL.md inside a Skill ZIP archive."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        skill_md_entries = [
            info.filename
            for info in zf.infolist()
            if not info.is_dir() and PurePosixPath(info.filename).name == "SKILL.md"
        ]
        if len(skill_md_entries) != 1:
            raise ValueError(
                f"Expected strictly 1 SKILL.md in skill archive, found {len(skill_md_entries)}"
            )
        skill_md_path = skill_md_entries[0]
        raw_text = zf.read(skill_md_path).decode("utf-8")

    frontmatter, body, err = parse_frontmatter(raw_text)
    if err or frontmatter is None:
        raise ValueError(f"Failed to parse SKILL.md frontmatter: {err}")
    return skill_md_path, frontmatter, body


def _canonicalize_skill_md(raw_text: str) -> bytes:
    """Canonicalize SKILL.md excluding kms_signature/kms_key_version so in-band signing is non-circular."""
    frontmatter, body, err = parse_frontmatter(raw_text)
    if err or frontmatter is None:
        raise ValueError(f"Cannot canonicalize invalid SKILL.md: {err}")

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
    """Build deterministic canonical payload bytes covering every file in the skill ZIP.

    For SKILL.md, `frontmatter.metadata.kms_signature` and `kms_key_version` are stripped
    before serialization so CI can sign the canonical content and embed the signature
    inside SKILL.md frontmatter without circular hashing. All other metadata, markdown body,
    references, and scripts are covered bit-for-bit.
    """
    chunks: list[bytes] = [b"agent-registry-skill-v1\x00"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        entries = sorted(
            (info for info in zf.infolist() if not info.is_dir()),
            key=lambda i: i.filename,
        )
        # Strip common top-level directory if SKILL.md is nested inside <dir>/SKILL.md
        skill_paths = [
            i.filename for i in entries if PurePosixPath(i.filename).name == "SKILL.md"
        ]
        top_prefix = ""
        if len(skill_paths) == 1:
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
    """Compute the 32-byte SHA-256 digest of the canonical skill payload (Cloud KMS digest input)."""
    return hashlib.sha256(compute_canonical_skill_payload(zip_bytes)).digest()


def compute_unsigned_deterministic_zip_digest(zip_bytes: bytes) -> bytes:
    """Compute SHA-256 digest of the deterministic unsigned ZIP (plugin_sync legacy/archive mode).

    Strips `kms_signature` while retaining `kms_key_version`, serializes SKILL.md YAML with
    `sort_keys=True, allow_unicode=True, default_flow_style=False, width=120`, and re-packs
    entries with ZIP date_time=(1980, 1, 1, 0, 0, 0) and compresslevel=9.
    """
    entries: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as src_zf:
        for info in sorted(
            (i for i in src_zf.infolist() if not i.is_dir()),
            key=lambda i: i.filename,
        ):
            raw = src_zf.read(info)
            mode = ((info.external_attr >> 16) & 0o777) or 0o644
            if PurePosixPath(info.filename).name == "SKILL.md":
                fm, body, err = parse_frontmatter(raw.decode("utf-8"))
                if err or fm is None:
                    raise ValueError(f"Invalid SKILL.md in archive: {err}")
                cleaned_fm = dict(fm)
                meta = cleaned_fm.get("metadata")
                if isinstance(meta, dict):
                    cleaned_meta = {k: v for k, v in meta.items() if k != "kms_signature"}
                    cleaned_fm["metadata"] = cleaned_meta
                yaml_text = yaml.safe_dump(
                    cleaned_fm,
                    sort_keys=True,
                    allow_unicode=True,
                    default_flow_style=False,
                    width=120,
                ).strip()
                cleaned_body = body.lstrip("\n")
                if cleaned_body and not cleaned_body.endswith("\n"):
                    cleaned_body += "\n"
                raw = f"---\n{yaml_text}\n---\n\n{cleaned_body}".encode("utf-8")
                mode = 0o644
            entries.append((info.filename, raw, mode))

    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as dst_zf:
        for arcname, data, mode in sorted(entries, key=lambda item: item[0]):
            zinfo = zipfile.ZipInfo(filename=arcname, date_time=PLUGIN_SYNC_ZIP_DATE_TIME)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.create_system = 3
            zinfo.external_attr = (0o100000 | (mode & 0o777)) << 16
            dst_zf.writestr(zinfo, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return hashlib.sha256(buf.getvalue()).digest()


def sign_raw_bytes(data: bytes, private_key_pem: bytes) -> str:
    """Sign arbitrary bytes with ECDSA P-256 SHA-256 and return base64 DER signature."""
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise ValueError("KMS signing key must be an ECDSA P-256 (SECP256R1) private key.")
    sig = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode("ascii")


def sign_skill_zip(
    zip_bytes: bytes,
    private_key_pem: bytes,
    *,
    key_version: str = "projects/acme-agent-governance/locations/us-central1/keyRings/skill-signing/cryptoKeys/skill-signer/cryptoKeyVersions/1",
) -> tuple[bytes, str]:
    """Sign a skill ZIP canonically and return `(signed_zip_bytes, base64_signature)`.

    Embeds `kms_signature` and `kms_key_version` into `SKILL.md` frontmatter `metadata`
    while preserving all other files and Unix permissions in the archive.
    """
    canonical_payload = compute_canonical_skill_payload(zip_bytes)
    sig_b64 = sign_raw_bytes(canonical_payload, private_key_pem)

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
                fm, body, err = parse_frontmatter(raw.decode("utf-8"))
                if err or fm is None:
                    raise ValueError(f"Invalid SKILL.md in archive: {err}")
                meta = dict(fm.get("metadata") or {})
                meta["kms_signature"] = sig_b64
                meta["kms_key_version"] = key_version
                fm["metadata"] = meta
                yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                new_text = f"---\n{yaml_block}\n---\n{body.lstrip(chr(10))}"
                raw = new_text.encode("utf-8")

            new_info = zipfile.ZipInfo(filename=info.filename, date_time=(2025, 1, 1, 0, 0, 0))
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr or (0o100644 << 16)
            dst_zf.writestr(new_info, raw)

    return out_buf.getvalue(), sig_b64


def _resolve_kms_oauth_token_sync(
    *,
    explicit_token: str = "",
    metadata_token_url: str = GCE_METADATA_TOKEN_URL,
    metadata_transport: httpx.BaseTransport | None = None,
) -> str:
    """Resolve an OAuth2 Bearer token for Cloud KMS /publicKey (env -> metadata server -> gcloud)."""
    token = (
        explicit_token.strip()
        or os.getenv("AGENT_REGISTRY_AUTH_TOKEN", "").strip()
        or os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    )
    if token:
        return token

    try:
        with httpx.Client(timeout=2.0, transport=metadata_transport) as meta_client:
            resp = meta_client.get(metadata_token_url, headers={"Metadata-Flavor": "Google"})
        if resp.status_code == 200:
            fetched = str(resp.json().get("access_token", "")).strip()
            if fetched:
                return fetched
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        pass

    for cmd in (
        ["gcloud", "auth", "application-default", "print-access-token"],
        ["gcloud", "auth", "print-access-token"],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()

    raise RuntimeError(
        "Unable to obtain GCP OAuth2 access token to fetch Cloud KMS public key. "
        "Provide AGENT_REGISTRY_AUTH_TOKEN / GOOGLE_OAUTH_ACCESS_TOKEN, run on Cloud Run/GCE, "
        "or authenticate with gcloud ADC."
    )


def fetch_kms_public_key_pem(
    kms_signing_key_version: str,
    *,
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
    auth_token: str = "",
    metadata_token_url: str = GCE_METADATA_TOKEN_URL,
    kms_transport: httpx.BaseTransport | None = None,
    metadata_transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Fetch an EC_SIGN_P256_SHA256 public key PEM from Cloud KMS REST `/publicKey`."""
    key_version = kms_signing_key_version.strip().lstrip("/")
    if "/cryptoKeyVersions/" not in key_version:
        raise ValueError(
            "KMS_SIGNING_KEY_VERSION must be a full CryptoKeyVersion resource name: "
            "projects/{p}/locations/{l}/keyRings/{r}/cryptoKeys/{k}/cryptoKeyVersions/{v}"
        )

    token = _resolve_kms_oauth_token_sync(
        explicit_token=auth_token,
        metadata_token_url=metadata_token_url,
        metadata_transport=metadata_transport,
    )
    url = f"{kms_endpoint.rstrip('/')}/{key_version}/publicKey"
    with httpx.Client(timeout=15.0, transport=kms_transport) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise RuntimeError(
            f"Cloud KMS publicKey fetch failed (HTTP {resp.status_code}) for '{key_version}': "
            f"{resp.text[:300]}"
        )
    payload = resp.json()
    algorithm = str(payload.get("algorithm", "")).strip()
    if algorithm and algorithm != EXPECTED_KMS_ALGORITHM:
        raise ValueError(
            f"Unsupported Cloud KMS key algorithm '{algorithm}'; expected '{EXPECTED_KMS_ALGORITHM}'."
        )
    pem_text = str(payload.get("pem", "")).strip()
    if not pem_text:
        raise RuntimeError(f"Cloud KMS publicKey response missing 'pem': {payload}")
    return (pem_text + "\n").encode("utf-8")


def load_kms_public_key(
    *,
    pem_path: str = "",
    pem_content: str = "",
    kms_signing_key_version: str = "",
    kms_endpoint: str = CLOUD_KMS_REST_BASE,
    kms_auth_token: str = "",
    metadata_token_url: str = GCE_METADATA_TOKEN_URL,
    kms_transport: httpx.BaseTransport | None = None,
    metadata_transport: httpx.BaseTransport | None = None,
) -> ec.EllipticCurvePublicKey:
    """Load an ECDSA P-256 public key from inline PEM, file path, or Cloud KMS `/publicKey`."""
    raw_pem: bytes
    if pem_content.strip():
        raw_pem = pem_content.encode("utf-8")
    elif pem_path.strip():
        path = Path(pem_path)
        if not path.is_file():
            raise FileNotFoundError(f"KMS public key PEM file not found: {path}")
        raw_pem = path.read_bytes()
    elif kms_signing_key_version.strip():
        raw_pem = fetch_kms_public_key_pem(
            kms_signing_key_version,
            kms_endpoint=kms_endpoint,
            auth_token=kms_auth_token,
            metadata_token_url=metadata_token_url,
            kms_transport=kms_transport,
            metadata_transport=metadata_transport,
        )
    else:
        raise ValueError(
            "REQUIRE_KMS_SIGNATURE is enabled, but none of KMS_PUBLIC_KEY_PEM_PATH, "
            "KMS_PUBLIC_KEY_PEM, or KMS_SIGNING_KEY_VERSION is configured."
        )

    pub = serialization.load_pem_public_key(raw_pem)
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(
        pub.curve, ec.SECP256R1
    ):
        raise ValueError("KMS public key must be an ECDSA P-256 (SECP256R1) public key.")
    return pub


def verify_ecdsa_p256_signature(
    zip_bytes: bytes,
    signature_b64: str,
    public_key: ec.EllipticCurvePublicKey,
    *,
    sha256_hex: str | None = None,
) -> bool:
    """Verify an ECDSA P-256 SHA-256 base64 signature over canonical skill content, deterministic unsigned ZIP, or raw ZIP bytes."""
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        return False
    if not signature_b64 or not signature_b64.strip():
        return False
    try:
        sig_bytes = base64.b64decode(signature_b64.strip(), validate=True)
    except Exception:
        return False

    # 1. Verify against canonical content payload (non-circular, matches Cloud KMS EC_SIGN_P256_SHA256
    #    over compute_canonical_skill_digest and current tools/plugin_sync).
    candidates: list[bytes] = []
    try:
        candidates.append(compute_canonical_skill_payload(zip_bytes))
    except Exception:
        pass
    candidates.append(zip_bytes)
    digest_hex = (
        normalize_sha256_hex(sha256_hex) or hashlib.sha256(zip_bytes).hexdigest()
    ).lower()
    candidates.append(digest_hex.encode("utf-8"))

    for candidate in candidates:
        try:
            public_key.verify(sig_bytes, candidate, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            continue

    # 2. Verify against plugin_sync deterministic unsigned ZIP SHA-256 digest (Prehashed SHA-256).
    try:
        unsigned_zip_digest = compute_unsigned_deterministic_zip_digest(zip_bytes)
        public_key.verify(
            sig_bytes,
            unsigned_zip_digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        return True
    except (InvalidSignature, ValueError, zipfile.BadZipFile):
        return False


class SecurityVerifier:
    """Enforces lifecycle state, vulnerability scan gate, SHA-256, and KMS signature checks."""

    def __init__(
        self,
        *,
        require_kms_signature: bool = True,
        kms_public_key_pem_path: str = "",
        kms_public_key_pem: str = "",
        kms_signing_key_version: str = "",
        kms_endpoint: str = CLOUD_KMS_REST_BASE,
        kms_auth_token: str = "",
        metadata_token_url: str = GCE_METADATA_TOKEN_URL,
        kms_transport: httpx.BaseTransport | None = None,
        metadata_transport: httpx.BaseTransport | None = None,
        allow_deprecated: bool = True,
    ) -> None:
        self.require_kms_signature = require_kms_signature
        self.kms_public_key_pem_path = kms_public_key_pem_path
        self.kms_public_key_pem = kms_public_key_pem
        self.kms_signing_key_version = kms_signing_key_version
        self.kms_endpoint = kms_endpoint
        self.kms_auth_token = kms_auth_token
        self.metadata_token_url = metadata_token_url
        self._kms_transport = kms_transport
        self._metadata_transport = metadata_transport
        self.allow_deprecated = allow_deprecated
        self._cached_kms_public_key: ec.EllipticCurvePublicKey | None = None

    def _load_public_key(self) -> ec.EllipticCurvePublicKey:
        if (
            not self.kms_public_key_pem.strip()
            and not self.kms_public_key_pem_path.strip()
            and self.kms_signing_key_version.strip()
            and self._cached_kms_public_key is not None
        ):
            return self._cached_kms_public_key

        pub = load_kms_public_key(
            pem_path=self.kms_public_key_pem_path,
            pem_content=self.kms_public_key_pem,
            kms_signing_key_version=self.kms_signing_key_version,
            kms_endpoint=self.kms_endpoint,
            kms_auth_token=self.kms_auth_token,
            metadata_token_url=self.metadata_token_url,
            kms_transport=self._kms_transport,
            metadata_transport=self._metadata_transport,
        )
        if (
            not self.kms_public_key_pem.strip()
            and not self.kms_public_key_pem_path.strip()
            and self.kms_signing_key_version.strip()
        ):
            self._cached_kms_public_key = pub
        return pub

    def check_metadata_policy(
        self, skill: dict[str, Any], revision: dict[str, Any] | None
    ) -> tuple[bool, list[str], str | None]:
        """Validate lifecycle state and scanResult before downloading media."""
        skill_name = str(skill.get("name", "<unknown>"))
        state = str(skill.get("state", "STATE_UNSPECIFIED"))
        warnings: list[str] = []

        if state == "STATE_ACTIVE":
            pass
        elif state == "STATE_DEPRECATED":
            if not self.allow_deprecated:
                return False, warnings, f"Skill '{skill_name}' is STATE_DEPRECATED and blocked by policy."
            warnings.append(
                f"Skill '{skill_name}' is STATE_DEPRECATED and scheduled for decommissioning."
            )
        else:
            return (
                False,
                warnings,
                f"Skill '{skill_name}' has disallowed lifecycle state '{state}' "
                "(only STATE_ACTIVE or STATE_DEPRECATED may be served).",
            )

        default_rev = str(skill.get("defaultRevision", ""))
        if not default_rev:
            return False, warnings, f"Skill '{skill_name}' has no promoted defaultRevision."

        if revision is None:
            return False, warnings, f"Skill '{skill_name}' defaultRevision metadata could not be loaded."

        scan_result = revision.get("scanResult")
        if not isinstance(scan_result, dict):
            return False, warnings, f"SkillRevision '{default_rev}' is missing scanResult."

        scan_state = str(scan_result.get("scanState", ""))
        if scan_state not in ALLOWED_SCAN_STATES:
            return (
                False,
                warnings,
                f"SkillRevision '{default_rev}' scanState is '{scan_state}'; "
                f"expected one of {sorted(ALLOWED_SCAN_STATES)}.",
            )

        findings = scan_result.get("findings") or []
        if len(findings) > 0 or int(skill.get("defaultRevisionFindingCount", 0)) > 0:
            return (
                False,
                warnings,
                f"SkillRevision '{default_rev}' has {max(len(findings), int(skill.get('defaultRevisionFindingCount', 0)))} "
                "unresolved security finding(s).",
            )

        return True, warnings, None

    def verify_skill_package(
        self,
        *,
        skill: dict[str, Any],
        revision: dict[str, Any],
        zip_bytes: bytes,
        downloaded_sha256: str | None = None,
    ) -> VerificationResult:
        """Run full defense-in-depth verification over skill metadata, ZIP SHA-256, and KMS signature."""
        skill_name = str(skill.get("name", "<unknown>"))
        rev_name = str(revision.get("name", skill.get("defaultRevision", "")))
        state = str(skill.get("state", "STATE_UNSPECIFIED"))

        ok, warnings, reason = self.check_metadata_policy(skill, revision)
        if not ok:
            return VerificationResult(
                approved=False,
                skill_name=skill_name,
                revision_name=rev_name,
                state=state,
                warnings=warnings,
                rejection_reason=reason,
            )

        computed_sha256 = hashlib.sha256(zip_bytes).hexdigest()
        expected_sha256 = normalize_sha256_hex(revision.get("sha256Hash"))
        normalized_downloaded = normalize_sha256_hex(downloaded_sha256)
        if not expected_sha256:
            return VerificationResult(
                approved=False,
                skill_name=skill_name,
                revision_name=rev_name,
                state=state,
                sha256=computed_sha256,
                warnings=warnings,
                rejection_reason=(
                    f"SkillRevision '{rev_name}' is missing required 'sha256Hash' integrity digest."
                ),
            )
        if normalized_downloaded and not hmac.compare_digest(
            computed_sha256, normalized_downloaded
        ):
            return VerificationResult(
                approved=False,
                skill_name=skill_name,
                revision_name=rev_name,
                state=state,
                sha256=computed_sha256,
                warnings=warnings,
                rejection_reason="Computed ZIP SHA-256 does not match reported download digest.",
            )
        if not hmac.compare_digest(computed_sha256, expected_sha256):
            return VerificationResult(
                approved=False,
                skill_name=skill_name,
                revision_name=rev_name,
                state=state,
                sha256=computed_sha256,
                warnings=warnings,
                rejection_reason=(
                    f"ZIP SHA-256 '{computed_sha256}' does not match SkillRevision.sha256Hash "
                    f"'{expected_sha256}'."
                ),
            )

        try:
            _skill_md_path, frontmatter, _body = extract_skill_md_from_zip(zip_bytes)
        except Exception as exc:
            return VerificationResult(
                approved=False,
                skill_name=skill_name,
                revision_name=rev_name,
                state=state,
                sha256=computed_sha256,
                warnings=warnings,
                rejection_reason=f"Invalid skill archive contents: {exc}",
            )

        metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
        kms_signature = str(metadata.get("kms_signature", "")).strip()
        kms_key_version = str(metadata.get("kms_key_version", "")).strip()
        kms_verified = False

        if self.require_kms_signature:
            if not kms_signature:
                return VerificationResult(
                    approved=False,
                    skill_name=skill_name,
                    revision_name=rev_name,
                    state=state,
                    sha256=computed_sha256,
                    frontmatter=frontmatter,
                    warnings=warnings,
                    rejection_reason=(
                        "REQUIRE_KMS_SIGNATURE is enabled, but SKILL.md frontmatter.metadata "
                        "is missing 'kms_signature'."
                    ),
                )
            try:
                pub_key = self._load_public_key()
            except Exception as exc:
                return VerificationResult(
                    approved=False,
                    skill_name=skill_name,
                    revision_name=rev_name,
                    state=state,
                    sha256=computed_sha256,
                    frontmatter=frontmatter,
                    warnings=warnings,
                    rejection_reason=f"Failed to load trusted KMS public key: {exc}",
                )

            if not verify_ecdsa_p256_signature(
                zip_bytes,
                kms_signature,
                pub_key,
                sha256_hex=computed_sha256,
            ):
                return VerificationResult(
                    approved=False,
                    skill_name=skill_name,
                    revision_name=rev_name,
                    state=state,
                    sha256=computed_sha256,
                    frontmatter=frontmatter,
                    warnings=warnings,
                    rejection_reason=(
                        "KMS ECDSA P-256 signature verification FAILED — skill payload may "
                        "have been tampered with or signed by an untrusted key."
                    ),
                )
            kms_verified = True

        return VerificationResult(
            approved=True,
            skill_name=skill_name,
            revision_name=rev_name,
            state=state,
            sha256=computed_sha256,
            frontmatter=frontmatter,
            warnings=warnings,
            kms_verified=kms_verified,
            kms_key_version=kms_key_version,
        )
