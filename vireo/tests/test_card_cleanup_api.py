"""API tests for the card-cleanup scan/manifest/delete endpoints.

Spec: docs/superpowers/specs/2026-08-07-card-cleanup-design.md

The `app_and_db` fixture comes from vireo/tests/conftest.py — a Flask app
plus its Database, pre-seeded with a couple of folders/photos unrelated to
these tests. Do not redefine it here.
"""
import json
import os
import sys
import threading

import card_cleanup
import pytest
from scanner import compute_file_hash as _sha
from wait import wait_for_job_via_client


def _wait_for_job(client, job_id, timeout=30):
    """Poll GET /api/jobs/<id> until it reaches a terminal state and
    assert it completed."""
    job = wait_for_job_via_client(client, job_id, timeout=timeout)
    assert job["status"] == "completed", job
    return job


def _archive_photo(db, tmp_path, name="IMG_0001.NEF", content=b"raw-one",
                   folder="archive/2026/2026-08-01"):
    """Create an archive file on disk + its cataloged, verified row."""
    folder_path = tmp_path / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    f = folder_path / name
    f.write_bytes(content)
    st = os.stat(f)
    fid = db.add_folder(str(folder_path))
    pid = db.add_photo(
        folder_id=fid, filename=name, extension=os.path.splitext(name)[1],
        file_size=st.st_size, file_mtime=st.st_mtime,
        file_hash=_sha(str(f)),
    )
    db.update_photo_hash_check(pid, "ok")
    return f, pid


def _card_file(tmp_path, name="IMG_0001.NEF", content=b"raw-one"):
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True, exist_ok=True)
    f = card / name
    f.write_bytes(content)
    return f


def _make_verified_pair(db, tmp_path):
    """Archive file + verified catalog row, plus a matching card file."""
    archive_file, _pid = _archive_photo(db, tmp_path)
    card_file = _card_file(tmp_path)
    return archive_file, card_file


