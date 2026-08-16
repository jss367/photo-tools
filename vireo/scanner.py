"""Scan folders, discover photos, read metadata, populate database."""

import contextlib
import errno
import hashlib
import inspect
import json
import logging
import multiprocessing
import os
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor, TimeoutError
from datetime import datetime
from pathlib import Path

import imagehash
from db import (
    KEYWORD_SOURCE_CONFLICT_SQL,
    KEYWORD_SOURCE_UNKNOWN,
    commit_with_retry,
)
from exif_orientation import orientation_swaps_axes as _orientation_swaps_axes
from image_loader import (
    RAW_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    ScanCancelled,
    extract_working_copy,
    is_excluded_scan_path,
    safe_iter_dir,
    safe_scan_walk,
)
from keyword_normalization import keyword_match_key
from metadata import EXIF_SUMMARY_COLUMNS, exif_summary_columns, extract_metadata
from PIL import Image
from preview_cache import (
    RecycledIdIndex,
    cleanup_cached_files_for_deleted_photos,
    purge_cached_files_for_recycled_id,
)
from render_source import exif_orientation as _exif_orientation_from_data
from render_source import is_undersized
from resource_ledger import (
    ResourceRequest,
    cpu_inference_request,
    cpu_phase_request,
    get_resource_ledger,
    suspend_resource_wait_timing,
)
from xmp import read_hierarchical_keywords, read_keywords

log = logging.getLogger(__name__)
EMPTY_FILE_SHA256 = hashlib.sha256(b"").hexdigest()


# ``ScanCancelled`` is defined in ``image_loader`` (where the low-level
# walkers raise it) and re-exported here for callers that use
# ``from scanner import ScanCancelled``.


class _ScanPauseRequested(RuntimeError):
    """Internal signal that the caller wants the scanner to release its
    process-wide CPU permits and park before resuming.

    Raised only inside ``_iter_features`` when a ``pause_check`` was
    supplied. It never escapes the scanner — the enclosing frame catches
    it, drains the pool, releases the CPU lease by exiting
    ``_claim_worker_count``, and then invokes the pause-aware
    ``cancel_check`` (which parks). Once the pause resolves, hashing
    reacquires a fresh lease and continues with the remaining files.
    """


def _status_callback_supports_phase(status_callback):
    """Return whether a callback binds the scanner's phase keyword fields."""
    if status_callback is None:
        return False
    try:
        signature = inspect.signature(status_callback)
    except (TypeError, ValueError):
        # Opaque extension callables cannot be checked without invoking them.
        # Prefer the current rich contract; any callback error must propagate
        # rather than being mistaken for a legacy signature and retried.
        return True
    try:
        signature.bind(
            "",
            phase_current=0,
            phase_total=1,
            phase_label="phase",
        )
    except TypeError:
        return False
    return True


def _call_status_callback(
    status_callback, message, *, phase_current=None, phase_total=None,
    phase_label=None, supports_phase=None,
):
    """Invoke a status callback once using its prevalidated argument shape."""
    has_phase = (
        phase_current is not None
        or phase_total is not None
        or phase_label is not None
    )
    if not has_phase:
        status_callback(message)
        return
    if supports_phase is None:
        supports_phase = _status_callback_supports_phase(status_callback)
    if supports_phase:
        status_callback(
            message,
            phase_current=phase_current,
            phase_total=phase_total,
            phase_label=phase_label,
        )
    else:
        status_callback(message)


# scan() runs inside JobRunner/pipeline_job background threads, so the
# default POSIX "fork" start method is unsafe here: forking a
# multithreaded process can deadlock. In a PyInstaller bundle, forkserver
# also fails on macOS — workers fork from a parent that has already
# loaded PIL/Foundation, and the worker's first Cocoa-touching call
# crashes the child, surfacing as EOFError on the forkserver handshake
# in the parent. spawn does fork+exec for each worker, giving a clean
# process; paired with multiprocessing.freeze_support() in app.py's
# entry point it works inside the frozen sidecar. Dev runs keep
# forkserver for its cheap warmup.
_SCAN_MP_METHOD = (
    "spawn"
    if getattr(sys, "frozen", False)
    else "forkserver"
    if "forkserver" in multiprocessing.get_all_start_methods()
    else "spawn"
)

# Windows' ProcessPoolExecutor raises ValueError when max_workers > 61
# (the WaitForMultipleObjects handle limit). Clamp on Windows so scans
# don't fail on high-core-count machines or misconfigured scan_workers.
_WINDOWS_MAX_WORKERS = 61


def _scaled_dimensions(width, height, max_size):
    try:
        width = int(width or 0)
        height = int(height or 0)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    if max_size and max_size > 0:
        long_edge = max(width, height)
        if long_edge > max_size:
            scale = max_size / long_edge
            width = round(width * scale)
            height = round(height * scale)
    return width, height


def _oriented_dimensions(width, height, exif_data):
    """Return (width, height) rotated to match what extract_working_copy writes.

    Stored ``width``/``height`` come straight from the sensor/file metadata,
    so for portrait shots taken on a landscape sensor they're the unrotated
    axes (e.g. 6000x4000 with EXIF Orientation 6). The request-path helpers
    in thumbnails/app/export/pipeline normalize these to display orientation
    before comparing against rendered pixels; scanner's RAW-undersize check
    must do the same or it sees the orientation-normalized JPEG written by
    ``extract_working_copy`` (4000x6000) as catastrophically undersized vs.
    the raw 6000x4000 and falls back to the companion JPEG.
    """
    if _orientation_swaps_axes(_exif_orientation_from_data(exif_data)):
        return height, width
    return width, height


