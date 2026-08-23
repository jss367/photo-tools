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

from db import commit_with_retry

log = logging.getLogger(__name__)

DEFAULT_QUOTA_MB = 20 * 1024
_eviction_lock = threading.Lock()


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
    """Return direct-file count/bytes and the configured quota."""
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
    """Delete oldest tracked working copies until usage is within quota.

    Only database-tracked ``working/<photo_id>.jpg`` files participate.  This
    avoids deleting a file another extraction thread has finished writing but
    has not yet committed to the catalog.  Files that cannot be removed keep
    their database references so storage accounting remains honest and a
    later pass can retry.

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
            "WHERE working_copy_path IS NOT NULL"
        ).fetchall()
        entries = []
        total = 0
        for row in rows:
            expected_rel = f"working/{row['id']}.jpg"
            if row["working_copy_path"] != expected_rel:
                # Do not let a malformed or legacy catalog path expand the
                # deletion scope outside Vireo's managed working directory.
                continue
            path = os.path.join(working_dir, f"{row['id']}.jpg")
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("Could not stat working-copy file %s: %s", path, exc)
                continue
            total += st.st_size
            entries.append((st.st_mtime_ns, row["id"], st.st_size, path, expected_rel))

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
            evicted.append((photo_id, expected_rel))
            total -= size
            freed_bytes += size

        for photo_id, expected_rel in evicted:
            db.conn.execute(
                "UPDATE photos SET working_copy_path=NULL, "
                "working_copy_evicted_mtime=COALESCE(file_mtime, -1) "
                "WHERE id=? AND working_copy_path=?",
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
