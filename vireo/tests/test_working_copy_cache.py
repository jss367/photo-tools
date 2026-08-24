import os
from types import SimpleNamespace


def _seed_working_copies(tmp_path, sizes):
    from db import Database

    db = Database(str(tmp_path / "vireo.db"))
    workspace_id = db.ensure_default_workspace()
    db.set_active_workspace(workspace_id)
    folder_id = db.add_folder(str(tmp_path / "photos"), name="photos")
    working_dir = tmp_path / "working"
    working_dir.mkdir()

    photo_ids = []
    for index, size in enumerate(sizes, 1):
        source_mtime = float(index * 10)
        photo_id = db.add_photo(
            folder_id=folder_id,
            filename=f"photo-{index}.NEF",
            extension=".nef",
            file_size=10_000,
            file_mtime=source_mtime,
            width=6000,
            height=4000,
        )
        path = working_dir / f"{photo_id}.jpg"
        path.write_bytes(b"x" * size)
        os.utime(path, (index * 100, index * 100))
        db.conn.execute(
            "UPDATE photos SET working_copy_path=? WHERE id=?",
            (f"working/{photo_id}.jpg", photo_id),
        )
        photo_ids.append(photo_id)
    db.conn.commit()
    return db, working_dir, photo_ids


def test_windows_identity_ignores_unstable_device_and_inode(monkeypatch):
    import working_copy_cache

    monkeypatch.setattr(working_copy_cache.os, "name", "nt")
    sampled = SimpleNamespace(
        st_dev=0, st_ino=0, st_size=123, st_mtime_ns=456, st_ctime_ns=789,
    )
    rechecked = SimpleNamespace(
        st_dev=7, st_ino=99, st_size=123, st_mtime_ns=456, st_ctime_ns=789,
    )

    assert working_copy_cache._file_identity(sampled) == (
        working_copy_cache._file_identity(rechecked)
    )


def test_evicts_oldest_working_copies_and_marks_catalog_rows(tmp_path):
    from scanner import working_copy_backfill_candidate_count
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(
        tmp_path, [600_000, 600_000, 600_000],
    )
    try:
        result = evict_if_over_quota(db, str(tmp_path), quota_mb=1)

        assert result["evicted"] == 2
        assert result["freed_bytes"] == 1_200_000
        assert not (working_dir / f"{photo_ids[0]}.jpg").exists()
        assert not (working_dir / f"{photo_ids[1]}.jpg").exists()
        assert (working_dir / f"{photo_ids[2]}.jpg").exists()

        rows = db.conn.execute(
            "SELECT id, file_mtime, working_copy_path, "
            "working_copy_evicted_mtime FROM photos ORDER BY id"
        ).fetchall()
        assert rows[0]["working_copy_path"] is None
        assert rows[0]["working_copy_evicted_mtime"] == rows[0]["file_mtime"]
        assert rows[1]["working_copy_path"] is None
        assert rows[1]["working_copy_evicted_mtime"] == rows[1]["file_mtime"]
        assert rows[2]["working_copy_path"] == f"working/{photo_ids[2]}.jpg"
        assert rows[2]["working_copy_evicted_mtime"] is None

        # Quota removals are deliberate cache misses, not startup-backfill
        # candidates. A changed source mtime makes the marker stale and lets
        # the working copy regenerate.
        assert working_copy_backfill_candidate_count(db) == 0
        db.conn.execute(
            "UPDATE photos SET file_mtime=file_mtime + 1 WHERE id=?",
            (photo_ids[0],),
        )
        db.conn.commit()
        assert working_copy_backfill_candidate_count(db) == 1
    finally:
        db.close()


def test_unlink_failure_keeps_working_copy_accounted(
    tmp_path, monkeypatch,
):
    import working_copy_cache

    db, working_dir, photo_ids = _seed_working_copies(
        tmp_path, [700_000, 700_000],
    )
    oldest = working_dir / f"{photo_ids[0]}.jpg"
    real_remove = working_copy_cache.os.remove

    def fail_oldest(path):
        if path == str(oldest):
            raise PermissionError("locked")
        return real_remove(path)

    monkeypatch.setattr(working_copy_cache.os, "remove", fail_oldest)
    try:
        result = working_copy_cache.evict_if_over_quota(
            db, str(tmp_path), quota_mb=1,
        )

        assert result["evicted"] == 1
        assert oldest.exists()
        assert not (working_dir / f"{photo_ids[1]}.jpg").exists()
        rows = db.conn.execute(
            "SELECT id, working_copy_path FROM photos ORDER BY id"
        ).fetchall()
        assert rows[0]["working_copy_path"] == f"working/{photo_ids[0]}.jpg"
        assert rows[1]["working_copy_path"] is None
        assert result["remaining_bytes"] == 700_000
    finally:
        db.close()


