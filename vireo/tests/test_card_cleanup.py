"""Unit tests for card cleanup: manifest IO, classification, and the
scan/delete safety gates. Spec:
docs/superpowers/specs/2026-08-07-card-cleanup-design.md
"""
import os
from datetime import UTC, datetime, timedelta

import card_cleanup
import pytest
from card_cleanup import (
    ManifestError,
    load_manifest,
    manifest_path,
    prune_manifests,
    write_manifest,
)


def _manifest(tmp_path, **overrides):
    m = {
        "schema_version": card_cleanup.MANIFEST_SCHEMA_VERSION,
        "scan_job_id": "scan-1",
        "created_at": datetime.now(UTC).isoformat(),
        "source_root": str(tmp_path / "card"),
        "recursive": True,
        "entries": [],
        "walk_errors": [],
        "totals": {},
    }
    m.update(overrides)
    return m


def test_write_then_load_roundtrip(tmp_path):
    mdir = str(tmp_path / "manifests")
    (tmp_path / "card").mkdir()
    write_manifest(mdir, _manifest(tmp_path))
    loaded = load_manifest(mdir, "scan-1")
    assert loaded["scan_job_id"] == "scan-1"


def test_write_is_atomic_no_leftover_tmp(tmp_path):
    mdir = str(tmp_path / "manifests")
    write_manifest(mdir, _manifest(tmp_path))
    names = os.listdir(mdir)
    assert names == ["scan-1.json"]


def test_load_missing_manifest_is_404(tmp_path):
    with pytest.raises(ManifestError) as exc:
        load_manifest(str(tmp_path), "nope")
    assert exc.value.http_status == 404


def test_load_rejects_corrupt_json(tmp_path):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "scan-1.json").write_text("{truncated")
    with pytest.raises(ManifestError) as exc:
        load_manifest(str(mdir), "scan-1")
    assert exc.value.http_status == 400


def test_load_rejects_unknown_schema(tmp_path):
    mdir = str(tmp_path / "manifests")
    write_manifest(mdir, _manifest(tmp_path, schema_version=99))
    with pytest.raises(ManifestError):
        load_manifest(mdir, "scan-1")


def test_load_rejects_missing_source_root(tmp_path):
    mdir = str(tmp_path / "manifests")
    write_manifest(mdir, _manifest(tmp_path, source_root=""))
    with pytest.raises(ManifestError):
        load_manifest(mdir, "scan-1")


def test_load_rejects_deletable_entry_outside_source_root(tmp_path):
    (tmp_path / "card").mkdir()
    entry = {
        "path": str(tmp_path / "elsewhere" / "x.nef"),
        "size": 1, "mtime_ns": 1, "hash": "h", "bucket": "deletable",
    }
    mdir = str(tmp_path / "manifests")
    write_manifest(mdir, _manifest(tmp_path, entries=[entry]))
    with pytest.raises(ManifestError):
        load_manifest(mdir, "scan-1")


def test_load_rejects_expired_manifest_at_request_time(tmp_path):
    (tmp_path / "card").mkdir()
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    mdir = str(tmp_path / "manifests")
    write_manifest(mdir, _manifest(tmp_path, created_at=old))
    with pytest.raises(ManifestError) as exc:
        load_manifest(mdir, "scan-1")
    assert exc.value.http_status == 404
    assert "re-scan" in str(exc.value)


def test_prune_removes_only_old_manifests(tmp_path):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    old_file = mdir / "old.json"
    new_file = mdir / "new.json"
    old_file.write_text("{}")
    new_file.write_text("{}")
    eight_days = 8 * 86400
    stale = os.stat(old_file).st_mtime - eight_days
    os.utime(old_file, (stale, stale))
    prune_manifests(str(mdir))
    assert not old_file.exists()
    assert new_file.exists()


def test_prune_removes_orphaned_tmp_files(tmp_path):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    orphan = mdir / ".scan-1.json.abc123.tmp"
    orphan.write_text("{")
    eight_days = 8 * 86400
    stale = os.stat(orphan).st_mtime - eight_days
    os.utime(orphan, (stale, stale))
    prune_manifests(str(mdir))
    assert not orphan.exists()


def test_hostile_scan_job_id_stays_inside_manifest_dir(tmp_path):
    (tmp_path / "card").mkdir()
    mdir = str(tmp_path / "manifests")
    hostile_id = "../../etc/passwd"
    path = manifest_path(mdir, hostile_id)
    assert os.path.dirname(path) == mdir

    write_manifest(mdir, _manifest(tmp_path, scan_job_id=hostile_id))
    loaded = load_manifest(mdir, hostile_id)
    assert loaded["scan_job_id"] == hostile_id
    assert sorted(os.listdir(mdir)) == ["passwd.json"]


def test_load_rejects_naive_created_at(tmp_path):
    (tmp_path / "card").mkdir()
    mdir = str(tmp_path / "manifests")
    naive = datetime.now().isoformat()  # deliberately tz-naive
    write_manifest(mdir, _manifest(tmp_path, created_at=naive))
    with pytest.raises(ManifestError) as exc:
        load_manifest(mdir, "scan-1")
    assert exc.value.http_status == 400


def test_write_manifest_json_dump_failure_leaves_no_tmp_file(tmp_path):
    mdir = str(tmp_path / "manifests")
    bad = _manifest(tmp_path, entries={"not serializable": {1, 2, 3}})
    with pytest.raises(TypeError):
        write_manifest(mdir, bad)
    assert os.listdir(mdir) == []
