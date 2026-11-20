"""Dual-source skill & plugin ingestion for Google Cloud Agent Registry (v1).

Supports two first-class ingestion sources:
  1. Option 1 (local): Ingest pre-prepared standalone skills (`local-skills/`),
     single skill directories, single Agent Plugins 1.0 bundles, multi-plugin
     directories, or hybrid layouts directly from disk.
  2. Option 2 (git): Perform a shallow clone (`git clone --depth 1`) of a
     centralized GitLab/GitHub/local Git repository into an auto-cleaned
     temporary directory, auto-detect the layout, record sanitized `source_repo`
     and exact 40-char `git_commit` provenance, and ingest all discovered assets.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from tools.plugin_sync.sync import (
    PluginSyncError,
    SyncPlan,
    VALID_SCANNING_POLICIES,
    VALID_TARGET_STATES,
    _detect_git_commit,
    apply_sync_plan,
    build_skill_zip,
    build_sync_plan,
)

DISALLOWED_BRAND_PREFIXES = ("antigravity", "gemini", "claude", "cursor")
ALLOWED_GIT_SCHEMES = frozenset({"https", "http", "ssh", "file", "git"})
SCP_SSH_RE = re.compile(r"^(?P<userinfo>[^/@\s]+)@(?P<host>[^:/\s]+):(?P<path>.+)$")
HEX_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
ACRONYM_WORDS = frozenset({"gcp", "iam", "sre", "sql", "bq", "mcp", "a2a", "kms", "iap", "vpc"})
LOCAL_SKILLS_ALIASES = frozenset({"local-skills", "local_skills"})
CONTROL_CHARS_RE = re.compile(r"[\r\n\x00]")


@dataclass(frozen=True)
class ClonedRepo:
    """Managed shallow clone of a Git repository with sanitized provenance."""

    path: Path
    sanitized_url: str
    git_commit: str
    ref: str | None = None


@dataclass(frozen=True)
class DiscoveredTargets:
    """Auto-discovered plugins and standalone skills inside a source directory."""

    target_root: Path
    layout: str  # single_plugin | multi_plugin | single_skill | standalone_skills | hybrid
    plugin_dirs: list[Path] = field(default_factory=list)
    standalone_skill_dirs: list[Path] = field(default_factory=list)


def sanitize_repo_url(url: str) -> str:
    """Strip embedded credentials (`user:pass@` or `token@`) from HTTP(S)/SSH URLs.

    Preserves scheme, host, port, path, query, fragment, and standard SCP-style
    SSH user notation (`git@host:org/repo.git`) cleanly.
    """
    cleaned = url.strip()
    if not cleaned:
        return ""

    if "://" in cleaned:
        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc

        if "@" in netloc and scheme in ("http", "https", "ssh", "git"):
            userinfo, hostport = netloc.rsplit("@", 1)
            if scheme == "ssh" and userinfo == "git":
                clean_netloc = f"git@{hostport}"
            else:
                clean_netloc = hostport
            return urlunsplit((parsed.scheme, clean_netloc, parsed.path, parsed.query, parsed.fragment))
        return cleaned

    scp_match = SCP_SSH_RE.match(cleaned)
    if scp_match:
        userinfo = scp_match.group("userinfo")
        host = scp_match.group("host")
        repo_path = scp_match.group("path")
        if ":" in userinfo:
            user_only = userinfo.split(":", 1)[0]
            if user_only == "git":
                return f"git@{host}:{repo_path}"
            return f"{host}:{repo_path}"
        return f"{userinfo}@{host}:{repo_path}"

    return cleaned


def sanitize_kebab_name(raw: str, *, fallback: str = "local-ops-toolkit") -> str:
    """Normalize a label into Antigravity-compliant kebab-case without disallowed prefixes."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = fallback

    for prefix in DISALLOWED_BRAND_PREFIXES:
        if cleaned == prefix or cleaned.startswith(prefix):
            cleaned = f"enterprise-{cleaned}"
            break

    cleaned = cleaned[:64].strip("-")
    return cleaned or fallback


def derive_default_plugin_name(source_label: str, fallback: str = "local-ops-toolkit") -> str:
    """Derive a clean, Antigravity-safe plugin bundle name from a folder name or Git repo URL."""
    cleaned = sanitize_repo_url(source_label).strip().rstrip("/")
    candidate = ""

    if cleaned:
        if "://" in cleaned:
            url_path = urlsplit(cleaned).path.rstrip("/")
            candidate = url_path.rsplit("/", 1)[-1] if url_path else ""
        else:
            scp_match = SCP_SSH_RE.match(cleaned)
            if scp_match:
                repo_path = scp_match.group("path").rstrip("/")
                candidate = repo_path.rsplit("/", 1)[-1] if repo_path else ""
            else:
                candidate = cleaned.replace("\\", "/").rsplit("/", 1)[-1]

    if candidate.lower().endswith(".git"):
        candidate = candidate[:-4]

    normalized_candidate = candidate.strip().lower()
    if not normalized_candidate or normalized_candidate in (".", "..") or normalized_candidate in LOCAL_SKILLS_ALIASES:
        return sanitize_kebab_name(fallback, fallback="local-ops-toolkit")

    return sanitize_kebab_name(candidate, fallback=fallback)


