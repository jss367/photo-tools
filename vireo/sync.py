"""Sync engine: reconcile database and XMP sidecars."""

import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from db import KEYWORD_SOURCE_UNKNOWN
from keyword_normalization import keyword_match_key
from xmp import (
    read_keywords,
    remove_keywords,
    remove_vireo_gps_location,
    write_edit_recipe,
    write_gps_location,
    write_pick_flag,
    write_rating,
    write_sidecar,
)

log = logging.getLogger(__name__)


def _get_xmp_path_for_photo(db, photo_id):
    """Determine the XMP sidecar path for a photo."""
    photo = db.get_photo(photo_id)
    if not photo:
        return None
    folders = {f["id"]: f["path"] for f in db.get_folder_tree()}
    folder_path = folders.get(photo["folder_id"], "")
    base = os.path.splitext(photo["filename"])[0]
    return os.path.join(folder_path, base + ".xmp")


def _sync_flags_to_xmp_enabled(db):
    """Return whether the active workspace should write flags to XMP."""
    try:
        import config as cfg

        return bool(db.get_effective_config(cfg.load()).get("sync_flags_to_xmp", False))
    except Exception:
        log.warning("Failed to read sync_flags_to_xmp config", exc_info=True)
        return False


def _write_assigned_location_to_xmp_enabled(db):
    """Return whether the active workspace should write assigned GPS to XMP."""
    try:
        import config as cfg

        return bool(
            db.get_effective_config(cfg.load()).get(
                "write_assigned_location_to_xmp", False
            )
        )
    except Exception:
        log.warning("Failed to read write_assigned_location_to_xmp config", exc_info=True)
        return False


_KEYWORD_CHANGE_TYPES = ("keyword_add", "keyword_remove", "keyword_remove_flat")


def _select_changes(changes, change_ids):
    """Restrict ``changes`` to ``change_ids`` plus their paired keyword changes.

    Auto-includes any unselected pending keyword_add / keyword_remove
    changes that share a (photo_id, normalized key) with a selected one.
    Both remove_keywords() (for keyword_remove) and the add-canonicalization
    pass in ``_remove_planned_keywords`` match by normalized key, so a
    rename's paired add(clean) + remove(legacy variant) split across two
    syncs lets each half clobber the sidecar entry the other half writes --
    the add-only sync strips the legacy ``<rdf:li>`` before writing the
    clean spelling, and a later remove-only sync strips the clean spelling
    under the same normalized match. Sync both sides together whenever the
    user picks either.
    """
    selected_ids = {int(cid) for cid in change_ids}
    kw_index = defaultdict(list)
    for c in changes:
        if c["change_type"] in _KEYWORD_CHANGE_TYPES and c["value"]:
            key = (c["photo_id"], keyword_match_key(c["value"]))
            kw_index[key].append(c["id"])
    for c in changes:
        if c["id"] not in selected_ids:
            continue
        if c["change_type"] not in _KEYWORD_CHANGE_TYPES or not c["value"]:
            continue
        key = (c["photo_id"], keyword_match_key(c["value"]))
        selected_ids.update(kw_index[key])
    return [c for c in changes if c["id"] in selected_ids]


@dataclass
class _PhotoSyncPlan:
    """Everything one photo's pending changes ask us to write to its sidecar."""

    keywords_to_add: set = field(default_factory=set)
    keywords_to_remove: set = field(default_factory=set)
    # ``keyword_remove_flat`` is queued by ``repair_duplicate_photo_species``
    # when a detached root spelling still appears as an ancestor segment of a
    # preserved hierarchy leaf. A regular hierarchical ``keyword_remove``
    # would strip that preserved ``lr:hierarchicalSubject`` entry; flat-only
    # removal touches only the stale ``dc:subject`` line.
    keywords_to_remove_flat: set = field(default_factory=set)
    rating: int | None = None
    flag: str | None = None
    edit_recipe_json: str | None = None
    sync_location: bool = False
    cleanup_location: bool = False
    supported_ids: list = field(default_factory=list)
    unsupported_changes: list = field(default_factory=list)


