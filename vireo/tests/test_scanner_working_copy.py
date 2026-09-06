"""Working copy extraction for large JPEGs."""
import os

from PIL import Image


def _make_jpeg(path, width, height):
    img = Image.new("RGB", (width, height), (128, 128, 128))
    img.save(path, "JPEG", quality=85)


def _wait_for_backfill_terminal(runner, timeout=60.0, poll=0.05):
    """Poll runner.list_jobs() until a working_copy_backfill job appears
    and reaches a terminal status (``completed`` / ``failed``). Returns
    the job dict.

    Generous default timeout because full-suite test runs accumulate
    daemon threads, write-lock contention, and FS pressure that can
    stretch an otherwise sub-second backfill past tighter deadlines —
    causing order-dependent flakes (passes in isolation, fails after
    2k+ tests have run). Successful runs return immediately on the
    poll-rate cadence; the timeout only matters on actual hangs.

    Distinguishes "job never appeared" from "job never completed" so
    failures point at the right cause.
    """
    import time
    deadline = time.time() + timeout
    last_seen = None
    while time.time() < deadline:
        backfill_jobs = [
            j for j in runner.list_jobs()
            if j["type"] == "working_copy_backfill"
        ]
        if backfill_jobs:
            last_seen = backfill_jobs[0]
            if last_seen["status"] in ("completed", "failed"):
                return last_seen
        time.sleep(poll)
    if last_seen is None:
        raise AssertionError(
            f"working_copy_backfill job never appeared in runner.list_jobs() "
            f"within {timeout}s — kickoff likely never fired"
        )
    raise AssertionError(
        f"working_copy_backfill job appeared but did not reach terminal "
        f"status within {timeout}s; last seen status="
        f"{last_seen.get('status')!r}"
    )


def test_extract_working_copy_for_large_jpeg(tmp_path, monkeypatch):
    """A JPEG larger than working_copy_max_size gets a working copy created."""
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    # Force a small max to avoid making huge fixture images.
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    src = folder / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)  # larger than 1000 cap

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    photo_id = db.add_photo(
        folder_id, "big.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )

    _extract_working_copies(db, str(vireo_dir))

    wc_path = vireo_dir / "working" / f"{photo_id}.jpg"
    assert wc_path.exists(), "working copy should be created for large JPEG"
    with Image.open(wc_path) as img:
        assert max(img.size) == 1000

    row = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,)
    ).fetchone()
    assert row["working_copy_path"] == f"working/{photo_id}.jpg"


def test_no_jpeg_working_copy_when_max_size_zero(tmp_path, monkeypatch):
    """working_copy_max_size=0 disables JPEG working-copy extraction.

    Zero is the "full resolution" sentinel; without the guard the SQL
    predicate ``p.width > 0 OR p.height > 0`` matches every JPEG with known
    dimensions and produces an expensive full-size duplicate for each.
    """
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 0, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    src = folder / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    photo_id = db.add_photo(
        folder_id, "big.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )

    _extract_working_copies(db, str(vireo_dir))

    wc_path = vireo_dir / "working" / f"{photo_id}.jpg"
    assert not wc_path.exists()
    row = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,)
    ).fetchone()
    assert row["working_copy_path"] is None


def _seed_large_jpeg(db, folder, filename):
    """Make a large JPEG on disk, register it in `db`, return photo_id."""
    src = folder / filename
    _make_jpeg(str(src), 2000, 1500)
    folder_id = db.add_folder(str(folder))
    return db.add_photo(
        folder_id, filename, ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )


def test_extract_working_copies_scope_restricts_to_given_folders(tmp_path, monkeypatch):
    """When `scope` is given, only photos in those folders get working copies."""
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder_a = tmp_path / "a"
    folder_a.mkdir()
    folder_b = tmp_path / "b"
    folder_b.mkdir()

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    a_id = _seed_large_jpeg(db, folder_a, "a.jpg")
    b_id = _seed_large_jpeg(db, folder_b, "b.jpg")

    _extract_working_copies(db, str(vireo_dir), scope=[str(folder_a)])

    assert (vireo_dir / "working" / f"{a_id}.jpg").exists()
    assert not (vireo_dir / "working" / f"{b_id}.jpg").exists()

    rows = {
        r["id"]: r["working_copy_path"]
        for r in db.conn.execute(
            "SELECT id, working_copy_path FROM photos WHERE id IN (?, ?)",
            (a_id, b_id),
        ).fetchall()
    }
    assert rows[a_id] == f"working/{a_id}.jpg"
    assert rows[b_id] is None


def test_extract_working_copies_scope_matches_subtrees(tmp_path, monkeypatch):
    """Scope entries match their subtree — a photo in a subfolder is included."""
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "2026-04-20"
    child.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    child_id = _seed_large_jpeg(db, child, "c.jpg")
    sibling_id = _seed_large_jpeg(db, sibling, "s.jpg")

    _extract_working_copies(db, str(vireo_dir), scope=[str(parent)])

    assert (vireo_dir / "working" / f"{child_id}.jpg").exists()
    assert not (vireo_dir / "working" / f"{sibling_id}.jpg").exists()


def test_extract_working_copies_empty_scope_is_noop(tmp_path, monkeypatch):
    """scope=[] → nothing is extracted, even with eligible photos present."""
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    photo_id = _seed_large_jpeg(db, folder, "big.jpg")

    _extract_working_copies(db, str(vireo_dir), scope=[])

    assert not (vireo_dir / "working" / f"{photo_id}.jpg").exists()
    row = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,)
    ).fetchone()
    assert row["working_copy_path"] is None


def test_subtree_like_pattern_posix():
    """Unix separator: LIKE pattern is the path followed by `/%`."""
    from scanner import _subtree_like_pattern
    assert _subtree_like_pattern("/photos/2024", sep="/") == "/photos/2024/%"


def test_subtree_like_pattern_windows_escapes_separator():
    r"""On Windows, the trailing `\` must be escape-doubled so `%` remains the
    wildcard under ``LIKE ? ESCAPE '\'`` — otherwise subtree matching silently
    matches only the exact folder.

    Input path `C:\a\b` with sep `\`:
      * every literal `\` in the path is doubled → `C:\\a\\b`
      * the trailing separator is also doubled → `\\`
      * the wildcard `%` is appended unescaped.
    """
    from scanner import _subtree_like_pattern
    assert _subtree_like_pattern("C:\\a\\b", sep="\\") == "C:\\\\a\\\\b\\\\%"


def test_subtree_like_pattern_escapes_literal_wildcards():
    """`_` and `%` inside folder names are escaped so they match literally."""
    from scanner import _subtree_like_pattern
    assert _subtree_like_pattern("/a/2024_06", sep="/") == "/a/2024\\_06/%"
    assert _subtree_like_pattern("/a/50%off", sep="/") == "/a/50\\%off/%"


def test_subtree_like_pattern_normalizes_trailing_separator():
    """Trailing separator in the scope path must not produce a double separator.

    Before this guard, `/photos/` produced `"//%"` and the root path `"/"`
    produced `"//%"` — neither matches any real descendant path.
    """
    from scanner import _subtree_like_pattern
    assert _subtree_like_pattern("/photos/", sep="/") == "/photos/%"
    assert _subtree_like_pattern("/photos///", sep="/") == "/photos/%"
    assert _subtree_like_pattern("/", sep="/") == "/%"
    assert _subtree_like_pattern("C:\\a\\", sep="\\") == "C:\\\\a\\\\%"