def _format_display_name(raw_display: str | None, plugin_name: str) -> str:
    """Format a readable displayName avoiding disallowed brand prefixes."""
    if raw_display and raw_display.strip():
        candidate = raw_display.strip()
    else:
        candidate = " ".join(
            word.upper() if word in ACRONYM_WORDS else word.capitalize()
            for word in plugin_name.split("-")
            if word
        )
    lowered = candidate.lower()
    for prefix in DISALLOWED_BRAND_PREFIXES:
        if lowered.startswith(prefix):
            candidate = f"Enterprise {candidate}"
            break
    return candidate or "Enterprise Toolkit"


def _extract_url_secrets(url: str) -> list[str]:
    """Extract embedded credential components from a Git URL for error scrubbing."""
    cleaned = url.strip()
    secrets: list[str] = []
    if not cleaned:
        return secrets

    if "://" in cleaned:
        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        if "@" in parsed.netloc and scheme in ("http", "https", "ssh", "git"):
            userinfo = parsed.netloc.rsplit("@", 1)[0]
            if userinfo and not (scheme == "ssh" and userinfo == "git"):
                secrets.append(userinfo)
                if ":" in userinfo:
                    user_part, pass_part = userinfo.split(":", 1)
                    if pass_part:
                        secrets.append(pass_part)
                        decoded_pass = unquote(pass_part)
                        if decoded_pass and decoded_pass != pass_part:
                            secrets.append(decoded_pass)
                    if user_part and user_part not in ("git", "oauth2"):
                        secrets.append(user_part)
        return secrets

    scp_match = SCP_SSH_RE.match(cleaned)
    if scp_match:
        userinfo = scp_match.group("userinfo")
        if ":" in userinfo:
            secrets.append(userinfo)
            _user, pass_part = userinfo.split(":", 1)
            if pass_part:
                secrets.append(pass_part)
    return secrets


def _scrub_git_error(
    text: str,
    *,
    raw_url: str,
    clone_target: str,
    sanitized_url: str,
    secrets_to_mask: list[str],
) -> str:
    """Scrub credentials and raw tokens from subprocess output before raising errors."""
    scrubbed = text
    for candidate_url in (raw_url, clone_target):
        if candidate_url and candidate_url != sanitized_url:
            scrubbed = scrubbed.replace(candidate_url, sanitized_url)
    for secret in sorted({s for s in secrets_to_mask if s}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "***")
    return scrubbed.strip()


def _run_git(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    sanitized_url: str,
) -> subprocess.CompletedProcess[str]:
    """Run a git command safely with hooks disabled, ext:: blocked, and timeout."""
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PluginSyncError("Required 'git' executable was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PluginSyncError(
            f"Git command timed out after {timeout}s while accessing '{sanitized_url}'."
        ) from exc


def _validate_git_source_url(repo_url: str) -> tuple[str, bool, str]:
    """Validate repo_url and return (clone_target, is_local_dir, scheme)."""
    if CONTROL_CHARS_RE.search(repo_url):
        raise PluginSyncError("Git repository URL must not contain control characters (CR, LF, or NUL).")
    cleaned = repo_url.strip()
    if not cleaned:
        raise PluginSyncError("Git repository URL must not be empty.")
    if cleaned.startswith("-"):
        raise PluginSyncError("Git repository URL must not start with '-'.")
    if cleaned.lower().startswith("ext::"):
        raise PluginSyncError("Unsupported git remote protocol 'ext::' is forbidden.")

    if "://" in cleaned:
        scheme = urlsplit(cleaned).scheme.lower()
        if scheme not in ALLOWED_GIT_SCHEMES:
            raise PluginSyncError(
                f"Unsupported Git URL scheme {scheme!r}; expected one of {sorted(ALLOWED_GIT_SCHEMES)}."
            )
        return cleaned, False, scheme

    if SCP_SSH_RE.match(cleaned):
        return cleaned, False, "ssh"

    local_path = Path(cleaned)
    if local_path.is_dir():
        return str(local_path.resolve()), True, "local"

    raise PluginSyncError(
        f"Invalid Git repository URL or nonexistent local repository path: '{sanitize_repo_url(cleaned)}'"
    )


