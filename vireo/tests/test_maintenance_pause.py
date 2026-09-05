"""Pause integration at real per-photo and database transaction boundaries."""
import json
import os
import threading
import time

import pytest
from PIL import Image
from wait import wait_for_job_via_client


def _wait_status(runner, job_id, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job["status"] == status:
            return job
        time.sleep(0.01)
    pytest.fail(f"Expected {status}: {runner.get(job_id)}")


def _second_photo(db, photo_id):
    photo = db.get_photo(photo_id)
    folder = db.conn.execute(
        "SELECT path FROM folders WHERE id=?", (photo["folder_id"],),
    ).fetchone()["path"]
    path = os.path.join(folder, "second.jpg")
    Image.new("RGB", (80, 60), "blue").save(path)
    return db.add_photo(
        folder_id=photo["folder_id"], filename="second.jpg", extension=".jpg",
        file_size=os.path.getsize(path), file_mtime=os.path.getmtime(path),
        width=80, height=60,
    )


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_card_scan_pauses_during_discovery(app_and_db, monkeypatch, tmp_path, action):
    import card_cleanup

    app, _db = app_and_db
    source = tmp_path / "card"
    source.mkdir()
    (source / "bird.jpg").write_bytes(b"photo")
    entered = threading.Event()
    release = threading.Event()
    continued = threading.Event()

    def walk(path, *, cancel_check=None, **kwargs):
        entered.set()
        assert release.wait(5)
        assert cancel_check is not None
        if cancel_check():
            raise card_cleanup.ScanCancelled("cancelled")
        continued.set()
        yield str(source), [], ["bird.jpg"]

    monkeypatch.setattr(card_cleanup, "safe_scan_walk", walk)
    client = app.test_client()
    runner = app._job_runner
    job_id = client.post("/api/card-cleanup/scan", json={
        "source": str(source), "recursive": True,
    }).get_json()["job_id"]
    manifest = card_cleanup.manifest_path(app.config["CARD_CLEANUP_DIR"], job_id)
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        assert not continued.is_set()
        assert not os.path.exists(manifest)
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled")
        assert os.path.exists(manifest) == (action == "resume")
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("phase", ["source_root", "file_discovery"])
def test_staging_verification_pauses_during_enumeration(
    client_with_photo, monkeypatch, action, phase,
):
    from contextlib import contextmanager

    import staging_recovery

    app, _db, _photo_id = client_with_photo
    vireo_dir = os.path.dirname(app.config["THUMB_CACHE_DIR"])
    root = os.path.join(vireo_dir, "staging", "pipeline-pause")
    source = os.path.join(root, "photos")
    os.makedirs(source)
    for name in ("bird.jpg", ".hidden.txt"):
        with open(os.path.join(source, name), "w") as f:
            f.write("staged data")
    entered = threading.Event()
    release = threading.Event()
    enumerated = []
    original = staging_recovery.os.scandir
    target = root if phase == "source_root" else source

    @contextmanager
    def scandir(path):
        with original(path) as entries:
            if str(path) != target:
                yield entries
                return

            def delayed_entries():
                for entry in entries:
                    enumerated.append(entry.name)
                    if len(enumerated) == 1:
                        entered.set()
                        assert release.wait(5)
                    yield entry

            yield delayed_entries()

    monkeypatch.setattr(staging_recovery.os, "scandir", scandir)
    client = app.test_client()
    runner = app._job_runner
    job_id = client.post("/api/import/orphaned-staging/verify", json={
        "path": root,
    }).get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        assert len(enumerated) == 1
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled")
        if action == "resume":
            assert finished["result"]["file_count"] == 2
            assert finished["result"]["unaccounted"] == 2
        else:
            assert len(enumerated) == 1
        assert os.path.exists(os.path.join(source, "bird.jpg"))
        assert os.path.exists(os.path.join(source, ".hidden.txt"))
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_culling_pauses_before_publishing_final_result(client_with_photo, monkeypatch, action):
    import culling

    app, db, _photo_id = client_with_photo
    entered = threading.Event()
    release = threading.Event()
    result = {
        "total_photos": 1, "suggested_keepers": 1, "suggested_rejects": 0,
        "species_groups": [],
    }

    def analyze(*args, pause_callback, **kwargs):
        pause_callback()
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(culling, "analyze_for_culling", analyze)
    cache = os.path.join(
        os.path.dirname(app.config["THUMB_CACHE_DIR"]),
        f"culling_results_ws{db._ws_id()}.json",
    )
    with open(cache, "w") as f:
        json.dump({"previous": True}, f)
    client = app.test_client()
    runner = app._job_runner
    job_id = client.post("/api/jobs/cull", json={}).get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        with open(cache) as f:
            assert json.load(f) == {"previous": True}
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled")
        with open(cache) as f:
            assert json.load(f) == (result if action == "resume" else {"previous": True})
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("endpoint", ["prepare-full-resolution", "offline-cache"])
def test_cache_pause_finishes_current_photo_and_preserves_progress(
    client_with_photo, monkeypatch, action, endpoint,
):
    import offline_cache

    app, db, first = client_with_photo
    second = _second_photo(db, first)
    runner = app._job_runner
    client = app.test_client()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = offline_cache.cache_photo_original

    def cache(*args, **kwargs):
        calls.append(args[1]["id"])
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(offline_cache, "cache_photo_original", cache)
    job_id = client.post(
        f"/api/jobs/{endpoint}", json={"photo_ids": [first, second]},
    ).get_json()["job_id"]
    try:
        assert entered.wait(5)
        response = client.post(f"/api/jobs/{job_id}/pause")
        assert response.status_code == 200
        assert runner.get(job_id)["status"] == "pausing"
        release.set()
        paused = _wait_status(runner, job_id, "paused")
        assert paused["progress"]["current"] == 1
        assert calls == [first]
        assert db.offline_original_get(first) is not None
        assert db.offline_original_get(second) is None
        # Pausing must release the writer, so unrelated catalog work can run.
        db.conn.execute("UPDATE photos SET rating=3 WHERE id=?", (first,))
        db.conn.commit()
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        if action == "resume":
            assert finished["status"] == "completed", finished
            assert calls == [first, second]
            assert db.offline_original_get(second) is not None
        else:
            assert finished["status"] == "cancelled", finished
            assert calls == [first]
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("photo_count", [1, 2])
@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_hash_verification_commits_before_pause(
    client_with_photo, monkeypatch, photo_count, action,
):
    import scanner

    app, db, first = client_with_photo
    if photo_count == 2:
        _second_photo(db, first)
    client = app.test_client()
    runner = app._job_runner
    original = scanner.compute_file_hash
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def hash_file(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(scanner, "compute_file_hash", hash_file)
    job_id = client.post("/api/jobs/verify-hashes").get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert client.post(f"/api/jobs/{job_id}/pause").status_code == 200
        release.set()
        _wait_status(runner, job_id, "paused")
        assert len(calls) == 1
        # The first hash was in an uncommitted batch when Pause arrived.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE hash_status='ok'",
        ).fetchone()[0] == 1
        assert not db.get_audit_runs()
        db.conn.execute("UPDATE photos SET rating=4 WHERE id=?", (first,))
        db.conn.commit()
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled")
        assert len(calls) == (photo_count if action == "resume" else 1)
        assert bool(db.get_audit_runs()) == (action == "resume")
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("endpoint", ["capture-time", "inat-export"])
@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_final_file_write_pauses_after_completion(
    client_with_photo, monkeypatch, tmp_path, endpoint, action,
):
    from types import SimpleNamespace

    app, db, photo_id = client_with_photo
    entered = threading.Event()
    release = threading.Event()
    writing = threading.Event()

    def finish_write():
        writing.set()
        entered.set()
        assert release.wait(5)
        writing.clear()

    if endpoint == "capture-time":
        import capture_time

        def run(*args, **kwargs):
            finish_write()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def refresh(database, ident, path):
            database.conn.execute("UPDATE photos SET rating=2 WHERE id=?", (ident,))

        monkeypatch.setattr(capture_time.shutil, "which", lambda _: "/usr/bin/exiftool")
        monkeypatch.setattr(capture_time.subprocess, "run", run)
        monkeypatch.setattr(capture_time, "_refresh_photo_metadata", refresh)
        url = "/api/jobs/capture-time"
        body = {"photo_ids": [photo_id], "mode": "manual", "shift_minutes": 60}
    else:
        import inat_export

        output = tmp_path / "exported.jpg"

        def export(*args, **kwargs):
            finish_write()
            output.write_bytes(b"exported photo")
            return str(output)

        monkeypatch.setattr(inat_export, "export_inat_photo", export)
        url = "/api/inat/export"
        body = {"submissions": [{"photo_id": photo_id}], "destination": str(tmp_path)}

    client = app.test_client()
    runner = app._job_runner
    response = client.post(url, json=body)
    assert response.status_code == 200, response.get_json()
    job_id = response.get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        assert runner.get(job_id)["status"] == "pausing"
        release.set()
        _wait_status(runner, job_id, "paused")
        assert not writing.is_set()
        if endpoint == "capture-time":
            assert db.get_photo(photo_id)["rating"] == 2
        else:
            assert output.exists()
        db.conn.execute("UPDATE photos SET rating=3 WHERE id=?", (photo_id,))
        db.conn.commit()
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled")
        if endpoint == "capture-time":
            assert finished["result"]["updated"] == 1
        else:
            assert len(finished["result"]["exported"]) == 1
            assert finished["result"]["revealed"] is False
            assert output.exists()
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_callback_checkpoint_retains_state_and_unwinds_on_cancel(app_and_db, action):
    from web.background_jobs import JobLaunch

    app, _db = app_and_db
    runner = app._job_runner
    ctx = JobLaunch(runner, None, None, None)
    entered = threading.Event()
    release = threading.Event()
    steps = []
    cleaned_up = threading.Event()

    def work(job):
        try:
            steps.append("first")
            entered.set()
            assert release.wait(5)
            ctx.checkpoint(job)
            steps.append("second")
            return {"steps": steps}
        finally:
            cleaned_up.set()

    job_id = ctx.start_job("test-checkpoint", work, pausable=True)
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        assert steps == ["first"]
        assert not cleaned_up.is_set()
        getattr(runner, f"{action}_job")(job_id)
        expected = "completed" if action == "resume" else "cancelled"
        finished = _wait_status(runner, job_id, expected)
        assert steps == (["first", "second"] if action == "resume" else ["first"])
        assert cleaned_up.wait(5)
        assert finished["errors"] == []
    finally:
        release.set()
        runner.cancel_job(job_id)


def test_import_result_poll_waits_through_long_pause():
    """A paused staging verification must not unlock its destructive next step."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the polling behavior test")
    template = (Path(__file__).parents[1] / "templates" / "import.html").read_text()
    start = template.index("async function pollJobResult(jobId)")
    end = template.index("\n}", start) + 2
    script = r'''
const assert = require('node:assert/strict');
let polls = 0;
const fetch = async () => ({ok: true, json: async () => {
  polls++;
  return polls <= 200
    ? {status: polls === 1 ? 'pausing' : 'paused', result: null}
    : {status: 'completed', result: {verified: 2}};
}});
const setTimeout = (callback) => callback();
FUNCTION
(async () => {
  assert.deepEqual(await pollJobResult('verify-1'), {verified: 2});
  assert.equal(polls, 201);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("FUNCTION", template[start:end])
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, json.dumps({"stdout": result.stdout, "stderr": result.stderr})


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_sharpness_auto_flags_observe_pause_and_cancel(
    client_with_photo, monkeypatch, action,
):
    import sharpness
    from db import Database

    app, db, first = client_with_photo
    second = _second_photo(db, first)
    runner = app._job_runner
    client = app.test_client()
    entered = threading.Event()
    release = threading.Event()
    flagged = []
    original = Database.update_photo_flag

    monkeypatch.setattr(sharpness, "score_collection_photos", lambda *args, **kwargs: {
        "results": [
            {"photo_id": pid, "sharpness": 100, "group_size": 2, "is_best": True}
            for pid in (first, second)
        ],
    })

    def flag_photo(worker_db, photo_id, flag, **kwargs):
        if not flagged:
            entered.set()
            assert release.wait(5)
        original(worker_db, photo_id, flag, **kwargs)
        flagged.append(photo_id)

    monkeypatch.setattr(Database, "update_photo_flag", flag_photo)
    response = client.post("/api/jobs/sharpness", json={})
    assert response.status_code == 200
    job_id = response.get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert client.post(f"/api/jobs/{job_id}/pause").status_code == 200
        assert runner.get(job_id)["status"] == "pausing"
        release.set()
        _wait_status(runner, job_id, "paused")
        assert flagged == [first]
        assert db.get_photo(first)["flag"] == "flagged"
        assert db.get_photo(second)["flag"] == "none"
        # The first flag's transaction must finish before the next checkpoint.
        db.conn.execute("UPDATE photos SET rating=3 WHERE id=?", (second,))
        db.conn.commit()
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        if action == "resume":
            assert finished["status"] == "completed", finished
            assert finished["result"]["auto_flagged"] == 2
            assert flagged == [first, second]
        else:
            assert finished["status"] == "cancelled", finished
            assert flagged == [first]
            assert db.get_photo(second)["flag"] == "none"
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_export_observes_pause_after_metadata_finishes(
    client_with_photo, monkeypatch, tmp_path, action,
):
    import export

    app, db, photo_id = client_with_photo
    db.conn.execute("UPDATE photos SET timestamp=? WHERE id=?", ("2026-08-01T12:30:00", photo_id))
    db.conn.commit()
    client = app.test_client()
    runner = app._job_runner
    entered = threading.Event()
    release = threading.Event()
    reaped = threading.Event()

    def metadata_batch(jobs, cancel_check=None):
        entered.set()
        assert release.wait(5)
        # The live subprocess's probe must not park its parent.
        assert cancel_check() is False
        reaped.set()
        return len(jobs), []

    monkeypatch.setattr(export, "_write_export_metadata_batch", metadata_batch)
    response = client.post("/api/jobs/export", json={
        "photo_ids": [photo_id], "destination": str(tmp_path / "export"),
        "metadata_fields": ["capture_date"], "reveal_after_export": False,
    })
    assert response.status_code == 200
    job_id = response.get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert client.post(f"/api/jobs/{job_id}/pause").status_code == 200
        assert runner.get(job_id)["status"] == "pausing"
        release.set()
        _wait_status(runner, job_id, "paused")
        assert reaped.is_set()
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == ("completed" if action == "resume" else "cancelled"), finished
        assert finished["result"]["exported"] == 1
    finally:
        release.set()
        runner.cancel_job(job_id)


def test_ingest_can_pause_during_discovery(app_and_db, monkeypatch, tmp_path):
    import ingest

    app, _db = app_and_db
    source = tmp_path / "card"
    source.mkdir()
    Image.new("RGB", (8, 8)).save(source / "bird.jpg")
    entered = threading.Event()
    release = threading.Event()
    continued = threading.Event()

    def walk(path, *, cancel_check=None, **kwargs):
        entered.set()
        assert release.wait(5)
        assert cancel_check is not None
        assert cancel_check() is False
        continued.set()
        yield str(source), [], ["bird.jpg"]

    monkeypatch.setattr(ingest, "safe_scan_walk", walk)
    client = app.test_client()
    runner = app._job_runner
    job_id = client.post("/api/jobs/ingest", json={
        "source": str(source), "destination": str(tmp_path / "archive"),
    }).get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        assert not continued.is_set()
        assert runner.cancel_job(job_id)
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == "cancelled", finished
        assert not continued.is_set()
    finally:
        release.set()
        runner.cancel_job(job_id)


@pytest.mark.parametrize("action", ["resume", "cancel"])
@pytest.mark.parametrize("skip_duplicates", [True, False])
def test_ingest_pauses_between_metadata_batches(
    app_and_db, monkeypatch, tmp_path, action, skip_duplicates,
):
    from datetime import datetime

    import import_dedup
    import ingest

    app, _db = app_and_db
    source = tmp_path / "card"
    source.mkdir()
    destination = tmp_path / "archive"
    for i in range(101):
        Image.new("RGB", (8, 8)).save(source / f"bird-{i:03}.jpg")
    entered = threading.Event()
    release = threading.Event()
    batches = []

    def timestamps(files):
        batches.append(list(files))
        if len(batches) == 1:
            entered.set()
            assert release.wait(5)
        return {f: datetime(2026, 8, 1, 12, 30) for f in files}

    # Duplicate preparation and folder planning use the same reader through
    # different imports; cover both the dedup and no-dedup preparation paths.
    monkeypatch.setattr(import_dedup, "source_capture_timestamps", timestamps)
    monkeypatch.setattr(ingest, "source_capture_timestamps", timestamps)
    client = app.test_client()
    runner = app._job_runner
    job_id = client.post("/api/jobs/ingest", json={
        "source": str(source), "destination": str(destination),
        "skip_duplicates": skip_duplicates,
    }).get_json()["job_id"]
    try:
        assert entered.wait(5)
        assert runner.pause_job(job_id)
        release.set()
        _wait_status(runner, job_id, "paused")
        assert [len(batch) for batch in batches] == [100]
        assert not list(destination.rglob("*.jpg"))
        assert client.post(f"/api/jobs/{job_id}/{action}").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        if action == "resume":
            assert finished["status"] == "completed", finished
            assert [len(batch) for batch in batches] == [100, 1]
            assert finished["result"]["copied"] == 101
        else:
            assert finished["status"] == "cancelled", finished
            assert [len(batch) for batch in batches] == [100]
            assert not list(destination.rglob("*.jpg"))
    finally:
        release.set()
        runner.cancel_job(job_id)
