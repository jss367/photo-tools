"""E2E tests for the folder-tree right-click context menu (Task 8).

Menu items:
- Filter by this folder
- separator
- Reveal in Finder/Folder
- Copy Path
- separator
- Work Locally…
- Move…
- Rescan this Folder

"Expand All Children", "Collapse All Children", and "Hide from this Workspace"
are intentionally deferred — no matching helpers exist yet.
"""

from playwright.sync_api import expect


def test_folder_tree_right_click_opens_menu(live_server, page):
    """Right-clicking a folder tree item shows the folder context menu."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    item.click(button="right")

    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()
    for label in [
        "Filter by this folder",
        "Reveal in",
        "Copy Path",
        "Work Locally…",
        "Move…",
        "Rescan this Folder",
    ]:
        expect(
            menu.locator(".vireo-ctx-item", has_text=label)
        ).to_be_visible()


def test_folder_tree_move_opens_move_page_with_source_selected(live_server, page):
    """Move… opens Quick Move with the right-clicked folder as its source."""
    live_server["db"].update_folder_counts()
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    fid = item.get_attribute("data-folder-id")

    item.click(button="right")
    page.locator(".vireo-ctx-menu .vireo-ctx-item", has_text="Move…").click()

    page.wait_for_url(f"**/move?folder_id={fid}")
    expect(page.locator("#quickFolderSelect")).to_have_value(fid)
    expect(page.locator("#quickFolderInfo")).not_to_be_empty()


def test_folder_tree_work_locally_copies_selected_root(live_server, page, tmp_path):
    """Work Locally… opens the chooser and copies the selected root."""
    db = live_server["db"]
    source = tmp_path / "nas-source"
    source.mkdir()
    (source / "bird.jpg").write_bytes(b"original")
    workspace_id = db.create_workspace("Browse Local Copy")
    db.set_active_workspace(workspace_id)
    folder_id = db.add_folder(str(source), name="nas-source")
    assert page.request.post(
        f"{live_server['url']}/api/workspaces/{workspace_id}/activate"
    ).ok

    page.goto(f"{live_server['url']}/browse")
    item = page.locator(f'.tree-item[data-folder-id="{folder_id}"]')
    item.wait_for(state="visible")
    expect(item.locator(".folder-local-status")).to_have_count(0)
    item.click(button="right")
    page.locator(
        ".vireo-ctx-menu .vireo-ctx-item", has_text="Work Locally…"
    ).click()

    modal = page.locator("#stageLocalFoldersModal")
    expect(modal).to_have_class("modal-overlay open")
    destination = tmp_path / "fast-storage"
    modal.locator("[data-local-destination-base]").fill(str(destination))
    expect(modal.locator("[data-local-destination-preview]")).to_contain_text(
        str(destination / "nas-source")
    )
    modal.get_by_role("button", name="Copy Locally", exact=True).click()

    expect(page.locator("#toastContainer")).to_contain_text(
        "Local folder update complete", timeout=15000
    )
    local_status = item.locator(".folder-local-status")
    expect(local_status).to_have_text("LOCAL", timeout=5000)
    expect(local_status).to_have_attribute("aria-label", "Working locally")
    local_root = destination / "nas-source"
    assert (local_root / "bird.jpg").read_bytes() == b"original"
    catalog_path = db.conn.execute(
        "SELECT path FROM folders WHERE id=?", (folder_id,)
    ).fetchone()["path"]
    assert catalog_path == str(local_root)


def test_folder_tree_marks_parent_when_only_descendant_is_local(
    live_server, page, tmp_path
):
    """A remote ancestor distinguishes partial local coverage from full coverage."""
    from services.local_folder import stage_folder

    db = live_server["db"]
    source = tmp_path / "trip"
    child = source / "2026-08-01"
    child.mkdir(parents=True)
    (child / "bird.jpg").write_bytes(b"original")
    workspace_id = db.create_workspace("Mixed Local Folders")
    parent_id = db.add_folder(
        str(source), name="trip", link_to_workspace=False
    )
    child_id = db.add_folder(
        str(child), name="2026-08-01", parent_id=parent_id,
        link_to_workspace=False,
    )
    db.add_workspace_folder(workspace_id, parent_id, is_root=True)
    db.add_workspace_folder(workspace_id, child_id, is_root=False)
    # The E2E app derives its managed-data directory from ``thumb_dir``;
    # conftest places that directory directly under tmp_path.
    stage_folder(db, child_id, str(tmp_path))

    assert page.request.post(
        f"{live_server['url']}/api/workspaces/{workspace_id}/activate"
    ).ok
    page.goto(f"{live_server['url']}/browse")

    parent = page.locator(f'.tree-item[data-folder-id="{parent_id}"]')
    expect(parent.locator(".folder-local-status")).to_have_text(
        "SOME LOCAL", timeout=5000
    )
    expect(parent.locator(".folder-local-status")).to_have_attribute(
        "aria-label", "Contains folders that are working locally"
    )

    parent.locator(".tree-toggle").click()
    child_row = page.locator(f'.tree-item[data-folder-id="{child_id}"]')
    expect(child_row).to_be_visible()
    expect(child_row.locator(".folder-local-status")).to_have_text("LOCAL")


def test_folder_tree_updates_local_operation_and_recovery_badges(live_server, page):
    """Shared local-folder events update status without rebuilding the tree."""
    page.goto(f"{live_server['url']}/browse")
    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    folder_id = int(item.get_attribute("data-folder-id"))

    def publish(payload):
        page.evaluate(
            """([data]) => {
              window.vireoLocalFolderData = data;
              window.dispatchEvent(new CustomEvent(
                'vireo:local-folder-status-changed', {detail: {data: data}}
              ));
            }""",
            [payload],
        )

    publish({
        "legacy_workspace_session": False,
        "folders": [{"requested_folder_id": folder_id, "state": "remote"}],
        "jobs": [{
            "id": "copy-job",
            "type": "work-locally-folder-stage",
            "folder_ids": [folder_id],
        }],
    })
    badge = item.locator(".folder-local-status")
    expect(badge).to_have_text("COPYING")
    expect(badge).to_have_attribute("aria-label", "Copying locally")

    publish({
        "legacy_workspace_session": False,
        "folders": [{
            "requested_folder_id": folder_id,
            "state": "recovery",
            "recovery_kind": "sync",
        }],
        "jobs": [],
    })
    expect(badge).to_have_text("LOCAL ISSUE")
    expect(badge).to_have_attribute("aria-label", "Local sync needs attention")


def test_folder_tree_job_targeting_covering_root_updates_visible_badge(
    live_server, page
):
    """Job targeting a shared ancestor's root_folder_id updates the visible child.

    workspace_status() identifies the visible folder by requested_folder_id,
    but sync/discard jobs report the covering root_folder_id — which is the
    ancestor and isn't necessarily in this workspace's tree. The badge on the
    visible child still has to swap from LOCAL to the job status.
    """
    page.goto(f"{live_server['url']}/browse")
    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    folder_id = int(item.get_attribute("data-folder-id"))
    # Choose an ancestor id that is *not* itself in the visible folder tree.
    ancestor_id = folder_id + 100000

    def publish(payload):
        page.evaluate(
            """([data]) => {
              window.vireoLocalFolderData = data;
              window.dispatchEvent(new CustomEvent(
                'vireo:local-folder-status-changed', {detail: {data: data}}
              ));
            }""",
            [payload],
        )

    publish({
        "legacy_workspace_session": False,
        "folders": [{
            "requested_folder_id": folder_id,
            "root_folder_id": ancestor_id,
            "state": "local",
        }],
        "jobs": [],
    })
    badge = item.locator(".folder-local-status")
    expect(badge).to_have_text("LOCAL")

    publish({
        "legacy_workspace_session": False,
        "folders": [{
            "requested_folder_id": folder_id,
            "root_folder_id": ancestor_id,
            "state": "local",
        }],
        "jobs": [{
            "id": "sync-job",
            "type": "work-locally-folder-sync",
            "folder_ids": [ancestor_id],
        }],
    })
    expect(badge).to_have_text("SYNCING")
    expect(badge).to_have_attribute(
        "aria-label", "Syncing local changes to source storage"
    )


def test_folder_tree_fallback_anchor_does_not_bleed_into_siblings(
    live_server, page, tmp_path
):
    """A fallback anchor's status must not spread through unrelated siblings.

    When a hidden local session is anchored to its nearest visible ancestor
    (its own row was pruned because check_folder_health flipped the rebased
    ``folders.path`` to missing), only the ancestor should carry LOCAL ISSUE
    / SYNCING / REMOVING. Recursively applying the status through every
    visible descendant would mislabel healthy remote siblings — the ancestor
    still hosts other folders that aren't part of the affected subtree
    (Codex review r3792031821).
    """
    db = live_server["db"]
    source = tmp_path / "trip-with-siblings"
    child_a = source / "kept-remote"
    child_a.mkdir(parents=True)
    (child_a / "a.jpg").write_bytes(b"a")
    child_b = source / "also-remote"
    child_b.mkdir(parents=True)
    (child_b / "b.jpg").write_bytes(b"b")
    workspace_id = db.create_workspace("Fallback Spread")
    parent_id = db.add_folder(
        str(source), name="trip-with-siblings", link_to_workspace=False
    )
    child_a_id = db.add_folder(
        str(child_a), name="kept-remote", parent_id=parent_id,
        link_to_workspace=False,
    )
    child_b_id = db.add_folder(
        str(child_b), name="also-remote", parent_id=parent_id,
        link_to_workspace=False,
    )
    db.add_workspace_folder(workspace_id, parent_id, is_root=True)
    db.add_workspace_folder(workspace_id, child_a_id, is_root=False)
    db.add_workspace_folder(workspace_id, child_b_id, is_root=False)

    assert page.request.post(
        f"{live_server['url']}/api/workspaces/{workspace_id}/activate"
    ).ok
    page.goto(f"{live_server['url']}/browse")

    parent = page.locator(f'.tree-item[data-folder-id="{parent_id}"]')
    parent.wait_for(state="visible")
    parent.locator(".tree-toggle").click()
    child_a_row = page.locator(f'.tree-item[data-folder-id="{child_a_id}"]')
    child_b_row = page.locator(f'.tree-item[data-folder-id="{child_b_id}"]')
    expect(child_a_row).to_be_visible()
    expect(child_b_row).to_be_visible()

    # Publish a fallback recovery entry: some hidden folder is in recovery,
    # anchored to the visible parent (as workspace_status returns when the
    # hidden folder's row was pruned as missing).
    hidden_id = max(parent_id, child_a_id, child_b_id) + 100000
    page.evaluate(
        """([data]) => {
          window.vireoLocalFolderData = data;
          window.dispatchEvent(new CustomEvent(
            'vireo:local-folder-status-changed', {detail: {data: data}}
          ));
        }""",
        [{
            "legacy_workspace_session": False,
            "folders": [{
                "requested_folder_id": hidden_id,
                "root_folder_id": hidden_id,
                "state": "recovery",
                "recovery_kind": "sync",
                "visible_ancestor_folder_id": parent_id,
                "folder_name": "hidden-child",
            }],
            "jobs": [],
        }],
    )

    expect(parent.locator(".folder-local-status")).to_have_text(
        "LOCAL ISSUE", timeout=5000
    )
    # Siblings sharing the anchor stay untouched — no LOCAL ISSUE bleed.
    expect(child_a_row.locator(".folder-local-status")).to_have_count(0)
    expect(child_b_row.locator(".folder-local-status")).to_have_count(0)


def test_folder_tree_synthesizes_row_for_top_level_missing_local_root(
    live_server, page
):
    """A top-level missing local root gains a phantom tree row for its badge.

    When the managed local copy of a top-level workspace root disappears,
    ``/api/folders`` drops that row (its status flips to 'missing') and
    ``visible_ancestor_folder_id`` is necessarily ``None`` — the fallback
    renderer has nowhere to attach the LOCAL ISSUE badge, and without a
    synthesized entry the recovery state vanishes exactly when the user
    needs it. renderFolderTree has to inject a phantom top-level row using
    the server-supplied ``folder_name`` (Codex review r3792031813).
    """
    page.goto(f"{live_server['url']}/browse")
    page.locator(".tree-item[data-folder-id]").first.wait_for(state="visible")
    existing_ids = page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#folderTree .tree-item[data-folder-id]'))"
        ".map(el => Number(el.dataset.folderId))"
    )
    phantom_id = (max(existing_ids) if existing_ids else 0) + 999_000

    page.evaluate(
        """([data]) => {
          window.vireoLocalFolderData = data;
          window.dispatchEvent(new CustomEvent(
            'vireo:local-folder-status-changed', {detail: {data: data}}
          ));
        }""",
        [{
            "legacy_workspace_session": False,
            "folders": [{
                "requested_folder_id": phantom_id,
                "root_folder_id": phantom_id,
                "state": "recovery",
                "recovery_kind": "stage",
                "visible_ancestor_folder_id": None,
                "folder_name": "unmounted-nas",
            }],
            "jobs": [],
        }],
    )

    phantom_row = page.locator(f'.tree-item[data-folder-id="{phantom_id}"]')
    expect(phantom_row).to_be_visible(timeout=5000)
    expect(phantom_row.locator(".folder-name")).to_have_text("unmounted-nas")
    expect(phantom_row.locator(".folder-local-status")).to_have_text(
        "LOCAL ISSUE"
    )


def test_folder_tree_does_not_synthesize_remote_only_roots(live_server, page):
    """A purely remote missing root must not appear as a phantom tree row.

    ``workspace_status()`` returns every workspace root, including remote
    ones that ``/api/folders`` legitimately omits (never staged, or an
    unmounted top-level source). Synthesizing a phantom for a remote item
    would restore each such missing root as a clickable zero-count folder
    with no local-status badge, effectively resurrecting deleted or
    unmounted remote folders in Browse (Codex review r3792082330).
    """
    page.goto(f"{live_server['url']}/browse")
    page.locator(".tree-item[data-folder-id]").first.wait_for(state="visible")
    existing_ids = page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#folderTree .tree-item[data-folder-id]'))"
        ".map(el => Number(el.dataset.folderId))"
    )
    ghost_id = (max(existing_ids) if existing_ids else 0) + 998_000

    page.evaluate(
        """([data]) => {
          window.vireoLocalFolderData = data;
          window.dispatchEvent(new CustomEvent(
            'vireo:local-folder-status-changed', {detail: {data: data}}
          ));
        }""",
        [{
            "legacy_workspace_session": False,
            "folders": [{
                "requested_folder_id": ghost_id,
                "root_folder_id": ghost_id,
                "state": "remote",
                "visible_ancestor_folder_id": None,
                "folder_name": "never-staged-nas",
            }],
            "jobs": [],
        }],
    )

    # Give the event loop a chance to run refreshFolderLocalStatusIndicators.
    page.wait_for_timeout(200)
    ghost_row = page.locator(f'.tree-item[data-folder-id="{ghost_id}"]')
    expect(ghost_row).to_have_count(0)


def test_folder_tree_stale_phantom_reloads_real_folder_tree(
    live_server, page
):
    """Discarding recovery on a synthesized phantom must restore the real tree.

    A top-level missing local session emits a phantom top-level row so the
    LOCAL ISSUE badge has somewhere to render. When the user later discards
    that recovery, the next local-folder payload no longer produces a
    phantom, but the injected row is still in ``browseFolderRows``. A
    slot-only refresh would remove the badge and leave an orphaned
    clickable zero-count ``status: 'missing'`` shell — Browse must instead
    detect the stale phantom and re-fetch ``/api/folders`` so the real row
    and count come back (Codex review r3792082333).
    """
    page.goto(f"{live_server['url']}/browse")
    page.locator(".tree-item[data-folder-id]").first.wait_for(state="visible")
    existing_ids = page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#folderTree .tree-item[data-folder-id]'))"
        ".map(el => Number(el.dataset.folderId))"
    )
    phantom_id = (max(existing_ids) if existing_ids else 0) + 997_000

    # 1. Publish a recovery session for a missing top-level root — this
    # synthesizes a phantom tree row that carries the LOCAL ISSUE badge.
    page.evaluate(
        """([data]) => {
          window.vireoLocalFolderData = data;
          window.dispatchEvent(new CustomEvent(
            'vireo:local-folder-status-changed', {detail: {data: data}}
          ));
        }""",
        [{
            "legacy_workspace_session": False,
            "folders": [{
                "requested_folder_id": phantom_id,
                "root_folder_id": phantom_id,
                "state": "recovery",
                "recovery_kind": "stage",
                "visible_ancestor_folder_id": None,
                "folder_name": "stale-phantom-root",
            }],
            "jobs": [],
        }],
    )
    phantom_row = page.locator(f'.tree-item[data-folder-id="{phantom_id}"]')
    expect(phantom_row).to_be_visible(timeout=5000)

    # 2. The user discards the recovery: the local-folder payload no longer
    # references the phantom root. Spy on /api/folders to verify the stale
    # phantom triggers a fresh fetch, and confirm the phantom row is gone.
    # ``safeFetch`` -> ``Vireo.api.json`` -> a closed-over ``browserFetch``
    # (not ``Vireo.api.fetch``), so wrap ``json`` — the only spy point that
    # observes every ``safeFetch('/api/folders')`` from ``loadFolders``.
    page.evaluate(
        "() => { window.__folderFetches = 0;"
        " const original = window.Vireo.api.json;"
        " window.Vireo.api.json = function(input, init, options) {"
        "   const url = typeof input === 'string' ? input : (input && input.url);"
        "   if (url && String(url).indexOf('/api/folders') !== -1) {"
        "     window.__folderFetches++;"
        "   }"
        "   return original.apply(this, arguments);"
        " }; }"
    )
    page.evaluate(
        """([data]) => {
          window.vireoLocalFolderData = data;
          window.dispatchEvent(new CustomEvent(
            'vireo:local-folder-status-changed', {detail: {data: data}}
          ));
        }""",
        [{
            "legacy_workspace_session": False,
            "folders": [],
            "jobs": [],
        }],
    )

    page.wait_for_function(
        "() => window.__folderFetches > 0", timeout=5000
    )
    expect(phantom_row).to_have_count(0, timeout=5000)


def test_folder_tree_filter_by_folder_fires_filter(live_server, page):
    """Clicking 'Filter by this folder' sets activeFolderId to that folder."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    fid = int(item.get_attribute("data-folder-id"))

    item.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()

    menu.locator(".vireo-ctx-item", has_text="Filter by this folder").click()
    expect(menu).to_be_hidden()

    # filterByFolder toggles activeFolderId; the first click should set it.
    page.wait_for_function(
        f"window.activeFolderId === {fid}", timeout=3000
    )