@contextmanager
def clone_git_repository(
    repo_url: str,
    *,
    ref: str | None = None,
    token_env: str | None = None,
    timeout: int = 60,
) -> Iterator[ClonedRepo]:
    """Shallow-clone a Git repository into a temporary directory that auto-cleans on exit.

    Supports `https://`, `http://`, `ssh://`, `git@...`, `file://`, and local Git
    directory paths. When `token_env` is specified, credentials are passed exclusively
    via origin-scoped ephemeral `GIT_CONFIG_*` `http.<origin>/.extraHeader` environment
    variables and are never written to `.git/config`, command-line arguments, or logs.
    """
    clone_target, is_local_dir, scheme = _validate_git_source_url(repo_url)
    sanitized_url = sanitize_repo_url(clone_target)

    if isinstance(ref, str) and CONTROL_CHARS_RE.search(ref):
        raise PluginSyncError("Git ref must not contain control characters (CR, LF, or NUL).")
    cleaned_ref = ref.strip() if isinstance(ref, str) and ref.strip() else None
    if cleaned_ref and cleaned_ref.startswith("-"):
        raise PluginSyncError("Git ref must not start with '-'.")

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    secrets_to_mask: list[str] = _extract_url_secrets(clone_target)

    if token_env is not None and token_env.strip():
        env_var_name = token_env.strip()
        raw_token = os.environ.get(env_var_name, "")
        if CONTROL_CHARS_RE.search(raw_token):
            raise PluginSyncError(
                f"Git authentication environment variable '{env_var_name}' contains invalid control characters."
            )
        token_value = raw_token.strip()
        if not token_value:
            raise PluginSyncError(
                f"Git authentication environment variable '{env_var_name}' is not set or is empty."
            )
        secrets_to_mask.append(token_value)
        if scheme in ("http", "https"):
            basic_b64 = base64.b64encode(f"oauth2:{token_value}".encode("utf-8")).decode("ascii")
            secrets_to_mask.append(basic_b64)
            parsed_sanitized = urlsplit(sanitized_url)
            origin_scope = f"{parsed_sanitized.scheme.lower()}://{parsed_sanitized.netloc}/"
            try:
                existing_count = int(env.get("GIT_CONFIG_COUNT", "0"))
            except ValueError:
                existing_count = 0
            env["GIT_CONFIG_COUNT"] = str(existing_count + 1)
            env[f"GIT_CONFIG_KEY_{existing_count}"] = f"http.{origin_scope}.extraHeader"
            env[f"GIT_CONFIG_VALUE_{existing_count}"] = f"Authorization: Basic {basic_b64}"

    with tempfile.TemporaryDirectory(prefix="plugin-sync-clone-") as tmp_dir:
        clone_dir = Path(tmp_dir) / "repo"

        base_clone_args = ["clone", "--depth", "1"]
        if is_local_dir:
            base_clone_args.append("--no-local")

        if cleaned_ref:
            clone_args = [*base_clone_args, "--branch", cleaned_ref, "--", clone_target, str(clone_dir)]
        else:
            clone_args = [*base_clone_args, "--", clone_target, str(clone_dir)]

        proc = _run_git(
            clone_args,
            env=env,
            timeout=timeout,
            sanitized_url=sanitized_url,
        )

        # Fallback when `--branch <ref>` fails because `ref` is a commit SHA or non-branch ref
        if proc.returncode != 0 and cleaned_ref:
            shutil.rmtree(clone_dir, ignore_errors=True)
            clone_dir.mkdir(parents=True, exist_ok=True)
            init_proc = _run_git(
                ["init", "--quiet", str(clone_dir)],
                env=env,
                timeout=timeout,
                sanitized_url=sanitized_url,
            )
            fetched = False
            if init_proc.returncode == 0:
                _run_git(
                    ["-C", str(clone_dir), "remote", "add", "origin", "--", clone_target],
                    env=env,
                    timeout=timeout,
                    sanitized_url=sanitized_url,
                )
                fetch_proc = _run_git(
                    ["-C", str(clone_dir), "fetch", "--depth", "1", "origin", "--", cleaned_ref],
                    env=env,
                    timeout=timeout,
                    sanitized_url=sanitized_url,
                )
                if fetch_proc.returncode == 0:
                    checkout_proc = _run_git(
                        ["-C", str(clone_dir), "checkout", "--quiet", "--detach", "FETCH_HEAD", "--"],
                        env=env,
                        timeout=timeout,
                        sanitized_url=sanitized_url,
                    )
                    if checkout_proc.returncode == 0:
                        fetched = True
                    else:
                        proc = checkout_proc
                else:
                    proc = fetch_proc

            if not fetched:
                shutil.rmtree(clone_dir, ignore_errors=True)
                full_clone_args = ["clone"]
                if is_local_dir:
                    full_clone_args.append("--no-local")
                full_clone_args.extend(["--", clone_target, str(clone_dir)])
                full_proc = _run_git(
                    full_clone_args,
                    env=env,
                    timeout=timeout,
                    sanitized_url=sanitized_url,
                )
                if full_proc.returncode != 0:
                    err_detail = _scrub_git_error(
                        full_proc.stderr or full_proc.stdout,
                        raw_url=repo_url,
                        clone_target=clone_target,
                        sanitized_url=sanitized_url,
                        secrets_to_mask=secrets_to_mask,
                    )
                    raise PluginSyncError(
                        f"Failed to clone Git repository '{sanitized_url}' (ref={cleaned_ref!r}): {err_detail}"
                    )
                checkout_proc = _run_git(
                    ["-C", str(clone_dir), "checkout", "--quiet", "--detach", cleaned_ref, "--"],
                    env=env,
                    timeout=timeout,
                    sanitized_url=sanitized_url,
                )
                if checkout_proc.returncode != 0:
                    err_detail = _scrub_git_error(
                        checkout_proc.stderr or checkout_proc.stdout,
                        raw_url=repo_url,
                        clone_target=clone_target,
                        sanitized_url=sanitized_url,
                        secrets_to_mask=secrets_to_mask,
                    )
                    raise PluginSyncError(
                        f"Failed to check out Git ref {cleaned_ref!r} in '{sanitized_url}': {err_detail}"
                    )
        elif proc.returncode != 0:
            err_detail = _scrub_git_error(
                proc.stderr or proc.stdout,
                raw_url=repo_url,
                clone_target=clone_target,
                sanitized_url=sanitized_url,
                secrets_to_mask=secrets_to_mask,
            )
            raise PluginSyncError(f"Failed to clone Git repository '{sanitized_url}': {err_detail}")

        sha_proc = _run_git(
            ["-C", str(clone_dir), "rev-parse", "HEAD"],
            env=env,
            timeout=timeout,
            sanitized_url=sanitized_url,
        )
        head_sha = sha_proc.stdout.strip()
        if sha_proc.returncode != 0 or not HEX_SHA40_RE.match(head_sha):
            raise PluginSyncError(
                f"Failed to resolve 40-char HEAD commit SHA for cloned repository '{sanitized_url}'."
            )

        yield ClonedRepo(
            path=clone_dir,
            sanitized_url=sanitized_url,
            git_commit=head_sha,
            ref=cleaned_ref,
        )