def test_extract_working_copies_scope_escapes_like_wildcards(tmp_path, monkeypatch):
    """An underscore in a scope path must not match unrelated siblings.

    SQLite LIKE treats `_` and `%` as wildcards. Without escaping, scoping to
    ``/photos/2024_06`` would also match a sibling like ``/photos/2024A06``.
    """
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    wanted = tmp_path / "2024_06"
    wanted.mkdir()
    # Sibling whose path would match the naive `2024_06/%` pattern because `_`
    # is a LIKE wildcard. Both folders end in a directory separator boundary
    # so the tail matches a single arbitrary character.
    sibling = tmp_path / "2024A06"
    sibling.mkdir()
    (sibling / "sub").mkdir()

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    wanted_id = _seed_large_jpeg(db, wanted, "w.jpg")
    sibling_sub = sibling / "sub"
    sibling_id = _seed_large_jpeg(db, sibling_sub, "s.jpg")

    _extract_working_copies(db, str(vireo_dir), scope=[str(wanted)])

    assert (vireo_dir / "working" / f"{wanted_id}.jpg").exists()
    assert not (vireo_dir / "working" / f"{sibling_id}.jpg").exists(), (
        "wildcard `_` in wanted path leaked into sibling match"
    )


def test_scan_non_recursive_scopes_working_copies_to_root_only(tmp_path, monkeypatch):
    """scan(..., recursive=False) must not backfill working copies in subfolders.

    Regression: without honoring `recursive`, the derived scope used a subtree
    match that touched photos the caller explicitly chose not to walk.
    """
    import config as cfg
    from db import Database
    from scanner import scan

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    root = tmp_path / "scan"
    root.mkdir()
    # A large JPEG sitting at the scan root (on disk + to-be-scanned).
    _make_jpeg(str(root / "top.jpg"), 2000, 1500)

    # A pre-existing subfolder photo already in the DB that the caller does
    # NOT want touched because `recursive=False`.
    sub = root / "sub"
    sub.mkdir()
    sub_id = _seed_large_jpeg(db, sub, "in_sub.jpg")

    scan(str(root), db, recursive=False, vireo_dir=str(vireo_dir))

    top_row = db.conn.execute(
        "SELECT id, working_copy_path FROM photos WHERE filename='top.jpg'"
    ).fetchone()
    assert top_row is not None
    assert top_row["working_copy_path"] == f"working/{top_row['id']}.jpg"

    # Subfolder photo is outside the non-recursive scan; must NOT be touched.
    assert not (vireo_dir / "working" / f"{sub_id}.jpg").exists()
    sub_wc = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (sub_id,)
    ).fetchone()["working_copy_path"]
    assert sub_wc is None


def test_scan_scopes_working_copies_to_scan_root(tmp_path, monkeypatch):
    """scan() with a root only extracts working copies for photos under that root.

    Regression: before the fix, scan backfilled working copies library-wide,
    so a fresh import triggered full-size extraction for every pre-existing
    large JPEG in the DB — slow and unrelated to what was just scanned.
    """
    import config as cfg
    from db import Database
    from scanner import scan

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    # Pre-existing large JPEG in the DB, in a folder OUTSIDE the scan root.
    outside = tmp_path / "outside"
    outside.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    outside_id = _seed_large_jpeg(db, outside, "pre.jpg")

    # New folder inside the scan root with its own large JPEG on disk.
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    new_file = scan_root / "new.jpg"
    _make_jpeg(str(new_file), 2000, 1500)

    scan(str(scan_root), db, vireo_dir=str(vireo_dir))

    # The photo inside the scan root gets a working copy.
    inside_row = db.conn.execute(
        "SELECT id, working_copy_path FROM photos WHERE filename='new.jpg'"
    ).fetchone()
    assert inside_row is not None
    assert inside_row["working_copy_path"] == f"working/{inside_row['id']}.jpg"

    # The pre-existing photo outside the scan root is NOT touched.
    assert not (vireo_dir / "working" / f"{outside_id}.jpg").exists()
    outside_wc = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (outside_id,)
    ).fetchone()["working_copy_path"]
    assert outside_wc is None


def test_no_working_copy_for_small_jpeg(tmp_path, monkeypatch):
    """A JPEG within the cap does NOT get a working copy."""
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    src = folder / "small.jpg"
    _make_jpeg(str(src), 800, 600)  # below 1000

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    photo_id = db.add_photo(
        folder_id, "small.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=800, height=600,
    )

    _extract_working_copies(db, str(vireo_dir))

    wc_path = vireo_dir / "working" / f"{photo_id}.jpg"
    assert not wc_path.exists()
    row = db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (photo_id,)
    ).fetchone()
    assert row["working_copy_path"] is None



# ---------------------------------------------------------------------------
# Candidate-count helper (drives the startup gate + before/after totals)
# ---------------------------------------------------------------------------


def test_candidate_count_excludes_small_jpegs(tmp_path, monkeypatch):
    """A row that the extractor would skip must not show up as a candidate.

    Small JPEGs (under ``working_copy_max_size``) are intentionally left
    without working copies, so a library of only small JPEGs has zero
    backfill work to do.
    """
    import config as cfg
    from db import Database
    from scanner import working_copy_backfill_candidate_count

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    src = folder / "small.jpg"
    _make_jpeg(str(src), 800, 600)

    db = Database(str(tmp_path / "test.db"))
    folder_id = db.add_folder(str(folder))
    db.add_photo(
        folder_id, "small.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=800, height=600,
    )

    assert working_copy_backfill_candidate_count(db) == 0