def test_folder_tree_rescan_fires_endpoint(live_server, page):
    """Clicking 'Rescan this Folder' POSTs and acknowledges the click."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    item.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()

    # The seed folder path doesn't exist on disk, so the server may respond
    # 400 "no longer exists" — that's fine. This test verifies the menu item
    # fires a POST at the rescan endpoint, not the job queueing itself (which
    # is covered by vireo/tests/test_folder_rescan_api.py).
    with page.expect_response(lambda r: "/rescan" in r.url):
        menu.locator(
            ".vireo-ctx-item", has_text="Rescan this Folder"
        ).click()
    expect(page.locator("#toastContainer")).to_contain_text(
        "Starting folder rescan"
    )


def test_folder_tree_reveal_fires_endpoint(live_server, page):
    """Clicking 'Reveal in Finder/Folder' POSTs to /api/files/reveal
    with a folder_id body."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    item.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()

    with page.expect_request(
        lambda r: r.url.endswith("/api/files/reveal") and r.method == "POST"
    ) as req_info:
        menu.locator(
            ".vireo-ctx-item", has_text="Reveal in"
        ).click()

    req = req_info.value
    body = req.post_data_json or {}
    assert "folder_id" in body, f"reveal request body missing folder_id: {body}"


def test_folder_tree_copy_path_fetches_folder(live_server, page):
    """Clicking 'Copy Path' fetches GET /api/folders/<id> to resolve the path."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    # Grant clipboard perms so the write call doesn't throw.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    fid = int(item.get_attribute("data-folder-id"))
    item.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()

    with page.expect_response(
        lambda r: r.url.endswith(f"/api/folders/{fid}") and r.status == 200
    ):
        menu.locator(".vireo-ctx-item", has_text="Copy Path").click()


def test_folder_tree_right_click_does_not_trigger_filter(live_server, page):
    """Right-click must not fire the left-click onclick handler (filterByFolder).

    Regression guard: the folder tree items use inline onclick; a bare
    right-click must preventDefault and NOT also toggle the filter.
    """
    url = live_server["url"]
    page.goto(f"{url}/browse")

    item = page.locator(".tree-item[data-folder-id]").first
    item.wait_for(state="visible")
    assert page.evaluate("window.activeFolderId") in (None, 0)

    item.click(button="right")
    expect(page.locator(".vireo-ctx-menu")).to_be_visible()

    # activeFolderId should not have been mutated by the right-click itself.
    assert page.evaluate("window.activeFolderId") in (None, 0)
