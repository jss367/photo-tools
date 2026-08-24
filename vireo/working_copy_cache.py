"""Disk accounting and quota enforcement for generated working copies.

Working copies are edit-quality JPEG renditions generated from RAW files (and
oversized JPEGs).  Unlike preview-cache entries they do not have a dedicated
LRU table, so quota eviction uses the files' modification times: the oldest
generated files leave first.

An evicted row keeps ``working_copy_path`` NULL and records the source mtime in
``working_copy_evicted_mtime``.  The scanner uses that marker to distinguish a
deliberately evicted copy from a missing copy that needs startup self-healing.
A source-file change makes the marker stale and allows extraction again.
"""

import logging
import os
import threading
import time

from db import commit_with_retry

log = logging.getLogger(__name__)

DEFAULT_QUOTA_MB = 20 * 1024
_UNTRACKED_WRITE_GRACE_SECONDS = 60
_eviction_lock = threading.Lock()


def _is_private_render_tempfile(name):
    """Return True for the on-demand extractor's private tempfiles.

    ``vireo/app.py`` writes each on-demand working copy into a private
    ``.<photo_id>.render.*.jpg.tmp`` file before atomically publishing it to
    the canonical path. A process kill during that window leaves the tempfile
    behind; counting it toward quota usage — while eviction is unable to
    remove it because it never matches a catalog row — lets an orphan
    permanently consume the working-copy budget.
    """
    return (
        name.startswith(".")
        and ".render." in name
        and name.endswith(".jpg.tmp")
    )


def working_copy_quota_bytes(quota_mb=None):
    """Return the configured working-copy budget in bytes."""
    if quota_mb is None:
        import config as cfg

        quota_mb = cfg.load().get(
            "working_copy_cache_max_mb", DEFAULT_QUOTA_MB,
        )
    try:
        quota_mb = int(quota_mb)
    except (TypeError, ValueError):
        quota_mb = DEFAULT_QUOTA_MB
    return max(0, quota_mb) * 1024 * 1024


