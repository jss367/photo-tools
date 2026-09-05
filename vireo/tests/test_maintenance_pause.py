"""Pause integration at real per-photo and database transaction boundaries."""
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


def test_hash_verification_commits_before_pause(client_with_photo, monkeypatch):
    import scanner

    app, db, first = client_with_photo
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
        db.conn.execute("UPDATE photos SET rating=4 WHERE id=?", (first,))
        db.conn.commit()
        assert client.post(f"/api/jobs/{job_id}/resume").status_code == 200
        finished = wait_for_job_via_client(client, job_id)
        assert finished["status"] == "completed", finished
        assert len(calls) == 2
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
