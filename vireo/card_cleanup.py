"""Free up card space: verified scan → preview → delete for import sources.

Spec: docs/superpowers/specs/2026-08-07-card-cleanup-design.md

The safety invariant, enforced immediately before every unlink:
(1) the card file's re-hashed bytes equal the manifest hash, and
(2) a fresh catalog query finds a row with hash_status='ok' whose
    archive file is outside the source tree, is not the card file
    itself (device+inode), and stats exactly at the cataloged
    file_size/file_mtime baseline. Archive bytes are never re-read —
    the archive lives on a VPN'd SMB mount, and a re-read of the whole
    deletable set would cost more than the import this tool reclaims.
"""
import contextlib
import errno  # noqa: F401 — used by Task 3-5 scan/delete
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path  # noqa: F401 — used by Task 3-5 scan/delete

import path_guard
from image_loader import (  # noqa: F401 — used by Task 3-5 scan/delete
    SUPPORTED_EXTENSIONS,
    is_excluded_scan_path,
    safe_iter_dir,
    safe_scan_walk,
)
from scanner import compute_file_hash  # noqa: F401 — used by Task 3-5 scan/delete

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MAX_AGE_DAYS = 7


class ManifestError(Exception):
    """Manifest missing, expired, or failing validation.

    http_status lets the endpoint distinguish gone/expired (404,
    "re-scan the card") from corrupt/invalid (400) without string
    matching.
    """

    def __init__(self, message, http_status=400):
        super().__init__(message)
        self.http_status = http_status


def manifest_path(manifest_dir, scan_job_id):
    # Job ids are runner-generated, but basename() keeps a hostile id
    # from escaping the manifest directory.
    return os.path.join(
        manifest_dir, f"{os.path.basename(str(scan_job_id))}.json"
    )


def write_manifest(manifest_dir, manifest):
    """Atomic write (sibling temp + os.replace): a crash mid-scan can
    never leave a truncated manifest that a later delete trusts."""
    os.makedirs(manifest_dir, exist_ok=True)
    path = manifest_path(manifest_dir, manifest["scan_job_id"])
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp",
        dir=manifest_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def prune_manifests(manifest_dir, max_age_days=MANIFEST_MAX_AGE_DAYS):
    """Remove manifests older than max_age_days, plus any orphaned
    ``*.tmp`` write-manifest temp files (a hard crash between
    ``mkstemp`` and ``os.replace`` would otherwise leave them
    forever)."""
    if not os.path.isdir(manifest_dir):
        return
    cutoff = time.time() - max_age_days * 86400
    for name in os.listdir(manifest_dir):
        if not (name.endswith(".json") or name.endswith(".tmp")):
            continue
        full = os.path.join(manifest_dir, name)
        with contextlib.suppress(OSError):
            if os.path.getmtime(full) < cutoff:
                os.unlink(full)


def load_manifest(manifest_dir, scan_job_id,
                  max_age_days=MANIFEST_MAX_AGE_DAYS):
    """Load + validate. Everything here must pass before any delete."""
    path = manifest_path(manifest_dir, scan_job_id)
    if not os.path.exists(path):
        raise ManifestError(
            "manifest expired — re-scan the card", http_status=404)
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise ManifestError(
            "manifest unreadable or corrupt — re-scan the card") from e
    if (not isinstance(manifest, dict)
            or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION):
        raise ManifestError("manifest schema not recognized — re-scan the card")
    source_root = manifest.get("source_root")
    if not source_root or not os.path.isabs(str(source_root)):
        raise ManifestError("manifest missing source root — re-scan the card")
    try:
        created = datetime.fromisoformat(manifest.get("created_at"))
        age = datetime.now(UTC) - created
    except (TypeError, ValueError) as e:
        raise ManifestError(
            "manifest timestamp invalid — re-scan the card") from e
    # Age is enforced here — at request time — not only by the
    # scan-start prune.
    if age.total_seconds() > max_age_days * 86400:
        raise ManifestError(
            "manifest expired — re-scan the card", http_status=404)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ManifestError("manifest entries malformed — re-scan the card")
    # path_guard.path_contains() returns True on "can't tell" — the
    # strict direction for a guard that *disqualifies* on containment.
    # Here containment is a *required* condition for the entry to be
    # accepted, so that polarity is inverted: an unresolvable path must
    # fail closed (rejected), not fail open (accepted). Resolve both
    # sides ourselves and use contains_resolved() directly; a realpath
    # OSError is treated as "outside" rather than "inside". Case-fold
    # acceptance inside contains_resolved is still fine here — a
    # case-swapped path within the root is genuinely still within it,
    # and the delete job's per-file gates are the deeper defense.
    for entry in entries:
        if entry.get("bucket") != "deletable":
            continue
        try:
            root_real = os.path.realpath(source_root)
            child_real = os.path.realpath(str(entry.get("path", "")))
        except OSError as e:
            raise ManifestError(
                "manifest entry outside its source root — re-scan the card"
            ) from e
        if not path_guard.contains_resolved(root_real, child_real):
            raise ManifestError(
                "manifest entry outside its source root — re-scan the card")
    return manifest
