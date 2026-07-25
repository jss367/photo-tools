"""Shared helpers for the preview_cache LRU.

The eviction pass is called from three places: the Flask request path
(``_serve_preview``), the startup migration in ``create_app``, and the
pipeline job's preview stage. Keeping the logic here avoids having the
pipeline import from ``app`` (which would be circular) or duplicating
the loop in two modules.
"""

import glob as _glob
import logging
import os

log = logging.getLogger(__name__)


def cleanup_cached_files_for_deleted_photos(
    thumb_cache_dir, files, progress_callback=None,
):
    """Remove thumbnail, preview, working-copy, and display files for deleted photos.

    ``files`` is the list returned by ``db.delete_photos`` /
    ``db.delete_folder``. The FK cascade drops preview_cache rows when
    photos are deleted, but the on-disk files stay unless we unlink
    them here — otherwise they leak into untracked bytes that eviction
    can't see, and on SQLite a retry that reuses one of the just-freed
    photo IDs would treat the stale ``{photo_id}.jpg`` as a valid
    thumbnail and skip regenerating it.

    Note: if an unlink fails (e.g. file locked on Windows), the file
    remains on disk as an orphan because the cascade has already removed
    the preview_cache row. "Clear cache" in Settings recovers by globbing
    the directory.
    """
    vireo_dir = os.path.dirname(thumb_cache_dir)
    preview_dir = os.path.join(vireo_dir, "previews")
    working_dir = os.path.join(vireo_dir, "working")
    originals_dir = os.path.join(vireo_dir, "originals")
    masks_dir = os.path.join(vireo_dir, "masks")
    edit_masks_dir = os.path.join(vireo_dir, "edit-masks")
    # Offline-cache layout: offline/{originals,xmp,companions}/{pid}{ext}.
    # The FK cascade drops the offline_originals row when the photo is
    # deleted, so we lose the exact stored paths — glob by photo id to
    # cover any source extension and any sidecar/companion that was
    # copied alongside it.
    offline_dirs = [
        os.path.join(vireo_dir, "offline", "originals"),
        os.path.join(vireo_dir, "offline", "xmp"),
        os.path.join(vireo_dir, "offline", "companions"),
    ]
    total = len(files)
    for idx, f in enumerate(files, start=1):
        pid = f["photo_id"]
        # {id}.jpg lives in these dirs as a legacy full preview, thumbnail,
        # working copy, or prepared full-resolution render. {id}_{size}.jpg
        # is used for sized preview variants.
        for d in [thumb_cache_dir, preview_dir, working_dir, originals_dir]:
            cached = os.path.join(d, f"{pid}.jpg")
            if os.path.isfile(cached):
                try:
                    os.remove(cached)
                except OSError as e:
                    log.warning(
                        "Failed to remove cached file %s after photo "
                        "delete — will be reclaimed by Clear Cache: %s",
                        cached, e,
                    )
        # Paired RAW+JPEG views keep source-specific thumbnail variants next
        # to the legacy/default thumbnail. They are disposable derivatives
        # and must follow the photo out of the cache on delete as well.
        for variant in _glob.glob(os.path.join(thumb_cache_dir, f"{pid}_*.jpg")):
            try:
                os.remove(variant)
            except OSError as e:
                log.warning(
                    "Failed to remove thumbnail variant %s after photo "
                    "delete — will be reclaimed by Clear Cache: %s",
                    variant, e,
                )
        for prepared_render in _glob.glob(
            os.path.join(originals_dir, f"{pid}_*.jpg")
        ):
            try:
                os.remove(prepared_render)
            except OSError as e:
                log.warning(
                    "Failed to remove cached file %s after photo delete — "
                    "will be reclaimed by Clear Cache: %s",
                    prepared_render, e,
                )
        for name in (f"{pid}.display.jpg",):
            cached = os.path.join(originals_dir, name)
            if os.path.isfile(cached):
                try:
                    os.remove(cached)
                except OSError as e:
                    log.warning(
                        "Failed to remove cached original rendition %s after "
                        "photo delete — will be reclaimed by Clear Cache: %s",
                        cached, e,
                    )
        for variant in _glob.glob(os.path.join(preview_dir, f"{pid}_*.jpg")):
            try:
                os.remove(variant)
            except OSError as e:
                log.warning(
                    "Failed to remove preview variant %s after photo "
                    "delete — will be reclaimed by Clear Cache: %s",
                    variant, e,
                )
        for d in offline_dirs:
            for orphan in _glob.glob(os.path.join(d, f"{pid}.*")):
                try:
                    os.remove(orphan)
                except OSError as e:
                    log.warning(
                        "Failed to remove offline cache file %s after "
                        "photo delete — will be reclaimed by Clear "
                        "Cache: %s",
                        orphan, e,
                    )
        # Subject masks (``masks/{pid}.png``, ``masks/{pid}.{variant}.png``)
        # and local-adjustment snapshots (``edit-masks/{pid}.{ref}.png``)
        # are id-keyed like everything above. photos.mask_path goes away
        # with the row, so a leftover file is invisible to the DB but is
        # picked back up by any photo that later inherits the id — the
        # renderer would apply another photo's mask to the local pass.
        for d in (masks_dir, edit_masks_dir):
            for orphan in _glob.glob(os.path.join(d, f"{pid}.png")) + _glob.glob(
                os.path.join(d, f"{pid}.*.png")
            ):
                try:
                    os.remove(orphan)
                except OSError as e:
                    log.warning(
                        "Failed to remove mask file %s after photo delete "
                        "— will be reclaimed by Clear Cache: %s",
                        orphan, e,
                    )
        if progress_callback:
            progress_callback(idx, total, f.get("filename") or str(pid))