def _plan_photo_sync(photo_changes, sync_flags, sync_locations):
    """Fold one photo's pending changes into a ``_PhotoSyncPlan``."""
    plan = _PhotoSyncPlan()
    for c in photo_changes:
        kind = c["change_type"]
        if kind == "keyword_add":
            plan.keywords_to_add.add(c["value"])
        elif kind == "keyword_remove":
            plan.keywords_to_remove.add(c["value"])
        elif kind == "keyword_remove_flat":
            plan.keywords_to_remove_flat.add(c["value"])
        elif kind == "rating":
            plan.rating = int(c["value"])
        elif kind == "flag":
            if not sync_flags:
                plan.unsupported_changes.append(c)
                continue
            plan.flag = c["value"] or "none"
        elif kind == "location":
            if sync_locations:
                plan.sync_location = True
            else:
                plan.cleanup_location = True
        elif kind == "edit_recipe":
            plan.edit_recipe_json = c["value"] or ""
        else:
            continue
        plan.supported_ids.append(c["id"])
    return plan


def _remove_planned_keywords(xmp_path, plan):
    """Strip sidecar keywords the plan removes or is about to re-add.

    Removals run BEFORE additions. remove_keywords() compares by normalized
    match key, so a remove of `‘apapane` matches any `<rdf:li>` whose text
    normalizes to `apapane` -- including a clean `apapane` we would otherwise
    have just added. A rename that queues remove `‘apapane` and add `apapane`
    for the same photo would then have its newly-written clean entry stripped
    along with the old quoted one, clearing pending changes and leaving the
    sidecar without the keyword. Applying the remove first strips only the
    pre-existing quoted variant; the subsequent write_sidecar then adds the
    clean spelling.

    Removals are split by whether they're paired with an add for the same
    normalized key. A paired remove+add is a normalization-only rename (e.g.
    remove `‘Birds` + add `Birds`); hierarchical mode would then strip
    unrelated hierarchies like `Animals|Birds|Hawk` because remove_keywords()
    matches by any pipe-segment key. Use flat-only removal for those paired
    removes so the rename only touches the flat `dc:subject` legacy entry.
    Solo removes keep hierarchical semantics so real keyword deletions still
    drop pipe-segment matches.
    """
    if plan.keywords_to_remove or plan.keywords_to_remove_flat:
        paired_keys = {keyword_match_key(kw) for kw in plan.keywords_to_add}
        paired_keys.discard("")
        paired_removes = {
            kw for kw in plan.keywords_to_remove
            if keyword_match_key(kw) in paired_keys
        }
        solo_removes = plan.keywords_to_remove - paired_removes
        if solo_removes:
            remove_keywords(xmp_path, solo_removes)
        # Merge repair-queued flat-only removes with the rename-paired flat
        # removes: both take exactly the ``hierarchical=False`` code path.
        flat_removes = paired_removes | plan.keywords_to_remove_flat
        if flat_removes:
            remove_keywords(xmp_path, flat_removes, hierarchical=False)

    # Strip any sidecar dc:subject entry that normalizes to a keyword we're
    # about to add. write_sidecar() dedupes with an exact-string set
    # difference, so a pure keyword_add for `apapane` against a legacy
    # sidecar `‘apapane` would append a second <rdf:li>. Canonicalizing
    # first collapses variants into the clean spelling that write_sidecar
    # writes next. Use the flat-only mode: a hierarchical remove (which
    # drops any entry whose segment matches) would delete unrelated
    # hierarchies such as `Animals|Birds|Hawk` when we add flat `Birds`.
    if plan.keywords_to_add:
        remove_keywords(xmp_path, plan.keywords_to_add, hierarchical=False)


def _write_photo_sync(db, photo_id, xmp_path, plan):
    """Apply a ``_PhotoSyncPlan`` to the photo's sidecar, in dependency order."""
    _remove_planned_keywords(xmp_path, plan)

    # Write keyword additions after removals so a same-photo remove+add
    # pair does not race (see _remove_planned_keywords).
    if plan.keywords_to_add:
        write_sidecar(
            xmp_path, flat_keywords=plan.keywords_to_add, hierarchical_keywords=set()
        )

    # Write flag before rating: write_pick_flag creates a sidecar if needed,
    # while write_rating intentionally only updates existing sidecars.
    if plan.flag is not None:
        write_pick_flag(xmp_path, plan.flag)

    if plan.sync_location:
        loc = db.get_assigned_photo_location(photo_id)
        if loc and loc.get("latitude") is not None and loc.get("longitude") is not None:
            write_gps_location(
                xmp_path,
                loc["latitude"],
                loc["longitude"],
                source=loc.get("source") or "assigned",
            )
        else:
            remove_vireo_gps_location(xmp_path)
    elif plan.cleanup_location:
        remove_vireo_gps_location(xmp_path)

    if plan.edit_recipe_json is not None:
        write_edit_recipe(xmp_path, plan.edit_recipe_json)

    # Write rating after every operation that can create a sidecar. Rating
    # alone intentionally remains a no-op for missing XMP, but a selected
    # keyword, flag, location, or edit write should make the same-photo
    # rating persist rather than silently clear it.
    if plan.rating is not None:
        write_rating(xmp_path, plan.rating)