def _assert_contained_non_symlink(path: Path, root_resolved: Path, *, label: str) -> Path:
    """Verify `path` is not a symlink and resolves strictly inside `root_resolved`."""
    if path.is_symlink():
        raise PluginSyncError(
            f"Symlink {label} is forbidden inside source root (symlink escape blocked): '{path.name}'."
        )
    resolved = path.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PluginSyncError(
            f"Resolved {label} '{path.name}' escapes source root (symlink escape blocked)."
        ) from exc
    return resolved


def _assert_plugin_skills_contained(plugin_dir: Path, root_resolved: Path) -> None:
    """Verify a discovered plugin's key manifests and subtrees contain no escaping symlinks."""
    for rel_file in ("mcp.json", "mcp-tool-spec.json"):
        file_path = plugin_dir / rel_file
        if file_path.exists() or file_path.is_symlink():
            _assert_contained_non_symlink(
                file_path,
                root_resolved,
                label=f"file '{plugin_dir.name}/{rel_file}'",
            )

    for rel_dir in ("skills", "agents"):
        sub_dir = plugin_dir / rel_dir
        if not (sub_dir.exists() or sub_dir.is_symlink()):
            continue
        _assert_contained_non_symlink(
            sub_dir,
            root_resolved,
            label=f"directory '{plugin_dir.name}/{rel_dir}'",
        )
        if sub_dir.is_dir():
            for entry in sorted(sub_dir.iterdir(), key=lambda p: p.name):
                if entry.name.startswith(".") or entry.name == "__pycache__":
                    continue
                if entry.is_symlink() or entry.is_dir():
                    _assert_contained_non_symlink(
                        entry,
                        root_resolved,
                        label=f"directory '{plugin_dir.name}/{rel_dir}/{entry.name}'",
                    )
                    if rel_dir == "agents":
                        card = entry / "agent-card.json"
                        if card.exists() or card.is_symlink():
                            _assert_contained_non_symlink(
                                card,
                                root_resolved,
                                label=f"file '{plugin_dir.name}/agents/{entry.name}/agent-card.json'",
                            )