def _recycled_id_probe_paths(thumb_cache_dir, photo_id):
    """One exact (non-globbed) path per id-keyed derivative family.

    Used as a cheap existence probe before the full globbing purge — see
    ``purge_cached_files_for_recycled_id``. These are the "legacy" (bare
    id, no variant suffix) names — the variant patterns are covered by
    ``_recycled_id_probe_patterns``.
    """
    vireo_dir = os.path.dirname(thumb_cache_dir)
    return (
        os.path.join(thumb_cache_dir, f"{photo_id}.jpg"),
        os.path.join(vireo_dir, "working", f"{photo_id}.jpg"),
        os.path.join(vireo_dir, "previews", f"{photo_id}.jpg"),
        os.path.join(vireo_dir, "originals", f"{photo_id}.display.jpg"),
        os.path.join(vireo_dir, "masks", f"{photo_id}.png"),
    )


def _recycled_id_probe_patterns(thumb_cache_dir, photo_id):
    """Glob patterns covering the id-keyed derivative *variants*.

    The exact-path probe above misses the common case where a family
    exists only in a variant form: sized previews (``previews/7_1920.jpg``,
    the standard shape written by ``scanner._extract_previews``), source-
    specific thumbnails (``thumbnails/7_raw.jpg`` / ``7_jpeg.jpg``),
    prepared full-res renders (``originals/7_1920.jpg``), model-scoped
    subject masks (``masks/7.sam2-large.png``), edit-mask snapshots
    (``edit-masks/7.abcdef012345.png``), and offline originals
    (``offline/originals/7.NEF``). Every one of these is unlinked by
    ``cleanup_cached_files_for_deleted_photos`` on the delete side, so
    the *probe* must also see them or the purge silently returns without
    firing and the new photo serves the previous owner's pixels through
    the request paths' lazy-adoption shortcuts.

    Callers use ``glob.iglob(pattern)`` with ``next(..., None)`` so each
    pattern short-circuits at the first hit — a fresh library pays only
    the ``os.scandir`` open on each empty derivative dir.
    """
    vireo_dir = os.path.dirname(thumb_cache_dir)
    return (
        os.path.join(thumb_cache_dir, f"{photo_id}_*.jpg"),
        os.path.join(vireo_dir, "previews", f"{photo_id}_*.jpg"),
        os.path.join(vireo_dir, "originals", f"{photo_id}_*.jpg"),
        os.path.join(vireo_dir, "masks", f"{photo_id}.*.png"),
        os.path.join(vireo_dir, "edit-masks", f"{photo_id}.png"),
        os.path.join(vireo_dir, "edit-masks", f"{photo_id}.*.png"),
        os.path.join(vireo_dir, "offline", "originals", f"{photo_id}.*"),
        os.path.join(vireo_dir, "offline", "xmp", f"{photo_id}.*"),
        os.path.join(vireo_dir, "offline", "companions", f"{photo_id}.*"),
    )