def test_working_copy_stats_include_all_direct_files(tmp_path):
    from working_copy_cache import working_copy_stats

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    (working_dir / "1.jpg").write_bytes(b"123")
    (working_dir / "leftover.tmp").write_bytes(b"4567")
    (working_dir / "nested").mkdir()
    (working_dir / "nested" / "ignored.jpg").write_bytes(b"ignored")

    assert working_copy_stats(str(tmp_path), quota_mb=2) == {
        "count": 2,
        "size": 7,
        "path": str(working_dir),
        "quota_bytes": 2 * 1024 * 1024,
    }


def test_working_copy_stats_skip_private_render_tempfiles(tmp_path):
    """A killed on-demand extractor leaves ``.render.*.jpg.tmp`` behind.

    Eviction has no catalog row to key off those names, so counting them
    toward quota usage would permanently inflate reported/enforced totals.
    """
    from working_copy_cache import working_copy_stats

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    (working_dir / "1.jpg").write_bytes(b"real")
    (working_dir / ".1.render.abcdef.jpg.tmp").write_bytes(b"orphaned tempfile")

    assert working_copy_stats(str(tmp_path), quota_mb=1) == {
        "count": 1,
        "size": 4,
        "path": str(working_dir),
        "quota_bytes": 1 * 1024 * 1024,
    }


def test_evict_ignores_private_render_tempfiles(tmp_path):
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [500_000])
    # Simulate a leftover on-demand extractor tempfile. If quota accounting
    # counted this toward ``total``, the eviction pass would delete the
    # legitimate working copy while the orphan lingered.
    orphan = working_dir / f".{photo_ids[0]}.render.deadbeef.jpg.tmp"
    orphan.write_bytes(b"x" * 900_000)
    try:
        result = evict_if_over_quota(db, str(tmp_path), quota_mb=1)

        assert result["evicted"] == 0
        assert (working_dir / f"{photo_ids[0]}.jpg").exists()
        # The orphan itself is left in place — eviction only touches files
        # backed by catalog rows — but it does not force real working copies
        # to be discarded to stay under quota.
        assert orphan.exists()
    finally:
        db.close()


def test_evict_sweeps_stale_render_tempfiles(tmp_path):
    """Old orphaned render tempfiles are reclaimed so bytes cannot leak.

    Excluding them from accounting protects working copies from being
    evicted for scratch space, but the disk bytes are still real. A
    process kill during on-demand extraction can otherwise accumulate
    tempfiles indefinitely.
    """
    import time

    from working_copy_cache import (
        _RENDER_TEMP_SWEEP_SECONDS,
        evict_if_over_quota,
    )

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [500_000])
    orphan = working_dir / f".{photo_ids[0]}.render.dead.jpg.tmp"
    orphan.write_bytes(b"x" * 800_000)
    stale_mtime = time.time() - _RENDER_TEMP_SWEEP_SECONDS - 60
    os.utime(orphan, (stale_mtime, stale_mtime))
    try:
        evict_if_over_quota(db, str(tmp_path), quota_mb=1)

        # Old orphan reclaimed; the published working copy is preserved.
        assert not orphan.exists()
        assert (working_dir / f"{photo_ids[0]}.jpg").exists()
    finally:
        db.close()


