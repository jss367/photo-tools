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