def test_candidate_count_includes_large_jpeg(tmp_path, monkeypatch):
    """An oversized JPEG is a real backfill candidate."""
    import config as cfg
    from db import Database
    from scanner import working_copy_backfill_candidate_count

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    src = folder / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)

    db = Database(str(tmp_path / "test.db"))
    folder_id = db.add_folder(str(folder))
    db.add_photo(
        folder_id, "big.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )

    assert working_copy_backfill_candidate_count(db) == 1


def test_candidate_count_includes_raw(tmp_path, monkeypatch):
    """RAW photos are always candidates regardless of recorded dimensions."""
    import config as cfg
    from db import Database
    from scanner import working_copy_backfill_candidate_count

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "photos"
    folder.mkdir()
    raw = folder / "shot.nef"
    raw.write_bytes(b"\x00" * 16)  # contents irrelevant for the SELECT

    db = Database(str(tmp_path / "test.db"))
    folder_id = db.add_folder(str(folder))
    db.add_photo(
        folder_id, "shot.nef", ".nef",
        file_size=os.path.getsize(str(raw)),
        file_mtime=os.path.getmtime(str(raw)),
        width=None, height=None,
    )

    assert working_copy_backfill_candidate_count(db) == 1


# ---------------------------------------------------------------------------
# Library-wide backfill (used by the startup self-healing job)
# ---------------------------------------------------------------------------


def test_backfill_processes_legacy_null_working_copy_path(tmp_path, monkeypatch):
    """``backfill_working_copies`` covers photos imported before the feature.

    Simulates a row that exists with ``working_copy_path=NULL`` from a prior
    scan that never had ``vireo_dir`` passed in. The new startup pass must
    pick it up library-wide (no ``scope`` argument).
    """
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder_a = tmp_path / "a"
    folder_a.mkdir()
    folder_b = tmp_path / "b"
    folder_b.mkdir()

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    a_id = _seed_large_jpeg(db, folder_a, "a.jpg")
    b_id = _seed_large_jpeg(db, folder_b, "b.jpg")

    result = backfill_working_copies(db, str(vireo_dir))

    # Both photos should now have a working copy on disk and in the DB.
    assert (vireo_dir / "working" / f"{a_id}.jpg").exists()
    assert (vireo_dir / "working" / f"{b_id}.jpg").exists()

    rows = {
        r["id"]: r["working_copy_path"]
        for r in db.conn.execute(
            "SELECT id, working_copy_path FROM photos WHERE id IN (?, ?)",
            (a_id, b_id),
        ).fetchall()
    }
    assert rows[a_id] == f"working/{a_id}.jpg"
    assert rows[b_id] == f"working/{b_id}.jpg"

    assert result["candidates"] == 2
    assert result["remaining"] == 0
    assert result["with_working_copy"] == 2


def test_backfill_skips_already_extracted(tmp_path, monkeypatch):
    """A photo that already has working_copy_path is not re-processed."""
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    pid = _seed_large_jpeg(db, folder, "a.jpg")

    # First pass creates the working copy.
    backfill_working_copies(db, str(vireo_dir))
    wc_path = vireo_dir / "working" / f"{pid}.jpg"
    first_mtime = wc_path.stat().st_mtime

    # Second pass: the row is no longer a candidate.
    result = backfill_working_copies(db, str(vireo_dir))
    assert result["candidates"] == 0
    # File is untouched (no rewrite).
    assert wc_path.stat().st_mtime == first_mtime


def test_backfill_failure_marker_prevents_retry_loop(tmp_path, monkeypatch):
    """A row whose extraction fails is marked and skipped on the next pass.

    Without this guard, every startup would re-attempt every broken file —
    an O(N) waste on each restart.
    """
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    # Register a row whose source file does NOT exist on disk —
    # extract_working_copy will fail.
    folder_id = db.add_folder(str(folder))
    pid = db.add_photo(
        folder_id, "missing.jpg", ".jpg",
        file_size=1000, file_mtime=42.0,
        width=2000, height=1500,
    )

    calls = {"n": 0}
    real = None

    import scanner as _scanner_mod

    def counting_extract(*args, **kwargs):
        calls["n"] += 1
        return False  # always fail

    monkeypatch.setattr(_scanner_mod, "extract_working_copy", counting_extract)

    backfill_working_copies(db, str(vireo_dir))
    assert calls["n"] == 1, "first pass should call extract once"

    row = db.conn.execute(
        "SELECT working_copy_path, working_copy_failed_at,"
        " working_copy_failed_mtime FROM photos WHERE id=?",
        (pid,),
    ).fetchone()
    assert row["working_copy_path"] is None
    assert row["working_copy_failed_at"] is not None
    assert row["working_copy_failed_mtime"] == 42.0

    # Second pass: candidate query must skip this row.
    backfill_working_copies(db, str(vireo_dir))
    assert calls["n"] == 1, "second pass must NOT retry a marked failure"


def test_backfill_failure_retries_when_mtime_changes(tmp_path, monkeypatch):
    """A user-replaced file (different mtime) clears the failure gate."""
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    pid = db.add_photo(
        folder_id, "missing.jpg", ".jpg",
        file_size=1000, file_mtime=42.0,
        width=2000, height=1500,
    )

    calls = {"n": 0}

    import scanner as _scanner_mod

    def counting_extract(*args, **kwargs):
        calls["n"] += 1
        return False

    monkeypatch.setattr(_scanner_mod, "extract_working_copy", counting_extract)

    backfill_working_copies(db, str(vireo_dir))
    assert calls["n"] == 1

    # Simulate the user replacing the file: mtime changes.
    db.conn.execute(
        "UPDATE photos SET file_mtime=? WHERE id=?", (99.0, pid),
    )
    db.conn.commit()

    backfill_working_copies(db, str(vireo_dir))
    assert calls["n"] == 2, "mtime change must clear the failure gate"


def test_backfill_failure_retries_after_grace_period_elapses(tmp_path, monkeypatch):
    """A stale failure marker is bypassed even when the file mtime is unchanged.

    Regression: gating retries solely on ``working_copy_failed_mtime ==
    file_mtime`` permanently suppressed retries for transient failures
    (external drive temporarily disconnected at startup, brief I/O
    blip, etc.). Files whose source bytes never change would never get a
    second chance once that first failure was recorded — undermining the
    self-healing intent. The predicate now also bypasses the gate when the
    failure timestamp is older than the configured grace period.
    """
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    pid = _seed_large_jpeg(db, folder, "a.jpg")
    file_mtime = db.conn.execute(
        "SELECT file_mtime FROM photos WHERE id=?", (pid,)
    ).fetchone()["file_mtime"]

    # Pretend the previous failure was recorded 48 hours ago against the
    # SAME file_mtime. Mtime equality alone would suppress the retry
    # forever; the time-based escape should override it.
    db.conn.execute(
        "UPDATE photos SET working_copy_failed_at = datetime('now', '-48 hours'),"
        " working_copy_failed_mtime = ?"
        " WHERE id = ?",
        (file_mtime, pid),
    )
    db.conn.commit()

    backfill_working_copies(db, str(vireo_dir))

    row = db.conn.execute(
        "SELECT working_copy_path, working_copy_failed_at,"
        " working_copy_failed_mtime FROM photos WHERE id=?",
        (pid,),
    ).fetchone()
    assert row["working_copy_path"] == f"working/{pid}.jpg"
    assert row["working_copy_failed_at"] is None
    assert row["working_copy_failed_mtime"] is None
    assert (vireo_dir / "working" / f"{pid}.jpg").exists()


def test_backfill_failure_does_not_retry_within_grace_period(tmp_path, monkeypatch):
    """A recent failure with unchanged mtime is still suppressed.

    Counterpart to ``test_backfill_failure_retries_after_grace_period_elapses``:
    the time-based escape must only trigger once enough time has passed —
    otherwise we'd be back to the original retry-loop problem on every
    restart.
    """
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    pid = db.add_photo(
        folder_id, "missing.jpg", ".jpg",
        file_size=1000, file_mtime=42.0,
        width=2000, height=1500,
    )
    # Record a very recent failure (~1 minute ago) against the same mtime.
    db.conn.execute(
        "UPDATE photos SET working_copy_failed_at = datetime('now', '-1 minute'),"
        " working_copy_failed_mtime = ?"
        " WHERE id = ?",
        (42.0, pid),
    )
    db.conn.commit()

    calls = {"n": 0}

    import scanner as _scanner_mod

    def counting_extract(*args, **kwargs):
        calls["n"] += 1
        return False

    monkeypatch.setattr(_scanner_mod, "extract_working_copy", counting_extract)

    backfill_working_copies(db, str(vireo_dir))

    assert calls["n"] == 0, (
        "fresh failure marker (within grace period) must suppress retry"
    )


def test_backfill_success_clears_prior_failure_marker(tmp_path, monkeypatch):
    """After a successful extraction, failure columns are reset."""
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    pid = _seed_large_jpeg(db, folder, "a.jpg")
    # Pretend a previous backfill failed against an older mtime.
    db.conn.execute(
        "UPDATE photos SET working_copy_failed_at=datetime('now'),"
        " working_copy_failed_mtime=?"
        " WHERE id=?",
        (1.0, pid),
    )
    db.conn.commit()

    backfill_working_copies(db, str(vireo_dir))

    row = db.conn.execute(
        "SELECT working_copy_path, working_copy_failed_at,"
        " working_copy_failed_mtime FROM photos WHERE id=?",
        (pid,),
    ).fetchone()
    assert row["working_copy_path"] == f"working/{pid}.jpg"
    assert row["working_copy_failed_at"] is None
    assert row["working_copy_failed_mtime"] is None


def test_backfill_progress_callback_streams_per_row(tmp_path, monkeypatch):
    """``progress_callback`` is invoked once per row with (current, total)."""
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    ids = [_seed_large_jpeg(db, folder, f"p{i}.jpg") for i in range(3)]

    events = []
    backfill_working_copies(
        db, str(vireo_dir),
        progress_callback=lambda c, t: events.append((c, t)),
    )

    assert events == [(1, 3), (2, 3), (3, 3)]
    for pid in ids:
        assert (vireo_dir / "working" / f"{pid}.jpg").exists()


def test_backfill_cancel_check_aborts_loop(tmp_path, monkeypatch):
    """``cancel_check`` returning True stops the loop after the current row."""
    import config as cfg
    from db import Database
    from scanner import backfill_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    folder = tmp_path / "a"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    ids = [_seed_large_jpeg(db, folder, f"p{i}.jpg") for i in range(5)]

    # Cancel on second iteration. cancel_check fires *before* the row, so
    # row 0 runs, cancel is requested, row 1 sees cancel and aborts.
    state = {"n": 0}

    def cancel_check():
        state["n"] += 1
        return state["n"] >= 2

    backfill_working_copies(db, str(vireo_dir), cancel_check=cancel_check)

    completed = sum(
        1 for pid in ids
        if (vireo_dir / "working" / f"{pid}.jpg").exists()
    )
    assert completed == 1, f"expected 1 completion before cancel, got {completed}"


# ---------------------------------------------------------------------------
# scan() inline extraction — the new-imports path
# ---------------------------------------------------------------------------


def test_scan_records_failure_marker_for_unreadable_file(tmp_path, monkeypatch):
    """When inline extraction fails during scan(), the row carries a marker.

    Confirms the inline path (not just backfill) records failures, so the
    next backfill pass will respect the marker rather than retrying.
    """
    import config as cfg
    from db import Database
    from scanner import scan

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    root = tmp_path / "scan"
    root.mkdir()
    src = root / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)

    # Force extraction to fail on the post-scan pass.
    import scanner as _scanner_mod
    monkeypatch.setattr(_scanner_mod, "extract_working_copy", lambda *a, **k: False)

    scan(str(root), db, vireo_dir=str(vireo_dir))

    row = db.conn.execute(
        "SELECT working_copy_path, working_copy_failed_at,"
        " working_copy_failed_mtime, file_mtime FROM photos"
        " WHERE filename='big.jpg'"
    ).fetchone()
    assert row["working_copy_path"] is None
    assert row["working_copy_failed_at"] is not None
    assert row["working_copy_failed_mtime"] == row["file_mtime"]


