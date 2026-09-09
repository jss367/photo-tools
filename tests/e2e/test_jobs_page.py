from playwright.sync_api import expect


def test_keep_awake_reminder_opens_with_keyboard_and_click(live_server, page):
    keeping_awake = True
    page.route(
        "**/api/jobs",
        lambda route: route.fulfill(
            json={"active": [], "keeping_awake": keeping_awake},
        ),
    )
    page.route("**/api/jobs/history?*", lambda route: route.fulfill(json=[]))
    page.goto(f"{live_server['url']}/jobs")

    note = page.locator("#keepingAwakeNote")
    summary = note.locator("summary")
    reminder = note.get_by_text("Keep your laptop open while jobs run", exact=True)
    expect(summary).to_be_visible()
    expect(reminder).to_be_hidden()

    summary.focus()
    summary.press("Enter")
    expect(reminder).to_be_visible()
    summary.press("Space")
    expect(reminder).to_be_hidden()
    summary.click()
    expect(reminder).to_be_visible()

    keeping_awake = False
    page.reload()
    expect(note).to_be_hidden()


def test_move_folder_job_shows_source_and_destination(live_server, page):
    job = {
        "id": "move-folder-route-test",
        "type": "move-folder",
        "status": "running",
        "started_at": "2026-08-16T21:34:53",
        "finished_at": None,
        "duration": None,
        "progress": {
            "current": 31,
            "total": 503,
            "current_file": "DSC_5656.NEF",
            "phase": "Organizing by capture date",
        },
        "result": None,
        "errors": [],
        "config": {
            "folder_id": 42,
            "destination": "/Volumes/Photos/Archive",
            "source_path": "/Volumes/Camera/Paris",
            "resolved_destination": "/Volumes/Photos/Archive",
            "folder_template": "%Y/%Y-%m-%d",
            "merge": False,
        },
        "workspace_id": live_server["db"]._active_workspace_id,
        "steps": [],
        "pausable": False,
    }

    page.route(
        "**/api/jobs",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            json={
                "active": [job],
                "active_workspace_id": live_server["db"]._active_workspace_id,
                "workspace_names": {},
                "keeping_awake": True,
            },
        ),
    )
    page.route(
        "**/api/jobs/history?*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", json=[]
        ),
    )

    page.goto(f"{live_server['url']}/jobs")

    move_route = page.locator(".job-move-route")
    expect(move_route).to_be_visible()
    expect(move_route.locator(".job-move-route-label")).to_have_text(
        ["From", "To"]
    )
    expect(move_route.locator(".job-move-route-path")).to_have_text(
        ["/Volumes/Camera/Paris", "/Volumes/Photos/Archive"]
    )
    expect(move_route.locator(".job-move-route-note")).to_contain_text(
        "Organizing photos into capture-date folders using %Y/%Y-%m-%d"
    )


def test_label_preparation_shows_progress_and_one_estimate(live_server, page):
    from datetime import datetime, timedelta

    started_at = (datetime.now() - timedelta(minutes=10)).isoformat()
    job = {
        "id": "pipeline-label-preparation",
        "type": "pipeline", "status": "running", "started_at": started_at,
        "workspace_id": live_server["db"]._active_workspace_id,
        "config": {"collection_name": "One photo"}, "errors": [],
        "progress": {
            "phase": "Preparing species labels for BioCLIP-2.5",
            "current": 16, "total": 104,
            "phase_current": 100, "phase_total": 1419, "phase_label": "Species labels",
        },
        "steps": [{
            "id": "model_loader", "label": "Load models", "status": "running",
            "started_at": started_at,
            "progress": {"current": 100, "total": 1419, "unit": "labels"},
            "current_file": "100 / 1,419 labels ready · about 127 min remaining",
        }],
    }
    page.route("**/api/jobs", lambda route: route.fulfill(json={"active": [job], "history": []}))
    page.route("**/api/jobs/history?*", lambda route: route.fulfill(json=[]))
    page.goto(f"{live_server['url']}/jobs")
    step = page.locator('[data-step-id="model_loader"]')
    expect(step).to_be_visible()
    expect(step.locator('.tree-step-current-file')).to_have_text(job["steps"][0]["current_file"])
    expect(step.locator('.tree-step-progress-text').first).to_have_text('100 / 1,419')
    # The browser must not invent a second estimate from total elapsed time,
    # which includes cached labels and time spent paused.
    expect(step.locator('.tree-step-throughput')).to_have_count(0)
