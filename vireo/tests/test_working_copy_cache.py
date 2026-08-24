import os


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