def test_scan_progress_callback_not_clobbered_by_working_copy_phase(tmp_path, monkeypatch):
    """The post-scan WC phase must not overwrite scan totals via progress_callback.

    Regression: ``_extract_working_copies`` was called with the same
    ``progress_callback`` the scan loop used to report per-file totals. In
    callers like the import job (vireo/app.py) the callback writes
    ``current``/``total`` into a shared ``job["progress"]`` dict that
    downstream phases read for the scan count. Passing the callback in
    again caused the WC phase to overwrite that total with the
    working-copy total — visually jumping the bar backward and feeding
    the wrong scan_count to later phases.
    """
    import config as cfg
    from db import Database
    from scanner import scan

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))

    root = tmp_path / "scan"
    root.mkdir()
    # Two scanned files; only one is large enough to need a working copy.
    # Without the fix, the WC phase emits (1, 1) and would clobber the
    # scan's final (2, 2).
    _make_jpeg(str(root / "small.jpg"), 600, 400)
    _make_jpeg(str(root / "big.jpg"), 2000, 1500)

    events = []

    def progress_cb(current, total):
        events.append((current, total))

    scan(str(root), db, progress_callback=progress_cb, vireo_dir=str(vireo_dir))

    assert events, "scan should report progress for the scan loop"
    # The last reported total must be the SCAN total (2 files), not the
    # working-copy total (1 file). Any (_, 1) appearing after a (_, 2)
    # would mean the WC phase overwrote the scan totals.
    seen_scan_total = False
    for _current, total in events:
        if total == 2:
            seen_scan_total = True
        elif seen_scan_total and total == 1:
            raise AssertionError(
                f"Working-copy phase clobbered scan totals: {events}"
            )
    assert seen_scan_total, f"never observed scan total of 2: {events}"

    # Sanity check: the working copy itself was still produced.
    big_row = db.conn.execute(
        "SELECT id, working_copy_path FROM photos WHERE filename='big.jpg'"
    ).fetchone()
    assert big_row["working_copy_path"] == f"working/{big_row['id']}.jpg"


# ---------------------------------------------------------------------------
# Startup self-healing kickoff (app.create_app -> ephemeral JobRunner job)
# ---------------------------------------------------------------------------


def test_startup_backfill_skips_when_no_candidates(tmp_path, monkeypatch):
    """If no photo needs work, no working_copy_backfill job is started."""
    import os

    import config as cfg
    import models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(models, "DEFAULT_MODELS_DIR", str(tmp_path / "vireo-models"))
    monkeypatch.setattr(models, "CONFIG_PATH", str(tmp_path / "models.json"))

    from app import create_app
    from db import Database

    db_path = str(tmp_path / "test.db")
    thumb_dir = str(tmp_path / "thumbs")
    os.makedirs(thumb_dir)
    Database(db_path)  # create empty DB with workspace

    app = create_app(db_path=db_path, thumb_cache_dir=thumb_dir, api_token="t")

    # Drive the kickoff synchronously instead of waiting for the 5s Timer.
    app._kickoff_working_copy_backfill()

    backfill_jobs = [
        j for j in app._job_runner.list_jobs()
        if j["type"] == "working_copy_backfill"
    ]
    assert backfill_jobs == []