def _iter_clean_child_dirs(parent: Path) -> list[Path]:
    """Return sorted non-hidden child directories under parent (including symlinks to dirs)."""
    if not parent.is_dir():
        return []
    children: list[Path] = []
    for entry in sorted(parent.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            children.append(entry)
    return children


def discover_ingest_targets(
    root_path: str | Path, *, subdir: str | None = None
) -> DiscoveredTargets:
    """Resolve `root_path` (+ optional contained `subdir`) and auto-detect repository layout.

    Detects all 5 layouts:
      1. `single_plugin`: `target_root/plugin.json` exists
      2. `single_skill`: `target_root/SKILL.md` exists
      3. `multi_plugin`: `plugins/*/plugin.json` or `*/plugin.json`
      4. `standalone_skills`: `skills/*/SKILL.md` or `*/SKILL.md`
      5. `hybrid`: both plugin directories and standalone skill directories discovered

    Strictly prevents `..` traversal, absolute `--subdir`, and symlink escapes outside `root_path`.
    """
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        raise PluginSyncError(f"Source directory does not exist: {root}")

    root_resolved = root.resolve()
    if subdir is not None and subdir.strip():
        raw_sub = subdir.strip()
        if "\x00" in raw_sub or "\\" in raw_sub or raw_sub.startswith("/") or Path(raw_sub).is_absolute():
            raise PluginSyncError(
                f"Invalid --subdir {subdir!r}: must be a relative POSIX path inside the source root."
            )
        if any(part == ".." for part in PurePosixPath(raw_sub).parts):
            raise PluginSyncError(
                f"Invalid --subdir {subdir!r}: path traversal ('..') is forbidden."
            )

        candidate = root_resolved / raw_sub
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise PluginSyncError(
                f"Invalid --subdir {subdir!r}: resolved path escapes source root (symlink escape blocked)."
            ) from exc

        if not resolved.exists() or not resolved.is_dir():
            raise PluginSyncError(
                f"Subdirectory {subdir!r} does not exist or is not a directory under {root_resolved}."
            )
        target_root = resolved
    else:
        target_root = root_resolved

    # 1. Single plugin layout
    root_plugin_json = target_root / "plugin.json"
    if root_plugin_json.is_symlink() or root_plugin_json.is_file():
        _assert_contained_non_symlink(root_plugin_json, root_resolved, label="file 'plugin.json'")
        if root_plugin_json.is_file():
            _assert_plugin_skills_contained(target_root, root_resolved)
            return DiscoveredTargets(
                target_root=target_root,
                layout="single_plugin",
                plugin_dirs=[target_root],
                standalone_skill_dirs=[],
            )

    # 2. Single standalone skill layout
    root_skill_md = target_root / "SKILL.md"
    if root_skill_md.is_symlink() or root_skill_md.is_file():
        _assert_contained_non_symlink(root_skill_md, root_resolved, label="file 'SKILL.md'")
        if root_skill_md.is_file():
            return DiscoveredTargets(
                target_root=target_root,
                layout="single_skill",
                plugin_dirs=[],
                standalone_skill_dirs=[target_root],
            )

    # 3/4/5. Multi-plugin, standalone skills collection, or hybrid layout
    plugin_by_resolved: dict[Path, Path] = {}
    plugins_subdir = target_root / "plugins"
    if plugins_subdir.is_symlink() or plugins_subdir.is_dir():
        _assert_contained_non_symlink(plugins_subdir, root_resolved, label="directory 'plugins'")
        if plugins_subdir.is_dir():
            for child in _iter_clean_child_dirs(plugins_subdir):
                child_resolved = _assert_contained_non_symlink(
                    child, root_resolved, label=f"plugin directory 'plugins/{child.name}'"
                )
                child_manifest = child / "plugin.json"
                if child_manifest.is_symlink() or child_manifest.is_file():
                    _assert_contained_non_symlink(
                        child_manifest,
                        root_resolved,
                        label=f"file 'plugins/{child.name}/plugin.json'",
                    )
                    if child_manifest.is_file():
                        _assert_plugin_skills_contained(child, root_resolved)
                        plugin_by_resolved[child_resolved] = child

    for child in _iter_clean_child_dirs(target_root):
        if child.name in ("plugins", "skills"):
            continue
        child_manifest = child / "plugin.json"
        if child_manifest.is_symlink() or child_manifest.is_file():
            child_resolved = _assert_contained_non_symlink(
                child, root_resolved, label=f"plugin directory '{child.name}'"
            )
            _assert_contained_non_symlink(
                child_manifest,
                root_resolved,
                label=f"file '{child.name}/plugin.json'",
            )
            if child_manifest.is_file():
                _assert_plugin_skills_contained(child, root_resolved)
                plugin_by_resolved.setdefault(child_resolved, child)

    plugin_dirs = [plugin_by_resolved[key] for key in sorted(plugin_by_resolved)]
    plugin_resolved_set = set(plugin_by_resolved.keys())

    skill_by_resolved: dict[Path, Path] = {}
    skills_subdir = target_root / "skills"
    if skills_subdir.is_symlink() or skills_subdir.is_dir():
        _assert_contained_non_symlink(skills_subdir, root_resolved, label="directory 'skills'")
        if skills_subdir.is_dir():
            for child in _iter_clean_child_dirs(skills_subdir):
                child_resolved = _assert_contained_non_symlink(
                    child, root_resolved, label=f"skill directory 'skills/{child.name}'"
                )
                if child_resolved in plugin_resolved_set:
                    continue
                child_skill_md = child / "SKILL.md"
                if child_skill_md.is_symlink() or child_skill_md.is_file():
                    _assert_contained_non_symlink(
                        child_skill_md,
                        root_resolved,
                        label=f"file 'skills/{child.name}/SKILL.md'",
                    )
                    if child_skill_md.is_file():
                        skill_by_resolved[child_resolved] = child

    for child in _iter_clean_child_dirs(target_root):
        if child.name in ("plugins", "skills"):
            continue
        child_skill_md = child / "SKILL.md"
        if child_skill_md.is_symlink() or child_skill_md.is_file():
            child_resolved = _assert_contained_non_symlink(
                child, root_resolved, label=f"skill directory '{child.name}'"
            )
            if child_resolved in plugin_resolved_set:
                continue
            _assert_contained_non_symlink(
                child_skill_md,
                root_resolved,
                label=f"file '{child.name}/SKILL.md'",
            )
            if child_skill_md.is_file():
                skill_by_resolved.setdefault(child_resolved, child)
        elif child.is_symlink():
            # Reject any direct child symlink directory that escapes the source root
            try:
                child.resolve().relative_to(root_resolved)
            except ValueError as exc:
                raise PluginSyncError(
                    f"Symlink directory '{child.name}' escapes source root (symlink escape blocked)."
                ) from exc

    standalone_skill_dirs = [skill_by_resolved[key] for key in sorted(skill_by_resolved)]

    if plugin_dirs and standalone_skill_dirs:
        return DiscoveredTargets(
            target_root=target_root,
            layout="hybrid",
            plugin_dirs=plugin_dirs,
            standalone_skill_dirs=standalone_skill_dirs,
        )
    if plugin_dirs:
        return DiscoveredTargets(
            target_root=target_root,
            layout="multi_plugin",
            plugin_dirs=plugin_dirs,
            standalone_skill_dirs=[],
        )
    if standalone_skill_dirs:
        return DiscoveredTargets(
            target_root=target_root,
            layout="standalone_skills",
            plugin_dirs=[],
            standalone_skill_dirs=standalone_skill_dirs,
        )

    lowercase_candidates = list(_iter_clean_child_dirs(target_root))
    if skills_subdir.is_dir() and not skills_subdir.is_symlink():
        lowercase_candidates.extend(_iter_clean_child_dirs(skills_subdir))
    lowercase_hint = ""
    if (target_root / "skill.md").is_file() or any(
        (child / "skill.md").is_file() for child in lowercase_candidates
    ):
        lowercase_hint = " Found lowercase 'skill.md'; Agent Registry requires strictly uppercase 'SKILL.md'."
    raise PluginSyncError(
        f"No plugins ('plugin.json') or standalone skills ('SKILL.md') discovered in '{target_root}'.{lowercase_hint}"
    )


def build_standalone_skills_plans(
    skill_dirs: list[Path] | list[str | Path],
    *,
    plugin_name: str,
    plugin_display_name: str | None = None,
    plugin_version: str = "1.0.0",
    source_repo: str = "",
    git_commit: str = "unknown",
    project: str,
    location: str,
    target_state: str = "TARGET_STATE_ACTIVE",
    scanning_policy: str = "ENABLED_BLOCKING",
    per_skill_plugin: bool = False,
    sign_pem: str | Path | None = None,
    kms_key_version: str | None = None,
    kms_auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    http_client: httpx.Client | None = None,
) -> list[SyncPlan]:
    """Package standalone `agentskills.io` skill directories into one or more SyncPlans."""
    if target_state not in VALID_TARGET_STATES:
        raise PluginSyncError(
            f"Invalid target_state {target_state!r}; expected one of {sorted(VALID_TARGET_STATES)}"
        )
    if scanning_policy not in VALID_SCANNING_POLICIES:
        raise PluginSyncError(
            f"Invalid scanning_policy {scanning_policy!r}; expected one of "
            f"{sorted(VALID_SCANNING_POLICIES)}"
        )

    normalized_dirs = sorted((Path(d) for d in skill_dirs), key=lambda p: p.name)
    if not normalized_dirs:
        raise PluginSyncError("At least one standalone skill directory is required.")

    sanitized_source_repo = sanitize_repo_url(source_repo) if source_repo else ""
    version_str = plugin_version.strip() if plugin_version and plugin_version.strip() else "1.0.0"

    if per_skill_plugin:
        plans: list[SyncPlan] = []
        for skill_dir in normalized_dirs:
            single_plugin_name = sanitize_kebab_name(skill_dir.name)
            single_display = _format_display_name(
                plugin_display_name if len(normalized_dirs) == 1 else None,
                single_plugin_name,
            )
            metadata_inject = {
                "plugin_name": single_plugin_name,
                "plugin_display_name": single_display,
                "plugin_version": version_str,
                "source_repo": sanitized_source_repo,
                "git_commit": git_commit,
            }
            packaged = build_skill_zip(
                skill_dir,
                metadata_inject,
                sign_pem=sign_pem,
                kms_key_version=kms_key_version,
                kms_auth_token=kms_auth_token,
                kms_base_url=kms_base_url,
                http_client=http_client,
            )
            plan = SyncPlan(
                plugin_name=single_plugin_name,
                plugin_display_name=single_display,
                plugin_version=version_str,
                source_repo=sanitized_source_repo,
                git_commit=git_commit,
                project=project,
                location=location,
                target_state=target_state,
                scanning_policy=scanning_policy,
                skills=[packaged],
                warnings=list(packaged.warnings),
            )
            plans.append(plan)
        return plans

    bundle_name = sanitize_kebab_name(plugin_name)
    bundle_display = _format_display_name(plugin_display_name, bundle_name)
    metadata_inject = {
        "plugin_name": bundle_name,
        "plugin_display_name": bundle_display,
        "plugin_version": version_str,
        "source_repo": sanitized_source_repo,
        "git_commit": git_commit,
    }
    plan = SyncPlan(
        plugin_name=bundle_name,
        plugin_display_name=bundle_display,
        plugin_version=version_str,
        source_repo=sanitized_source_repo,
        git_commit=git_commit,
        project=project,
        location=location,
        target_state=target_state,
        scanning_policy=scanning_policy,
    )
    for skill_dir in normalized_dirs:
        packaged = build_skill_zip(
            skill_dir,
            metadata_inject,
            sign_pem=sign_pem,
            kms_key_version=kms_key_version,
            kms_auth_token=kms_auth_token,
            kms_base_url=kms_base_url,
            http_client=http_client,
        )
        plan.skills.append(packaged)
        plan.warnings.extend(packaged.warnings)
    return [plan]


def _detect_local_source_repo(target_root: Path) -> str:
    """Detect sanitized Git origin URL for a local directory, else a safe local:// label."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(target_root),
                "config",
                "--get",
                "remote.origin.url",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return sanitize_repo_url(proc.stdout.strip())
    except Exception:
        pass
    return f"local://{target_root.name}"


def _resolve_plugin_source_repo(
    plugin_dir: Path,
    *,
    override: str | None,
    fallback_source_repo: str,
) -> str:
    """Resolve and sanitize source_repo before packaging/signing a plugin's SKILL.md."""
    if override and override.strip():
        return sanitize_repo_url(override)

    manifest_repo = ""
    manifest_path = plugin_dir / "plugin.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                raw_repo = manifest.get("repository")
                if isinstance(raw_repo, str) and raw_repo.strip():
                    manifest_repo = raw_repo.strip()
                elif (
                    isinstance(raw_repo, dict)
                    and isinstance(raw_repo.get("url"), str)
                    and raw_repo["url"].strip()
                ):
                    manifest_repo = raw_repo["url"].strip()
        except (OSError, json.JSONDecodeError):
            pass

    if manifest_repo:
        return sanitize_repo_url(manifest_repo)
    return sanitize_repo_url(fallback_source_repo) if fallback_source_repo else ""


def build_ingest_plans(
    discovered: DiscoveredTargets,
    *,
    project: str,
    location: str,
    plugin_name: str | None = None,
    plugin_display_name: str | None = None,
    plugin_version: str = "1.0.0",
    per_skill_plugin: bool = False,
    only_skills: bool = False,
    target_state: str = "TARGET_STATE_ACTIVE",
    scanning_policy: str = "ENABLED_BLOCKING",
    source_repo: str = "",
    git_commit: str = "unknown",
    plugin_source_repo_override: str | None = None,
    sign_pem: str | Path | None = None,
    kms_key_version: str | None = None,
    kms_auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    probe_mcp: bool = True,
    http_client: httpx.Client | None = None,
) -> list[SyncPlan]:
    """Build all SyncPlans for a DiscoveredTargets snapshot (plugins + standalone skills)."""
    plans: list[SyncPlan] = []

    for p_dir in discovered.plugin_dirs:
        effective_plugin_source_repo = _resolve_plugin_source_repo(
            p_dir,
            override=plugin_source_repo_override,
            fallback_source_repo=source_repo,
        )
        plan = build_sync_plan(
            p_dir,
            project=project,
            location=location,
            target_state=target_state,
            scanning_policy=scanning_policy,
            source_repo=effective_plugin_source_repo or None,
            git_commit=git_commit,
            sign_pem=sign_pem,
            kms_key_version=kms_key_version,
            kms_auth_token=kms_auth_token,
            kms_base_url=kms_base_url,
            probe_mcp=False if only_skills else probe_mcp,
            http_client=http_client,
        )
        plans.append(plan)

    if discovered.standalone_skill_dirs:
        resolved_bundle_name = (
            plugin_name.strip()
            if isinstance(plugin_name, str) and plugin_name.strip()
            else derive_default_plugin_name(
                discovered.standalone_skill_dirs[0].name
                if discovered.layout == "single_skill"
                else discovered.target_root.name
            )
        )
        plans.extend(
            build_standalone_skills_plans(
                discovered.standalone_skill_dirs,
                plugin_name=resolved_bundle_name,
                plugin_display_name=plugin_display_name,
                plugin_version=plugin_version,
                source_repo=source_repo,
                git_commit=git_commit,
                project=project,
                location=location,
                target_state=target_state,
                scanning_policy=scanning_policy,
                per_skill_plugin=per_skill_plugin,
                sign_pem=sign_pem,
                kms_key_version=kms_key_version,
                kms_auth_token=kms_auth_token,
                kms_base_url=kms_base_url,
                http_client=http_client,
            )
        )

    if only_skills:
        for plan in plans:
            plan.mcp_services.clear()
            plan.agent_services.clear()

    return plans


def ingest_source(
    *,
    source_type: str,
    path: str | Path | None = None,
    repo_url: str | None = None,
    ref: str | None = None,
    subdir: str | None = None,
    plugin_name: str | None = None,
    plugin_display_name: str | None = None,
    plugin_version: str = "1.0.0",
    per_skill_plugin: bool = False,
    git_token_env: str | None = None,
    only_skills: bool = False,
    registry_url: str = "https://agentregistry.googleapis.com/v1",
    project: str,
    location: str = "us-central1",
    target_state: str = "TARGET_STATE_ACTIVE",
    scanning_policy: str = "ENABLED_BLOCKING",
    source_repo: str | None = None,
    git_commit: str | None = None,
    sign_pem: str | Path | None = None,
    kms_key_version: str | None = None,
    auth_token: str | None = None,
    kms_base_url: str = "https://cloudkms.googleapis.com/v1",
    output_dir: str | Path | None = None,
    probe_mcp: bool = True,
    dry_run: bool = False,
    clone_timeout: int = 60,
    http_client: httpx.Client | None = None,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    """Orchestrate Option 1 (local) or Option 2 (git) skill/plugin ingestion into Agent Registry."""
    normalized_type = source_type.strip().lower()
    if normalized_type not in ("local", "git"):
        raise PluginSyncError(f"Unsupported source_type {source_type!r}; expected 'local' or 'git'.")

    cleaned_repo_url = repo_url.strip() if isinstance(repo_url, str) and repo_url.strip() else None
    cleaned_subdir = subdir.strip() if isinstance(subdir, str) and subdir.strip() else None
    cleaned_source_repo = (
        sanitize_repo_url(source_repo)
        if isinstance(source_repo, str) and source_repo.strip()
        else None
    )
    cleaned_git_commit = (
        git_commit.strip() if isinstance(git_commit, str) and git_commit.strip() else None
    )
    cleaned_plugin_name = (
        plugin_name.strip() if isinstance(plugin_name, str) and plugin_name.strip() else None
    )
    cleaned_display_name = (
        plugin_display_name.strip()
        if isinstance(plugin_display_name, str) and plugin_display_name.strip()
        else None
    )
    cleaned_token_env = (
        git_token_env.strip() if isinstance(git_token_env, str) and git_token_env.strip() else None
    )

    if normalized_type == "local":
        if cleaned_repo_url is not None:
            raise PluginSyncError("'repo_url' cannot be used with source_type='local'.")
        if path is None or not str(path).strip():
            raise PluginSyncError("'path' is required when source_type='local'.")

        discovered = discover_ingest_targets(path, subdir=cleaned_subdir)
        resolved_source_repo = cleaned_source_repo or _detect_local_source_repo(discovered.target_root)
        resolved_git_commit = cleaned_git_commit or _detect_git_commit(discovered.target_root)
        default_standalone_name = cleaned_plugin_name or derive_default_plugin_name(
            discovered.standalone_skill_dirs[0].name
            if discovered.layout == "single_skill"
            else discovered.target_root.name
        )
        plans = build_ingest_plans(
            discovered,
            project=project,
            location=location,
            plugin_name=default_standalone_name,
            plugin_display_name=cleaned_display_name,
            plugin_version=plugin_version,
            per_skill_plugin=per_skill_plugin,
            only_skills=only_skills,
            target_state=target_state,
            scanning_policy=scanning_policy,
            source_repo=resolved_source_repo,
            git_commit=resolved_git_commit,
            plugin_source_repo_override=cleaned_source_repo,
            sign_pem=sign_pem,
            kms_key_version=kms_key_version,
            kms_auth_token=auth_token,
            kms_base_url=kms_base_url,
            probe_mcp=probe_mcp,
            http_client=http_client,
        )
        layout = discovered.layout
    else:
        if path is not None and str(path).strip():
            raise PluginSyncError("'path' cannot be used with source_type='git'.")
        if cleaned_repo_url is None:
            raise PluginSyncError("'repo_url' is required when source_type='git'.")

        with clone_git_repository(
            cleaned_repo_url,
            ref=ref,
            token_env=cleaned_token_env,
            timeout=clone_timeout,
        ) as cloned:
            discovered = discover_ingest_targets(cloned.path, subdir=cleaned_subdir)
            resolved_source_repo = cleaned_source_repo or cloned.sanitized_url
            resolved_git_commit = cleaned_git_commit or cloned.git_commit
            default_standalone_name = cleaned_plugin_name or derive_default_plugin_name(
                discovered.standalone_skill_dirs[0].name
                if discovered.layout == "single_skill"
                else cloned.sanitized_url
            )
            plans = build_ingest_plans(
                discovered,
                project=project,
                location=location,
                plugin_name=default_standalone_name,
                plugin_display_name=cleaned_display_name,
                plugin_version=plugin_version,
                per_skill_plugin=per_skill_plugin,
                only_skills=only_skills,
                target_state=target_state,
                scanning_policy=scanning_policy,
                source_repo=resolved_source_repo,
                git_commit=resolved_git_commit,
                plugin_source_repo_override=resolved_source_repo,
                sign_pem=sign_pem,
                kms_key_version=kms_key_version,
                kms_auth_token=auth_token,
                kms_base_url=kms_base_url,
                probe_mcp=probe_mcp,
                http_client=http_client,
            )
            layout = discovered.layout

    plan_results: list[dict[str, Any]] = []
    used_out_subdirs: set[str] = set()
    base_out = Path(output_dir) if output_dir is not None else None
    if base_out is not None:
        base_out.mkdir(parents=True, exist_ok=True)

    for idx, plan in enumerate(plans):
        plan_out_dir: Path | None = None
        if base_out is not None:
            sub_name = plan.plugin_name
            if sub_name in used_out_subdirs:
                sub_name = f"{sub_name}-{idx + 1}"
            used_out_subdirs.add(sub_name)
            plan_out_dir = base_out / sub_name

        plan_results.append(
            apply_sync_plan(
                plan,
                registry_url=registry_url,
                auth_token=auth_token,
                dry_run=dry_run,
                output_dir=plan_out_dir,
                http_client=http_client,
                poll_interval=poll_interval,
            )
        )

    summary: dict[str, Any] = {
        "source_type": normalized_type,
        "source_repo": resolved_source_repo,
        "git_commit": resolved_git_commit,
        "layout": layout,
        "dry_run": dry_run,
        "only_skills": only_skills,
        "project": project,
        "location": location,
        "registry_url": registry_url.rstrip("/"),
        "plans_count": len(plan_results),
        "skills_count": sum(len(p["skills"]) for p in plan_results),
        "mcp_services_count": sum(len(p["mcp_services"]) for p in plan_results),
        "agent_services_count": sum(len(p["agent_services"]) for p in plan_results),
        "plans": plan_results,
    }

    if base_out is not None:
        (base_out / "ingest-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )

    return summary