# How many distinct failure reasons a sync reports up to the job layer. A NAS
# that rejects every write produces one reason repeated thousands of times;
# the summary exists to name the cause, not to reproduce the log.
_MAX_REPORTED_FAILURE_REASONS = 5


def _failure_reason(exc):
    """Path-free failure cause for grouping identical failures across photos.

    ``str(OSError)`` renders as ``[Errno 13] Permission denied: '/path/to.xmp'``
    -- the trailing per-file path makes every entry unique, so counting raw
    error strings would turn one NAS-wide EACCES into thousands of distinct
    ``(1 photo)`` reasons and defeat the summary. Rebuild the message from
    ``errno`` / ``strerror`` so the per-photo path drops out.
    """
    if isinstance(exc, OSError) and exc.strerror:
        if exc.errno is not None:
            return f"[Errno {exc.errno}] {exc.strerror}"
        return exc.strerror
    return str(exc)


def _sync_result(synced, failures):
    """Build the sync result, telling the job layer whether it actually worked.

    ``ok`` / ``errors`` are the JobRunner's partial-failure convention: a run
    that wrote 10 sidecars and failed on 2,230 must land in history as
    "failed", not "completed", so the UI cannot report success over a NAS
    that rejected every write.
    """
    # Count each (reason, photo_id) pair once: a photo with two queued
    # unsupported changes of the same type produces two failure records with
    # identical reasons, but the summary reports "photos", not records.
    seen = set()
    counts = Counter()
    for f in failures:
        reason = f.get("reason") or f["error"]
        key = (reason, f.get("photo_id"))
        if key in seen:
            continue
        seen.add(key)
        counts[reason] += 1
    reasons = [
        f"{reason} ({count} photo{'s' if count != 1 else ''})"
        for reason, count in counts.most_common(_MAX_REPORTED_FAILURE_REASONS)
    ]
    remaining = len(counts) - len(reasons)
    if remaining > 0:
        reasons.append(f"...and {remaining} more distinct error(s)")
    return {
        "synced": synced,
        "failed": len(failures),
        "failures": failures,
        "ok": not failures,
        "errors": reasons,
    }


def sync_to_xmp(db, progress_callback=None, change_ids=None):
    """Write pending changes to XMP sidecars.

    Args:
        db: Database instance
        progress_callback: optional callable(current, total)
        change_ids: optional pending_changes ids to sync. When provided, any
            other queued changes are left pending.

    Returns:
        dict with synced, failed, failures counts
    """
    changes = db.get_pending_changes()
    if change_ids is not None:
        changes = _select_changes(changes, change_ids)
    if not changes:
        return _sync_result(0, [])

    by_photo = defaultdict(list)
    for c in changes:
        by_photo[c["photo_id"]].append(c)

    sync_flags = _sync_flags_to_xmp_enabled(db)
    sync_locations = _write_assigned_location_to_xmp_enabled(db)
    synced = 0
    failures = []
    synced_ids = []

    total = len(by_photo)
    for i, (photo_id, photo_changes) in enumerate(by_photo.items()):
        xmp_path = _get_xmp_path_for_photo(db, photo_id)
        if not xmp_path:
            failures.append({"photo_id": photo_id, "error": "photo not found in DB"})
            continue

        # Check if the folder exists (NAS might be offline)
        folder = os.path.dirname(xmp_path)
        if not os.path.isdir(folder):
            failures.append({
                "photo_id": photo_id,
                "error": f"folder not accessible: {folder}",
                # Strip the per-folder path so many photos on an offline NAS
                # summarise as one cause instead of one per subfolder.
                "reason": "folder not accessible",
            })
            continue

        try:
            plan = _plan_photo_sync(photo_changes, sync_flags, sync_locations)
            _write_photo_sync(db, photo_id, xmp_path, plan)
        except Exception as e:
            failures.append({
                "photo_id": photo_id,
                "error": str(e),
                "reason": _failure_reason(e),
            })
            log.warning("Failed to sync photo %d: %s", photo_id, e)
        else:
            if plan.supported_ids:
                synced += 1
                synced_ids.extend(plan.supported_ids)
            for c in plan.unsupported_changes:
                failures.append({
                    "photo_id": photo_id,
                    "change_id": c["id"],
                    "error": f"unsupported change type: {c['change_type']}",
                })

        if progress_callback:
            progress_callback(i + 1, total)

    # Clear successfully synced changes
    if synced_ids:
        db.clear_pending(
            synced_ids, clear_equivalent_flat_removals=True,
        )

    log.info("Sync complete: %d synced, %d failed", synced, len(failures))
    return _sync_result(synced, failures)