def test_startup_backfill_skips_for_small_jpeg_only_library(tmp_path, monkeypatch):
    """Small JPEGs (under working_copy_max_size) are intentionally not extracted.

    The startup gate must skip them rather than launching a no-op backfill on
    every restart. Regression test for a library that contains only small
    JPEGs — naive ``working_copy_path IS NULL`` check would fire forever.
    """
    import os

    import config as cfg
    import models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(models, "DEFAULT_MODELS_DIR", str(tmp_path / "vireo-models"))
    monkeypatch.setattr(models, "CONFIG_PATH", str(tmp_path / "models.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    from app import create_app
    from db import Database

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    thumb_dir = vireo_dir / "thumbnails"
    thumb_dir.mkdir()
    db_path = str(vireo_dir / "test.db")

    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    src = photos_dir / "small.jpg"
    _make_jpeg(str(src), 800, 600)  # below the 1000 cap

    db = Database(db_path)
    folder_id = db.add_folder(str(photos_dir))
    db.add_photo(
        folder_id, "small.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=800, height=600,
    )
    db.conn.close()

    app = create_app(db_path=db_path, thumb_cache_dir=str(thumb_dir), api_token="t")
    app._kickoff_working_copy_backfill()

    backfill_jobs = [
        j for j in app._job_runner.list_jobs()
        if j["type"] == "working_copy_backfill"
    ]
    assert backfill_jobs == [], (
        "small-JPEG-only library should not trigger working_copy_backfill"
    )


def test_startup_backfill_runs_when_candidates_exist(tmp_path, monkeypatch):
    """A photo with NULL working_copy_path triggers an ephemeral backfill job
    that produces the working copy and completes successfully.
    """
    import os

    import config as cfg
    import models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(models, "DEFAULT_MODELS_DIR", str(tmp_path / "vireo-models"))
    monkeypatch.setattr(models, "CONFIG_PATH", str(tmp_path / "models.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    from app import create_app
    from db import Database

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    thumb_dir = vireo_dir / "thumbnails"
    thumb_dir.mkdir()
    db_path = str(vireo_dir / "test.db")

    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    src = photos_dir / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)

    db = Database(db_path)
    folder_id = db.add_folder(str(photos_dir))
    pid = db.add_photo(
        folder_id, "big.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )
    db.conn.close()

    app = create_app(db_path=db_path, thumb_cache_dir=str(thumb_dir), api_token="t")
    app._kickoff_working_copy_backfill()

    job = _wait_for_backfill_terminal(app._job_runner)
    assert job["status"] == "completed", f"job: {job}"
    assert job.get("ephemeral") is True

    # The working copy actually exists.
    assert (vireo_dir / "working" / f"{pid}.jpg").exists()

    # The DB row was updated with the working copy path.
    db2 = Database(db_path)
    row = db2.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (pid,)
    ).fetchone()
    assert row["working_copy_path"] == f"working/{pid}.jpg"


def test_startup_backfill_does_not_persist_to_history(tmp_path, monkeypatch):
    """Ephemeral backfill job must NOT land in job_history.

    Otherwise every restart adds a noise row.
    """
    import os

    import config as cfg
    import models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(models, "DEFAULT_MODELS_DIR", str(tmp_path / "vireo-models"))
    monkeypatch.setattr(models, "CONFIG_PATH", str(tmp_path / "models.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    from app import create_app
    from db import Database

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    thumb_dir = vireo_dir / "thumbnails"
    thumb_dir.mkdir()
    db_path = str(vireo_dir / "test.db")

    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    src = photos_dir / "big.jpg"
    _make_jpeg(str(src), 2000, 1500)

    db = Database(db_path)
    folder_id = db.add_folder(str(photos_dir))
    db.add_photo(
        folder_id, "big.jpg", ".jpg",
        file_size=os.path.getsize(str(src)),
        file_mtime=os.path.getmtime(str(src)),
        width=2000, height=1500,
    )
    db.conn.close()

    app = create_app(db_path=db_path, thumb_cache_dir=str(thumb_dir), api_token="t")
    app._kickoff_working_copy_backfill()

    _wait_for_backfill_terminal(app._job_runner)

    db2 = Database(db_path)
    rows = db2.conn.execute(
        "SELECT id FROM job_history WHERE type='working_copy_backfill'"
    ).fetchall()
    assert rows == [], "ephemeral job must not persist to history"


def _wait_for_backfill_status(runner, statuses, timeout=30.0, poll=0.02):
    """Poll until the backfill job reports one of *statuses*; return the job.

    Used by the pause tests, which need to observe transient states
    (``pausing`` -> ``paused``) that ``_wait_for_backfill_terminal`` would
    poll straight past.
    """
    import time
    deadline = time.time() + timeout
    last_seen = None
    while time.time() < deadline:
        jobs = [
            j for j in runner.list_jobs()
            if j["type"] == "working_copy_backfill"
        ]
        if jobs:
            last_seen = jobs[0]
            if last_seen["status"] in statuses:
                return last_seen
        time.sleep(poll)
    raise AssertionError(
        f"working_copy_backfill never reached {sorted(statuses)} within "
        f"{timeout}s; last seen status={(last_seen or {}).get('status')!r}"
    )


def _prepare_backfill_app(tmp_path, monkeypatch, filenames):
    """Create an app whose catalog has one oversized JPEG per name in
    *filenames*, all awaiting a working copy. Returns (app, vireo_dir, ids).
    """
    import os

    import config as cfg
    import models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(models, "DEFAULT_MODELS_DIR", str(tmp_path / "vireo-models"))
    monkeypatch.setattr(models, "CONFIG_PATH", str(tmp_path / "models.json"))
    cfg.save({**cfg.DEFAULTS, "working_copy_max_size": 1000, "working_copy_quality": 90})

    from app import create_app
    from db import Database

    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    thumb_dir = vireo_dir / "thumbnails"
    thumb_dir.mkdir()
    db_path = str(vireo_dir / "test.db")

    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()

    db = Database(db_path)
    folder_id = db.add_folder(str(photos_dir))
    ids = []
    for name in filenames:
        src = photos_dir / name
        _make_jpeg(str(src), 2000, 1500)
        ids.append(db.add_photo(
            folder_id, name, ".jpg",
            file_size=os.path.getsize(str(src)),
            file_mtime=os.path.getmtime(str(src)),
            width=2000, height=1500,
        ))
    db.conn.close()

    app = create_app(
        db_path=db_path, thumb_cache_dir=str(thumb_dir), api_token="t",
    )
    return app, vireo_dir, ids


def test_startup_backfill_job_is_pausable(tmp_path, monkeypatch):
    """The backfill must register as pausable.

    ``JobRunner.pause_job`` refuses any job without this flag, and the jobs
    page only renders Pause/Resume for ``job.pausable`` — so without it a
    multi-day backfill on a large RAW library can only be cancelled, never
    stood down and picked back up.
    """
    app, _vireo_dir, _ids = _prepare_backfill_app(
        tmp_path, monkeypatch, ["big.jpg"],
    )
    app._kickoff_working_copy_backfill()

    job = _wait_for_backfill_terminal(app._job_runner)
    assert job["status"] == "completed", f"job: {job}"
    assert job.get("pausable") is True


def test_startup_backfill_pause_parks_between_rows_and_resumes(
    tmp_path, monkeypatch,
):
    """Pause parks the worker between rows; resume finishes the remaining work.

    Gates the extractor so the pause request lands while row 1 is in flight,
    then asserts the worker stops *after* that row (no further extraction
    while paused) and completes every candidate once resumed. Pausing must
    not lose the rows already written.
    """
    import threading

    import scanner

    app, vireo_dir, ids = _prepare_backfill_app(
        tmp_path, monkeypatch, ["a.jpg", "b.jpg", "c.jpg"],
    )

    real_extract = scanner.extract_working_copy
    first_call_started = threading.Event()
    release_first_call = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def gated_extract(*args, **kwargs):
        with calls_lock:
            calls.append(args[0])
            is_first = len(calls) == 1
        if is_first:
            first_call_started.set()
            # Hold row 1 inside the extractor so the test can request the
            # pause while the worker is past its checkpoint for this row.
            assert release_first_call.wait(timeout=30), "test never released row 1"
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(scanner, "extract_working_copy", gated_extract)

    app._kickoff_working_copy_backfill()

    assert first_call_started.wait(timeout=30), "backfill never started extracting"
    job_id = [
        j for j in app._job_runner.list_jobs()
        if j["type"] == "working_copy_backfill"
    ][0]["id"]

    assert app._job_runner.pause_job(job_id) is True, (
        "pause_job refused the backfill — pausable flag missing?"
    )
    release_first_call.set()

    paused = _wait_for_backfill_status(app._job_runner, {"paused"})
    assert paused["id"] == job_id

    # Parked, not merely slow: no further row is extracted while paused.
    import time
    time.sleep(0.5)
    with calls_lock:
        assert len(calls) == 1, (
            f"worker kept extracting while paused: {calls}"
        )

    # Row 1's result survives the pause rather than being rolled back.
    assert (vireo_dir / "working" / f"{ids[0]}.jpg").exists()

    assert app._job_runner.resume_job(job_id) is True

    job = _wait_for_backfill_terminal(app._job_runner)
    assert job["status"] == "completed", f"job: {job}"
    with calls_lock:
        assert len(calls) == 3, f"not every candidate was extracted: {calls}"
    for pid in ids:
        assert (vireo_dir / "working" / f"{pid}.jpg").exists()


def test_startup_backfill_skips_row_when_id_reused_during_pause(
    tmp_path, monkeypatch,
):
    """A row whose photo id was reused during pause must not be extracted.

    Regression for a P1 codex review finding on PR #1607: because
    ``photos.id`` is not AUTOINCREMENT, SQLite reuses a deleted photo's
    id for the next INSERT. If that happens while the backfill is parked
    on a pause request, the resumed loop's snapshot still points at the
    deleted photo's folder/filename/size/mtime — extracting from that
    stale source and updating ``working_copy_path WHERE id=?`` would
    silently attach the deleted photo's JPEG to the newly-imported row.

    Sets up two candidates, pauses after row 1 finishes, deletes row 2,
    reinserts a different file (which reuses the freed id), then resumes.
    The resumed worker must skip the stale row rather than write a
    working_copy_path onto the replacement.
    """
    import threading

    import scanner

    app, vireo_dir, ids = _prepare_backfill_app(
        tmp_path, monkeypatch, ["a.jpg", "b.jpg"],
    )
    stale_id = ids[1]

    real_extract = scanner.extract_working_copy
    first_call_started = threading.Event()
    release_first_call = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def gated_extract(*args, **kwargs):
        with calls_lock:
            calls.append(args[0])
            is_first = len(calls) == 1
        if is_first:
            first_call_started.set()
            assert release_first_call.wait(timeout=30), "test never released row 1"
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(scanner, "extract_working_copy", gated_extract)

    app._kickoff_working_copy_backfill()

    assert first_call_started.wait(timeout=30), "backfill never started extracting"
    job_id = [
        j for j in app._job_runner.list_jobs()
        if j["type"] == "working_copy_backfill"
    ][0]["id"]

    assert app._job_runner.pause_job(job_id) is True
    release_first_call.set()

    paused = _wait_for_backfill_status(app._job_runner, {"paused"})
    assert paused["id"] == job_id

    # Delete row 2's photo and reinsert a different file. On an
    # ``INTEGER PRIMARY KEY`` without AUTOINCREMENT SQLite reuses the
    # freed id (which is >max(id) after a delete of the highest row), so
    # the replacement lands on the same photo_id the snapshot points at.
    from db import Database
    db_path = app.config["DB_PATH"]
    admin_db = Database(db_path)
    stale_row = admin_db.conn.execute(
        "SELECT p.folder_id, f.path AS folder_path"
        " FROM photos p JOIN folders f ON f.id=p.folder_id"
        " WHERE p.id=?",
        (stale_id,),
    ).fetchone()
    assert stale_row is not None
    folder_id = stale_row["folder_id"]
    photos_dir = stale_row["folder_path"]
    admin_db.conn.execute("DELETE FROM photos WHERE id=?", (stale_id,))
    admin_db.conn.commit()

    replacement = os.path.join(photos_dir, "replacement.jpg")
    # Different dimensions → guaranteed different file_size so the
    # identity guard flags the mismatch even if the two saves land in
    # the same coarse mtime bucket on this filesystem.
    _make_jpeg(replacement, 1600, 1200)
    new_id = admin_db.add_photo(
        folder_id, "replacement.jpg", ".jpg",
        file_size=os.path.getsize(replacement),
        file_mtime=os.path.getmtime(replacement),
        width=1600, height=1200,
    )
    admin_db.conn.close()
    assert new_id == stale_id, (
        f"test setup: expected id reuse (freed {stale_id}, got {new_id}); "
        "SQLite behavior may have changed"
    )

    assert app._job_runner.resume_job(job_id) is True

    job = _wait_for_backfill_terminal(app._job_runner)
    assert job["status"] == "completed", f"job: {job}"

    # The stale snapshot row must not have been extracted: the source
    # path (photos/b.jpg) no longer exists on disk, but even if it did,
    # writing that JPEG under working/<stale_id>.jpg and attaching it to
    # the replacement row is the bug this guard prevents.
    with calls_lock:
        for src in calls[1:]:
            assert "b.jpg" not in src, (
                f"worker extracted the stale row's source after resume: {src}"
            )

    # And the replacement row's working_copy_path must not have been set
    # by this run — that's the deleted photo's identity, not the new one.
    verify_db = Database(db_path)
    wc_path = verify_db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (stale_id,),
    ).fetchone()["working_copy_path"]
    verify_db.conn.close()
    assert wc_path is None, (
        f"replacement photo (id={stale_id}) received a working_copy_path "
        f"from the stale snapshot: {wc_path!r}"
    )


def test_startup_backfill_discards_orphan_when_id_reused_during_extraction(
    tmp_path, monkeypatch,
):
    """A row whose id is reused *during* extraction must not leave an orphan.

    Regression for a P2 codex review finding on PR #1607: the
    identity-guarded UPDATE correctly matches zero rows when the row was
    deleted (and its id reused for a new import) between the pre-extraction
    snapshot check and the post-extraction commit — but the just-written
    ``working/<reused-id>.jpg`` was left on disk. A later quota pass would
    treat those bytes as the replacement row's rendition, and if
    ``_evict_once`` reclaimed the file it would stamp
    ``working_copy_evicted_mtime`` on the replacement, suppressing its own
    backfill.

    Holds the extractor *after* it writes ``wc_abs`` so the test can swap
    the row's identity (delete + reinsert to reuse the id) before releasing
    it. After the job completes, the orphan file must be gone and the
    replacement row's ``working_copy_path`` must remain NULL.
    """
    import threading

    import scanner
    from db import Database

    app, vireo_dir, ids = _prepare_backfill_app(
        tmp_path, monkeypatch, ["target.jpg"],
    )
    target_id = ids[0]

    real_extract = scanner.extract_working_copy
    extract_started = threading.Event()
    release_extract = threading.Event()

    def gated_extract(*args, **kwargs):
        # Let real extraction write ``wc_abs`` atomically.
        result = real_extract(*args, **kwargs)
        extract_started.set()
        # Hold here so the test can delete the row and reinsert a
        # different file at the same reused id before the guarded
        # UPDATE runs — the exact window this fix closes.
        assert release_extract.wait(timeout=30), (
            "test never released extraction"
        )
        return result

    monkeypatch.setattr(scanner, "extract_working_copy", gated_extract)

    app._kickoff_working_copy_backfill()

    assert extract_started.wait(timeout=30), "extraction never started"

    # Row was written; now swap the identity while the worker is parked
    # between the write and the guarded UPDATE. INTEGER PRIMARY KEY
    # without AUTOINCREMENT reuses the freed id for the next INSERT.
    db_path = app.config["DB_PATH"]
    admin_db = Database(db_path)
    target_row = admin_db.conn.execute(
        "SELECT p.folder_id, f.path AS folder_path"
        " FROM photos p JOIN folders f ON f.id=p.folder_id"
        " WHERE p.id=?",
        (target_id,),
    ).fetchone()
    assert target_row is not None
    folder_id = target_row["folder_id"]
    photos_dir = target_row["folder_path"]
    admin_db.conn.execute("DELETE FROM photos WHERE id=?", (target_id,))
    admin_db.conn.commit()

    replacement = os.path.join(photos_dir, "replacement.jpg")
    # Different dimensions → guaranteed different file_size so the
    # identity guard flags the mismatch even if the two saves land in
    # the same coarse mtime bucket on this filesystem.
    _make_jpeg(replacement, 1600, 1200)
    new_id = admin_db.add_photo(
        folder_id, "replacement.jpg", ".jpg",
        file_size=os.path.getsize(replacement),
        file_mtime=os.path.getmtime(replacement),
        width=1600, height=1200,
    )
    admin_db.conn.close()
    assert new_id == target_id, (
        f"test setup: expected id reuse (freed {target_id}, got {new_id})"
    )

    wc_abs = vireo_dir / "working" / f"{target_id}.jpg"
    assert wc_abs.exists(), (
        "extraction should have atomically published wc_abs before "
        "the gate held it"
    )

    release_extract.set()

    job = _wait_for_backfill_terminal(app._job_runner)
    assert job["status"] == "completed", f"job: {job}"

    # The orphan bytes at working/<reused-id>.jpg must have been removed
    # under the publication guard; otherwise a later quota pass would
    # attribute them to the replacement row.
    assert not wc_abs.exists(), (
        f"orphan working copy at {wc_abs} was not cleaned up after the "
        "identity-guarded UPDATE matched no row"
    )

    # And the replacement row's working_copy_path must remain NULL —
    # the just-extracted bytes are from the deleted photo, not this one.
    verify_db = Database(db_path)
    wc_path = verify_db.conn.execute(
        "SELECT working_copy_path FROM photos WHERE id=?", (target_id,),
    ).fetchone()["working_copy_path"]
    verify_db.conn.close()
    assert wc_path is None, (
        f"replacement photo (id={target_id}) received a working_copy_path "
        f"from the stale snapshot: {wc_path!r}"
    )


def _make_noisy_jpeg(path, width, height):
    """A high-entropy JPEG so extracted working copies stay large enough to
    exercise the quota tracker (uniform-gray JPEGs compress to a few KB)."""
    import numpy as np

    rng = np.random.default_rng(1234)
    arr = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(str(path), "JPEG", quality=95)


def _photo_id_of_file(db, folder_id, filename, path):
    return db.add_photo(
        folder_id, filename, ".jpg",
        file_size=os.path.getsize(str(path)),
        file_mtime=os.path.getmtime(str(path)),
        width=2000, height=1500,
    )


def test_batch_generation_stops_when_quota_exhausted(tmp_path, monkeypatch):
    """A single backfill batch cannot generate multiples of the quota.

    Regression: without incremental enforcement, a library much larger than
    ``working_copy_cache_max_mb`` would produce the entire set of working
    copies before the post-loop eviction ran, temporarily using far more
    disk than the configured cap. The loop now stops once cumulative new
    bytes reach the quota, and the post-loop enforce reclaims to fit.
    """
    import config as cfg
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    # A tiny 1 MB quota with noisy JPEGs (~700 KB each at 1000 px q=90) means
    # two files fill the batch cap; the rest must be deferred.
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    photo_ids = []
    for i in range(6):
        src = folder / f"big-{i}.jpg"
        _make_noisy_jpeg(src, 2000, 1500)
        photo_ids.append(
            _photo_id_of_file(db, folder_id, f"big-{i}.jpg", src)
        )

    _extract_working_copies(db, str(vireo_dir))

    working_dir = vireo_dir / "working"
    on_disk = list(working_dir.glob("*.jpg")) if working_dir.exists() else []
    total_bytes = sum(p.stat().st_size for p in on_disk)
    quota_bytes = 1 * 1024 * 1024
    assert total_bytes <= quota_bytes, (
        f"post-loop enforce must keep on-disk usage within the {quota_bytes} "
        f"byte quota; observed {total_bytes} bytes across {len(on_disk)} files"
    )
    # And the loop must NOT have generated all six files (each ~700 KB).
    assert len(on_disk) < len(photo_ids), (
        "batch generation must stop before producing multiples of the quota; "
        f"produced {len(on_disk)} of {len(photo_ids)} candidates"
    )


def test_incremental_enforcement_bounds_transient_overshoot(tmp_path, monkeypatch):
    """Mid-batch enforce runs so transient usage stays close to the quota.

    Records every call to ``evict_if_over_quota`` during a batch that
    generates significantly more than the quota — at least one call must
    fire before the post-loop enforce so the transient overshoot is
    bounded.
    """
    import config as cfg
    import working_copy_cache
    from db import Database
    from scanner import _extract_working_copies

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    folder_id = db.add_folder(str(folder))
    for i in range(4):
        src = folder / f"big-{i}.jpg"
        _make_noisy_jpeg(src, 2000, 1500)
        _photo_id_of_file(db, folder_id, f"big-{i}.jpg", src)

    call_sites = []
    real_evict = working_copy_cache.evict_if_over_quota

    def tracking_evict(*args, **kwargs):
        call_sites.append(True)
        return real_evict(*args, **kwargs)

    # scanner reimports evict_if_over_quota via ``from working_copy_cache
    # import evict_if_over_quota`` INSIDE _extract_working_copies, so the
    # local binding resolves against this attribute at call time.
    monkeypatch.setattr(
        working_copy_cache, "evict_if_over_quota", tracking_evict,
    )

    _extract_working_copies(db, str(vireo_dir))

    # Post-loop enforce + at least one incremental enforce = >= 2 calls.
    assert len(call_sites) >= 2, (
        f"expected incremental enforcement to fire during a large batch; "
        f"observed {len(call_sites)} evict_if_over_quota calls"
    )


def test_capacity_deferred_rows_do_not_retrigger_backfill(tmp_path, monkeypatch):
    """Rows deferred by the quota stop must not re-trigger startup backfill.

    Regression: when ``_extract_working_copies`` breaks out of the loop
    because cumulative new bytes reached the quota, previously the
    unprocessed rows still satisfied ``_working_copy_candidate_predicate``.
    The next launch's startup gate saw a non-zero candidate count and
    kicked off another backfill, which wrote yet another quota-sized
    batch only to evict the previous one — churning disk on every
    restart without ever changing steady-state usage.
    """
    import config as cfg
    from db import Database
    from scanner import (
        _extract_working_copies,
        working_copy_backfill_candidate_count,
    )

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        for i in range(6):
            src = folder / f"big-{i}.jpg"
            _make_noisy_jpeg(src, 2000, 1500)
            _photo_id_of_file(db, folder_id, f"big-{i}.jpg", src)

        _extract_working_copies(db, str(vireo_dir))

        # After the first pass with a 1 MB quota against ~700 KB files, the
        # loop must have stopped and marked the rest deferred so the startup
        # gate sees no eligible candidates until the quota or source changes.
        remaining = working_copy_backfill_candidate_count(db)
        assert remaining == 0, (
            "expected capacity-deferred rows to be excluded from the "
            f"candidate predicate; still {remaining} eligible"
        )

        # A subsequent run must be a no-op: no additional files decoded,
        # no additional bytes written on disk (i.e. no churn).
        working_dir = vireo_dir / "working"
        before = sorted(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in working_dir.glob("*.jpg")
        )
        _extract_working_copies(db, str(vireo_dir))
        after = sorted(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in working_dir.glob("*.jpg")
        )
        assert after == before, (
            "second backfill pass must not regenerate any working copy "
            "while the quota is unchanged (churn regression)"
        )

        # Raising the quota clears the deferred markers and the deferred rows
        # become eligible again — the exact escape hatch we want to preserve.
        db.conn.execute(
            "UPDATE photos SET working_copy_evicted_mtime=NULL"
            " WHERE working_copy_path IS NULL"
        )
        db.conn.commit()
        assert working_copy_backfill_candidate_count(db) > 0
    finally:
        db.close()


def test_capacity_deferral_does_not_mark_reused_photo_id(
    tmp_path, monkeypatch,
):
    """A delete/re-import during a long backfill keeps the new row eligible.

    ``photos.id`` can reuse the highest deleted rowid. The capacity stop must
    therefore validate the candidate identity captured before extraction,
    rather than stamping whichever row happens to own that id later.
    """
    import config as cfg
    import scanner
    import working_copy_cache
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        photo_ids = []
        for index in range(3):
            source = folder / f"candidate-{index}.jpg"
            _make_jpeg(str(source), 2000, 1500)
            photo_ids.append(
                _photo_id_of_file(db, folder_id, source.name, source)
            )

        def sized_extract(_source, output, **_kwargs):
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(output, "wb") as handle:
                handle.truncate(600_000)
            return True

        monkeypatch.setattr(scanner, "extract_working_copy", sized_extract)

        real_evict = working_copy_cache.evict_if_over_quota
        eviction_calls = 0
        replacement_id = None

        def replace_pending_row_after_second_eviction(*args, **kwargs):
            nonlocal eviction_calls, replacement_id
            result = real_evict(*args, **kwargs)
            eviction_calls += 1
            if eviction_calls == 2:
                pending_id = photo_ids[-1]
                db.conn.execute("DELETE FROM photos WHERE id=?", (pending_id,))
                db.conn.commit()
                replacement_id = db.add_photo(
                    folder_id, "replacement.jpg", ".jpg",
                    file_size=321, file_mtime=999.0,
                    width=2000, height=1500,
                )
                assert replacement_id == pending_id
            return result

        monkeypatch.setattr(
            working_copy_cache,
            "evict_if_over_quota",
            replace_pending_row_after_second_eviction,
        )

        scanner._extract_working_copies(db, str(vireo_dir))

        assert replacement_id == photo_ids[-1]
        replacement = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id=?",
            (replacement_id,),
        ).fetchone()
        assert replacement["working_copy_path"] is None
        assert replacement["working_copy_evicted_mtime"] is None
        assert scanner.working_copy_backfill_candidate_count(db) == 1
    finally:
        db.close()


def test_oversized_copy_does_not_defer_later_fitting_candidate(
    tmp_path, monkeypatch,
):
    """An individually oversized rendition must not stop the batch.

    The incremental quota pass removes a generated file larger than the
    entire budget. Its reclaimed bytes must not count toward the batch stop,
    or every later candidate is marked capacity-deferred even when it fits.
    """
    import config as cfg
    import scanner
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    working_dir = vireo_dir / "working"
    working_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        oversized_source = folder / "oversized.jpg"
        fitting_source = folder / "fitting.jpg"
        _make_jpeg(str(oversized_source), 2000, 1500)
        _make_jpeg(str(fitting_source), 2000, 1500)
        oversized_id = _photo_id_of_file(
            db, folder_id, oversized_source.name, oversized_source,
        )
        fitting_id = _photo_id_of_file(
            db, folder_id, fitting_source.name, fitting_source,
        )

        generated_sources = []

        def sized_extract(source, output, **_kwargs):
            generated_sources.append(os.path.basename(source))
            size = (
                1024 * 1024 + 1
                if os.path.basename(source) == oversized_source.name
                else 128 * 1024
            )
            with open(output, "wb") as handle:
                handle.truncate(size)
            return True

        monkeypatch.setattr(scanner, "extract_working_copy", sized_extract)

        scanner._extract_working_copies(db, str(vireo_dir))

        assert generated_sources == [
            oversized_source.name,
            fitting_source.name,
        ]
        assert not (working_dir / f"{oversized_id}.jpg").exists()
        assert (working_dir / f"{fitting_id}.jpg").exists()
        fitting_row = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id=?",
            (fitting_id,),
        ).fetchone()
        assert fitting_row["working_copy_path"] == (
            f"working/{fitting_id}.jpg"
        )
        assert fitting_row["working_copy_evicted_mtime"] is None
    finally:
        db.close()


def test_backfill_adopts_quota_increase_before_deferring_rows(
    tmp_path, monkeypatch,
):
    """A mid-batch quota raise cannot leave stale deferred markers."""
    import config as cfg
    import scanner
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    (vireo_dir / "working").mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        photo_ids = []
        for index in range(3):
            source = folder / f"candidate-{index}.jpg"
            _make_jpeg(str(source), 2000, 1500)
            photo_ids.append(
                _photo_id_of_file(
                    db, folder_id, source.name, source,
                )
            )

        generated = 0

        def extract_and_raise_quota(_source, output, **_kwargs):
            nonlocal generated
            generated += 1
            with open(output, "wb") as handle:
                handle.truncate(600_000)
            if generated == 2:
                cfg.set("working_copy_cache_max_mb", 2)
            return True

        monkeypatch.setattr(
            scanner, "extract_working_copy", extract_and_raise_quota,
        )

        scanner._extract_working_copies(db, str(vireo_dir))

        assert generated == 3
        rows = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id IN (?, ?, ?) ORDER BY id",
            photo_ids,
        ).fetchall()
        assert all(row["working_copy_path"] for row in rows)
        assert all(row["working_copy_evicted_mtime"] is None for row in rows)
    finally:
        db.close()


def test_backfill_enforces_quota_lowered_during_extraction(
    tmp_path, monkeypatch,
):
    """A settings save cannot let an in-flight batch refill the old quota."""
    import config as cfg
    import scanner
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 10,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        photo_ids = []
        for i in range(2):
            src = folder / f"big-{i}.jpg"
            _make_jpeg(str(src), 2000, 1500)
            photo_ids.append(
                _photo_id_of_file(db, folder_id, f"big-{i}.jpg", src)
            )

        generated = 0

        def extract_and_lower_quota(_source, output, **_kwargs):
            nonlocal generated
            generated += 1
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(output, "wb") as handle:
                handle.truncate(600_000)
            cfg.set("working_copy_cache_max_mb", 0)
            return True

        monkeypatch.setattr(
            scanner, "extract_working_copy", extract_and_lower_quota,
        )

        scanner._extract_working_copies(db, str(vireo_dir))

        assert generated == 1
        assert not list((vireo_dir / "working").glob("*.jpg"))
        rows = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id IN (?, ?) ORDER BY id",
            photo_ids,
        ).fetchall()
        assert all(row["working_copy_path"] is None for row in rows)
        assert all(row["working_copy_evicted_mtime"] is not None for row in rows)
    finally:
        db.close()