def compute_file_hash(file_path, chunk_size=65536):
    """Compute SHA-256 hash of a file. Returns hex digest string."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_fd_hash(fd, chunk_size=65536):
    """Compute SHA-256 hash of the object referred to by ``fd``.

    Reading from the descriptor rather than re-opening the pathname keeps
    a concurrent rename from substituting a FIFO or other blocking object
    behind us — the caller pins the object with ``os.open`` + ``fstat``,
    and this function only ever hashes whatever that descriptor already
    refers to. Does NOT seek: pass a freshly-opened descriptor, or the
    hash covers only the bytes past the current file offset.
    """
    h = hashlib.sha256()
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _compute_file_features(path_str):
    """Compute (phash, file_hash) for one image.

    Module-level so ProcessPoolExecutor can pickle it. Mirrors the
    best-effort behavior the main scan loop used to have inline: any
    failure yields None for that field rather than raising.
    """
    phash = None
    with contextlib.suppress(Exception), Image.open(path_str) as img:
        phash = str(imagehash.phash(img))
    file_hash = None
    with contextlib.suppress(Exception):
        file_hash = compute_file_hash(path_str)
    return phash, file_hash


def _resolve_worker_count(files_to_process):
    """Decide how many workers to use for feature computation.

    Returns 1 (sequential) when the batch is tiny or config disables
    parallelism; otherwise honors ``scan_workers`` (0 = auto, cap at
    cpu_count and batch size).
    """
    n = len(files_to_process)
    if n < 8:
        return 1
    try:
        import config as cfg
        configured = int(cfg.get("scan_workers") or 0)
    except Exception:
        configured = 0
    if configured == 1:
        return 1
    cpu = os.cpu_count() or 1
    if configured <= 0:
        workers = cpu
    else:
        workers = min(configured, cpu)
    if sys.platform == "win32":
        workers = min(workers, _WINDOWS_MAX_WORKERS)
    return max(1, min(workers, n))


@contextlib.contextmanager
def _claim_worker_count(files_to_process, cancel_check=None):
    """Yield the scanner worker grant from the process-wide CPU budget."""
    desired = _resolve_worker_count(files_to_process)
    ledger = get_resource_ledger()
    inference_threads = cpu_inference_request(ledger.cpu_capacity).preferred
    spare_after_inference = ledger.cpu_capacity - inference_threads
    cpu_reserve = inference_threads if spare_after_inference > 0 else 0
    request = ResourceRequest(
        cpu=cpu_phase_request(
            ledger.cpu_capacity,
            minimum=1,
            preferred=desired,
            maximum=desired,
        ),
        # The ledger applies this reserve to the aggregate allocation, so two
        # concurrent flexible scans cannot each consume the same nominal
        # spare capacity and collectively block an exact inference claim.
        cpu_reserve=cpu_reserve,
        label="scanner hashing",
    )
    with ledger.acquire(request, cancel_check=cancel_check) as lease:
        yield lease.cpu_permits


def _import_keywords_for_photo(db, photo_id, xmp_path_str):
    """Read flat and hierarchical keywords from XMP and populate the database."""
    flat_keywords = read_keywords(xmp_path_str)
    hier_keywords = read_hierarchical_keywords(xmp_path_str)
    pending_flat_removals = db.get_pending_keyword_removal_keys(photo_id)
    pending_hierarchical_removals = db.get_pending_keyword_removal_keys(
        photo_id, hierarchical=True,
    )

    # Build hierarchy from lr:hierarchicalSubject
    # e.g., 'Birds|Raptors|Black kite' creates Birds -> Raptors -> Black kite
    # Skip a hierarchical entry whose chain contains any segment that
    # normalizes to `""` (e.g. `"|Birds"` or `"Birds|'|Hawk"`). add_keyword()
    # raises ValueError on those, and letting it propagate would abort the
    # whole scan on a malformed sidecar entry instead of ignoring it.
    for hier in hier_keywords:
        parts = hier.split("|")
        if any(not keyword_match_key(part) for part in parts):
            continue
        if any(
            keyword_match_key(part) in pending_hierarchical_removals
            for part in parts
        ):
            continue
        parent_id = None
        for part in parts:
            kid = db.add_keyword(part, parent_id=parent_id)
            parent_id = kid
        # Tag with the leaf keyword. A sidecar term is genuinely ambiguous —
        # the user may have typed it in Lightroom, or Vireo may have written
        # it out itself — so this is one of the few writers that declines to
        # claim manual authorship.
        db.tag_photo(photo_id, parent_id, source=KEYWORD_SOURCE_UNKNOWN)

    # Also add any flat keywords not already covered by hierarchy. Compare
    # via the normalized match key on both sides: DB names are stored in
    # their cleaned form (add_keyword normalizes on insert), so a raw
    # `dc:subject` value like `‘apapane` that matches an existing clean
    # `apapane` on this photo would otherwise fall through to add_keyword
    # and get tagged again as a redundant top-level row. Same empty-key
    # filter as above — a lone smart-quote entry would raise inside
    # add_keyword() and abort the scan.
    existing_keys = {
        keyword_match_key(k["name"]) for k in db.get_photo_keywords(photo_id)
    }
    for kw in flat_keywords:
        key = keyword_match_key(kw)
        if not key:
            continue
        # A sidecar write can be intentionally deferred in the sync panel.
        # Do not resurrect the DB association from the still-stale sidecar
        # while its removal is pending.
        if key in pending_flat_removals:
            continue
        if key in existing_keys:
            continue
        kid = db.add_keyword(kw)
        # Same ambiguity as the hierarchical branch above.
        db.tag_photo(photo_id, kid, source=KEYWORD_SOURCE_UNKNOWN)
        existing_keys.add(key)


def _extract_dimensions(exif_group, file_group, extension=None):
    """Extract width and height from ExifTool metadata groups.

    For standard images (JPEG, PNG, etc.):
    1. EXIF:ExifImageWidth / EXIF:ExifImageHeight
    2. EXIF:ImageWidth / EXIF:ImageHeight
    3. File:ImageWidth / File:ImageHeight

    For RAW files (NEF, CR2, ARW, etc.), ExifImageWidth/Height contains the
    embedded JPEG thumbnail dimensions (e.g. 160x120), not the actual image.
    Priority for RAW:
    1. File:ImageWidth / File:ImageHeight (actual decoded dimensions)
    2. EXIF:ImageWidth / EXIF:ImageHeight
    """
    is_raw = extension and extension.lower() in RAW_EXTENSIONS

    if is_raw:
        width = file_group.get("ImageWidth")
        if width is None:
            width = exif_group.get("ImageWidth")
        height = file_group.get("ImageHeight")
        if height is None:
            height = exif_group.get("ImageHeight")
    else:
        width = exif_group.get("ExifImageWidth")
        if width is None:
            width = exif_group.get("ImageWidth")
        if width is None:
            width = file_group.get("ImageWidth")
        height = exif_group.get("ExifImageHeight")
        if height is None:
            height = exif_group.get("ImageHeight")
        if height is None:
            height = file_group.get("ImageHeight")

    if width is not None:
        width = int(width)
    if height is not None:
        height = int(height)
    return width, height


def _extract_timestamp(exif_group):
    """Extract and normalize timestamp from ExifTool EXIF group.

    Checks EXIF:DateTimeOriginal first, then EXIF:CreateDate.
    If SubSecTimeOriginal (or SubSecTime) is present and numeric,
    it is included as fractional seconds for sub-second precision.
    Returns ISO format string or None.
    """
    dto = exif_group.get("DateTimeOriginal") or exif_group.get("CreateDate")
    if not dto:
        return None
    try:
        dt = datetime.strptime(str(dto), "%Y:%m:%d %H:%M:%S")
        # Attempt to add sub-second precision
        subsec = exif_group.get("SubSecTimeOriginal") or exif_group.get("SubSecTime")
        if subsec is not None:
            subsec_str = str(subsec).strip()
            if subsec_str.isdigit():
                # Pad or truncate to 6 digits (microseconds)
                us_str = subsec_str[:6].ljust(6, "0")
                dt = dt.replace(microsecond=int(us_str))
        return dt.isoformat()
    except (ValueError, TypeError):
        log.debug("Unparseable EXIF timestamp dropped: %r", dto)
        return None


def _pair_raw_jpeg_companions(db, vireo_dir=None, thumb_cache_dir=None):
    """Find raw+JPEG pairs in the same folder and merge them.

    When both IMG_001.cr3 and IMG_001.jpg exist in the same folder,
    keep the raw as the primary photo and set companion_path to the JPEG filename.
    Delete the duplicate JPEG-only photo record.

    Returns the set of companion photo ids merged away. Callers that count
    indexed photos must discount these: both files were counted on the way
    in, but only the RAW row survives, so the scan would otherwise claim
    two photos where the catalog holds one — and RAW+JPEG is the common
    shooting mode, so that overstates nearly every import.

    Ids, not a bare count, because this query covers the *whole* photos
    table: it also merges pairs left pending anywhere else in the catalog
    (an interrupted earlier scan, an older build). A caller must intersect
    with the ids it actually counted, or a scoped scan would be debited
    for merges it had nothing to do with and could report a negative
    total.
    """
    raw_exts = {".nef", ".cr2", ".cr3", ".arw", ".raf", ".dng", ".rw2", ".orf"}
    jpeg_exts = {".jpg", ".jpeg"}

    rows = db.conn.execute(
        "SELECT id, folder_id, filename, extension FROM photos"
        " WHERE companion_path IS NULL"
        " OR (companion_path IS NOT NULL AND extension IN"
        " ('.nef','.cr2','.cr3','.arw','.raf','.dng','.rw2','.orf'))"
        " ORDER BY folder_id, filename"
    ).fetchall()

    # Group by folder_id + base name (without extension)
    groups = defaultdict(list)
    for row in rows:
        base = os.path.splitext(row["filename"])[0]
        groups[(row["folder_id"], base)].append(dict(row))

    # Filesystem changes are collected here and executed only after
    # commit_with_retry succeeds. All pairs share one transaction, so a
    # failure on any later iteration (or on the commit itself) rolls back
    # every companion row that was DELETEd earlier — but any files we'd
    # already unlinked inline are gone for good, leaving the restored
    # companion rows pointing at missing thumbnails, working copies,
    # masks, offline originals, and moved mask snapshots. Deferring gives
    # us "all DB changes durable, then all FS changes" — commit failure
    # aborts both halves.
    post_commit_fs_actions = []
    # Companion rows merged away, reported to the caller so it can correct
    # its indexed count. Collected at the DELETE but only returned after
    # the commit below succeeds — the whole loop shares one transaction,
    # so a commit failure rolls every deletion back and none of them
    # happened.
    merged_ids = set()

    for (_folder_id, _base), members in groups.items():
        if len(members) < 2:
            continue

        raws = [m for m in members if m["extension"] in raw_exts]
        jpegs = [m for m in members if m["extension"] in jpeg_exts]

        if not raws or not jpegs:
            continue

        # Use first raw as primary, first JPEG as companion
        primary = raws[0]
        companion = jpegs[0]

        # Transfer metadata from companion to primary if primary lacks it.
        # Includes the promoted EXIF summary columns
        # (``EXIF_SUMMARY_COLUMNS``) so a RAW row that got merged with its
        # JPEG companion doesn't lose ``camera_make``/``camera_model``/
        # ``lens``/``aperture``/``shutter_speed``/``iso`` when the JPEG row
        # is deleted — otherwise the new universal filters miss the paired
        # photo even though ExifTool extracted those values.
        # ``focal_length`` is part of ``EXIF_SUMMARY_COLUMNS`` — do not name it
        # again here or the loop below would append a second ``focal_length=?``
        # assignment and SQLite would reject the UPDATE for duplicate columns.
        transfer_cols = (
            "timestamp, rating, flag, latitude, longitude, exif_data, "
            "width, height, "
            + ", ".join(EXIF_SUMMARY_COLUMNS)
        )
        primary_full = db.conn.execute(
            f"SELECT {transfer_cols} FROM photos WHERE id = ?",
            (primary["id"],),
        ).fetchone()
        companion_full = db.conn.execute(
            f"SELECT {transfer_cols} FROM photos WHERE id = ?",
            (companion["id"],),
        ).fetchone()

        updates = []
        params = []
        if not primary_full["timestamp"] and companion_full["timestamp"]:
            updates.append("timestamp = ?")
            params.append(companion_full["timestamp"])
        if primary_full["rating"] == 0 and companion_full["rating"] != 0:
            updates.append("rating = ?")
            params.append(companion_full["rating"])
        if (
            primary_full["flag"] == "none"
            and companion_full["flag"] not in ("none", "rejected")
        ):
            # Never copy 'rejected': the duplicate auto-resolver runs earlier
            # in the same scan and rejects companion JPEGs that lose to a
            # byte-identical twin elsewhere — stamping that onto the RAW
            # would silently hide a unique photo.
            updates.append("flag = ?")
            params.append(companion_full["flag"])
        if primary_full["latitude"] is None and companion_full["latitude"] is not None:
            updates.extend(["latitude = ?", "longitude = ?"])
            params.extend([companion_full["latitude"], companion_full["longitude"]])
        if not primary_full["exif_data"] and companion_full["exif_data"]:
            updates.append("exif_data = ?")
            params.append(companion_full["exif_data"])
        # Fill any promoted EXIF summary column the RAW row is missing but
        # its JPEG companion has. ``EXIF_SUMMARY_COLUMNS`` already contains
        # ``focal_length`` alongside the camera/exposure fields, so a single
        # loop covers all promoted columns — a separate ``focal_length``
        # transfer would fire twice and SQLite would reject the UPDATE for
        # assigning the same column twice. Only writes when the primary is
        # NULL — a non-NULL primary already reflects a rescan (which clears
        # absent columns to NULL via ``EXIF_SUMMARY_COLUMNS``), so
        # overwriting it would trample fresh metadata.
        for column in EXIF_SUMMARY_COLUMNS:
            if primary_full[column] is None and companion_full[column] is not None:
                updates.append(f"{column} = ?")
                params.append(companion_full[column])
        if not primary_full["width"] and companion_full["width"]:
            updates.extend(["width = ?", "height = ?"])
            params.extend([companion_full["width"], companion_full["height"]])
        if updates:
            params.append(primary["id"])
            db.conn.execute(
                f"UPDATE photos SET {', '.join(updates)} WHERE id = ?", params
            )

        # Transfer keywords from companion to primary
        companion_keywords = db.conn.execute(
            "SELECT keyword_id, source FROM photo_keywords WHERE photo_id = ?",
            (companion["id"],),
        ).fetchall()
        for kw in companion_keywords:
            # Move the association's provenance with it: pairing a RAW with
            # its camera JPEG must not turn the user's hand-added keywords
            # into "unknown" rows a retirement pass would treat as generated.
            # The shared conflict clause keeps whichever side's claim is
            # stronger when the primary already carries the keyword.
            db.conn.execute(
                "INSERT INTO photo_keywords (photo_id, keyword_id, source) "
                "VALUES (?, ?, ?) " + KEYWORD_SOURCE_CONFLICT_SQL,
                (primary["id"], kw["keyword_id"], kw["source"]),
            )

        db.conn.execute(
            "UPDATE photos SET companion_path = ? WHERE id = ?",
            (companion["filename"], primary["id"]),
        )
        if vireo_dir:
            # An unedited RAW display cache may have been rendered before the
            # camera JPEG was paired. File mtimes cannot tell which source
            # produced that cache, so discard it when the companion changes.
            # Deferred to post-commit: if this pair's DB changes roll back
            # (because a later iteration or the final commit fails), the
            # primary's companion_path stays NULL and there's no reason to
            # have invalidated its display cache — the RAW render is still
            # valid.
            _primary_id = primary["id"]
            _thumb_dir = thumb_cache_dir or os.path.join(
                vireo_dir, "thumbnails",
            )
            _vireo_dir = vireo_dir

            def _invalidate_primary_variants(
                pid=_primary_id, td=_thumb_dir, vd=_vireo_dir,
            ):
                _invalidate_raw_display_cache(vd, pid)
                jpeg_variant = os.path.join(td, f"{pid}_jpeg.jpg")
                try:
                    if os.path.exists(jpeg_variant):
                        os.remove(jpeg_variant)
                except OSError:
                    log.debug(
                        "Could not delete stale companion thumbnail %s",
                        jpeg_variant,
                        exc_info=True,
                    )

            post_commit_fs_actions.append(_invalidate_primary_variants)

        # Transfer detections (and their cascaded predictions) from companion to primary.
        # Detection IDs are content-addressed on (photo_id, detector_model, box,
        # category) — see vireo/detection_id.py. A bare `UPDATE photo_id` would
        # leave the row's `id` column stale (still hashed against the companion's
        # photo_id); a later detector run on the primary that produced the same
        # box would then either collide on the stale id (if SQLite reused the
        # companion's rowid for a new photo) or, more commonly, never match and
        # so the stale row would be reaped by the stale-cleanup DELETE in
        # `_upsert_detection_rows`, cascading away its predictions. Recompute
        # the id, redirect predictions to the new id, then drop the stale row.
        from detection_id import detection_id as _detection_id

        moving = db.conn.execute(
            "SELECT id, detector_model, box_x, box_y, box_w, box_h,"
            " detector_confidence, category"
            " FROM detections WHERE photo_id = ?",
            (companion["id"],),
        ).fetchall()
        for det in moving:
            new_id = _detection_id(
                primary["id"], det["detector_model"],
                (det["box_x"], det["box_y"], det["box_w"], det["box_h"]),
                det["category"],
            )
            if new_id == det["id"]:
                # Cannot happen in practice (photo_id changed) but defensive.
                db.conn.execute(
                    "UPDATE detections SET photo_id = ? WHERE id = ?",
                    (primary["id"], det["id"]),
                )
                continue
            # Insert the row under the new id. If the primary already has a
            # detection for the same (model, box, category), the UPSERT no-ops
            # and we just discard the companion's row below.
            db.conn.execute(
                "INSERT INTO detections"
                " (id, photo_id, detector_model, box_x, box_y, box_w, box_h,"
                "  detector_confidence, category)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO NOTHING",
                (new_id, primary["id"], det["detector_model"],
                 det["box_x"], det["box_y"], det["box_w"], det["box_h"],
                 det["detector_confidence"], det["category"]),
            )
            # Redirect predictions to the new detection id. ON CONFLICT in
            # predictions is unlikely (would require the primary already had
            # a prediction for this species against the same detection) but
            # IGNORE keeps us defensive.
            db.conn.execute(
                "UPDATE OR IGNORE predictions SET detection_id = ? WHERE detection_id = ?",
                (new_id, det["id"]),
            )
            # If a companion prediction collided with an existing primary
            # prediction, its row stayed on the old detection id. Preserve any
            # workspace review state by moving it onto the surviving prediction
            # before the old detection delete cascades the duplicate away.
            db.conn.execute(
                """INSERT INTO prediction_review
                     (prediction_id, workspace_id, status, reviewed_at,
                      individual, group_id, vote_count, total_votes)
                   SELECT survivor.id, pr.workspace_id, pr.status,
                          pr.reviewed_at, pr.individual, pr.group_id,
                          pr.vote_count, pr.total_votes
                     FROM predictions duplicate
                     JOIN predictions survivor
                       ON survivor.detection_id = ?
                      AND survivor.classifier_model = duplicate.classifier_model
                      AND survivor.labels_fingerprint = duplicate.labels_fingerprint
                      AND survivor.species IS duplicate.species
                     JOIN prediction_review pr
                       ON pr.prediction_id = duplicate.id
                    WHERE duplicate.detection_id = ?
                   ON CONFLICT(prediction_id, workspace_id)
                   DO UPDATE SET status = excluded.status,
                                 reviewed_at = excluded.reviewed_at,
                                 individual = COALESCE(
                                     excluded.individual,
                                     prediction_review.individual
                                 ),
                                 group_id = COALESCE(
                                     excluded.group_id,
                                     prediction_review.group_id
                                 ),
                                 vote_count = COALESCE(
                                     excluded.vote_count,
                                     prediction_review.vote_count
                                 ),
                                 total_votes = COALESCE(
                                     excluded.total_votes,
                                     prediction_review.total_votes
                                 )
                    WHERE prediction_review.status = 'pending'
                      AND excluded.status <> 'pending'""",
                (new_id, det["id"]),
            )
            # Redirect classifier_runs too — they're the cache key the
            # non-reclassify gate consults. Without this, paired photos with
            # cached predictions would look unclassified to
            # `get_classifier_run_keys(new_id)` and rerun the classifier
            # after every pair-up. Same OR IGNORE pattern as predictions:
            # primary's own run rows win on (detection_id, classifier_model,
            # labels_fingerprint) conflicts.
            db.conn.execute(
                "UPDATE OR IGNORE classifier_runs SET detection_id = ? WHERE detection_id = ?",
                (new_id, det["id"]),
            )
            # Drop any predictions/classifier_runs that lost the UPDATE race
            # (duplicate-key) along with the now-orphan companion detection
            # row — CASCADE on both FKs cleans up any remaining rows tied to
            # the old id.
            db.conn.execute(
                "DELETE FROM detections WHERE id = ?",
                (det["id"],),
            )

        # Transfer pending_changes from companion to primary. No dedup needed
        # here (unlike the inat_submissions block below): pending_changes has
        # no UNIQUE constraint that would crash on collision, and duplicate
        # rows from a raw+JPEG pairing are harmless and vanishingly unlikely.
        db.conn.execute(
            "UPDATE pending_changes SET photo_id = ? WHERE photo_id = ?",
            (primary["id"], companion["id"]),
        )
        # Transfer iNaturalist submissions: deduplicate on (photo_id, observation_id)
        # before reassigning to avoid UNIQUE constraint violation.
        db.conn.execute(
            """DELETE FROM inat_submissions
               WHERE photo_id = ? AND observation_id IN (
                   SELECT observation_id FROM inat_submissions WHERE photo_id = ?
               )""",
            (companion["id"], primary["id"]),
        )
        db.conn.execute(
            "UPDATE inat_submissions SET photo_id = ? WHERE photo_id = ?",
            (primary["id"], companion["id"]),
        )
        # Preserve non-destructive edits when a JPEG companion is folded into
        # a RAW primary. If both already have recipes, the primary wins.
        transferred_recipe = db.conn.execute(
            """INSERT OR IGNORE INTO photo_edit_recipes
                   (photo_id, recipe_json, updated_at)
               SELECT ?, recipe_json, updated_at
               FROM photo_edit_recipes
               WHERE photo_id = ?""",
            (primary["id"], companion["id"]),
        )
        if transferred_recipe.rowcount:
            db.conn.execute(
                """UPDATE edit_history_items
                   SET photo_id = ?
                   WHERE photo_id = ?
                     AND edit_id IN (
                         SELECT id FROM edit_history
                         WHERE action_type = 'edit_recipe'
                     )""",
                (primary["id"], companion["id"]),
            )
            if vireo_dir:
                # Local-adjustment mask snapshots are looked up by
                # (photo_id, ref); the transferred recipe now points at
                # primary["id"], so the files must move with it or every
                # render silently disables the local pass.
                # Deferred to post-commit: transfer_snapshots MOVES files
                # and _invalidate_derived_caches DELETES them. Running
                # either before the transaction is durable risks moving
                # the companion's snapshots onto the primary and then
                # rolling back the recipe transfer — the render would
                # then load snapshots the DB no longer knows about.
                _primary_id = primary["id"]
                _companion_id = companion["id"]
                _tcd = thumb_cache_dir
                _vd = vireo_dir

                def _apply_recipe_transfer_fs(
                    pid=_primary_id, cid=_companion_id, tcd=_tcd, vd=_vd,
                ):
                    from local_masks import transfer_snapshots
                    # transfer_snapshots falls back to a copy when the
                    # rename fails, so ``failed`` means the snapshot is
                    # genuinely unreachable — ``load_snapshot`` only ever
                    # builds snapshot_path(vireo_dir, pid, ref), so the
                    # local pass for those refs renders without its mask.
                    # Say so rather than degrading the image silently.
                    transferred = transfer_snapshots(vd, cid, pid)
                    if transferred["failed"]:
                        log.warning(
                            "Pairing moved photo %s's edit recipe to %s but "
                            "could not relocate %d local-mask snapshot(s) "
                            "(refs %s); those local adjustments will render "
                            "without their mask until recreated",
                            cid, pid, len(transferred["failed"]),
                            ", ".join(transferred["failed"]),
                        )
                    if transferred.get("enumerate_failed"):
                        # ``os.listdir(edit-masks/)`` failed, so the snapshot
                        # refs are unknown. The recipe has been transferred
                        # to the primary and load_snapshot always builds
                        # ``snapshot_path(vireo_dir, primary_id, ref)`` — any
                        # snapshot still under the companion's id is
                        # unreachable, and the local pass renders without
                        # its mask until the snapshot is recreated. Say so
                        # (CORE_PHILOSOPHY: no black boxes) instead of
                        # letting the exception vanish into the deferred-
                        # action guard.
                        log.warning(
                            "Pairing moved photo %s's edit recipe to %s but "
                            "could not enumerate edit-masks/ to move any "
                            "snapshots; every affected local adjustment will "
                            "render without its mask until recreated",
                            cid, pid,
                        )
                    _invalidate_derived_caches(
                        db, vd, pid, thumb_cache_dir=tcd,
                    )

                post_commit_fs_actions.append(_apply_recipe_transfer_fs)
            else:
                db.conn.execute(
                    "UPDATE photos SET thumb_path = NULL WHERE id = ?",
                    (primary["id"],),
                )
                db.conn.execute(
                    "DELETE FROM preview_cache WHERE photo_id = ?",
                    (primary["id"],),
                )
        # Remove keyword associations then the duplicate JPEG record
        db.conn.execute("DELETE FROM photo_keywords WHERE photo_id = ?", (companion["id"],))
        db.conn.execute("DELETE FROM photos WHERE id = ?", (companion["id"],))
        merged_ids.add(companion["id"])
        # The companion's rowid is now free for SQLite to hand to the next
        # insert. Its derivatives must be unlinked so the next photo to
        # inherit the id can't adopt the companion's thumbnail / working
        # copy / masks (see ``purge_cached_files_for_recycled_id``).
        # Deferred to post-commit: unlinking inline and then losing this
        # pair's DELETE to a later rollback would leave a restored
        # companion row pointing at gone-from-disk derivatives, and none
        # of ``purge_cached_files_for_recycled_id``'s protections would
        # help — the rowid is still occupied by that restored row, not
        # available for reuse.
        if vireo_dir:
            _companion_id = companion["id"]
            _thumb_dir_for_companion = thumb_cache_dir or os.path.join(
                vireo_dir, "thumbnails",
            )

            def _cleanup_companion(
                cid=_companion_id, td=_thumb_dir_for_companion,
                vd=vireo_dir,
            ):
                cleanup_cached_files_for_deleted_photos(
                    td, [{"photo_id": cid}], vireo_dir=vd,
                )

            post_commit_fs_actions.append(_cleanup_companion)

    commit_with_retry(db.conn)
    # DB state is durable now. Run the collected filesystem operations —
    # any exception here is per-action so a single failing unlink doesn't
    # skip the rest, and the recycled-id purge on the next insert would
    # eventually recover anything we missed.
    for action in post_commit_fs_actions:
        try:
            action()
        except Exception:
            log.exception(
                "Deferred filesystem cleanup after raw+JPEG pairing "
                "failed; may leave stale derivative files that the "
                "recycled-id purge or Clear Cache will reclaim later",
            )
    # ``_invalidate_derived_caches`` inside a deferred action issues DB
    # updates (clears thumb_path / working_copy_path, drops preview_cache
    # rows). Those auto-opened a new transaction; commit it so the state
    # is durable and doesn't leak into the caller's next write.
    if db.conn.in_transaction:
        commit_with_retry(db.conn)
    return merged_ids


def _invalidate_raw_display_cache(vireo_dir, photo_id):
    display_file = os.path.join(
        vireo_dir, "originals", f"{photo_id}.display.jpg",
    )
    if os.path.exists(display_file):
        try:
            os.remove(display_file)
        except OSError:
            log.debug(
                "Could not delete stale RAW display rendition %s",
                display_file,
                exc_info=True,
            )


def _invalidate_derived_caches(db, vireo_dir, photo_id, thumb_cache_dir=None):
    """Delete cached thumbnail / working copy / display / preview for a photo.

    Called when the scanner detects that an existing photo's source content
    has changed (different file_hash). Thumbnails, working copies, unedited
    full-resolution display renditions, and preview-pyramid sizes are all
    derived from the source bytes, so they're stale as soon as the source
    changes.

    Scope is intentionally O(1) per photo — untracked preview files
    (no preview_cache row) are handled by
    ``_sweep_untracked_previews_for_photos`` once at the end of
    ``scan()`` instead, so large rescans don't re-enumerate previews/
    for every invalidated photo (O(N × M) work).

    Also clears ``working_copy_path`` in the database so the scanner's
    working-copy extraction pass at the end of ``scan()`` picks this row
    back up and rebuilds the working copy.

    Requires an explicit ``vireo_dir``: DB path and cache root are
    independently configurable (--db vs --thumb-dir), so we can't guess
    the cache location from the DB. No-op when the caller omits it.

    ``thumb_cache_dir`` overrides the thumbnail location. ``--thumb-dir``
    may point to any directory name — it is not constrained to
    ``vireo_dir/thumbnails``. Callers that have the configured value
    (Flask routes, audit entry points) should pass it here or stale
    thumbs survive; ``previews/`` and ``working/`` are always siblings
    of ``vireo_dir`` by convention and need no override.
    """
    if not vireo_dir:
        return

    thumb_dir = thumb_cache_dir or os.path.join(vireo_dir, "thumbnails")
    thumb_path = os.path.join(thumb_dir, f"{photo_id}.jpg")
    thumb_removed = False
    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
            thumb_removed = True
        except OSError:
            log.debug("Could not delete stale thumbnail %s", thumb_path, exc_info=True)
    else:
        # File already gone but the column may still point at it (e.g.
        # external cache wipe). Keep the column in sync so the pipeline
        # planner's "thumb_path IS NULL" gate matches disk reality.
        thumb_removed = True
    # Explicit RAW/JPEG pair views use source-specific thumbnail names. They
    # derive from the same source bytes and must be invalidated alongside the
    # legacy thumbnail, especially when a hash change preserves file mtime.
    for source in ("raw", "jpeg"):
        variant_path = os.path.join(thumb_dir, f"{photo_id}_{source}.jpg")
        if not os.path.exists(variant_path):
            continue
        try:
            os.remove(variant_path)
        except OSError:
            log.debug(
                "Could not delete stale paired thumbnail %s",
                variant_path,
                exc_info=True,
            )
    if thumb_removed:
        # Mirror the working_copy_path / preview_cache cleanup below: any
        # path that drops the cached thumbnail file must also clear
        # photos.thumb_path so count_photos_missing_thumb (used by the
        # Thumbnails & Previews plan card) doesn't see a phantom "done"
        # state for a row whose JPEG no longer exists on disk.
        db.conn.execute(
            "UPDATE photos SET thumb_path = NULL WHERE id = ?",
            (photo_id,),
        )

    wc_file = os.path.join(vireo_dir, "working", f"{photo_id}.jpg")
    if os.path.exists(wc_file):
        try:
            os.remove(wc_file)
        except OSError:
            log.debug("Could not delete stale working copy %s", wc_file, exc_info=True)
    # Also clear any failure markers: a content change is a meaningful
    # input change so the next backfill / scan extraction pass should retry,
    # not be permanently locked out by a stale failure record.
    db.conn.execute(
        "UPDATE photos SET working_copy_path = NULL,"
        " working_copy_failed_at = NULL,"
        " working_copy_failed_mtime = NULL,"
        " working_copy_failed_source = NULL"
        " WHERE id = ?",
        (photo_id,),
    )

    _invalidate_raw_display_cache(vireo_dir, photo_id)

    # Preview pyramid + its LRU accounting. Only drop a preview_cache row
    # for sizes whose file was successfully removed (or was already
    # missing): if unlink fails (e.g. Windows file lock) and we drop the
    # row anyway, the serve path's lazy-adoption shortcut re-adopts the
    # stranded file and hands out stale pre-change bytes. Mirrors the
    # self-healing semantics in preview_cache.evict_if_over_quota.
    preview_dir = os.path.join(vireo_dir, "previews")
    rows = db.conn.execute(
        "SELECT size FROM preview_cache WHERE photo_id = ?", (photo_id,)
    ).fetchall()
    deleted_sizes = []
    for row in rows:
        size = row["size"]
        path = os.path.join(preview_dir, f"{photo_id}_{size}.jpg")
        try:
            os.remove(path)
        except FileNotFoundError:
            deleted_sizes.append(size)
        except OSError:
            log.debug("Could not delete stale preview %s", path, exc_info=True)
        else:
            deleted_sizes.append(size)

    if deleted_sizes:
        db.conn.executemany(
            "DELETE FROM preview_cache WHERE photo_id = ? AND size = ?",
            [(photo_id, s) for s in deleted_sizes],
        )


def _sweep_untracked_previews_for_photos(db, vireo_dir, photo_ids):
    """Batched sweep of preview files with no preview_cache row.

    Legacy / orphan preview files (written by older code paths or left
    over from interrupted inserts) would be lazy-adopted on the next
    ``/photos/<id>/preview`` request and served as valid cache hits.
    After a content change that's stale data — the app serves
    pre-change bytes. We sweep them.

    Runs once per ``scan()`` call, enumerating ``previews/`` at most
    one time regardless of how many photos were invalidated. Files
    whose ``(photo_id, size)`` still has a live preview_cache row are
    preserved (row-driven cleanup in ``_invalidate_derived_caches``
    keeps rows when unlink fails, and we must not orphan those files
    here either).
    """
    if not vireo_dir or not photo_ids:
        return
    preview_dir = os.path.join(vireo_dir, "previews")
    if not os.path.isdir(preview_dir):
        return

    photo_ids_set = {int(p) for p in photo_ids}
    ids_list = list(photo_ids_set)
    # Chunk to stay under SQLITE_MAX_VARIABLE_NUMBER (default 999 on
    # older builds). Without this, a rescan that invalidates thousands
    # of photos crashes scan post-processing with "too many SQL variables".
    _CHUNK = 900
    still_tracked: set[tuple[int, int]] = set()
    for i in range(0, len(ids_list), _CHUNK):
        chunk = ids_list[i : i + _CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = db.conn.execute(
            f"SELECT photo_id, size FROM preview_cache WHERE photo_id IN ({ph})",
            chunk,
        ).fetchall()
        still_tracked.update((r["photo_id"], r["size"]) for r in rows)

    try:
        entries = os.listdir(preview_dir)
    except OSError:
        return
    for fname in entries:
        if not fname.endswith(".jpg"):
            continue
        stem = fname[: -len(".jpg")]
        parts = stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            size = int(parts[1])
        except ValueError:
            continue
        if pid not in photo_ids_set:
            continue
        if (pid, size) in still_tracked:
            continue
        path = os.path.join(preview_dir, fname)
        try:
            os.remove(path)
        except OSError:
            log.debug("Could not delete untracked preview %s", path, exc_info=True)


def _subtree_like_pattern(path, sep=None):
    """Build a SQLite LIKE parameter that matches ``path`` + any descendant.

    Intended for use with ``LIKE ? ESCAPE '\\'``. Escapes ``\\``, ``%``, and
    ``_`` inside the path and the separator, so literal wildcards in folder
    names don't leak into sibling matches and — critically on Windows — the
    trailing backslash separator doesn't turn the appended ``%`` into a
    literal character.

    Trailing separators on the input are collapsed to exactly one before the
    wildcard. Without this, ``"/photos/"`` and the filesystem root ``"/"``
    produce ``"//%"``, which matches nothing.
    """
    if sep is None:
        sep = os.sep

    while path.endswith(sep):
        path = path[: -len(sep)]

    def _escape(s):
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    return _escape(path) + _escape(sep) + "%"


# How long a recorded extraction failure suppresses retries when the file's
# mtime hasn't changed. Without an upper bound, a transient failure (e.g. an
# external drive briefly unavailable at startup) would block backfill forever
# for unchanged files — undermining the self-healing intent. With it, a truly
# broken file is retried at most once per ``_FAILURE_RETRY_AFTER`` hours
# instead of every restart, and a recovered environment heals on its own.
_FAILURE_RETRY_AFTER = "-24 hours"


def _override_identity_matches(
        override_path, expected_size, expected_mtime_ns):
    """True when ``source_paths`` override is safe to substitute for the archive.

    The card-side override is trusted iff BOTH size and mtime match the
    identity captured when the file was copied. Size alone is not enough:
    a reused card mount or a rewritten source file can present the SAME
    byte length at the same path but with different content, and the
    extractor would then cache a working copy for the wrong bytes and set
    ``working_copy_path`` — normal backfill would never regenerate it
    from the verified archive copy. mtime narrows the trust window from
    "any file with the same size" to "the exact file we just copied":
    a rewrite by the camera or an OS-level file replacement bumps mtime;
    a remount of a different card presents mtimes from an unrelated
    session. Callers still need to guard against the (extremely rare)
    same-size-same-mtime coincidence by preferring the archive copy on
    any extraction failure — that retry lives in ``_extract_working_copies``.

    Any missing input (unknown expected size, unknown expected mtime,
    OSError from an unmounted card, or the file itself is gone) makes
    the override untrusted, so extraction falls back to the verified
    archive path.
    """
    if expected_size is None or expected_mtime_ns is None:
        return False
    try:
        st = os.stat(override_path)
    except OSError:
        return False
    return (
        st.st_size == int(expected_size)
        and st.st_mtime_ns == int(expected_mtime_ns)
    )


def _working_copy_candidate_predicate(wc_max_size, alias=""):
    """Build the WHERE-clause fragment selecting photos eligible for working-copy
    extraction, plus its bind parameters.

    Mirrors the candidate criteria inside ``_extract_working_copies`` so the
    startup self-healing gate and the backfill summary counts don't drift from
    the extractor's actual SELECT — otherwise a library of only small JPEGs
    (which the extractor intentionally skips) would still satisfy a naive
    ``working_copy_path IS NULL`` check and trigger a no-op backfill on every
    restart.

    Failure suppression has two escape hatches: a content change (mtime
    differs from the recorded ``working_copy_failed_mtime``) and a stale
    timestamp (the failure was recorded more than ``_FAILURE_RETRY_AFTER``
    ago). The latter prevents transient I/O / environment failures (e.g.
    external drive temporarily disconnected at startup) from gating retries
    forever for files whose source bytes haven't moved.

    ``alias`` is the table alias (e.g. ``"p"``) when the caller's query joins
    other tables; pass ``""`` when ``photos`` is unaliased.
    """
    p = (alias + ".") if alias else ""
    placeholders = ",".join("?" for _ in RAW_EXTENSIONS)
    params = list(RAW_EXTENSIONS)
    jpeg_clause = ""
    if wc_max_size and wc_max_size > 0:
        jpeg_clause = (
            f" OR (LOWER({p}extension) IN ('.jpg', '.jpeg', 'jpg', 'jpeg')"
            f"     AND ({p}width > ? OR {p}height > ?))"
        )
        params.extend([wc_max_size, wc_max_size])
    where = (
        f"{p}working_copy_path IS NULL"
        f" AND ({p}extension IN ({placeholders}){jpeg_clause})"
        f" AND ({p}working_copy_failed_at IS NULL"
        f"      OR {p}working_copy_failed_mtime IS NULL"
        f"      OR {p}file_mtime IS NULL"
        f"      OR {p}working_copy_failed_mtime != {p}file_mtime"
        f"      OR datetime({p}working_copy_failed_at)"
        f"         < datetime('now', ?))"
    )
    params.append(_FAILURE_RETRY_AFTER)
    return where, params


def working_copy_backfill_candidate_count(db):
    """Count photos that ``_extract_working_copies`` would actually process.

    Used by the startup gate (skip the backfill job entirely when zero) and
    by ``backfill_working_copies`` for accurate before/after reporting.
    """
    import config as cfg

    user_cfg = cfg.load()
    wc_max_size = user_cfg.get("working_copy_max_size", 4096)
    where, params = _working_copy_candidate_predicate(wc_max_size)
    return db.conn.execute(
        f"SELECT COUNT(*) FROM photos WHERE {where}", params
    ).fetchone()[0]


def _extract_working_copies(db, vireo_dir, progress_callback=None,
                            status_callback=None, scope=None,
                            cancel_check=None, source_paths=None):
    """Extract working copies for all RAW photos missing one.

    For each RAW photo without a working_copy_path, extract a JPEG working
    copy into ``<vireo_dir>/working/<photo_id>.jpg``.  When the photo has a
    companion JPEG (RAW+JPEG pair), the companion is used as the extraction
    source because the in-camera JPEG is higher quality than extracting from
    the RAW.

    Rows that previously failed extraction (``working_copy_failed_at`` set)
    are skipped unless ``file_mtime`` differs from the recorded
    ``working_copy_failed_mtime`` — a user-replaced file gets a fresh
    attempt; a permanently-broken file is not retried every pass.

    ``scope`` restricts which folders are considered:
      * ``None`` (default) — library-wide backfill (every missing WC).
      * list/tuple of entries — only folders matching an entry are eligible.
        Each entry is either:
          - a path string → matches the folder and every descendant (subtree);
          - a ``(path, "exact")`` tuple → matches the folder only;
          - a ``(path, "subtree")`` tuple → explicit form of the string case.
      * empty list/tuple — no-op (used by callers that want an explicit
        "scan matched nothing" signal instead of backfilling everything).

    ``progress_callback(current, total)`` is invoked once per processed row
    so long-running backfills can stream progress to the UI.

    ``cancel_check()`` is polled before each row; returning truthy aborts the
    loop cleanly with whatever was already committed.

    ``source_paths`` optionally maps a photo's cataloged absolute path to an
    ``(alternate_path, expected_size, expected_mtime_ns)`` tuple. The import
    job passes its card->archive mapping (with size + mtime captured at
    copy time) so extraction reads the fast local card instead of re-
    reading the just-written archive copy (which may live on a slow
    network volume). An override is used only when the file at
    ``alternate_path`` currently has the exact size AND mtime captured at
    copy time — anything else (rewritten card, remounted different card,
    unmounted card, coincidental same-size collision) falls back to the
    verified archive path. Applies to both the primary and the companion
    lookup; paths absent from the map read from the catalog location as
    usual.
    """
    import config as cfg

    if scope is not None and len(scope) == 0:
        return

    user_cfg = cfg.load()
    wc_max_size = user_cfg.get("working_copy_max_size", 4096)
    wc_quality = user_cfg.get("working_copy_quality", 92)

    # Candidate criteria (NULL working_copy_path + RAW or oversized JPEG +
    # not blocked by a stale failure marker) is shared with the startup gate
    # via ``_working_copy_candidate_predicate`` so the two stay in sync.
    candidate_where, params = _working_copy_candidate_predicate(
        wc_max_size, alias="p"
    )

    scope_clause = ""
    if scope is not None:
        scope_rows = []
        for entry in scope:
            if isinstance(entry, tuple):
                path, mode = entry
            else:
                path, mode = entry, "subtree"
            path = str(path)
            if mode == "exact":
                scope_rows.append((path, mode, None))
            else:
                # Subtree match. The LIKE pattern needs to escape `_`, `%`,
                # and the escape char itself — both in the path and in the
                # separator — so literal wildcards in folder names cannot
                # leak into siblings.
                scope_rows.append(
                    (path, "subtree", _subtree_like_pattern(path)),
                )

        # A snapshot can span thousands of exact directories. Expanding one
        # OR term per directory hits SQLite's default MAX_EXPR_DEPTH=1000.
        # Keep the scope connection-local in a TEMP table instead; executemany
        # also avoids the bound-parameter limit. Commit only the transaction
        # opened here so the subsequent main-WAL read cannot retain a stale
        # snapshot if another job writes before this extractor does.
        started_in_transaction = db.conn.in_transaction
        try:
            db.conn.execute(
                """CREATE TEMP TABLE IF NOT EXISTS working_copy_scope (
                       path TEXT NOT NULL,
                       mode TEXT NOT NULL,
                       like_pattern TEXT,
                       PRIMARY KEY (path, mode)
                   )"""
            )
            db.conn.execute(
                "CREATE INDEX IF NOT EXISTS working_copy_scope_mode "
                "ON working_copy_scope (mode)"
            )
            db.conn.execute("DELETE FROM working_copy_scope")
            db.conn.executemany(
                "INSERT OR IGNORE INTO working_copy_scope "
                "(path, mode, like_pattern) VALUES (?, ?, ?)",
                scope_rows,
            )
            if not started_in_transaction and db.conn.in_transaction:
                commit_with_retry(db.conn)
        except Exception:
            if not started_in_transaction and db.conn.in_transaction:
                db.conn.rollback()
            raise
        scope_clause = """AND (
              EXISTS (
                  SELECT 1 FROM working_copy_scope wcs
                   WHERE wcs.path = f.path
              )
              OR EXISTS (
                  SELECT 1 FROM working_copy_scope wcs
                   WHERE wcs.mode = 'subtree'
                     AND f.path LIKE wcs.like_pattern ESCAPE '\\'
              )
           )"""

    rows = db.conn.execute(
        f"""
        SELECT p.id, p.filename, p.companion_path, p.working_copy_path,
               p.extension, p.width, p.height, p.exif_data, p.file_mtime,
               p.file_size,
               f.path AS folder_path
          FROM photos p
          JOIN folders f ON f.id = p.folder_id
         WHERE {candidate_where}
           {scope_clause}
        """,
        params,
    ).fetchall()

    # Revalidate cataloged paths at execution time. A user-selected source can
    # be replaced with a symlink into a protected macOS library after the API
    # request was validated but before this deferred extraction pass begins.
    # Filtering here keeps every caller from following stale folder rows into
    # another app's managed bundle.
    rows = [
        row for row in rows
        if not is_excluded_scan_path(row["folder_path"])
    ]

    if not rows:
        return

    total = len(rows)
    status_supports_phase = _status_callback_supports_phase(status_callback)

    def _emit_working_copy_progress(current):
        """Publish the secondary working-copy phase without replacing scan progress."""
        unit = "working copy" if total == 1 else "working copies"
        message = (
            f"Generating working copies: {current:,} of {total:,}"
            if current
            else f"Generating {total:,} {unit}..."
        )
        if status_callback:
            _call_status_callback(
                status_callback,
                message,
                phase_current=current,
                phase_total=total,
                phase_label="Generating working copies",
                supports_phase=status_supports_phase,
            )
        if progress_callback is not None and current:
            progress_callback(current, total)

    _emit_working_copy_progress(0)

    # Commit per row so the writer lock is released between iterations.
    # Otherwise the first UPDATE in a batch auto-opens a transaction and
    # subsequent slow extract_working_copy() calls (RAW decode → JPEG
    # encode, multi-second per file on a slow disk) hold the writer lock
    # for the whole batch, starving any concurrent writer (e.g. a parallel
    # pipeline's add_photo INSERT past the 30s busy_timeout).
    for i, row in enumerate(rows, 1):
        if cancel_check is not None and cancel_check():
            log.info("Working-copy extraction cancelled after %d/%d rows", i - 1, total)
            break

        # Close the remaining gap between candidate selection and this file
        # read in case an alias is swapped while earlier rows are processed.
        if is_excluded_scan_path(row["folder_path"]):
            log.warning(
                "Skipping working-copy extraction inside excluded bundle: %s",
                row["folder_path"],
            )
            _emit_working_copy_progress(i)
            continue

        wc_rel = f"working/{row['id']}.jpg"
        wc_abs = os.path.join(vireo_dir, "working", f"{row['id']}.jpg")

        # Working copies are the edit-quality source. For non-RAW primaries
        # we still prefer the companion JPEG outright (fast path). For RAW
        # primaries we decode the RAW with image_loader's highlight-preserving
        # settings instead of baking in a camera JPEG that may already have
        # clipped highlights — but if libraw cannot decode this RAW variant
        # (and the embedded thumb is unusable too) we still fall back to the
        # companion so an extractable JPEG copy isn't refused outright.
        catalog_primary = os.path.join(row["folder_path"], row["filename"])
        primary_path = catalog_primary
        primary_override_used = False
        if source_paths:
            # ``source_paths`` overrides the catalog path with the card-side
            # source so extraction reads local card bytes instead of the
            # just-written archive copy over a slow NAS. Existence alone is
            # not enough: if the card was unmounted, its mount point was
            # reused for a different card, or the source file itself was
            # rewritten between copy and this extraction pass, the override
            # points at bytes that no longer match what the archive holds.
            # Reading them here would cache a working copy for the wrong
            # image and — because ``working_copy_path`` gets set — normal
            # backfill would never regenerate it from the archive. Verify
            # the override's size AND mtime against the identity captured
            # by the import job at copy time (a rewritten source bumps
            # mtime; a same-size collision on a reused card mount has an
            # unrelated mtime); anything else falls back to the verified
            # archive copy.
            override_entry = source_paths.get(catalog_primary)
            if override_entry:
                override_path, exp_size, exp_mtime_ns = override_entry
                if _override_identity_matches(
                    override_path, exp_size, exp_mtime_ns,
                ):
                    primary_path = override_path
                    primary_override_used = True
        primary_is_raw = (
            os.path.splitext(row["filename"])[1].lower() in RAW_EXTENSIONS
        )
        companion_path = None
        catalog_companion = None
        companion_override_used = False
        if row["companion_path"]:
            catalog_companion = os.path.join(
                row["folder_path"], row["companion_path"],
            )
            candidate = catalog_companion
            if source_paths:
                # Companion size is not on the RAW's row (pairing merged
                # the JPEG's photos row into the RAW's), but the import
                # ledger recorded the companion's card-side size and
                # mtime at copy time — use those to identity-check the
                # override the same way the primary does. Without this,
                # a rewritten card-side JPEG (or a remounted different
                # card) would silently poison the RAW's working copy
                # via the companion-fallback path. On identity mismatch
                # (or missing entry), read the verified archive
                # companion instead.
                override_entry = source_paths.get(catalog_companion)
                if override_entry:
                    override_path, exp_size, exp_mtime_ns = override_entry
                    if _override_identity_matches(
                        override_path, exp_size, exp_mtime_ns,
                    ):
                        candidate = override_path
                        companion_override_used = True
            if os.path.isfile(candidate):
                companion_path = candidate

        if not primary_is_raw and companion_path:
            source = companion_path
            failure_source = "companion"
        else:
            source = primary_path
            failure_source = "source"

        # extract_working_copy is slow (RAW decode + JPEG encode); run it
        # before any DB write so no transaction is open while it runs.
        ok = extract_working_copy(source, wc_abs, max_size=wc_max_size, quality=wc_quality)
        # Card-side override failed but the verified archive copy exists
        # — retry from the archive before falling through to the RAW→
        # companion fallback or recording a failure marker. Without this,
        # a transient card I/O error (or a card that was unmounted just
        # after the size check above) would leave the row marked failed
        # even though the archive copy has the correct bytes.
        if not ok:
            retry_source = None
            if source == primary_path and primary_override_used:
                retry_source = catalog_primary
            elif source == companion_path and companion_override_used:
                retry_source = catalog_companion
            if retry_source and os.path.isfile(retry_source):
                log.info(
                    "Card-side working-copy extraction failed for photo "
                    "%s (%s); retrying from archive %s",
                    row["id"], source, retry_source,
                )
                source = retry_source
                if retry_source == catalog_primary:
                    primary_path = catalog_primary
                    primary_override_used = False
                else:
                    companion_path = catalog_companion
                    companion_override_used = False
                ok = extract_working_copy(
                    source, wc_abs, max_size=wc_max_size, quality=wc_quality,
                )
        raw_failed_then_companion = False
        # libraw returns an embedded JPEG when it can't demosaic a RAW; that
        # preview is often a fraction of sensor resolution, so ok=True here
        # can still produce an undersized working copy. Treat a substantially
        # undersized RAW extraction the same as a hard failure so the
        # companion-fallback branch below replaces it instead of caching the
        # downscaled preview.
        if (
            ok
            and primary_is_raw
            and companion_path
            and row["width"]
            and row["height"]
        ):
            # Stored width/height are the unrotated sensor axes; swap them
            # before scaling so portrait files (e.g. 6000x4000 + Orientation 6)
            # produce the same expected dimensions as the orientation-normalized
            # JPEG that extract_working_copy actually writes.
            oriented_w, oriented_h = _oriented_dimensions(
                row["width"], row["height"], row["exif_data"],
            )
            expected_w, expected_h = _scaled_dimensions(
                oriented_w, oriented_h, wc_max_size,
            )
            try:
                with Image.open(wc_abs) as _wc:
                    wc_w, wc_h = _wc.size
            except Exception:
                # PIL couldn't open the file: corrupt / truncated /
                # unsupported. Treat as a failed extraction so the
                # companion fallback below runs instead of caching an
                # unreadable working copy.
                log.info(
                    "RAW working-copy extraction for photo %s produced "
                    "an unreadable file %s; retrying from companion "
                    "JPEG %s",
                    row["id"], wc_abs, companion_path,
                )
                wc_w = wc_h = 0
                ok = False
            # Use a 1% relative tolerance (not the request paths' 1px slack):
            # libraw can emit the active image area a few pixels narrower than
            # the full sensor, and a strict check would mark a valid extraction
            # as failed and route every edited render away from the RAW.
            if ok and is_undersized(
                wc_w, wc_h, expected_w, expected_h, abs_slack=0, rel_slack=0.01,
            ):
                log.info(
                    "RAW working-copy extraction for photo %s produced "
                    "undersized result (%dx%d, expected %dx%d); "
                    "retrying from companion JPEG %s",
                    row["id"], wc_w, wc_h, expected_w, expected_h,
                    companion_path,
                )
                ok = False
        if not ok and primary_is_raw and companion_path:
            log.info(
                "RAW working-copy extraction failed for photo %s (%s); "
                "falling back to companion JPEG %s",
                row["id"], primary_path, companion_path,
            )
            source = companion_path
            # Keep failure_source = "source": the RAW already failed, and
            # _has_current_working_copy_failure ignores "companion" markers
            # while both files exist — overwriting here would silently
            # un-shield request paths from the known RAW failure if the
            # companion extraction also fails.
            ok = extract_working_copy(
                source, wc_abs, max_size=wc_max_size, quality=wc_quality,
            )
            # Companion attempt used a card override and failed — retry
            # from the verified archive companion before recording failure.
            # Same rationale as the primary retry above: a transient card
            # I/O error or an unmounted card must not force this row into
            # a persistent failure marker when the archive copy is available.
            if (
                not ok
                and companion_override_used
                and catalog_companion
                and os.path.isfile(catalog_companion)
            ):
                log.info(
                    "Card-side companion extraction failed for photo %s "
                    "(%s); retrying from archive companion %s",
                    row["id"], source, catalog_companion,
                )
                source = catalog_companion
                companion_path = catalog_companion
                companion_override_used = False
                ok = extract_working_copy(
                    source, wc_abs,
                    max_size=wc_max_size, quality=wc_quality,
                )
            raw_failed_then_companion = ok
        if ok:
            if raw_failed_then_companion:
                # The companion-derived working copy is usable, but request
                # paths still need to know the RAW itself failed: edited RAW
                # render paths (preview/edit-preview/original/export) gate
                # companion selection in _recipe_render_source on a present
                # "source" failure marker. Clearing it here would push those
                # paths back through the unsupported RAW decode and 500.
                db.conn.execute(
                    "UPDATE photos SET working_copy_path=?,"
                    " working_copy_failed_at=datetime('now'),"
                    " working_copy_failed_mtime=?,"
                    " working_copy_failed_source='source'"
                    " WHERE id=?",
                    (wc_rel, row["file_mtime"], row["id"]),
                )
            else:
                db.conn.execute(
                    "UPDATE photos SET working_copy_path=?,"
                    " working_copy_failed_at=NULL,"
                    " working_copy_failed_mtime=NULL,"
                    " working_copy_failed_source=NULL"
                    " WHERE id=?",
                    (wc_rel, row["id"]),
                )
        else:
            # Mark failure gated on current file_mtime so a future content
            # change (mtime bump) clears the gate and we retry. The
            # ``working_copy_failed_at`` timestamp also expires the gate
            # after ``_FAILURE_RETRY_AFTER`` so transient I/O / environment
            # failures recover even if the file itself is unchanged.
            # Logged at warning so the user sees that a specific file is
            # the cause.
            log.warning(
                "Working copy extraction failed for photo %s (%s); "
                "marked as failed and will retry on file change or after %s",
                row["id"], source, _FAILURE_RETRY_AFTER.lstrip("-"),
            )
            db.conn.execute(
                "UPDATE photos SET working_copy_failed_at=datetime('now'),"
                " working_copy_failed_mtime=?,"
                " working_copy_failed_source=?"
                " WHERE id=?",
                (row["file_mtime"], failure_source, row["id"]),
            )
        commit_with_retry(db.conn)

        _emit_working_copy_progress(i)


def backfill_working_copies(db, vireo_dir, progress_callback=None,
                            status_callback=None, cancel_check=None):
    """Library-wide backfill of missing working copies.

    Convenience wrapper around ``_extract_working_copies`` with no folder
    scope — used by the startup self-healing job to cover photos that
    never went through ``scan(..., vireo_dir=...)`` (e.g. legacy rows from
    before working-copy generation existed) or whose previous extraction
    failed against an older mtime.

    Returns a dict with ``processed`` (rows whose status changed,
    success+failure) so callers can summarize the run. Sequential by
    design: the bottleneck is a slow external disk where parallel reads
    thrash. If profiling later disagrees, swap in a worker pool here.
    """
    before_pending = working_copy_backfill_candidate_count(db)

    _extract_working_copies(
        db, vireo_dir,
        progress_callback=progress_callback,
        status_callback=status_callback,
        scope=None,
        cancel_check=cancel_check,
    )

    after_pending = working_copy_backfill_candidate_count(db)

    succeeded = db.conn.execute(
        "SELECT COUNT(*) FROM photos WHERE working_copy_path IS NOT NULL"
    ).fetchone()[0]

    return {
        "candidates": int(before_pending),
        "remaining": int(after_pending),
        "with_working_copy": int(succeeded),
    }


_EMPTY_SCAN_COUNTS = {
    "discovered": 0, "indexed": 0, "vanished": 0, "skipped_uncataloged": 0,
    "merged_companions": 0,
}


def scan(root, db, progress_callback=None, incremental=False, extract_full_metadata=True, photo_callback=None, skip_paths=None, status_callback=None, recursive=True, restrict_dirs=None, restrict_files=None, vireo_dir=None, thumb_cache_dir=None, permission_error_callback=None, cancel_check=None, pause_check=None, cancel_only_check=None, skip_working_copies=False, repair_missing_metadata=False, register_restrict_dirs_as_roots=True, allow_photo_inserts=True, counts=None, discovered_files=None):
    """Walk a folder tree, discover photos, read metadata, populate database.

    Args:
        root: path to the root folder to scan
        db: Database instance
        progress_callback: optional callable(current, total) for progress reporting
        incremental: if True, skip files unchanged since last scan
        repair_missing_metadata: in incremental mode, force rows whose
            ExifTool payload is NULL through metadata extraction even when a
            fallback timestamp was available during the original scan
        extract_full_metadata: if True, store full ExifTool JSON in exif_data column
        photo_callback: optional callable(photo_id, path_str) called after each photo is committed
        skip_paths: optional set of absolute path strings to exclude from scanning
        status_callback: optional callable(message) for phase status updates.
            Callers may also accept keyword-only ``phase_current``,
            ``phase_total``, and ``phase_label`` for sub-phase progress.
        recursive: if True (default), scan subfolders; if False, only scan root directory
        restrict_dirs: optional list of directory paths to scan instead of the
            full tree. When provided, only files in these directories are
            discovered (non-recursively), but ``root`` is still used as the
            folder hierarchy root so parent links are preserved correctly.
        restrict_files: optional iterable of absolute file paths. When
            provided alongside ``restrict_dirs``, only files whose path
            is in this set are discovered — untracked files in the same
            directory are ignored.
        vireo_dir: optional path to the vireo data directory (e.g. ``~/.vireo``).
            When provided, working copies are extracted for RAW photos after
            companion pairing, and derived-cache invalidation fires on
            content-changed photos.
        thumb_cache_dir: optional override for the thumbnail cache
            directory. ``--thumb-dir`` is independently configurable and
            can point anywhere — defaulting to ``vireo_dir/thumbnails``
            silently misses the real cache when those diverge. Callers
            with the configured value (Flask routes, audit entry points)
            should pass it. When omitted, falls back to
            ``vireo_dir/thumbnails``.
        permission_error_callback: optional callable(path_str) invoked
            once per directory the kernel refuses to enumerate (EPERM
            from macOS TCC, EACCES from POSIX mode bits). The scan
            continues; accessible siblings are still discovered. Without
            this hook, a denied subdir would silently produce zero hits
            — a "Found 0 images" black box from the user's perspective.
        cancel_check: optional callable returning truthy when the caller
            wants scanning to stop promptly. When set, scan raises
            ScanCancelled at cancellation checkpoints. This probe MAY
            park (pipeline callers wire it to ``_pause_checkpoint``
            which blocks on Pause); it is only called between leases,
            never inside ``_claim_worker_count``.
        pause_check: optional NON-parking probe returning truthy when a
            pause is pending. Checked at every inside-lease boundary so
            the scanner can raise ``_ScanPauseRequested`` and unwind
            the lease before the caller's parking ``cancel_check`` runs.
        cancel_only_check: optional NON-parking probe returning truthy
            when the job is cancelled (but NOT for pause — that is
            ``pause_check``'s job). Used inside the lease to detect
            cancellation without triggering the parking cancel path.
            Without this, the pause_check/cancel_check ordering has a
            race window: pause set between the pause probe and the
            (parking) cancel probe would park the job while still
            holding CPU permits and the process pool. Callers should
            pass ``runner.cancellation_requested`` when they have a
            runner. When omitted, the inside-lease boundaries still
            call the parking ``cancel_check`` — the race window
            described in that Codex finding remains open in that
            fallback path, but nothing regresses vs. the pre-fix
            behavior.
        skip_working_copies: if True, suppress the end-of-scan
            ``_extract_working_copies`` pass while still running
            ``_pair_raw_jpeg_companions`` (with cache context) and
            ``_invalidate_derived_caches`` on content changes. Used by
            the per-batch import scan: the import job runs one deferred
            end-of-run extraction pass over all touched folders (a
            per-batch pass would race RAW+JPEG pairing across batch
            boundaries), but pairing itself still needs ``vireo_dir`` /
            ``thumb_cache_dir`` to move local-mask snapshots when a
            newly imported RAW pairs with an already-cataloged JPEG that
            has an edit recipe. Passing ``vireo_dir=None`` to suppress
            extraction would also silently drop those masks. See PR
            #1107 review.
        register_restrict_dirs_as_roots: restricted scans traditionally treat
            each explicit directory as a newly adopted workspace root. Set
            False when the restricted files already live below registered
            roots (for example a new-images snapshot or metadata repair):
            discovered descendants are linked to the workspace but are not
            promoted to additional roots.
        allow_photo_inserts: when False, update existing photo rows only.
            Files without an existing ``photos`` row are skipped. This gives
            Process metadata repair a mechanically enforced no-admission
            contract while retaining the shared metadata refresh code.
        counts: optional dict the scan fills in as it runs, with the same
            keys it returns. Because scan() commits incrementally and can
            raise after many photos have landed (cancellation, a post-loop
            pass, a DB error), the return value alone would force callers
            to report zero for a run that really did catalog thousands.
            Pass a dict here to read accurate counts on any exit path.
        discovered_files: optional frozen iterable of paths from a preceding
            discovery pass. When supplied, scan skips its filesystem walk and
            processes exactly this manifest. Import-in-place uses this to
            establish a stable all-source progress denominator before work.

    Returns:
        dict with ``discovered`` (files the walk turned up), ``indexed``
        (files that ended this run with a catalog row — including ones an
        incremental scan revalidated without changing, so this is "rows
        this run vouched for", not "rows newly created"), ``vanished``
        (discovered but gone by the time we stat'd them, the shape a
        network share dropping mid-scan takes), ``skipped_uncataloged``
        (update-only scans declining to insert), and
        ``merged_companions`` (JPEGs folded into a same-basename RAW's
        row, which are discounted from ``indexed`` because they stop
        being photos of their own).
        Build user-facing counts from ``indexed``, never from the progress
        counter — progress advances for skipped files too.
    """
    # Same object the caller passed (so it can read counts after an
    # exception) or a fresh one; either way start from a known shape.
    counts = {} if counts is None else counts
    counts.update(_EMPTY_SCAN_COUNTS)
    status_supports_phase = _status_callback_supports_phase(status_callback)
    root_path = Path(root)
    # Don't open the root at all if the root is, or sits inside, an
    # other-app data bundle. prune_scan_dirs below only filters
    # *children*, so a root of e.g.
    # ``~/Pictures/Photos Library.photoslibrary`` — or a directly
    # selected/stale subfolder like ``.../Photos Library.photoslibrary/originals``
    # — would still trigger the macOS "access data from other apps" TCC
    # prompt this guard exists to avoid. Check every ancestor, not just
    # the leaf name. This must run BEFORE ``root_path.is_dir()``: is_dir
    # follows symlinks and stat's the target, so for a directly selected
    # bundle (or a symlink to one), the existence test alone is enough
    # to trip the TCC prompt — mirroring the restrict_dirs branch below.
    # Both bail-outs below return the same counts shape as a completed
    # scan (all zeros) rather than None, so callers can total up
    # ``indexed`` across roots without special-casing the roots that
    # never ran. See the summary block at the end of this function.
    if is_excluded_scan_path(root_path):
        log.info(
            "Skipping other-app data bundle as scan root: %s", root_path,
        )
        return counts
    # A frozen manifest may be any iterable, including a generator. Consume
    # it exactly once so the missing-root guard can distinguish an empty
    # manifest from promised work without exhausting the later work queue.
    frozen_files = (
        None if discovered_files is None else list(discovered_files)
    )
    if not root_path.is_dir():
        # A frozen manifest promised these files exist; the missing root
        # is a source-level failure the caller must see (an SD card
        # ejected between discovery and scan, or a network mount that
        # dropped). Silently returning zero counts would hide the loss
        # from ``root_errors``, so the multi-source coordinator would
        # advance its "Overall" denominator without ever processing this
        # source and mark the import successful on an incomplete set.
        # Without a manifest, missing roots stay a benign no-op — an
        # empty-manifest run wants the same shape a completed one has.
        if frozen_files:
            raise FileNotFoundError(
                errno.ENOENT,
                "scan root disappeared between discovery and scan",
                str(root_path),
            )
        log.warning("Root path does not exist or is not a directory: %s", root)
        return counts

    def _check_cancelled():
        if cancel_check is None:
            return
        # ``_check_cancelled`` is threaded into ``ResourceLedger.acquire``
        # through ``_claim_worker_count`` as its cancellation probe. On a
        # standalone ``/api/jobs/scan`` or ``/api/jobs/import-photos`` job
        # the caller-supplied ``cancel_check`` is the runner's pause-aware
        # probe, so it parks for the whole pause; without the suspend
        # bracket, the ledger's active wait timer accumulates that idle
        # time as resource contention and inflates the persisted
        # ``resource_wait_seconds`` diagnostic by the full pause length.
        # The pipeline path already brackets its checkpoints with
        # ``suspend_resource_wait_timing()`` inside ``_pause_checkpoint``;
        # mirror that here so both call sites report contention honestly.
        # When there is no active wait (every non-``ledger.acquire`` call
        # site), the suspend context is a cheap no-op.
        with suspend_resource_wait_timing():
            cancelled = cancel_check()
        if cancelled:
            raise ScanCancelled("scan cancelled")

    def _emit_status(message, phase_current=None, phase_total=None, phase_label=None):
        if not status_callback:
            return
        _call_status_callback(
            status_callback,
            message,
            phase_current=phase_current,
            phase_total=phase_total,
            phase_label=phase_label,
            supports_phase=status_supports_phase,
        )

    # Discover all image files (incremental enumeration for progress reporting)
    # unless the caller already froze an all-source manifest. Copy the input:
    # scan sorts its work queue and callers may retain their source mapping.
    log.info("Discovering files in %s ...", root)
    _check_cancelled()
    if frozen_files is None and status_callback:
        _emit_status("Discovering files...")
    excluded_frozen = 0
    if frozen_files is not None:
        # A frozen manifest was captured before any per-source scan began, so
        # a later source can wait minutes behind earlier ones. In that window
        # a nested child dir, mount, or symlink under this source may be
        # replaced with (or into) a macOS app-managed library — a swap the
        # app-level pre-scan mount-identity check cannot see, because it
        # only revalidates the source root. Rerun the bundle guard on every
        # frozen path here so the later ``image_path.stat()`` cannot follow
        # a replacement into ``Photos Library.photoslibrary`` (or a sibling
        # excluded bundle) and re-trip the TCC prompt this guard exists to
        # avoid — or catalog files from a substituted subtree.
        image_files = []
        for path in frozen_files:
            candidate = Path(path)
            if is_excluded_scan_path(candidate):
                excluded_frozen += 1
                continue
            image_files.append(candidate)
        if excluded_frozen:
            log.warning(
                "Frozen manifest for %s: %d path(s) now resolve into an "
                "excluded app-managed bundle and were skipped",
                root, excluded_frozen,
            )
    else:
        image_files = []

    # os.walk + onerror, not Path.rglob: rglob silently skips any
    # subdir that raises during enumeration, so a TCC-denied folder
    # turns into "Found 0 images" with no signal that anything went
    # wrong. macOS TCC raises EPERM ("Operation not permitted");
    # POSIX mode-bit denials raise EACCES. Both must be reported,
    # not swallowed. Other OSError flavors are still surfaced via
    # log so we don't mask unexpected I/O problems. Defined here
    # (not nested in a branch) so the restrict_dirs path below can
    # also surface denials through the same callback.
    #
    # Partial-success (skip the denied dir, keep scanning siblings)
    # is *opt-in* via permission_error_callback. Without a callback,
    # re-raise the PermissionError: existing OSError-aware callers
    # (e.g. pipeline_job repair scan's `except (OSError, RuntimeError)`
    # unreachable counter) rely on the failure to surface, and silent
    # skipping would let a denied folder be reported as successfully
    # repaired. Callers that want to continue past denials must pass
    # the callback to acknowledge they've taken responsibility for
    # streaming the denial somewhere actionable.
    def _on_walk_error(err):
        if err.errno in (errno.EPERM, errno.EACCES):
            denied = err.filename or str(root_path)
            log.warning(
                "Permission denied enumerating %s — skipping. "
                "On macOS this usually means TCC (Privacy & Security "
                "→ Files and Folders / Removable Volumes) needs to "
                "grant Vireo access.", denied,
            )
            if permission_error_callback is not None:
                permission_error_callback(denied)
                return
            raise err
        else:
            log.warning("os.walk error at %s: %s", err.filename, err)

    # Tracks restrict_dirs entries that survive the bundle guard, so the
    # working-copy extraction pass below scopes its SQL query to the same
    # set the discovery loop actually visited. Without this, a stale folder
    # row inside an excluded bundle (carried over from before the guard)
    # would be re-touched by ``_extract_working_copies`` reading
    # ``folder_path/filename`` — re-tripping the macOS TCC prompt this guard
    # exists to avoid.
    effective_restrict_dirs = []
    if frozen_files is not None:
        # The manifest already applied the file filters. Retain only the
        # directory scope used by the later working-copy extraction pass;
        # do not enumerate those directories a second time.
        effective_restrict_dirs = [
            d for d in (restrict_dirs or [])
            if not is_excluded_scan_path(Path(d))
        ]
    elif restrict_dirs is not None:
        # Only enumerate files in the specified directories (non-recursive).
        # root is still used as the folder hierarchy root for _ensure_folder.
        restrict_files_set = set(restrict_files) if restrict_files is not None else None
        # Heartbeat counter, same interval as the recursive branch below.
        # A restricted dir is not necessarily small — the import job's
        # duplicate-folder link scan points this at archive day-folders
        # holding a whole card's worth of files, and on a network mount
        # that enumeration runs for minutes. Without the emit the caller
        # has nothing to show between "Discovering files..." and the
        # finished count.
        checked = 0
        for d in restrict_dirs:
            _check_cancelled()
            dp = Path(d)
            # The outer ``is_excluded_scan_path(root_path)`` guard above only
            # covers ``root``. restrict_dirs entries can independently point
            # into an other-app data bundle — e.g. a stale folder row from
            # before this guard, or a duplicate the caller built from
            # workspace_folders pointing at
            # ``~/Pictures/Photos Library.photoslibrary/originals``. Calling
            # ``dp.is_dir()`` / ``dp.iterdir()`` on that subtree would still
            # trip the macOS "access data from other apps" TCC prompt this
            # guard exists to avoid. Reject before any filesystem access.
            if is_excluded_scan_path(dp):
                log.info(
                    "Skipping other-app data bundle in restrict_dirs: %s", dp,
                )
                continue
            effective_restrict_dirs.append(d)
            if dp.is_dir():
                # safe_iter_dir mirrors iterdir() but drops excluded
                # bundle children (direct ``Photos Library.photoslibrary``
                # entries or symlinks pointing into one) before the
                # is_file()/suffix filter below would stat them — that
                # stat alone would re-trip the macOS "access data from
                # other apps" TCC prompt this guard exists to avoid.
                # Permission denials route through _on_walk_error: when
                # ``permission_error_callback`` is registered the helper
                # logs + invokes it and returns, so the generator yields
                # nothing and the for-loop falls through to the next
                # ``d``. Without a callback, _on_walk_error re-raises so
                # callers like pipeline_job's repair scan (``except
                # (OSError, RuntimeError)``) keep their loud failure
                # semantics — we deliberately don't catch that raise.
                #
                # Consume the generator directly rather than materializing
                # with ``list(...)``: on a slow network mount the scandir
                # walk that backs safe_iter_dir takes minutes for a full
                # day-folder, and materializing here would delay every
                # heartbeat below until after that wait — the same silent
                # hang the heartbeat exists to break (PR #1385 Codex /
                # CodeRabbit review).
                for f in safe_iter_dir(str(dp), onerror=_on_walk_error):
                    _check_cancelled()
                    checked += 1
                    if checked % 500 == 0 and status_callback:
                        _emit_status(
                            f"Discovering files... ({len(image_files)} found)"
                        )
                    if (f.is_file()
                            and f.suffix.lower() in SUPPORTED_EXTENSIONS
                            and not f.name.startswith(".")
                            and (skip_paths is None or str(f) not in skip_paths)
                            and (restrict_files_set is None
                                 or str(f) in restrict_files_set)):
                        image_files.append(f)
    else:
        if recursive:
            checked = 0
            # safe_scan_walk replaces os.walk + prune_scan_dirs. It excludes
            # other-app data bundles (e.g. "Photos Library.photoslibrary"
            # sitting in ~/Pictures) without ever stat-following a symlink
            # to one — os.walk's classification call follows symlinks and
            # would re-trip the macOS "access data from other apps" TCC
            # prompt for a child like ``LibraryAlias -> Photos
            # Library.photoslibrary`` before prune_scan_dirs could reject it.
            # Pass cancel_check into safe_scan_walk so it polls between
            # scandir entries as well: a single very large directory (a
            # media dump with 1M+ files) can otherwise consume the whole
            # ``os.scandir`` loop before yielding, leaving the per-yield
            # ``_check_cancelled()`` below unreachable until the walker
            # finishes filling its dirs/nondirs lists.
            for dirpath, _dirnames, filenames in safe_scan_walk(
                str(root_path), onerror=_on_walk_error, cancel_check=cancel_check,
            ):
                _check_cancelled()
                for name in filenames:
                    checked += 1
                    if checked % 100 == 0:
                        _check_cancelled()
                    if checked % 500 == 0 and status_callback:
                        _emit_status(
                            f"Discovering files... ({len(image_files)} found)"
                        )
                    ext = os.path.splitext(name)[1].lower()
                    if (ext in SUPPORTED_EXTENSIONS
                            and not name.startswith(".")):
                        full = os.path.join(dirpath, name)
                        if skip_paths is not None and full in skip_paths:
                            continue
                        # os.walk includes broken symlinks in `filenames`,
                        # but the pre-pass below calls image_path.stat()
                        # which would raise FileNotFoundError and abort
                        # the whole scan. The previous Path.rglob path
                        # filtered these via is_file(); preserve that.
                        # os.path.isfile follows symlinks and returns
                        # False (not raise) for dangling targets.
                        if not os.path.isfile(full):
                            continue
                        image_files.append(Path(full))
        else:
            # safe_iter_dir mirrors iterdir() but drops excluded bundle
            # children before the is_file() filter below would stat
            # them. A normal root like ~/Pictures can hold ``Photos
            # Library.photoslibrary`` (or a symlink to one) as a direct
            # child; a bare iterdir + is_file() would stat that bundle
            # and re-trip the macOS "access data from other apps" TCC
            # prompt this guard exists to avoid, even though the
            # extension filter would have rejected it afterwards.
            # Permission denials route through _on_walk_error (callback
            # → empty entries, no callback → re-raise) the same way the
            # restrict_dirs branch above does.
            entries = list(safe_iter_dir(str(root_path), onerror=_on_walk_error))
            for checked, f in enumerate(entries, 1):
                if checked % 100 == 0:
                    _check_cancelled()
                if checked % 500 == 0 and status_callback:
                    _emit_status(
                        f"Discovering files... ({len(image_files)} found)"
                    )
                if (f.is_file()
                        and f.suffix.lower() in SUPPORTED_EXTENSIONS
                        and not f.name.startswith(".")
                        and (skip_paths is None or str(f) not in skip_paths)):
                    image_files.append(f)
    image_files.sort()
    _check_cancelled()

    # Excluded frozen paths were part of the caller's promised manifest.
    # Account for them as vanished work so progress still reaches that
    # frozen denominator and import-in-place surfaces a partial-source
    # failure instead of silently succeeding on a smaller queue.
    total = len(image_files) + excluded_frozen
    counts["discovered"] = total
    counts["vanished"] = excluded_frozen
    log.info("Found %d images in %s", total, root)
    if progress_callback:
        progress_callback(excluded_frozen, total)

    # Build existing photo lookup for incremental mode
    existing_photos = {}
    existing_file_hashes = {}
    exif_extracted = set()  # photo IDs where ExifTool has already run
    summary_needs_extract = set()  # rows needing Phase-1 EXIF summary re-extraction
    if incremental:
        all_photos = db.get_photos(per_page=999999)
        for p in all_photos:
            # Key by folder_id + filename won't work easily, so use a second lookup
            existing_photos[p["id"]] = p
        existing_file_hashes = {
            row["id"]: row["file_hash"]
            for row in db.conn.execute("SELECT id, file_hash FROM photos")
        }
        # Path-based lookup keyed by absolute path, driving the
        # incremental "skip on mtime" fast path. Queried GLOBALLY (not
        # workspace-scoped, unlike ``db.get_photos()`` / ``db.get_folder_tree()``)
        # so a twin already in the catalog still hits the skip path even
        # when its folder is not yet linked as a ``workspace_folders``
        # row in the active workspace. That is exactly the case the
        # import job's duplicate-folder link scan exists to repair:
        # scoping the lookup to the active workspace re-opened and
        # re-hashed every already-known twin just to link the folder
        # (PR #1385 Codex review). The sibling lookups above
        # (``existing_file_hashes``, ``exif_extracted``,
        # ``summary_needs_extract`` below) are already global for the
        # same reason.
        existing_by_path = {}
        folder_paths = {
            row["id"]: row["path"]
            for row in db.conn.execute("SELECT id, path FROM folders")
        }
        for row in db.conn.execute(
            "SELECT id, folder_id, filename, extension, file_size, "
            "file_mtime, xmp_mtime, timestamp, width, height FROM photos"
        ):
            folder_path = folder_paths.get(row["folder_id"], "")
            if not folder_path:
                continue
            existing_by_path[os.path.join(folder_path, row["filename"])] = row
        # Track which photos have had ExifTool metadata extracted (exif_data
        # is non-NULL). Photos with NULL exif_data need re-extraction.
        for row in db.conn.execute("SELECT id FROM photos WHERE exif_data IS NOT NULL"):
            exif_extracted.add(row["id"])
        # Rows whose Phase-1 promoted EXIF columns (camera_make etc.) were
        # never populated need one re-extraction so those universal filter
        # fields work. The DB migration clears the ``'{}'`` marker to NULL
        # for photos scanned before Phase 1 shipped, and this query picks
        # them up on the next incremental pass. Without this trigger the
        # standard ``metadata_missing`` check would still skip these rows
        # (their timestamp is populated, so none of the existing reasons —
        # repair mode, missing timestamp, suspect dims — fire). After
        # re-extraction the scanner writes the promoted columns and
        # rewrites ``exif_data`` (JSON or ``'{}'`` marker), so the row no
        # longer matches this query — no perpetual retry.
        for row in db.conn.execute(
            "SELECT id FROM photos WHERE exif_data IS NULL "
            "AND camera_make IS NULL AND camera_model IS NULL "
            "AND lens IS NULL AND aperture IS NULL "
            "AND shutter_speed IS NULL AND iso IS NULL"
        ):
            summary_needs_extract.add(row["id"])

    # Build folder cache: path -> folder_id
    folder_cache = {}

    # When the scan is restricted to specific subfolders, those subfolders —
    # not the broad scan root — are the user-facing workspace roots. A
    # templated copy-import lands files in ``<destination>/<template>/...``
    # dirs and passes those leaf dirs as ``restrict_dirs`` while keeping the
    # destination base as ``root`` only for parent-chain creation. Promoting
    # the base to a workspace root would make the new-images walk treat every
    # un-imported sibling under it as "new" (e.g. a whole archive of past
    # shoots sharing the destination). Mark the restricted dirs as roots
    # instead; the base stays linked but is_root=0. See
    # ``new_images.mapped_roots``. ``effective_restrict_dirs`` (bundle-filtered)
    # is the set actually enumerated, so root marking matches what was scanned.
    _effective_restrict_paths = None
    _restrict_root_paths = None
    if restrict_dirs is not None:
        _effective_restrict_paths = {
            os.path.normpath(str(d)) for d in effective_restrict_dirs
        }
        _restrict_root_paths = (
            _effective_restrict_paths
            if register_restrict_dirs_as_roots else set()
        )

    def _ensure_folder(folder_path):
        """Ensure a folder and all its parents exist in the DB. Returns folder_id."""
        folder_str = str(folder_path)
        if folder_str in folder_cache:
            return folder_cache[folder_str]

        parent_id = None
        if folder_path != root_path:
            parent_id = _ensure_folder(folder_path.parent)

        if _restrict_root_paths is not None:
            is_ws_root = os.path.normpath(folder_str) in _restrict_root_paths
        else:
            is_ws_root = (folder_path == root_path)

        # In restricted mode, the scan root (``root_path``) and every
        # intermediate ancestor between it and ``restrict_dirs`` exist
        # only to satisfy the ``folders.parent_id`` chain — they are not
        # part of what the user asked to import. Linking them to the
        # workspace would fire ``_add_workspace_folder_no_commit`` and
        # its path-prefix subtree cascade, pulling every pre-existing
        # cataloged descendant of ``root_path`` (unrelated archive
        # subtrees from prior scans / other workspaces) into the active
        # workspace. Only the ``_restrict_root_paths`` themselves should
        # link. See PR #1107 review (line 1186).
        normalized_folder = os.path.normpath(folder_str)
        is_restrict_target = (
            _effective_restrict_paths is not None
            and normalized_folder in _effective_restrict_paths
        )
        link_to_ws = (
            _effective_restrict_paths is None
            or (register_restrict_dirs_as_roots and is_restrict_target)
        )

        folder_id = db.add_folder(
            path=folder_str,
            name=folder_path.name,
            parent_id=parent_id,
            workspace_root=is_ws_root,
            link_to_workspace=link_to_ws,
        )
        if (
            is_restrict_target
            and not register_restrict_dirs_as_roots
            and db._active_workspace_id is not None
        ):
            # Snapshot imports and metadata repair select exact leaf folders
            # below an existing workspace root. Link only that leaf: the
            # regular subtree-linking API would also attach unrelated known
            # descendants that this restricted scan never touched.
            db.add_workspace_folder_exact(
                db._active_workspace_id, folder_id, is_root=False,
            )
        folder_cache[folder_str] = folder_id
        return folder_id

    # Track folders whose scan touched them (so we can flag them 'partial'
    # if anything between the pre-pass and scan completion dies midway) and
    # the outer scan scope as a fallback. The scope matters when
    # ``touched_folder_ids`` is empty — e.g. a pre-pass XMP commit that
    # aborts before the main loop has added any folder, or a successful
    # no-op incremental scan that processes zero files.
    touched_folder_ids = set()
    # Photo IDs whose derived caches were invalidated this scan. Collected
    # so the untracked-preview sweep can run once as a batch instead of
    # per-photo (avoids O(N × M) directory walks on large rescans).
    invalidated_photo_ids: set[int] = set()
    # Photo IDs that already own cached derivative files, for the
    # recycled-rowid check on insert. Snapshotted once (lazily, on the
    # first insert) for the same batching reason as the sweep above.
    recycled_id_index = RecycledIdIndex(
        thumb_cache_dir or (
            os.path.join(vireo_dir, "thumbnails") if vireo_dir else ""
        ),
        vireo_dir=vireo_dir,
    )
    scoped_paths = {str(root_path)}
    if restrict_dirs is not None:
        scoped_paths.update(str(d) for d in restrict_dirs)

    def _update_folder_status(new_status, only_from_partial):
        """Stamp folders in the scan scope with ``new_status``.

        Applies to every folder matched by ``scoped_paths`` (outer roots)
        OR ``touched_folder_ids`` (folder rows the main loop has reached).
        When ``only_from_partial`` is True, restricts the UPDATE to rows
        already in ``'partial'`` — used on the success path so completed
        scans don't clobber ``'missing'`` or future statuses.
        """
        guard = " AND status = 'partial'" if only_from_partial else ""
        if scoped_paths:
            path_placeholders = ",".join("?" * len(scoped_paths))
            db.conn.execute(
                f"UPDATE folders SET status = ? "
                f"WHERE path IN ({path_placeholders}){guard}",
                (new_status, *scoped_paths),
            )
        if touched_folder_ids:
            id_placeholders = ",".join("?" * len(touched_folder_ids))
            db.conn.execute(
                f"UPDATE folders SET status = ? "
                f"WHERE id IN ({id_placeholders}){guard}",
                (new_status, *touched_folder_ids),
            )
        commit_with_retry(db.conn)

    # First pass: determine which files need full processing (for incremental mode).
    # Handle XMP-only changes inline; collect files needing metadata extraction.
    files_to_process = []
    processed_count = excluded_frozen
    # ``processed_count`` advances for every file the scan *disposes of*,
    # including ones it deliberately skips — that is what a progress bar
    # needs to reach 100%. It is NOT the number of photos indexed, and the
    # two diverge exactly when something is wrong (a share that unmounts
    # mid-scan makes every remaining file vanish). Every ``processed_count``
    # bump below is therefore paired with a bump of exactly one bucket in
    # ``counts``, so the summary reports what actually landed in the
    # catalog. Classifying at each site (rather than deriving one bucket by
    # subtraction) means a disposition added later has to state which
    # bucket it belongs to instead of silently defaulting to "indexed" —
    # the invariant check after the loop enforces it.
    #
    # The ids behind ``counts["indexed"]``. Needed because the companion
    # pairing pass below runs against the entire photos table, so its
    # merges must be intersected with what *this* invocation counted (see
    # ``_pair_raw_jpeg_companions``).
    indexed_photo_ids = set()
    try:
        # Eagerly register the explicit scan targets so they end up linked
        # to the active workspace even when zero photos are inserted (e.g.
        # every file is in skip_paths because the photos are already in
        # the global photos table). Without this, importing a folder
        # whose contents are already known silently skipped the
        # workspace_folders link and the folder never became visible in
        # the active workspace. Inside the try so a DB failure here still
        # routes through the partial-status recovery path.
        _ensure_folder(root_path)
        if restrict_dirs is not None:
            for d in restrict_dirs:
                dp = Path(d)
                # Same bundle guard as the discovery loop above — never
                # register or stat a path inside an other-app data bundle,
                # even when the caller stuffed one into ``restrict_dirs``.
                if is_excluded_scan_path(dp):
                    continue
                if dp.is_dir():
                    _ensure_folder(dp)

        for image_path in image_files:
            _check_cancelled()
            try:
                stat = image_path.stat()
            except OSError:
                # File deleted/renamed between discovery and this pass —
                # skip it instead of aborting the whole scan (the discovery
                # walk has the same guard for broken symlinks).
                log.info("File vanished during scan, skipping: %s", image_path)
                processed_count += 1
                counts["vanished"] += 1
                if progress_callback:
                    progress_callback(processed_count, total)
                continue
            file_mtime = stat.st_mtime
            xmp_path = image_path.with_suffix(".xmp")
            try:
                xmp_mtime = xmp_path.stat().st_mtime
            except OSError:
                # Covers both "no sidecar" and a sidecar deleted between
                # exists() and stat() — same outcome either way.
                xmp_mtime = None

            if incremental:
                full_path_str = str(image_path)
                existing = existing_by_path.get(full_path_str)
                if existing:
                    file_unchanged = existing["file_mtime"] == file_mtime
                    xmp_unchanged = existing["xmp_mtime"] == xmp_mtime
                    # Re-process if ExifTool never ran for this photo (both
                    # timestamp and exif_data are NULL). Photos with genuinely
                    # missing timestamps (screenshots, exports) will have
                    # exif_data set after one extraction attempt.
                    # Also flag rows where a RAW file has absurdly small
                    # dimensions (<1000px) — that's the embedded JPEG thumb
                    # leaking through when ExifTool's File group was missing
                    # on the original scan.
                    dims_suspect = (
                        existing["extension"] in RAW_EXTENSIONS
                        and existing["width"] is not None
                        and existing["width"] < 1000
                    )
                    metadata_missing = (
                        existing["id"] not in exif_extracted
                        and (
                            repair_missing_metadata
                            or existing["timestamp"] is None
                            or dims_suspect
                        )
                    ) or existing["id"] in summary_needs_extract
                    existing_file_hash = existing_file_hashes.get(existing["id"])
                    empty_hash_needs_repair = (
                        existing["file_size"] == 0
                        and existing_file_hash == EMPTY_FILE_SHA256
                    )

                    if (
                        file_unchanged and xmp_unchanged
                        and not metadata_missing
                        and not empty_hash_needs_repair
                    ):
                        processed_count += 1
                        counts["indexed"] += 1
                        indexed_photo_ids.add(existing["id"])
                        if photo_callback:
                            photo_callback(existing["id"], full_path_str)
                        if progress_callback:
                            progress_callback(processed_count, total)
                        continue

                    # XMP changed: re-import keywords
                    if not xmp_unchanged and xmp_mtime is not None:
                        _import_keywords_for_photo(db, existing["id"], str(xmp_path))
                        db.conn.execute(
                            "UPDATE photos SET xmp_mtime = ? WHERE id = ?",
                            (xmp_mtime, existing["id"]),
                        )
                        commit_with_retry(db.conn)
                    elif not xmp_unchanged:
                        # Sidecar deleted: clear the stored mtime so the row
                        # converges instead of looking "XMP changed" on every
                        # later scan (this skip path never reaches the main
                        # loop, so nothing else would ever reset it).
                        db.conn.execute(
                            "UPDATE photos SET xmp_mtime = NULL WHERE id = ?",
                            (existing["id"],),
                        )
                        commit_with_retry(db.conn)

                    if (
                        file_unchanged
                        and not metadata_missing
                        and not empty_hash_needs_repair
                    ):
                        processed_count += 1
                        counts["indexed"] += 1
                        indexed_photo_ids.add(existing["id"])
                        if photo_callback:
                            photo_callback(existing["id"], full_path_str)
                        if progress_callback:
                            progress_callback(processed_count, total)
                        continue

            files_to_process.append(image_path)
    except BaseException:
        # Pre-pass died (e.g. non-retryable DB error on an XMP commit).
        # Route through the same partial-status path as a main-loop failure
        # so users see the badge and can rescan.
        try:
            db.conn.rollback()
        except Exception:
            log.exception("Rollback after pre-pass failure also failed")
        try:
            _update_folder_status("partial", only_from_partial=False)
        except Exception:
            log.exception("Failed to flag folders partial after pre-pass failure")
        raise

    # Batch extract metadata via ExifTool only for files that need processing
    paths_to_extract = [str(ip) for ip in files_to_process]
    if paths_to_extract and status_callback:
        metadata_total = len(paths_to_extract)
        _emit_status(
            f"Extracting metadata (0 / {metadata_total} files)...",
            phase_current=0,
            phase_total=metadata_total,
            phase_label="Extracting metadata",
        )
    _check_cancelled()

    def _metadata_progress(current, total):
        _emit_status(
            f"Extracting metadata ({current} / {total} files)...",
            phase_current=current,
            phase_total=total,
            phase_label="Extracting metadata",
        )

    metadata_map = (
        extract_metadata(
            paths_to_extract,
            progress_callback=_metadata_progress,
            checkpoint=_check_cancelled,
        )
        if paths_to_extract else {}
    )
    _check_cancelled()

    # Compute phash + file_hash in parallel across all files that need
    # processing. These are the two per-file operations that actually read
    # every byte of the image; everything else in the loop is cheap DB or
    # dict work. Results stream in order, so workers keep computing the tail
    # while the main thread commits the head — no O(n) buffer of features.
    def _pause_pending():
        return pause_check is not None and pause_check()

    def _check_cancelled_no_park():
        """Cancel probe safe to call INSIDE ``_claim_worker_count``.

        Never parks. When ``cancel_only_check`` was supplied the
        pause/cancel probes are strictly non-parking, so a Pause
        arriving between the two calls raises ``_ScanPauseRequested``
        instead of triggering the caller's parking ``cancel_check``.
        That lets the enclosing frame drain workers and release CPU
        permits before parking on the outer ``_check_cancelled`` call.

        When ``cancel_only_check`` is not supplied this falls back to
        the pause-then-parking-cancel sequence with the race window
        Codex flagged — pause set between the two calls parks the job
        while it still holds the lease. Not worse than the pre-fix
        behavior, and every pipeline caller now wires the non-parking
        probe.
        """
        if _pause_pending():
            raise _ScanPauseRequested()
        if cancel_only_check is not None:
            if cancel_only_check():
                raise ScanCancelled("scan cancelled")
            return
        _check_cancelled()

    def _iter_features():
        if not files_to_process:
            return
        # Track remaining work outside the ``_claim_worker_count`` context so
        # a pause can drop the lease, park at the caller's pause-aware
        # ``cancel_check``, then reacquire a fresh lease for whatever is
        # left. Without this, ``_check_cancelled`` parking inside the pool
        # loop would hold every CPU permit for the entire pause — blocking
        # replacement scans, CPU inference, and model loads even though the
        # user asked this job to stop competing for resources.
        remaining = deque(
            zip(files_to_process, paths_to_extract, strict=True),
        )
        while remaining:
            # A standalone caller may provide a non-parking cancel probe.
            # Do not repeatedly claim permits and construct a process pool
            # while its independent pause probe remains true.
            if _pause_pending():
                _check_cancelled()
                if _pause_pending():
                    time.sleep(0.05)
                continue
            with _claim_worker_count(
                [ip for ip, _ in remaining],
                cancel_check=(
                    _check_cancelled if cancel_check is not None else None
                ),
            ) as workers:
                if status_callback:
                    _emit_status(
                        f"Hashing {len(remaining)} files "
                        f"({workers} worker{'s' if workers != 1 else ''})..."
                    )
                log.info(
                    "Scanner hashing granted %d worker(s) for %d files",
                    workers, len(remaining),
                )
                if workers > 1:
                    mp_ctx = multiprocessing.get_context(_SCAN_MP_METHOD)
                    pool = ProcessPoolExecutor(max_workers=workers, mp_context=mp_ctx)
                    paused = False
                    try:
                        # Bounded in-flight window instead of pool.map(): on Python
                        # 3.11 Executor.map eagerly submits every input, so on a
                        # 200k-file scan we would hold 200k queued futures in RAM.
                        # A few submissions per worker is enough to keep them fed
                        # while the main thread drains results in order.
                        #
                        # Pause is checked BEFORE ``_check_cancelled`` at every
                        # boundary. The pipeline's ``cancel_check`` parks on a
                        # pending pause, so calling it first would trap the
                        # scanner inside the lease context; raising
                        # ``_ScanPauseRequested`` while the pool is still
                        # running lets the enclosing frame drain workers and
                        # release CPU permits before we park.
                        def _feature_result(fut):
                            while True:
                                # ``_check_cancelled_no_park`` raises
                                # ``_ScanPauseRequested`` on pending pause
                                # and only calls the parking probe if it
                                # cannot be avoided (no ``cancel_only_check``
                                # supplied). Closes the race Codex flagged
                                # where ``_pause_pending`` returns False,
                                # Pause fires, and the previously-called
                                # ``_check_cancelled`` parked while still
                                # inside the lease and pool.
                                _check_cancelled_no_park()
                                try:
                                    return fut.result(timeout=0.2)
                                except TimeoutError:
                                    continue

                        max_in_flight = workers * 4
                        pending = deque()
                        while remaining:
                            _check_cancelled_no_park()
                            image_path, path_str = remaining[0]
                            pending.append((
                                image_path, path_str,
                                pool.submit(_compute_file_features, path_str),
                            ))
                            remaining.popleft()
                            if len(pending) >= max_in_flight:
                                done_path, _done_str, done_fut = pending[0]
                                _check_cancelled_no_park()
                                result = _feature_result(done_fut)
                                if _pause_pending():
                                    raise _ScanPauseRequested()
                                pending.popleft()
                                yield done_path, result
                        while pending:
                            done_path, _done_str, done_fut = pending[0]
                            _check_cancelled_no_park()
                            result = _feature_result(done_fut)
                            if _pause_pending():
                                raise _ScanPauseRequested()
                            pending.popleft()
                            yield done_path, result
                    except _ScanPauseRequested:
                        # Requeue every submitted-but-undrained item so the
                        # next pass reruns them under a fresh lease. Their
                        # workers are torn down below.
                        paused = True
                        for image_path, path_str, _fut in reversed(pending):
                            remaining.appendleft((image_path, path_str))
                        # Terminate now, wait below with the lease still
                        # held so a replacement scan cannot claim the same
                        # permits while old workers are still consuming CPU.
                        for proc in list(getattr(pool, "_processes", {}).values()):
                            with contextlib.suppress(Exception):
                                proc.terminate()
                        pool.shutdown(wait=True, cancel_futures=True)
                    except BaseException:
                        # Actively terminate the worker processes so their CPU
                        # work stops now, then wait for them to actually exit
                        # before letting ``_claim_worker_count`` release its
                        # CPU permits. ``shutdown(wait=False)`` alone would
                        # unwind the lease while old workers were still
                        # hashing — a replacement scan or CPU inference could
                        # then receive the same permits and defeat the
                        # process-wide budget, and ``JobRunner.shutdown()``
                        # could report completion while hashing continued.
                        for proc in list(getattr(pool, "_processes", {}).values()):
                            with contextlib.suppress(Exception):
                                proc.terminate()
                        pool.shutdown(wait=True, cancel_futures=True)
                        raise
                    else:
                        pool.shutdown(wait=True)
                else:
                    paused = False
                    while remaining:
                        # ``_check_cancelled_no_park`` raises
                        # ``_ScanPauseRequested`` on pending pause and
                        # only invokes the parking probe if no
                        # ``cancel_only_check`` was supplied. Caught and
                        # translated to ``paused = True`` so this branch
                        # keeps its "drop the lease, then park outside"
                        # symmetry with the pool branch above.
                        try:
                            _check_cancelled_no_park()
                        except _ScanPauseRequested:
                            paused = True
                            break
                        image_path, path_str = remaining[0]
                        result = _compute_file_features(path_str)
                        if _pause_pending():
                            paused = True
                            break
                        remaining.popleft()
                        yield image_path, result
            # Lease is released here (``_claim_worker_count`` exited).
            # Park until the caller resumes (or cancels) before rebuilding
            # the pool with the remaining files. ``_check_cancelled`` is
            # pause-aware: when the pipeline is pausing, ``cancel_check``
            # blocks inside it until resume, and raises ``ScanCancelled``
            # if the pause turns into a cancellation.
            if paused:
                _check_cancelled()
        # The final result is consumed while this generator is suspended.
        # Check once more only after the last lease has unwound so a pause or
        # cancellation arriving during that consumer work cannot strand the
        # pool and its CPU permits.
        _check_cancelled()

    try:
        for image_path, (phash, file_hash) in _iter_features():
            # File stats — first touch of the path in this loop. A file
            # deleted/renamed between discovery and here must skip, not
            # abort the scan and flag every folder in scope 'partial'.
            try:
                stat = image_path.stat()
            except OSError:
                log.info("File vanished during scan, skipping: %s", image_path)
                processed_count += 1
                counts["vanished"] += 1
                if progress_callback:
                    progress_callback(processed_count, total)
                continue

            folder_id = _ensure_folder(image_path.parent)
            touched_folder_ids.add(folder_id)
            file_size = stat.st_size
            file_mtime = stat.st_mtime
            if file_size == 0 and file_hash == EMPTY_FILE_SHA256:
                log.warning(
                    "Empty image file detected; skipping duplicate identity hash: %s",
                    image_path,
                )
                file_hash = None

            # XMP sidecar
            xmp_path = image_path.with_suffix(".xmp")
            try:
                xmp_mtime = xmp_path.stat().st_mtime
            except OSError:
                xmp_mtime = None

            # Get pre-extracted metadata for this file
            file_meta = metadata_map.get(str(image_path), {})
            file_group = file_meta.get("File", {})
            exif_group = file_meta.get("EXIF", {})
            composite = file_meta.get("Composite", {})

            # Dimensions from ExifTool (works for all file types including RAW)
            width, height = _extract_dimensions(exif_group, file_group, extension=image_path.suffix.lower())

            # Fallback if ExifTool didn't provide dimensions
            if width is None or height is None:
                ext = image_path.suffix.lower()
                if ext in RAW_EXTENSIONS:
                    try:
                        import rawpy

                        with rawpy.imread(str(image_path)) as raw:
                            width = raw.sizes.width
                            height = raw.sizes.height
                    except Exception:
                        log.debug("Could not read RAW dimensions from %s", image_path)
                else:
                    try:
                        with Image.open(str(image_path)) as img:
                            width, height = img.size
                    except Exception:
                        log.debug("Could not read dimensions from %s", image_path)

            # Timestamp from ExifTool
            timestamp = _extract_timestamp(exif_group)

            # Focal length is written via the EXIF summary columns loop
            # below (see ``EXIF_SUMMARY_COLUMNS``) so a rescan that loses
            # the tag clears the column instead of leaving a stale value.

            # Burst ID (ImageUniqueID)
            burst_id = exif_group.get("ImageUniqueID")
            if burst_id:
                burst_id = str(burst_id)

            # GPS coordinates — ExifTool with -n gives decimal degrees directly
            latitude = composite.get("GPSLatitude")
            if latitude is None:
                latitude = exif_group.get("GPSLatitude")
            longitude = composite.get("GPSLongitude")
            if longitude is None:
                longitude = exif_group.get("GPSLongitude")

            # Pre-check: capture prior content identity AND whether the
            # row existed before add_photo touches it. Existing rows take
            # the content-change invalidation below; brand-new rows take
            # the cheaper recycled-rowid probe right after the insert, so
            # a large initial scan doesn't pay for O(N) UPDATE + commit
            # round-trips it has no reason to make.
            existing_row = db.conn.execute(
                "SELECT file_hash, flag FROM photos WHERE folder_id = ? AND filename = ?",
                (folder_id, image_path.name),
            ).fetchone()
            row_already_existed = existing_row is not None
            prev_file_hash = existing_row["file_hash"] if existing_row else None

            # Process may refresh metadata for cataloged photos, but it must
            # never admit a filesystem path as a side effect. A row can
            # disappear between repair-scope resolution and this point; skip
            # that race rather than recreating it through add_photo().
            if not allow_photo_inserts and not row_already_existed:
                log.info(
                    "Update-only scan skipped uncataloged file: %s", image_path,
                )
                processed_count += 1
                counts["skipped_uncataloged"] += 1
                if progress_callback:
                    progress_callback(processed_count, total)
                continue

            photo_id = db.add_photo(
                folder_id=folder_id,
                filename=image_path.name,
                extension=image_path.suffix.lower(),
                file_size=file_size,
                file_mtime=file_mtime,
                xmp_mtime=xmp_mtime,
                timestamp=timestamp,
                width=width,
                height=height,
            )
            # Credit the photo the moment its row is durable — add_photo
            # commits before returning. Several fallible steps run below
            # (cache invalidation, XMP keyword import, duplicate
            # auto-resolve, photo_callback), and one of them raising must
            # not leave the sink reporting fewer photos than the catalog
            # actually holds. ``processed_count`` stays at the end of the
            # iteration: it drives the progress bar, which should only
            # advance once the file is genuinely done with.
            counts["indexed"] += 1
            indexed_photo_ids.add(photo_id)

            # A brand-new row may have claimed a *recycled* rowid (see
            # ``purge_cached_files_for_recycled_id``). Cached derivatives
            # from the id's previous owner would otherwise be served as
            # this photo's — a wrong-bird Life List card. The index makes
            # this an O(1) set lookup per insert; only ids that actually
            # collide do real work.
            if (
                not row_already_existed
                and vireo_dir
                and purge_cached_files_for_recycled_id(
                    thumb_cache_dir or os.path.join(vireo_dir, "thumbnails"),
                    photo_id,
                    id_index=recycled_id_index,
                    vireo_dir=vireo_dir,
                    db=db,
                    file_mtime=file_mtime,
                )
            ):
                invalidated_photo_ids.add(photo_id)

            # Update metadata columns (also fixes existing photos that were
            # inserted before ExifTool metadata was available)
            updates = []
            update_params = []
            if timestamp is not None:
                updates.append("timestamp=?")
                update_params.append(timestamp)
            if width is not None:
                updates.append("width=?")
                update_params.append(width)
            if height is not None:
                updates.append("height=?")
                update_params.append(height)
            if latitude is not None:
                updates.extend(["latitude=?", "longitude=?"])
                update_params.extend([latitude, longitude])
            if phash is not None:
                updates.append("phash=?")
                update_params.append(phash)
            if burst_id is not None:
                updates.append("burst_id=?")
                update_params.append(burst_id)
            if file_hash is not None:
                updates.append("file_hash=?")
                update_params.append(file_hash)
                if row_already_existed and prev_file_hash != file_hash:
                    # The stored baseline is being replaced, so any prior
                    # integrity verdict applied to bytes that no longer
                    # exist. Clear the verification markers rather than
                    # carry them forward — the audit summary must only
                    # claim "checked" for baselines verify_hashes (or an
                    # explicit user accept) actually vouched for. A rescan
                    # that recomputes the same hash leaves coverage intact.
                    updates.append("hash_checked_at=NULL")
                    updates.append("hash_status=NULL")
            elif file_size == 0:
                updates.append("file_hash=NULL")
                if row_already_existed and prev_file_hash is not None:
                    updates.append("hash_checked_at=NULL")
                    updates.append("hash_status=NULL")
                # Historical rows where the empty SHA leaked in are repaired
                # here by clearing file_hash above. We deliberately leave the
                # ``flag`` column untouched: a 'rejected' value could come
                # from the user (Browse / culling) just as easily as from
                # past duplicate auto-resolution, and we have no marker that
                # distinguishes them. Silently un-rejecting a user's
                # placeholder would be worse than leaving it as-is; the
                # duplicates page already flags empty-byte groups for
                # manual review.
            if file_meta:
                # Promoted EXIF summary columns (universal filter fields).
                # Written whenever ExifTool ran, independent of whether the
                # full JSON blob is stored below. Absent columns are cleared
                # to NULL rather than skipped so a rescan of a file whose
                # metadata lost a field (e.g. sidecar edited, replaced with a
                # different camera's file) doesn't leave stale values that
                # /api/photos/query and /api/filters/values keep matching.
                cols = exif_summary_columns(file_meta)
                for column in EXIF_SUMMARY_COLUMNS:
                    updates.append(f"{column}=?")
                    update_params.append(cols.get(column))
            if file_meta and extract_full_metadata:
                updates.append("exif_data=?")
                update_params.append(json.dumps(file_meta))
            elif file_meta:
                # Store minimal marker so we know ExifTool ran (even when
                # extract_full_metadata is off) — prevents perpetual retry
                updates.append("exif_data=COALESCE(exif_data, ?)")
                update_params.append("{}")
            if row_already_existed:
                # add_photo is INSERT OR IGNORE, so the fresh stat values it
                # was passed never reach an existing row. Without these the
                # incremental pre-pass keeps comparing against the stale
                # stored mtime and re-processes a changed file on every scan
                # forever. Only advance file_mtime/file_size when the content
                # hash succeeded: _compute_file_features hashes every
                # processed file, so file_hash is None only when the bytes
                # couldn't be read (transient permission/I-O error). Marking
                # such a file's mtime current would make the next incremental
                # scan skip it forever with a stale hash and stale derived
                # caches; leaving the old mtime in place retries it instead.
                if file_hash is not None or file_size == 0:
                    updates.extend(["file_mtime=?", "file_size=?"])
                    update_params.extend([file_mtime, file_size])
                # xmp_mtime stays unconditional — the sidecar is a separate
                # file whose keyword import below runs regardless of image
                # hash success, and it may be None here: writing NULL is
                # correct (a deleted sidecar otherwise re-trips the "XMP
                # changed" check on every scan).
                updates.append("xmp_mtime=?")
                update_params.append(xmp_mtime)
            if updates:
                update_params.append(photo_id)
                db.conn.execute(
                    f"UPDATE photos SET {', '.join(updates)} WHERE id=?",
                    update_params,
                )
                commit_with_retry(db.conn)

            # Content-change self-heal: when the computed hash differs
            # from what was stored before this scan, derived caches are
            # stale. Includes the NULL → concrete transition for legacy
            # rows that predate hash tracking — we can't prove their
            # caches match current bytes, so safer to flush and
            # regenerate. Also fires when a file is truncated to zero
            # bytes: file_hash is None in that branch (empty files don't
            # carry duplicate identity), so the plain
            # ``file_hash is not None`` guard would otherwise leave
            # thumbnails and working copies from the old bytes in place.
            # The zero-byte clause covers BOTH non-empty → empty and
            # legacy NULL → empty: a pre-hash row whose file is now
            # truncated still has thumbnails rendered from its old
            # non-empty bytes, and before this fix the previous code
            # path (which stored the concrete empty SHA) would have
            # invalidated it via the NULL → concrete branch. Skips the
            # empty → empty repair case (prev was already the empty SHA)
            # because the bytes didn't actually change. Gated on
            # ``row_already_existed`` so brand-new inserts
            # (prev_file_hash is always NULL there) don't trigger
            # pointless UPDATE + commit round-trips on large initial
            # scans. Requires explicit vireo_dir; callers must pass it
            # (scan can't guess because --db and --thumb-dir are
            # independently configurable).
            content_identity_changed = (
                file_hash is not None and prev_file_hash != file_hash
            ) or (
                file_size == 0
                and prev_file_hash != EMPTY_FILE_SHA256
            )
            if (row_already_existed
                    and content_identity_changed
                    and vireo_dir):
                _invalidate_derived_caches(
                    db, vireo_dir, photo_id, thumb_cache_dir=thumb_cache_dir,
                )
                invalidated_photo_ids.add(photo_id)
                commit_with_retry(db.conn)

            # Import XMP keywords if sidecar exists — must land BEFORE the
            # duplicate auto-resolve hook below, so if this row turns out to be
            # the loser its keywords are visible to apply_duplicate_resolution's
            # metadata query and get merged onto the winner. Otherwise the
            # keywords would be stranded on the rejected row.
            if xmp_path.exists():
                _import_keywords_for_photo(db, photo_id, str(xmp_path))

            # Trigger duplicate auto-resolve now that file_hash AND XMP keywords
            # are committed. add_photo was called without the hash, so the hook
            # there was a no-op — we own firing it here.
            if file_hash is not None:
                db.check_and_resolve_duplicates_for_hash(file_hash)

            if photo_callback:
                photo_callback(photo_id, str(image_path))

            processed_count += 1
            if progress_callback:
                progress_callback(processed_count, total)
    except BaseException:
        # Per-file loop died mid-way (DB error, signal, etc). Roll back any
        # half-applied write so the partial-status UPDATE below runs on a
        # clean transaction, then flag every folder in scope as 'partial' so
        # callers can detect and re-scan.
        try:
            db.conn.rollback()
        except Exception:
            log.exception("Rollback after scan failure also failed")
        try:
            _update_folder_status("partial", only_from_partial=False)
        except Exception:
            log.exception("Failed to flag folders partial after scan failure")
        raise
    else:
        # Per-file loop completed cleanly. Clear any stale 'partial' flag on
        # scanned folders so a successful rescan restores full visibility.
        # Uses both the scan scope (root + restrict_dirs) AND the touched
        # folder ids: a successful no-op incremental scan has an empty
        # ``touched_folder_ids`` set but must still clear the badge for the
        # roots the user asked us to scan; a recursive scan that failed and
        # then succeeds needs the touched ids to reach touched subfolders.
        try:
            _update_folder_status("ok", only_from_partial=True)
        except Exception:
            log.exception("Failed to clear partial flag after scan success")

    # Pair raw+JPEG companions: raw is primary, JPEG becomes companion_path.
    # Wrap post-processing so folder counts are always updated, even on failure.
    # On exception, roll back any uncommitted partial writes before updating
    # counts — otherwise update_folder_counts()'s commit would persist
    # half-applied pairing or working-copy records.
    try:
        # A JPEG merged into its RAW's row stops being its own photo, so
        # discount it: both files were counted on the way in but only the
        # RAW survives. Applied to the sink immediately (not just the
        # returned dict) so a caller reading counts after a later failure
        # in this block still sees the corrected number.
        _merged_ids = _pair_raw_jpeg_companions(
            db, vireo_dir=vireo_dir, thumb_cache_dir=thumb_cache_dir,
        )
        # Discount only companions THIS scan counted. The pairing pass
        # queries the whole photos table, so it also cleans up pairs left
        # pending elsewhere in the catalog (an interrupted earlier scan,
        # an older build). Debiting a scoped scan for those would make it
        # undercount, and scanning a root with no new files while one
        # stale pair was pending would report -1 photos indexed.
        _mine = _merged_ids & indexed_photo_ids
        counts["merged_companions"] += len(_mine)
        counts["indexed"] -= len(_mine)

        # Extract working copies for RAW photos (after pairing so companion is known).
        # Scope to the folders the caller just scanned so a fresh import doesn't
        # trigger library-wide backfill for every pre-existing large JPEG.
        # Match-mode mirrors what scan() actually traversed: restrict_dirs and
        # non-recursive scans only touch direct children, so the scope uses an
        # exact-folder match; a recursive walk from `root` matches the subtree.
        #
        # Deliberately do NOT forward ``progress_callback`` here: callers
        # like app.py's import job feed the scan callback into a shared
        # ``job["progress"]`` slot that gates downstream phase totals
        # (``scan_count = job["progress"]["total"]``). Emitting working-copy
        # (current, total) through the same callback would overwrite the
        # scan total with the working-copy total and visually jump the bar
        # backward. ``status_callback`` still announces the phase.
        if vireo_dir and not skip_working_copies:
            if restrict_dirs is not None:
                # Use the bundle-filtered list — see ``effective_restrict_dirs``
                # above. Reusing the raw ``restrict_dirs`` here would let a
                # stale DB row inside an excluded bundle re-enter the
                # working-copy extractor, which reads ``folder_path/filename``
                # and re-trips the macOS TCC prompt the scan loop's guard
                # already skipped.
                wc_scope = [(str(d), "exact") for d in effective_restrict_dirs]
            elif not recursive:
                wc_scope = [(str(root_path), "exact")]
            else:
                wc_scope = [str(root_path)]
            _extract_working_copies(
                db, vireo_dir,
                progress_callback=None,
                status_callback=status_callback,
                scope=wc_scope,
                cancel_check=cancel_check,
            )

        # Batched untracked-preview sweep. One os.listdir(previews/) for
        # the whole scan instead of one per invalidated photo — essential
        # when a rescan touches thousands of content-changed files.
        if invalidated_photo_ids:
            _sweep_untracked_previews_for_photos(
                db, vireo_dir, invalidated_photo_ids,
            )
    except BaseException:
        db.conn.rollback()
        raise
    finally:
        db.update_folder_counts()

    # Every file the loop disposed of landed in exactly one bucket. If this
    # ever trips, a new disposition was added without classifying it, and
    # the summary below would silently over-claim by that many photos —
    # log it rather than raising, since the scan's real work is committed
    # and a miscounted summary is no reason to fail the run.
    vanished_count = counts["vanished"]
    skipped_uncataloged_count = counts["skipped_uncataloged"]
    merged_count = counts["merged_companions"]
    indexed_count = counts["indexed"]
    accounted = (
        indexed_count + vanished_count + skipped_uncataloged_count
        + merged_count
    )
    if accounted != processed_count:
        log.error(
            "Scan count invariant broken: indexed=%d + vanished=%d + "
            "skipped=%d + merged=%d != processed=%d (a file disposition is "
            "unclassified)",
            indexed_count, vanished_count, skipped_uncataloged_count,
            merged_count, processed_count,
        )

    # Report what reached the catalog, not what the walk turned up. These
    # used to be the same number ("Scan complete: %d photos indexed" logged
    # ``total``), which reads as a success line and stays reassuring
    # precisely when the scan achieved nothing — an archive share that
    # dropped mid-scan logged "984 photos indexed" having indexed zero.
    summary = f"Scan complete: {indexed_count} photos indexed"
    if merged_count:
        summary += f", {merged_count} JPEG(s) merged into their RAW"
    if vanished_count:
        summary += f", {vanished_count} vanished"
    if skipped_uncataloged_count:
        summary += f", {skipped_uncataloged_count} uncataloged (skipped)"
    if merged_count or vanished_count or skipped_uncataloged_count:
        summary += f" of {total} discovered"
    log.info(summary)
    return counts