def _recycled_id_has_stale_derivative(thumb_cache_dir, photo_id):
    """True when *any* id-keyed derivative for ``photo_id`` still exists.

    Exact paths (cheap ``stat``) first; falls through to variant globs
    only if none of the legacy names matched.

    Single-id probe — for the *batch* case (ingest, which asks this once
    per inserted photo) use :class:`RecycledIdIndex` instead. The variant
    globs enumerate a whole directory when they don't match, so asking
    per-photo across a populated cache is O(new photos × cached files):
    measured at 117 ms per miss against a 76k-thumbnail library, i.e.
    ~10 minutes of pure ``scandir`` for a 5000-photo import.
    """
    for p in _recycled_id_probe_paths(thumb_cache_dir, photo_id):
        if os.path.exists(p):
            return True
    for pattern in _recycled_id_probe_patterns(thumb_cache_dir, photo_id):
        if next(_glob.iglob(pattern), None) is not None:
            return True
    return False


def _derivative_dirs(thumb_cache_dir):
    """Every directory that stores files keyed by bare photo id."""
    vireo_dir = os.path.dirname(thumb_cache_dir)
    return (
        thumb_cache_dir,
        os.path.join(vireo_dir, "previews"),
        os.path.join(vireo_dir, "working"),
        os.path.join(vireo_dir, "originals"),
        os.path.join(vireo_dir, "masks"),
        os.path.join(vireo_dir, "edit-masks"),
        os.path.join(vireo_dir, "offline", "originals"),
        os.path.join(vireo_dir, "offline", "xmp"),
        os.path.join(vireo_dir, "offline", "companions"),
    )


def _leading_photo_id(name):
    """Parse the photo id a derivative filename is keyed by, or ``None``.

    Every derivative name is the bare id followed by ``.`` or ``_`` and
    then a suffix / variant: ``4.jpg``, ``4_1920.jpg``, ``4.display.jpg``,
    ``4.sam2-large.png``, ``4.NEF``. Requiring that separator is what
    stops ``40.jpg`` from registering as id 4.
    """
    digits = 0
    while digits < len(name) and name[digits].isdigit():
        digits += 1
    if not digits or digits == len(name) or name[digits] not in "._":
        return None
    return int(name[:digits])


class RecycledIdIndex:
    """The set of photo ids that already have a cached derivative on disk.

    Ingest needs the same question answered once per inserted photo, and
    the per-id probe answers it by enumerating directories — see
    :func:`_recycled_id_has_stale_derivative` for the measured cost. One
    ``scandir`` per derivative directory answers it for every id at once,
    so a scan pays O(cached files) total instead of per photo. Mirrors
    the batching in ``scanner._sweep_untracked_previews_for_photos``,
    which exists for exactly this reason.

    Built lazily on first query, so a scan that inserts nothing never
    touches the filesystem. The snapshot is deliberately taken before
    ingest generates any derivative of its own: working copies and
    previews are extracted at the end of ``scan()``, and those belong to
    the new rows — a live re-read would see them and wrongly conclude the
    ids collided.
    """

    def __init__(self, thumb_cache_dir):
        self._thumb_cache_dir = thumb_cache_dir
        self._ids = None

    def _build(self):
        ids = set()
        for directory in _derivative_dirs(self._thumb_cache_dir):
            try:
                entries = os.scandir(directory)
            except OSError:
                continue  # dir absent on a fresh install, or unreadable
            with entries:
                for entry in entries:
                    photo_id = _leading_photo_id(entry.name)
                    if photo_id is not None:
                        ids.add(photo_id)
        log.info(
            "Indexed %d photo ids with cached derivatives for "
            "recycled-rowid detection", len(ids),
        )
        return ids

    def __contains__(self, photo_id):
        if self._ids is None:
            self._ids = self._build()
        return photo_id in self._ids


