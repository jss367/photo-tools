"""Persist structural review edits in the workspace's ordinary undo history."""

import copy
import json
import os
from contextlib import contextmanager


class GroupingHistoryConflict(ValueError):
    """A newer grouping cannot safely be replaced by an older history entry.

    Bare ``GroupingHistoryConflict`` is transient — another writer holds the
    workspace regroup lock and undo is worth retrying. ``GroupingHistoryStale``
    means the cached encounter structure has diverged so far from this entry's
    snapshot that no future retry could succeed; callers must retire the
    history row instead of leaving it to block older undoable edits forever.
    """


class GroupingHistoryStale(GroupingHistoryConflict):
    """The cache no longer matches this entry — retire, don't retry."""


# Fields that ``/api/encounters/species`` mutates on the pipeline cache
# without changing encounter structure. Undo is strict LIFO, so any
# species edit that landed after a grouping edit has already been reverted
# by the time we compare snapshots here — its leftover cache state is not
# "newer work" that a grouping undo would silently overwrite.
_CACHE_SPECIES_ENCOUNTER_FIELDS = ("confirmed_species", "species_confirmed")
_CACHE_SPECIES_BURST_FIELDS = ("species_override",)


def _grouping_signature(encounters):
    """Return an encounters snapshot without cache-only species fields."""
    signature = []
    for enc in encounters or []:
        stripped = {
            k: v for k, v in enc.items()
            if k not in _CACHE_SPECIES_ENCOUNTER_FIELDS and k != "bursts"
        }
        stripped["bursts"] = [
            {k: v for k, v in b.items() if k not in _CACHE_SPECIES_BURST_FIELDS}
            for b in enc.get("bursts", [])
        ]
        signature.append(stripped)
    return signature


def _photo_ids_key(entry):
    """Stable dict key for the photo composition of an encounter or burst."""
    ids = entry.get("photo_ids") if isinstance(entry, dict) else None
    return tuple(ids or ())


def _preserve_cleared_species_overrides(target, current):
    """Carry ``clearBurstOverride`` clears from current into the restored target.

    ``clearBurstOverride`` writes ``species_override = None`` straight through
    ``/api/pipeline/save-cache`` without recording a history entry, so its
    change is invisible to ``_grouping_signature``. Without this pass, a
    subsequent grouping undo would silently resurrect the pre-clear override
    on the same burst. Match by photo_ids so bursts that moved between
    encounters (e.g. across a burst detach) still find their pair.

    Non-null species state in ``current`` is left alone: it's either the
    intended after-state of this grouping edit or leftover cache from a
    species edit that was itself undone (LIFO), and the caller relies on
    ``_grouping_signature`` treating that as unchanged. Only clears
    (values that ``current`` shows explicitly as ``None``) get carried
    forward.
    """
    current_bursts = {}
    current_encs = {}
    for enc in current or []:
        if not isinstance(enc, dict):
            continue
        current_encs[_photo_ids_key(enc)] = enc
        for b in enc.get("bursts", []) or []:
            if isinstance(b, dict):
                current_bursts[_photo_ids_key(b)] = b
    for enc in target or []:
        if not isinstance(enc, dict):
            continue
        cur_enc = current_encs.get(_photo_ids_key(enc))
        if cur_enc is not None:
            if "confirmed_species" in cur_enc and cur_enc.get("confirmed_species") is None:
                enc["confirmed_species"] = None
            if "species_confirmed" in cur_enc and not cur_enc.get("species_confirmed"):
                enc["species_confirmed"] = False
        for b in enc.get("bursts", []) or []:
            if not isinstance(b, dict):
                continue
            cur_burst = current_bursts.get(_photo_ids_key(b))
            if cur_burst is None:
                continue
            if "species_override" in cur_burst and cur_burst.get("species_override") is None:
                b["species_override"] = None


def _restored_summary(current_summary, encounters):
    """Rebuild the cache summary's structural counts for a restored group.

    ``restore_grouping_edit`` swaps ``encounters`` back to an older snapshot,
    but the surrounding ``summary`` mirrors the after state — its
    ``encounter_count`` and ``burst_count`` would keep advertising the
    post-edit structure and ``/api/pipeline/page-init`` would report
    counts that disagree with what the review page renders.
    """
    if not isinstance(current_summary, dict):
        summary = {}
    else:
        summary = dict(current_summary)
    summary["encounter_count"] = len(encounters or [])
    summary["burst_count"] = sum(
        len(enc.get("bursts") or []) for enc in (encounters or [])
        if isinstance(enc, dict)
    )
    return summary


def save_grouping_edit(db, before, after, description):
    """Save a detach and its history together, restoring the cache on failure.

    Callers hold the workspace regroup lock and SQLite writer transaction.
    Store only encounter structure, so undo never replaces photo metadata,
    flags, scores, or settings with an old whole-catalog snapshot.
    """
    from pipeline import save_results_raw

    cache_dir = os.path.dirname(db._db_path)
    if before["encounters"] == after["encounters"]:
        return
    db.record_edit(
        "pipeline_grouping", description,
        json.dumps({"before": before["encounters"], "after": after["encounters"]}),
        [], _commit=False,
    )
    saved = False
    try:
        save_results_raw(after, cache_dir, db._ws_id())
        saved = True
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        if saved:
            save_results_raw(before, cache_dir, db._ws_id())
        raise
    db._prune_edit_history()


@contextmanager
def restore_grouping_edit(db, entry, *, undo):
    """Restore structure, retaining history if the write/commit fails.

    Refuse a stale snapshot after recomputation or a later structural edit
    instead of silently erasing that work. A leftover cache-only species
    override or confirmation from an already-reverted species edit is not a
    conflict — undo is strict LIFO, so any species entry newer than this
    grouping entry has already been undone by the time we get here.
    """
    from pipeline import load_results_raw, save_results_raw
    from pipeline_locks import acquire_workspace_regroup

    # Undo already holds the database writer lock. Never wait here: processing
    # takes the regroup lock first and may need the database before releasing
    # it. Ordinary photo undo does not need this lock at all.
    lock = acquire_workspace_regroup(db._ws_id())
    if not lock.acquire(blocking=False):
        raise GroupingHistoryConflict('Photo groups are being updated. Try again when processing finishes.')
    try:
        cache_dir = os.path.dirname(db._db_path)
        current = load_results_raw(cache_dir, db._ws_id())
        change = json.loads(entry["new_value"])
        expected = change["after" if undo else "before"]
        target = change["before" if undo else "after"]
        if current is None or _grouping_signature(current.get("encounters")) != _grouping_signature(expected):
            raise GroupingHistoryStale(
                "The photo groups have changed since this edit. "
                "These groups cannot be restored without replacing newer work."
            )
        restored = copy.deepcopy(current)
        restored["encounters"] = copy.deepcopy(target)
        _preserve_cleared_species_overrides(
            restored["encounters"], current.get("encounters"),
        )
        restored["summary"] = _restored_summary(
            current.get("summary"), restored["encounters"],
        )
        save_results_raw(restored, cache_dir, db._ws_id())
        try:
            yield
        except Exception:
            save_results_raw(current, cache_dir, db._ws_id())
            raise
    finally:
        lock.release()