def test_evict_does_not_clear_concurrently_replaced_working_copy(
    tmp_path, monkeypatch,
):
    """A concurrent on-demand write must not be marked evicted.

    A peer request can regenerate ``working/<id>.jpg`` and commit the same
    ``working_copy_path`` between our ``os.remove`` and our subsequent
    DB UPDATE. Clearing the row then would leave untracked bytes on disk.
    """
    import working_copy_cache

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [700_000])
    victim_id = photo_ids[0]
    victim_path = working_dir / f"{victim_id}.jpg"
    real_remove = working_copy_cache.os.remove

    def remove_then_repopulate(path):
        real_remove(path)
        if path == str(victim_path):
            # Simulate an on-demand extractor writing a fresh copy after
            # our unlink but before our UPDATE.
            victim_path.write_bytes(b"y" * 500_000)

    monkeypatch.setattr(working_copy_cache.os, "remove", remove_then_repopulate)
    try:
        result = working_copy_cache.evict_if_over_quota(
            db, str(tmp_path), quota_mb=0,
        )

        assert result["evicted"] == 1
        assert victim_path.exists()
        row = db.conn.execute(
            "SELECT working_copy_path FROM photos WHERE id=?", (victim_id,),
        ).fetchone()
        # The row still points at the freshly written replacement, so the
        # bytes are tracked and future eviction can find and remove them.
        assert row["working_copy_path"] == f"working/{victim_id}.jpg"
    finally:
        db.close()


def test_evict_does_not_unlink_replacement_published_after_scan(
    tmp_path, monkeypatch,
):
    """An atomic replacement after sampling is not the selected victim."""
    import working_copy_cache

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [700_000])
    photo_id = photo_ids[0]
    victim = working_dir / f"{photo_id}.jpg"
    replacement = working_dir / ".replacement.jpg"
    replacement.write_bytes(b"new" * 100_000)
    real_stat = working_copy_cache.os.stat
    replaced = False

    def replace_before_identity_check(path, *args, **kwargs):
        nonlocal replaced
        if os.path.abspath(path) == os.path.abspath(victim) and not replaced:
            replaced = True
            os.replace(replacement, victim)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        working_copy_cache.os, "stat", replace_before_identity_check,
    )
    try:
        result = working_copy_cache.evict_if_over_quota(
            db, str(tmp_path), quota_mb=0,
        )

        assert replaced
        assert result["evicted"] == 0
        assert victim.read_bytes().startswith(b"new")
        row = db.conn.execute(
            "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,),
        ).fetchone()
        assert row["working_copy_path"] == f"working/{photo_id}.jpg"
    finally:
        db.close()


def test_evicts_legacy_canonical_file_without_catalog_path(tmp_path):
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [100])
    photo_id = photo_ids[0]
    db.conn.execute(
        "UPDATE photos SET working_copy_path=NULL WHERE id=?", (photo_id,)
    )
    db.conn.commit()
    try:
        result = evict_if_over_quota(db, str(tmp_path), quota_mb=0)

        assert result["evicted"] == 1
        assert result["remaining_bytes"] == 0
        assert not (working_dir / f"{photo_id}.jpg").exists()
        row = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id=?", (photo_id,),
        ).fetchone()
        assert row["working_copy_path"] is None
        assert row["working_copy_evicted_mtime"] == 10.0
    finally:
        db.close()


def test_recent_untracked_working_copy_gets_writer_grace(tmp_path):
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [100])
    photo_id = photo_ids[0]
    path = working_dir / f"{photo_id}.jpg"
    os.utime(path, None)
    db.conn.execute(
        "UPDATE photos SET working_copy_path=NULL WHERE id=?", (photo_id,)
    )
    db.conn.commit()
    try:
        result = evict_if_over_quota(db, str(tmp_path), quota_mb=0)

        assert result["evicted"] == 0
        assert result["remaining_bytes"] == 100
        assert path.exists()
    finally:
        db.close()


def test_startup_bypasses_writer_grace_for_legacy_files(tmp_path):
    """At startup no cache writer can be running, so a legacy
    ``working/<id>.jpg`` whose mtime happens to fall inside the grace window
    (recent modify time, or a future-dated timestamp copied from an archive)
    must still be reclaimed on the first pass — otherwise the file lingers
    above the ceiling until another restart after the window closes."""
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [100])
    photo_id = photo_ids[0]
    path = working_dir / f"{photo_id}.jpg"
    os.utime(path, None)  # bump mtime into the grace window
    db.conn.execute(
        "UPDATE photos SET working_copy_path=NULL WHERE id=?", (photo_id,)
    )
    db.conn.commit()
    try:
        result = evict_if_over_quota(
            db, str(tmp_path), quota_mb=0, startup=True,
        )

        assert result["evicted"] == 1
        assert result["remaining_bytes"] == 0
        assert not path.exists()
    finally:
        db.close()