def purge_cached_files_for_recycled_id(
    thumb_cache_dir, photo_id, id_index=None,
):
    """Drop any cached derivative left over from a previous owner of ``photo_id``.

    ``photos.id`` is ``INTEGER PRIMARY KEY`` *without* ``AUTOINCREMENT``,
    so SQLite hands the next insert ``max(rowid) + 1`` — the ids freed by
    deleting the highest-numbered rows get handed straight back out. Every
    derivative in ``~/.vireo`` is keyed by bare photo id
    (``thumbnails/<id>.jpg``, ``working/<id>.jpg``, ``masks/<id>.png``, …),
    so a new photo that inherits a freed id silently adopts the *previous*
    photo's pixels: the Life List card for a gull renders whatever bird
    used to own that row.

    Delete paths are supposed to unlink these files, but several
    (``Database._merge_into_existing``, ``scanner._merge_companion_pair``,
    ``audit.remove_orphans``) drop photo rows with raw SQL and leave the
    cache behind, and any unlink that fails leaves an orphan too. Rather
    than trusting every delete site, ingest re-checks at the one moment
    the collision can actually happen — when a brand-new row claims a
    rowid.

    Returns ``True`` when something was purged. The caller uses that to
    schedule the batched untracked-preview sweep, which is too expensive
    to run per-photo.

    Pass a :class:`RecycledIdIndex` as ``id_index`` when calling this in a
    loop (ingest does): the probe becomes an O(1) set lookup against one
    ``scandir`` per derivative directory instead of a per-photo directory
    enumeration. Without it, falls back to the single-id probe.
    """
    if not thumb_cache_dir:
        return False
    if id_index is not None:
        if photo_id not in id_index:
            return False
    elif not _recycled_id_has_stale_derivative(thumb_cache_dir, photo_id):
        return False
    log.info(
        "Photo %s reused a freed rowid with cached derivatives still on "
        "disk — purging them so the new photo renders its own pixels",
        photo_id,
    )
    cleanup_cached_files_for_deleted_photos(
        thumb_cache_dir, [{"photo_id": photo_id}],
    )
    return True


def evict_if_over_quota(db, vireo_dir):
    """Evict oldest preview_cache entries until under preview_cache_max_mb.

    Walks rows in ascending ``last_access_at`` order, removes files and
    rows, and stops as soon as total <= quota. Self-healing: if a file
    is already missing, the ghost row is still deleted. If ``unlink``
    fails for any other OS reason the row is *left in place* so the
    bytes stay accounted for and a future pass can retry; otherwise the
    accounting under-reports and eviction stops targeting the leaked
    bytes.

    Deletes are batched into one transaction to avoid hundreds of
    fsyncs when the quota is shrunk dramatically.
    """
    import config as cfg

    quota_mb = cfg.load().get("preview_cache_max_mb", 20480)
    max_bytes = int(quota_mb) * 1024 * 1024
    total = db.preview_cache_total_bytes()
    if total <= max_bytes:
        return

    preview_dir = os.path.join(vireo_dir, "previews")
    to_delete = []
    freed_bytes = 0
    for row in db.preview_cache_oldest_first():
        if total <= max_bytes:
            break
        path = os.path.join(
            preview_dir, f"{row['photo_id']}_{row['size']}.jpg"
        )
        removed = True
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Failed to remove preview cache file %s: %s", path, e)
            removed = False
        if removed:
            to_delete.append((row["photo_id"], row["size"]))
            total -= row["bytes"]
            freed_bytes += row["bytes"]

    if to_delete:
        db.conn.executemany(
            "DELETE FROM preview_cache WHERE photo_id=? AND size=?",
            to_delete,
        )
        db.conn.commit()
        log.info(
            "Preview cache eviction: removed %d entries, freed %.1f MB",
            len(to_delete), freed_bytes / 1024 / 1024,
        )


def reconcile_preview_cache(db, vireo_dir):
    """Drop preview_cache rows whose on-disk file is missing.

    Counterpart to ``evict_if_over_quota``'s self-heal: that path only
    cleans up ghost rows when the cache is *over* quota. If the cache
    accounting drifts while *under* quota — e.g. files deleted by an
    external process, or a previous eviction pass that removed files
    after the row's ``last_access_at`` was recently touched — the
    table keeps reporting ``total_bytes`` for files that no longer
    exist, and eviction stays asleep. That's invisible to the user
    until the next pipeline run regenerates everything from RAW
    because none of the cache files actually exist.

    Run at startup so a stale table can't poison the rest of the
    session. Returns the number of rows dropped.
    """
    preview_dir = os.path.join(vireo_dir, "previews")
    to_delete = []
    for row in db.preview_cache_oldest_first():
        path = os.path.join(
            preview_dir, f"{row['photo_id']}_{row['size']}.jpg"
        )
        if not os.path.exists(path):
            to_delete.append((row["photo_id"], row["size"]))

    if to_delete:
        db.conn.executemany(
            "DELETE FROM preview_cache WHERE photo_id=? AND size=?",
            to_delete,
        )
        db.conn.commit()
        log.info(
            "Preview cache reconcile: dropped %d ghost rows (files missing on disk)",
            len(to_delete),
        )
    return len(to_delete)
