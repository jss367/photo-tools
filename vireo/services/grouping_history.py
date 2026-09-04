"""Persist structural review edits in the workspace's ordinary undo history."""

import copy
import json
import os
from contextlib import contextmanager


class GroupingHistoryConflict(ValueError):
    """A newer grouping cannot safely be replaced by an older history entry."""


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

    Refuse a stale snapshot after recomputation or a later structural/species
    edit instead of silently erasing that work.
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
        if current is None or current.get("encounters") != expected:
            raise GroupingHistoryConflict(
                "The photo groups have changed since this edit. "
                "These groups cannot be restored without replacing newer work."
            )
        restored = copy.deepcopy(current)
        restored["encounters"] = target
        save_results_raw(restored, cache_dir, db._ws_id())
        try:
            yield
        except Exception:
            save_results_raw(current, cache_dir, db._ws_id())
            raise
    finally:
        lock.release()