def test_sweep_abandoned_render_tempfiles_removes_orphans(tmp_path):
    """A process kill during ``_extract_original_copy`` can leave a
    ``.<id>.render.*.jpg.tmp`` orphan in ``working/`` that quota accounting
    intentionally skips. The startup sweep is its only cleanup path."""
    from working_copy_cache import sweep_abandoned_render_tempfiles

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    canonical = working_dir / "42.jpg"
    canonical.write_bytes(b"canonical bytes")
    orphan_a = working_dir / ".42.render.abcdef.jpg.tmp"
    orphan_a.write_bytes(b"partial write")
    orphan_b = working_dir / ".99.render.deadbeef.jpg.tmp"
    orphan_b.write_bytes(b"another orphan")
    scanner_orphan = working_dir / ".42.jpg.cafebabe.jpg.tmp"
    scanner_orphan.write_bytes(b"scanner partial write")
    unrelated = working_dir / "leftover.tmp"
    unrelated.write_bytes(b"unrelated non-render tempfile")
    nested = working_dir / "nested"
    nested.mkdir()

    sweep_abandoned_render_tempfiles(str(tmp_path))

    assert canonical.exists()
    assert not orphan_a.exists()
    assert not orphan_b.exists()
    assert not scanner_orphan.exists()
    assert unrelated.exists()
    assert nested.exists()


def test_sweep_abandoned_render_tempfiles_missing_dir_is_noop(tmp_path):
    from working_copy_cache import sweep_abandoned_render_tempfiles

    # Should not raise even if ``working/`` was never created.
    sweep_abandoned_render_tempfiles(str(tmp_path))


def test_evict_reconciles_tracked_row_when_file_is_missing(tmp_path):
    """A tracked row with no on-disk file — a leftover from a prior eviction
    whose DB commit failed after the unlinks — must be reset to NULL so
    scanner backfill can regenerate it instead of skipping it forever."""
    from scanner import working_copy_backfill_candidate_count
    from working_copy_cache import evict_if_over_quota

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [500_000])
    try:
        # Row still claims a working copy, but the file has been removed
        # out from under it.
        (working_dir / f"{photo_ids[0]}.jpg").unlink()

        result = evict_if_over_quota(db, str(tmp_path), quota_mb=1)

        assert result["evicted"] == 0
        row = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id=?",
            (photo_ids[0],),
        ).fetchone()
        assert row["working_copy_path"] is None
        # No eviction marker: this is reconciliation, not a quota decision.
        assert row["working_copy_evicted_mtime"] is None
        # The row now looks like a normal backfill candidate again.
        assert working_copy_backfill_candidate_count(db) == 1
    finally:
        db.close()


def test_reconcile_rechecks_missing_file_before_clearing_row(
    tmp_path, monkeypatch,
):
    """A replacement published after the scan remains tracked."""
    import working_copy_cache

    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [500_000])
    photo_id = photo_ids[0]
    path = working_dir / f"{photo_id}.jpg"
    path.unlink()
    real_exists = working_copy_cache.os.path.exists
    republished = False

    def republish_before_reconcile(candidate):
        nonlocal republished
        if os.path.abspath(candidate) == os.path.abspath(path) and not republished:
            republished = True
            path.write_bytes(b"replacement")
            return True
        return real_exists(candidate)

    monkeypatch.setattr(
        working_copy_cache.os.path, "exists", republish_before_reconcile,
    )
    try:
        result = working_copy_cache.evict_if_over_quota(
            db, str(tmp_path), quota_mb=1,
        )

        assert republished
        assert result["evicted"] == 0
        assert path.is_file()
        row = db.conn.execute(
            "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,),
        ).fetchone()
        assert row["working_copy_path"] == f"working/{photo_id}.jpg"
    finally:
        db.close()


def test_zero_quota_skips_working_copy_generation(
    tmp_path, monkeypatch,
):
    import config as cfg
    import scanner

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_cache_max_mb": 0})
    db, working_dir, photo_ids = _seed_working_copies(tmp_path, [100])
    path = working_dir / f"{photo_ids[0]}.jpg"
    path.unlink()
    db.conn.execute(
        "UPDATE photos SET working_copy_path=NULL WHERE id=?",
        (photo_ids[0],),
    )
    db.conn.commit()

    monkeypatch.setattr(
        scanner, "extract_working_copy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("zero quota must skip decoding")
        ),
    )
    try:
        assert scanner.working_copy_backfill_candidate_count(db) == 0
        scanner._extract_working_copies(db, str(tmp_path))
        assert not path.exists()
    finally:
        db.close()