def sync_from_xmp(db, photo_ids):
    """Re-read XMP sidecars and update database keywords.

    Args:
        db: Database instance
        photo_ids: list of photo ids to re-sync
    """
    folders = {f["id"]: f["path"] for f in db.get_folder_tree()}

    for photo_id in photo_ids:
        photo = db.get_photo(photo_id)
        if not photo:
            continue

        folder_path = folders.get(photo["folder_id"], "")
        base = os.path.splitext(photo["filename"])[0]
        xmp_path = os.path.join(folder_path, base + ".xmp")

        if not os.path.exists(xmp_path):
            continue

        # Serialize the sidecar read, DB reconciliation, and mtime stamp as
        # one writer transaction. Background migrations use the same SQLite
        # writer lock, so they cannot act on a pre-reconciliation association
        # snapshot between this read and the final xmp_mtime update.
        db.conn.execute("BEGIN IMMEDIATE")
        try:
            # Read current XMP keywords. Compare with a normalized match key on
            # both sides so an XMP variant like `‘apapane` matches a DB row
            # stored as `apapane` (add_keyword normalizes on insert). A plain
            # `.lower()` comparison would treat them as different names, making
            # the add-side an INSERT-OR-IGNORE no-op and then prune the DB tag
            # because the raw DB name is not in the raw XMP set -- leaving the
            # photo untagged.
            #
            # Skip XMP entries whose normalized match key is empty (e.g. a
            # lone ASCII or smart quote). add_keyword() now raises ValueError
            # for names that normalize to empty, so keeping such entries would
            # abort the whole sidecar reconcile on a malformed edge-quote
            # keyword instead of ignoring it and processing the rest.
            xmp_keywords = read_keywords(xmp_path)
            pending_removals = db.get_pending_keyword_removal_keys(photo_id)
            pending_hierarchical_removals = db.get_pending_keyword_removal_keys(
                photo_id, hierarchical=True,
            )
            pending_flat_only_removals = (
                pending_removals - pending_hierarchical_removals
            )
            xmp_keywords_by_key = {}
            for kw in xmp_keywords:
                key = keyword_match_key(kw)
                if not key or key in pending_removals:
                    continue
                xmp_keywords_by_key.setdefault(key, kw)

            # Get current DB keywords
            db_keywords = db.get_photo_keywords(photo_id)
            db_keywords_by_key = {
                keyword_match_key(k["name"]): k for k in db_keywords
            }

            # Reconcile DB keyword associations to match the current XMP file.
            for kw_key, kw_name in xmp_keywords_by_key.items():
                if kw_key in db_keywords_by_key:
                    continue
                kid = db.add_keyword(kw_name, _commit=False)
                # Reconciling *from* a sidecar cannot tell a hand-typed Lightroom
                # keyword from one Vireo wrote out, so this writer stays
                # provenance-neutral instead of claiming manual authorship.
                db.tag_photo(
                    photo_id,
                    kid,
                    source=KEYWORD_SOURCE_UNKNOWN,
                    _commit=False,
                )

            for kw in db_keywords:
                kw_key = keyword_match_key(kw["name"])
                preserve_hierarchy = (
                    kw["parent_id"] is not None
                    and kw_key in pending_flat_only_removals
                )
                if kw_key not in xmp_keywords_by_key and not preserve_hierarchy:
                    db.untag_photo(photo_id, kw["id"], _commit=False)

            # Update xmp_mtime in the same transaction as reconciliation.
            xmp_mtime = os.path.getmtime(xmp_path)
            db.conn.execute(
                "UPDATE photos SET xmp_mtime = ? WHERE id = ?",
                (xmp_mtime, photo_id),
            )
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise

        log.info(
            "Synced XMP -> DB for photo %d: %d keywords", photo_id, len(xmp_keywords)
        )