def test_scan_rejects_missing_source(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    resp = client.post(
        "/api/card-cleanup/scan", json={"source": "/nope/missing"})
    assert resp.status_code == 400


def test_scan_rejects_archive_overlap(app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    db.add_folder(str(archive_root))

    # Exact overlap.
    resp = client.post(
        "/api/card-cleanup/scan", json={"source": str(archive_root)})
    assert resp.status_code == 400
    assert "removable media" in resp.get_json()["error"]

    # Source CONTAINS the archive root.
    resp2 = client.post(
        "/api/card-cleanup/scan", json={"source": str(tmp_path)})
    assert resp2.status_code == 400

    # Source is INSIDE the archive root.
    sub = archive_root / "2026"
    sub.mkdir()
    resp3 = client.post(
        "/api/card-cleanup/scan", json={"source": str(sub)})
    assert resp3.status_code == 400


@pytest.mark.skipif(
    sys.platform not in ("darwin", "win32"),
    reason="case-insensitive overlap only applies on darwin/win32 by default",
)
def test_scan_rejects_case_swapped_overlap(app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()
    archive_root = tmp_path / "Archive"
    archive_root.mkdir()
    db.add_folder(str(archive_root))

    resp = client.post(
        "/api/card-cleanup/scan",
        json={"source": str(tmp_path / "archive")})
    assert resp.status_code == 400


def test_scan_then_manifest_then_delete_end_to_end(app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()
    archive_file, card_file = _make_verified_pair(db, tmp_path)

    resp = client.post(
        "/api/card-cleanup/scan",
        json={"source": str(tmp_path / "card")})
    assert resp.status_code == 200
    scan_job_id = resp.get_json()["job_id"]
    _wait_for_job(client, scan_job_id)

    manifest_resp = client.get(f"/api/card-cleanup/{scan_job_id}/manifest")
    assert manifest_resp.status_code == 200
    manifest = manifest_resp.get_json()
    assert manifest["totals"]["deletable"]["count"] == 1

    delete_resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": scan_job_id})
    assert delete_resp.status_code == 200
    delete_job_id = delete_resp.get_json()["job_id"]
    _wait_for_job(client, delete_job_id)

    assert not card_file.exists()
    assert archive_file.exists()


def test_scan_job_result_carries_totals_not_entries(app_and_db, tmp_path):
    """Spec: the scan job's RESULT carries only bucket totals, resolved
    source root, and the manifest path — never the (potentially
    multi-MB) per-file entries list. The UI fetches entries from the
    manifest endpoint instead."""
    app, db = app_and_db
    client = app.test_client()
    _make_verified_pair(db, tmp_path)

    resp = client.post(
        "/api/card-cleanup/scan",
        json={"source": str(tmp_path / "card")})
    scan_job_id = resp.get_json()["job_id"]
    _wait_for_job(client, scan_job_id)

    job_resp = client.get(f"/api/jobs/{scan_job_id}")
    assert job_resp.status_code == 200
    job = job_resp.get_json()
    result = job["result"]
    assert "entries" not in result
    assert result["cancelled"] is False
    assert result["totals"]["deletable"]["count"] == 1
    assert result["source_root"] == os.path.realpath(str(tmp_path / "card"))
    assert result["manifest_path"] == card_cleanup.manifest_path(
        app.config["CARD_CLEANUP_DIR"], scan_job_id)
    assert isinstance(result["walk_errors"], int)


def test_delete_unknown_scan_job_404(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": "nope"})
    assert resp.status_code == 404


def test_delete_refuses_cancelled_scan(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    db.conn.execute(
        "INSERT INTO job_history (id, type, status, started_at) "
        "VALUES (?, ?, ?, ?)",
        ("scan-c", "card-cleanup-scan", "cancelled", "2026-08-08T00:00:00"),
    )
    db.conn.commit()
    resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": "scan-c"})
    assert resp.status_code == 400


def test_delete_refuses_running_scan_without_telling_user_to_rescan(
        app_and_db):
    """A delete requested while the scan is still going is early, not
    doomed — the error must say to wait, not to re-scan."""
    app, db = app_and_db
    client = app.test_client()
    db.conn.execute(
        "INSERT INTO job_history (id, type, status, started_at) "
        "VALUES (?, ?, ?, ?)",
        ("scan-run", "card-cleanup-scan", "running", "2026-08-08T00:00:00"),
    )
    db.conn.commit()
    resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": "scan-run"})
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "still running" in error
    assert "re-scan" not in error


def test_manifest_unknown_scan_job_404(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    resp = client.get("/api/card-cleanup/nope/manifest")
    assert resp.status_code == 404


def test_delete_after_restart_uses_history_and_disk_manifest(
        app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()
    archive_file, card_file = _make_verified_pair(db, tmp_path)

    manifest_dir = app.config["CARD_CLEANUP_DIR"]
    result = card_cleanup.scan_card(
        db, str(tmp_path / "card"), True, manifest_dir, "scan-r")
    assert result["totals"]["deletable"]["count"] == 1

    db.conn.execute(
        "INSERT INTO job_history "
        "(id, type, status, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("scan-r", "card-cleanup-scan", "completed",
         "2026-08-08T00:00:00", "2026-08-08T00:00:01"),
    )
    db.conn.commit()

    delete_resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": "scan-r"})
    assert delete_resp.status_code == 200
    delete_job_id = delete_resp.get_json()["job_id"]
    _wait_for_job(client, delete_job_id)

    assert not card_file.exists()
    assert archive_file.exists()


def test_delete_expired_manifest_404(app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()
    _make_verified_pair(db, tmp_path)

    resp = client.post(
        "/api/card-cleanup/scan",
        json={"source": str(tmp_path / "card")})
    scan_job_id = resp.get_json()["job_id"]
    _wait_for_job(client, scan_job_id)

    manifest_dir = app.config["CARD_CLEANUP_DIR"]
    mpath = card_cleanup.manifest_path(manifest_dir, scan_job_id)
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    from datetime import UTC, datetime, timedelta
    manifest["created_at"] = (
        datetime.now(UTC) - timedelta(days=8)).isoformat()
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    delete_resp = client.post(
        "/api/card-cleanup/delete", json={"scan_job_id": scan_job_id})
    assert delete_resp.status_code == 404
    assert "re-scan" in delete_resp.get_json()["error"]


def test_delete_concurrent_delete_409(app_and_db, tmp_path, monkeypatch):
    app, db = app_and_db
    client = app.test_client()
    _make_verified_pair(db, tmp_path)

    resp = client.post(
        "/api/card-cleanup/scan",
        json={"source": str(tmp_path / "card")})
    scan_job_id = resp.get_json()["job_id"]
    _wait_for_job(client, scan_job_id)

    started = threading.Event()
    release = threading.Event()
    real_delete_verified = card_cleanup.delete_verified

    def blocking_delete_verified(db_, manifest, progress_cb=None,
                                 should_cancel=None):
        started.set()
        release.wait(timeout=15)
        return {
            "deleted": 0, "deleted_bytes": 0, "skipped": [], "failed": [],
            "cancelled": False, "remaining": 0,
        }

    monkeypatch.setattr(
        card_cleanup, "delete_verified", blocking_delete_verified)
    # Bound before the try so a failure on the first POST surfaces as
    # itself, not as a NameError raised from the finally drain.
    job1_id = None
    try:
        resp1 = client.post(
            "/api/card-cleanup/delete", json={"scan_job_id": scan_job_id})
        assert resp1.status_code == 200
        job1_id = resp1.get_json()["job_id"]
        assert started.wait(timeout=15)

        runner = app._job_runner

        def _running():
            job = runner.get(job1_id)
            return job is not None and job.get("status") == "running"
        import time
        deadline = time.monotonic() + 15
        while not _running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _running()

        resp2 = client.post(
            "/api/card-cleanup/delete", json={"scan_job_id": scan_job_id})
        assert resp2.status_code == 409
    finally:
        release.set()
        monkeypatch.setattr(
            card_cleanup, "delete_verified", real_delete_verified)
        if job1_id:
            _wait_for_job(client, job1_id)


def test_scan_rejects_relative_or_nondir_source(app_and_db, tmp_path):
    app, db = app_and_db
    client = app.test_client()

    resp = client.post(
        "/api/card-cleanup/scan", json={"source": "relative/path"})
    assert resp.status_code == 400

    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("x")
    resp2 = client.post(
        "/api/card-cleanup/scan", json={"source": str(a_file)})
    assert resp2.status_code == 400

    resp3 = client.post("/api/card-cleanup/scan", json={})
    assert resp3.status_code == 400


def test_scan_rejects_non_boolean_recursive(app_and_db, tmp_path):
    # bool("false") is True — stringly-typed clients must get a 400, not
    # the opposite of what they asked for.
    app, _ = app_and_db
    card = tmp_path / "card"
    card.mkdir()
    resp = app.test_client().post(
        "/api/card-cleanup/scan",
        json={"source": str(card), "recursive": "false"})
    assert resp.status_code == 400
    assert "boolean" in resp.get_json()["error"]


def test_delete_result_carries_exact_totals(app_and_db, tmp_path):
    # The job result bounds skipped/failed to a sample; *_total fields
    # carry the exact counts the UI renders.
    app, db = app_and_db
    client = app.test_client()
    _make_verified_pair(db, tmp_path)
    resp = client.post("/api/card-cleanup/scan",
                       json={"source": str(tmp_path / "card")})
    scan_job_id = resp.get_json()["job_id"]
    _wait_for_job(client, scan_job_id)
    resp = client.post("/api/card-cleanup/delete",
                       json={"scan_job_id": scan_job_id})
    job_id = resp.get_json()["job_id"]
    _wait_for_job(client, job_id)
    result = client.get(f"/api/jobs/{job_id}").get_json()["result"]
    assert result["skipped_total"] == len(result["skipped"]) == 0
    assert result["failed_total"] == len(result["failed"]) == 0
    assert result["deleted"] == 1


def test_endpoints_reject_non_object_json_body(app_and_db):
    # get_json returns 5 for a valid non-object JSON document; the
    # endpoints must 400, not 500 on body.get.
    app, _ = app_and_db
    client = app.test_client()
    for url in ("/api/card-cleanup/scan", "/api/card-cleanup/delete"):
        resp = client.post(url, json=5)
        assert resp.status_code == 400, url
        assert "JSON object" in resp.get_json()["error"]


def test_card_cleanup_page_renders(app_and_db):
    # The flow lives on its own page now (spec:
    # docs/superpowers/specs/2026-08-08-card-cleanup-page-design.md).
    app, _ = app_and_db
    resp = app.test_client().get("/card-cleanup")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "card-cleanup-section" in body
    assert "card-cleanup-scan-btn" in body
    # The integrity-audit affordance is part of the page, not the import page.
    assert "card-cleanup-audit-btn" in body


def test_import_and_card_cleanup_share_folder_browser(app_and_db):
    """Both pages use one browser implementation, partial, and stylesheet.

    This pins the architectural reason for the refactor: count rendering and
    navigation fixes must land once rather than drift between template copies.
    """
    app, _ = app_and_db
    client = app.test_client()
    import_body = client.get("/import").get_data(as_text=True)
    cleanup_body = client.get("/card-cleanup").get_data(as_text=True)
    for body in (import_body, cleanup_body):
        assert '/static/vireo-folder-browser.js' in body
        assert '/static/vireo-folder-browser.css' in body
        assert 'data-folder-browser-action="select"' in body
    assert client.get("/static/vireo-folder-browser.js").status_code == 200
    assert client.get("/static/vireo-folder-browser.css").status_code == 200


def test_import_page_links_to_card_cleanup_instead_of_hosting_it(app_and_db):
    app, _ = app_and_db
    resp = app.test_client().get("/import")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "card-cleanup-section" not in body
    # The entry point stays: it now navigates to the dedicated page.
    assert "btnFreeUpCardSpace" in body
    assert "/card-cleanup" in body


def test_audit_callout_reason_stays_in_sync(app_and_db):
    """The page's audit callout counts kept entries by matching a tail of
    KEEP_NOT_VERIFIED. That coupling is invisible from either side, so pin
    both ends: the served page must carry the literal it matches on, and
    the reason it matches must still contain it."""
    app, _ = app_and_db
    matched = "run the integrity audit"
    assert matched in card_cleanup.KEEP_NOT_VERIFIED
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    assert f"CARD_CLEANUP_AUDIT_REASON = '{matched}'" in body


def test_hash_failed_callout_reason_stays_in_sync(app_and_db):
    """The hash-failed callout has the same shape of coupling as the
    audit callout (Codex P2 review): the page filters kept entries by a
    tail of KEEP_ARCHIVE_HASH_FAILED. Pin both ends so a reword on either
    side stops silently. Also assert the two literals do NOT overlap —
    if they did, a KEEP_ARCHIVE_HASH_FAILED entry would count toward the
    audit callout and get double-remediated."""
    app, _ = app_and_db
    matched = "see the Audit page"
    assert matched in card_cleanup.KEEP_ARCHIVE_HASH_FAILED
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    assert f"CARD_CLEANUP_HASH_FAILED_REASON = '{matched}'" in body
    assert matched not in card_cleanup.KEEP_NOT_VERIFIED
    assert "run the integrity audit" not in card_cleanup.KEEP_ARCHIVE_HASH_FAILED


def test_hash_failed_callout_states_audit_workspace_scope(app_and_db):
    """Second-order Codex P2 review: the audit remedy suggested by the
    hash-failed callout is only reachable when the failed row is in the
    active workspace, because Database.get_integrity_flagged() filters
    to the active workspace but _load_catalog_by_hash() matches globally.
    The callout must say so up front so the user does not click through
    to an empty Audit page — mirroring the workspace-scope note added
    to the verify-hashes callout for the same asymmetry."""
    app, _ = app_and_db
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    # A dedicated hint element for the scope note, rendered inside the
    # hash-failed callout container.
    assert 'id="card-cleanup-hash-failed-scope"' in body
    # The copy itself: names the current-workspace scope of the Audit
    # page and tells the user how to reach a failed row in a different
    # workspace's folders. Pinned literally so a reword does not quietly
    # drop the guidance the Codex P2 asked for.
    assert "Audit page shows flagged rows for the current workspace only" in body
    assert "switch to that workspace to find it there" in body


def test_finish_audit_disables_delete_until_rescan(app_and_db):
    """Codex P2 review (commit 1213fec): finishing or cancelling the
    verify-hashes run must invalidate the preview's Delete affordance,
    because verification can flip previously-ok rows to modified/corrupt/
    unreadable — the confirmation dialog would then advertise stale file
    and byte totals against a subset the user never agreed to. The audit
    handler must both disable the Delete button and swap in a hint that
    tells the user to re-scan (the button surfaced next to the audit
    status). Pin the literal here so the guardrail can't be silently
    reworded away."""
    app, _ = app_and_db
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    finish = body.index("function cardCleanupFinishAudit(")
    # Snap out a window around the handler body. The next function in the
    # file is cardCleanupBindBucket; slice up to whichever declaration
    # follows so this test does not accidentally match code elsewhere.
    end = body.index("function cardCleanupBindBucket(", finish)
    section = body[finish:end]
    # The Delete button is disabled inside the handler, not just via the
    # separate cardCleanupSetBusy(false) that ran first (which restores
    # the pre-verify enabled state).
    assert "deleteBtn.disabled = true" in section
    # The user-facing hint that replaces whatever cardCleanupSetBusy
    # restored — pinned verbatim so a reword can't drop the "re-scan
    # first" instruction the Codex P2 asked for.
    assert (
        "Verification may have changed which files count as verified — "
        "re-scan the card before deleting."
    ) in section


def test_confirm_delete_locks_page_before_post(app_and_db):
    """Codex P2 reviews (commits 96137e3 and 012aec2c): cardCleanupConfirmDelete
    must set the page-wide busy state *before* the POST /api/card-cleanup/delete
    request is issued, and its failure paths must treat HTTP failures and
    network errors differently.

    Ordering: the confirm dialog can be dismissed (backdrop click or Escape)
    while the request is pending. If Scan and Verify remained enabled during
    that window, the user could kick off a second job whose watch would
    overwrite this delete's jobId — the exact orphan the single-busy-state
    owner exists to prevent, and one that could strand a destructive
    deletion. Busy must be set before the fetch so a dismissal never opens
    the gap.

    Failure handling: an HTTP response proves whether the server queued the
    delete, so !resp.ok can hand the buttons back. A `fetch` rejection is
    ambiguous — the POST may have reached the server and started the
    destructive job before the connection dropped — so the catch must NOT
    unlock, and must direct the user to the Jobs page. Pinning both here so
    a refactor can't quietly re-open the gap."""
    app, _ = app_and_db
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    start = body.index("async function cardCleanupConfirmDelete(")
    end = body.index("function cardCleanupRenderDeleteResult(", start)
    section = body[start:end]
    # Busy must be set BEFORE the fetch — locate both and require the
    # setBusy call to come first.
    fetch_idx = section.index("/api/card-cleanup/delete")
    busy_true_idx = section.index("cardCleanupSetBusy(true")
    assert busy_true_idx < fetch_idx, (
        "cardCleanupSetBusy(true) must run before the delete POST is issued "
        "so a dialog dismissal during the pending request can't leave "
        "Scan/Verify live and race a second job into cardCleanupWatchJob"
    )
    # An HTTP response that proves nothing was queued (!resp.ok) must
    # release the lock so the user isn't stranded with a dead page.
    ok_branch_start = section.index("if (!resp.ok)")
    ok_branch_end = section.index("cardCleanupWatchJob", ok_branch_start)
    ok_branch = section[ok_branch_start:ok_branch_end]
    assert "cardCleanupSetBusy(false)" in ok_branch, (
        "the !resp.ok branch must call cardCleanupSetBusy(false) — the "
        "server said the delete was not queued, so the page can safely hand "
        "the buttons back"
    )
    # The catch branch is different: `fetch` rejects for network errors
    # where the request may or may not have reached the server. If the
    # server already queued the delete, the destructive job is running
    # unseen — unlocking would let a second job overwrite this page's
    # jobId (the exact orphan the single-busy-state owner exists to
    # prevent). Codex P2 (commit 012aec2c) called this out: only unlock
    # after an HTTP response proves no job was queued; treat network
    # errors as unknown-running and direct the user to Jobs.
    catch_start = section.index("} catch (e) {")
    catch_end = section.index("} finally {", catch_start)
    catch_body = section[catch_start:catch_end]
    assert "cardCleanupSetBusy(false)" not in catch_body, (
        "the network-error catch must NOT release the page lock — the "
        "server may have queued the delete before the connection dropped, "
        "and unlocking would let a second job race the destructive job"
    )
    assert "Jobs page" in catch_body, (
        "the network-error catch must direct the user to the Jobs page so "
        "they can find out whether the server queued the delete before the "
        "connection dropped"
    )


def test_start_audit_keeps_page_locked_on_ambiguous_start(app_and_db):
    """Codex P1 review (commit 5afcb0ba): cardCleanupStartAudit must not
    release the page-wide busy lock on outcomes that don't prove whether the
    server queued the verify-hashes job.

    If the audit is silently running while Scan/Verify/Delete come back live,
    a subsequent delete can race it. delete_verified() trusts the currently
    committed hash_status plus archive size/mtime rather than re-hashing
    archive bytes, so a row the audit is about to flip from `ok` to
    modified/corrupt/unreadable could still qualify for deletion — removing
    the good card copy of a file whose archive copy is silently rotting. Same
    guarantee the delete-start path pins in test_confirm_delete_locks_page_
    before_post; pinning both invariants here so a refactor can't quietly
    re-open the gap."""
    app, _ = app_and_db
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    start = body.index("async function cardCleanupStartAudit(")
    end = body.index("function cardCleanupFinishAudit(", start)
    section = body[start:end]
    # An HTTP response that proves nothing was queued (!resp.ok) must
    # release the lock so the user isn't stranded with a dead page.
    ok_branch_start = section.index("if (!resp.ok)")
    # The branch closes before the JSON re-parse below; scope to just that
    # arm by ending at the trailing "return;" and its closing brace.
    ok_branch_end = section.index("let data;", ok_branch_start)
    ok_branch = section[ok_branch_start:ok_branch_end]
    assert "cardCleanupSetBusy(false)" in ok_branch, (
        "the !resp.ok branch must call cardCleanupSetBusy(false) — the "
        "server said the verify-hashes job was not queued, so the page can "
        "safely hand the buttons back"
    )
    # The network-error catch on the fetch itself must NOT unlock — the
    # POST may have reached the server and started api_job_verify_hashes()
    # before the connection dropped, and the delete path trusts hash_status
    # in the DB rather than re-hashing archive bytes, so a concurrent delete
    # could remove the good card copy of a file the audit is silently
    # detecting as corrupt.
    fetch_catch_start = section.index("resp = await fetch(")
    fetch_catch_start = section.index("} catch (e) {", fetch_catch_start)
    fetch_catch_end = section.index("if (!resp.ok)", fetch_catch_start)
    fetch_catch_body = section[fetch_catch_start:fetch_catch_end]
    assert "cardCleanupSetBusy(false)" not in fetch_catch_body, (
        "the fetch-rejection catch on cardCleanupStartAudit must NOT release "
        "the page lock — the POST may have reached the server and queued the "
        "verify-hashes job before the connection dropped, and unlocking "
        "would let a concurrent delete race the audit"
    )
    assert "Jobs page" in fetch_catch_body, (
        "the fetch-rejection catch must direct the user to the Jobs page so "
        "they can find out whether the server queued the verify-hashes job"
    )
    # The JSON-parse catch on the OK response is the second ambiguous
    # outcome: a 2xx status means api_job_verify_hashes() returned, but if
    # the body couldn't be decoded we can't be sure the job wasn't queued.
    # Same reasoning — keep the lock, route to Jobs.
    json_catch_start = section.index("data = await resp.json()")
    json_catch_start = section.index("} catch (e) {", json_catch_start)
    json_catch_end = section.index("cardCleanupWatchJob(", json_catch_start)
    json_catch_body = section[json_catch_start:json_catch_end]
    assert "cardCleanupSetBusy(false)" not in json_catch_body, (
        "the JSON-parse catch on a 2xx response must NOT release the page "
        "lock — the server returned OK, so api_job_verify_hashes() may have "
        "queued the audit even though we can't read the response body"
    )
    assert "Jobs page" in json_catch_body, (
        "the JSON-parse catch on a 2xx response must direct the user to the "
        "Jobs page for the actual outcome"
    )


def test_load_manifest_discards_stale_scan_response(app_and_db):
    """Codex P2 review (commit 16a3f8e4): cardCleanupLoadManifest must
    discard responses whose scan job id is no longer current.

    Scenario: cardCleanupFinishJob unlocks the buttons and then awaits
    cardCleanupLoadManifest() for scan #1. During that await a user can
    change the source and start scan #2, which sets scanJobId to jobId2.
    If scan #1's response then arrives it would (a) render scan #1's
    source and totals over scan #2's state via cardCleanupRenderManifest
    — including re-enabling the Delete button based on scan #1's
    deletable count — while the delete POST uses cardCleanupState.
    scanJobId, which is now jobId2, so the confirmation dialog would
    advertise a set that does not match the manifest the destructive job
    would operate on; or (b) on a 404, null out scanJobId and clobber
    scan #2's identity; or (c) surface scan #1's fetch/parse error as if
    scan #2 had failed.

    Fix: capture scanJobId at request time and drop the response on both
    the success and error paths when it has moved on. Pinning both paths
    here so a refactor cannot quietly re-open either one.
    """
    app, _ = app_and_db
    body = app.test_client().get("/card-cleanup").get_data(as_text=True)
    start = body.index("async function cardCleanupLoadManifest(")
    end = body.index("function cardCleanupRenderManifest(", start)
    section = body[start:end]
    # The success path must run its stale-response check before ANY
    # state mutation or render — both the 404 handler (which nulls
    # scanJobId) and cardCleanupRenderManifest (which shows scan #1's
    # totals and re-enables Delete based on them) would corrupt scan #2's
    # state if reached with a stale response.
    first_mutation_idx = min(
        section.index("cardCleanupState.scanJobId = null"),
        section.index("cardCleanupRenderManifest("),
    )
    pre_mutation = section[:first_mutation_idx]
    assert "cardCleanupState.scanJobId !==" in pre_mutation, (
        "cardCleanupLoadManifest must compare state.scanJobId against the "
        "job id captured before the fetch, and return early on mismatch, "
        "BEFORE touching state or rendering — otherwise a scan started "
        "during the fetch will be overwritten by the stale response and "
        "the Delete confirmation will advertise numbers that do not match "
        "the manifest the delete POST would operate on"
    )
    # The error path (network failure, unreadable body, !resp.ok throw)
    # must also discard on mismatch — a scan #2 in flight should not
    # inherit scan #1's error banner as if it had failed itself.
    catch_idx = section.index("} catch (e) {")
    catch_body = section[catch_idx:]
    assert "cardCleanupState.scanJobId !==" in catch_body, (
        "the catch branch of cardCleanupLoadManifest must also check "
        "state.scanJobId — a fetch or parse error on the stale scan #1 "
        "response must not surface as an error message while scan #2 is "
        "running, because it would misattribute the failure to the scan "
        "the user is currently watching"
    )