def test_batch_stops_when_quota_rotates_fitting_working_copies(
    tmp_path, monkeypatch,
):
    """One-for-one eviction of fitting copies stops further decoding."""
    import config as cfg
    import scanner
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    (vireo_dir / "working").mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        for index in range(8):
            source = folder / f"candidate-{index}.jpg"
            _make_jpeg(str(source), 2000, 1500)
            _photo_id_of_file(db, folder_id, source.name, source)

        generated = 0

        def fitting_extract(_source, output, **_kwargs):
            nonlocal generated
            generated += 1
            with open(output, "wb") as handle:
                handle.truncate(600_000)
            return True

        monkeypatch.setattr(scanner, "extract_working_copy", fitting_extract)

        scanner._extract_working_copies(db, str(vireo_dir))

        assert generated == 2
        assert scanner.working_copy_backfill_candidate_count(db) == 0
        on_disk = list((vireo_dir / "working").glob("*.jpg"))
        assert len(on_disk) == 1
        assert on_disk[0].stat().st_size == 600_000
    finally:
        db.close()


def test_scanner_does_not_track_copy_evicted_before_catalog_commit(
    tmp_path, monkeypatch,
):
    """A lost publication cannot leave a stale working_copy_path."""
    import config as cfg
    import scanner
    from db import Database

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg.save({
        **cfg.DEFAULTS,
        "working_copy_max_size": 1000,
        "working_copy_quality": 90,
        "working_copy_cache_max_mb": 1,
    })

    folder = tmp_path / "photos"
    folder.mkdir()
    source = folder / "candidate.jpg"
    _make_jpeg(str(source), 2000, 1500)
    vireo_dir = tmp_path / "vireo"
    vireo_dir.mkdir()
    (vireo_dir / "working").mkdir()
    db = Database(str(vireo_dir / "test.db"))
    try:
        folder_id = db.add_folder(str(folder))
        photo_id = _photo_id_of_file(
            db, folder_id, source.name, source,
        )

        def publish_then_lose(_source, output, **_kwargs):
            with open(output, "wb") as handle:
                handle.write(b"published")
            os.unlink(output)
            db.conn.execute(
                "UPDATE photos SET working_copy_path=NULL, "
                "working_copy_evicted_mtime=COALESCE(file_mtime, -1) "
                "WHERE id=?",
                (photo_id,),
            )
            db.conn.commit()
            return True

        monkeypatch.setattr(
            scanner, "extract_working_copy", publish_then_lose,
        )

        scanner._extract_working_copies(db, str(vireo_dir))

        row = db.conn.execute(
            "SELECT working_copy_path, working_copy_evicted_mtime "
            "FROM photos WHERE id=?",
            (photo_id,),
        ).fetchone()
        assert row["working_copy_path"] is None
        assert row["working_copy_evicted_mtime"] is not None
    finally:
        db.close()