def working_copy_stats(vireo_dir, quota_mb=None):
    """Return direct-file count/bytes and the configured quota.

    Skips the private ``.<id>.render.*.jpg.tmp`` files the on-demand
    extractor uses as a staging area — those are not user-visible cache
    entries and eviction cannot reclaim them.
    """
    working_dir = os.path.join(vireo_dir, "working")
    count = 0
    total = 0
    if os.path.isdir(working_dir):
        try:
            with os.scandir(working_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file():
                            continue
                        if _is_private_render_tempfile(entry.name):
                            continue
                        total += entry.stat().st_size
                        count += 1
                    except OSError as exc:
                        log.warning(
                            "Could not stat working-copy file %s: %s",
                            entry.path, exc,
                        )
        except OSError as exc:
            log.warning("Could not inspect working-copy cache %s: %s", working_dir, exc)
    return {
        "count": count,
        "size": total,
        "path": working_dir,
        "quota_bytes": working_copy_quota_bytes(quota_mb),
    }


def evict_if_over_quota(db, vireo_dir, quota_mb=None):
    """Delete oldest canonical working copies until usage is within quota.

    Legacy Vireo versions wrote ``working/<photo_id>.jpg`` without recording
    ``working_copy_path``. Those canonical files participate too, otherwise a
    zero quota can never reclaim them. Recently modified untracked files get a
    short grace period so a concurrent writer can finish and commit its row.
    Unknown files are counted toward usage but never deleted. Files that
    cannot be removed keep their database references so accounting remains
    honest and a later pass can retry.

    Returns a small result payload useful to startup/config callers and tests.
    """
    max_bytes = working_copy_quota_bytes(quota_mb)
    working_dir = os.path.join(vireo_dir, "working")
    if not os.path.isdir(working_dir):
        return {
            "evicted": 0, "freed_bytes": 0, "remaining_bytes": 0,
            "quota_bytes": max_bytes,
        }

    with _eviction_lock:
        rows = db.conn.execute(
            "SELECT id, working_copy_path, file_mtime FROM photos "
        ).fetchall()
        files = {}
        total = 0
        try:
            with os.scandir(working_dir) as directory_entries:
                for entry in directory_entries:
                    try:
                        if not entry.is_file():
                            continue
                        if _is_private_render_tempfile(entry.name):
                            # A leftover on-demand extractor tempfile that
                            # eviction cannot reclaim (no catalog row keys off
                            # this name). Counting it toward ``total`` would
                            # force eviction to churn real working copies to
                            # stay under quota while the orphan lingered.
                            continue
                        st = entry.stat()
                    except OSError as exc:
                        log.warning(
                            "Could not stat working-copy file %s: %s",
                            entry.path, exc,
                        )
                        continue
                    files[entry.name] = (entry.path, st)
                    total += st.st_size
        except OSError as exc:
            log.warning("Could not inspect working-copy cache %s: %s", working_dir, exc)

        entries = []
        untracked_cutoff_ns = (
            time.time_ns() - _UNTRACKED_WRITE_GRACE_SECONDS * 1_000_000_000
        )
        stale_tracked_ids = []
        for row in rows:
            expected_rel = f"working/{row['id']}.jpg"
            tracked = row["working_copy_path"] == expected_rel
            if row["working_copy_path"] is not None and not tracked:
                # Do not let a malformed or legacy catalog path expand the
                # deletion scope outside Vireo's managed working directory.
                continue
            file_entry = files.get(f"{row['id']}.jpg")
            if file_entry is None:
                if tracked:
                    # A prior eviction unlinked the file but lost its DB
                    # transition (typically a commit failure after the
                    # unlinks). Reconcile so scanner backfill can regenerate
                    # this row instead of skipping it forever because
                    # working_copy_path is non-NULL.
                    stale_tracked_ids.append(row["id"])
                continue
            path, st = file_entry
            if not tracked and st.st_mtime_ns > untracked_cutoff_ns:
                # This may be an extraction that has published its final path
                # but has not committed working_copy_path yet.
                continue
            entries.append(
                (st.st_mtime_ns, row["id"], st.st_size, path, expected_rel)
            )

        if stale_tracked_ids:
            db.conn.executemany(
                "UPDATE photos SET working_copy_path=NULL "
                "WHERE id=? AND working_copy_path=?",
                [(pid, f"working/{pid}.jpg") for pid in stale_tracked_ids],
            )
            commit_with_retry(db.conn)

        if total <= max_bytes:
            return {
                "evicted": 0, "freed_bytes": 0, "remaining_bytes": total,
                "quota_bytes": max_bytes,
            }

        evicted = []
        freed_bytes = 0
        for _mtime_ns, photo_id, size, path, expected_rel in sorted(entries):
            if total <= max_bytes:
                break
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("Failed to remove working-copy file %s: %s", path, exc)
                continue
            evicted.append((photo_id, expected_rel, path))
            total -= size
            freed_bytes += size

        for photo_id, expected_rel, path in evicted:
            # Between the ``os.remove`` above and this UPDATE, an on-demand
            # ``/photos/<id>/original`` request can regenerate the same
            # ``working/<id>.jpg`` and commit ``working_copy_path`` to the
            # same relative path. Clearing the row now would mark that
            # freshly written replacement as evicted while its bytes remain
            # on disk — the file becomes untracked and eviction skips it
            # forever after the writer-grace window closes. Only clear the
            # row if the file is still missing.
            if os.path.exists(path):
                continue
            db.conn.execute(
                "UPDATE photos SET working_copy_path=NULL, "
                "working_copy_evicted_mtime=COALESCE(file_mtime, -1) "
                "WHERE id=? AND (working_copy_path IS NULL "
                "OR working_copy_path=?)",
                (photo_id, expected_rel),
            )
        if evicted:
            commit_with_retry(db.conn)
            log.info(
                "Working-copy quota eviction: removed %d files, freed %.1f MB",
                len(evicted), freed_bytes / 1024 / 1024,
            )

        return {
            "evicted": len(evicted),
            "freed_bytes": freed_bytes,
            "remaining_bytes": total,
            "quota_bytes": max_bytes,
        }
