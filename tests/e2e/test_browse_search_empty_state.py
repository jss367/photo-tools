import json

from playwright.sync_api import expect


def test_folder_health_refresh_preserves_active_collection(live_server, page, tmp_path):
    """A folder-health refresh must not kick the user out of a collection scope.

    Regression: ``resetAndLoad()``'s default clears ``activeCollectionId``
    for non-dashboard scopes, and the previous refresh path also bumped
    ``browseScopeGen``. Both would combine to drop the user back to the
    unscoped workspace grid when a drive or share reconnected while they
    were viewing a normal collection — either during the brief
    bootstrap window before ``filterByCollection`` swallows the id into
    filter chips, or via the chip path being wiped by the reset
    (CodeRabbit review r3684913393, Codex review r3684907518).
    """
    db = live_server["db"]
    park = tmp_path / "park"
    yard = tmp_path / "yard"
    park.mkdir()
    yard.mkdir()
    folder_ids = live_server["data"]["folders"]
    # park is repointed to a real path so the health check confirms it's
    # ok (no status change); yard is marked missing so the check flips it
    # back to ok and broadcasts vireo:folder-health-changed exactly once.
    db.conn.execute(
        "UPDATE folders SET path = ?, status = 'ok' WHERE id = ?",
        (str(park), folder_ids[0]),
    )
    db.conn.execute(
        "UPDATE folders SET path = ?, status = 'missing' WHERE id = ?",
        (str(yard), folder_ids[1]),
    )
    db.conn.commit()

    rules = json.dumps([{"field": "extension", "op": "is", "value": ".jpg"}])
    collection_id = db.add_collection("All JPGs", rules)

    # Deep-link into the collection so bootstrap scopes the first paint
    # through ``activeCollectionId`` and then loads the saved expression
    # into the filter bar. yard is still missing, so only park's 3 hawks
    # show.
    page.goto(f"{live_server['url']}/browse?collection_id={collection_id}")
    page.wait_for_function("window.VireoFilter && VireoFilter.isReady()")
    page.wait_for_function(
        "document.querySelector('.vf-chips') && "
        "document.querySelector('.vf-chips').textContent.includes('File extension')",
        timeout=4000,
    )
    expect(page.locator(".grid-card")).to_have_count(3)

    # Health-refresh path must call resetAndLoad with preserveCollection so
    # the option's short-circuit is exercised directly. If a future refactor
    # drops the flag, this asserts the option is still honored.
    result = page.evaluate(
        f"(async () => {{"
        f"  activeCollectionId = {collection_id};"
        f"  await resetAndLoad({{ preserveCollection: true }});"
        f"  return activeCollectionId;"
        f"}})()"
    )
    assert result == collection_id

    with page.expect_response(
        lambda response: response.url.endswith("/api/folders/check-health")
    ) as health_response:
        page.evaluate("openMissingFoldersModal()")
    assert health_response.value.json()["changed"] == 1

    # The collection scope must survive the refresh: the collection's
    # rules stay in the filter bar and the grid now includes yard's
    # photos because the collection matches every .jpg.
    expect(page.locator(".grid-card")).to_have_count(5)
    expect(page.locator(".vf-chips")).to_contain_text("File extension")


def test_reconnected_folders_refresh_empty_browse(live_server, page, tmp_path):
    """A health check that restores folders must repopulate an open Browse."""
    db = live_server["db"]
    park = tmp_path / "park"
    yard = tmp_path / "yard"
    park.mkdir()
    yard.mkdir()
    folder_ids = live_server["data"]["folders"]
    db.conn.executemany(
        "UPDATE folders SET path = ?, status = 'missing' WHERE id = ?",
        [(str(park), folder_ids[0]), (str(yard), folder_ids[1])],
    )
    db.conn.commit()

    page.goto(f"{live_server['url']}/browse")
    expect(page.locator("#welcomeState")).to_be_visible()
    expect(page.locator(".grid-card")).to_have_count(0)

    with page.expect_response(
        lambda response: response.url.endswith("/api/folders/check-health")
    ) as health_response:
        page.evaluate("openMissingFoldersModal()")

    assert health_response.value.json()["changed"] == 2
    expect(page.locator(".grid-card")).to_have_count(5)
    expect(page.locator("#welcomeState")).to_be_hidden()
    expect(page.locator("#filterSummary")).to_contain_text("of 5")


def test_background_health_recovery_refreshes_browse(live_server, page, tmp_path):
    """The 10-minute /api/folders/missing poll must refresh Browse when a
    server-side background reconnect flips a folder missing→ok.

    Regression: the folder-health event only fired from the modal's
    /api/folders/check-health POST. The server's own _folder_health_loop
    runs independently every 10 minutes and can restore a folder with no
    client involvement; without a diff in the periodic
    ``checkMissingFolders()`` poll a long-lived Browse view kept its
    pre-flip empty grid until the user reopened the modal or reloaded
    (Codex review r3685083009).
    """
    db = live_server["db"]
    park = tmp_path / "park"
    yard = tmp_path / "yard"
    park.mkdir()
    yard.mkdir()
    folder_ids = live_server["data"]["folders"]
    db.conn.executemany(
        "UPDATE folders SET path = ?, status = 'missing' WHERE id = ?",
        [(str(park), folder_ids[0]), (str(yard), folder_ids[1])],
    )
    db.conn.commit()

    page.goto(f"{live_server['url']}/browse")
    expect(page.locator("#welcomeState")).to_be_visible()
    expect(page.locator(".grid-card")).to_have_count(0)

    # Wait for the initial poll snapshot to be recorded — otherwise the
    # next checkMissingFolders() would see _missingFoldersLastIds === null
    # and treat the first-since-load state as "no change".
    page.wait_for_function(
        "typeof _missingFoldersLastIds !== 'undefined' && "
        "_missingFoldersLastIds !== null && _missingFoldersLastIds.length === 2"
    )

    # Simulate the server-side _folder_health_loop restoring both folders.
    # This mutates DB state without ever touching /api/folders/check-health.
    db.conn.execute(
        "UPDATE folders SET status = 'ok' WHERE id IN (?, ?)",
        (folder_ids[0], folder_ids[1]),
    )
    db.conn.commit()

    # Fire the periodic poll manually (the real code runs it on a 10-minute
    # interval). It must diff the missing set, detect the missing→ok
    # transition, and dispatch vireo:folder-health-changed.
    page.evaluate("checkMissingFolders()")

    expect(page.locator(".grid-card")).to_have_count(5)
    expect(page.locator("#welcomeState")).to_be_hidden()
    expect(page.locator("#filterSummary")).to_contain_text("of 5")


def test_keyword_search_empty_state_and_clear(live_server, page):
    """A zero-result keyword search must not look like an empty library."""
    url = live_server["url"]
    page.goto(f"{url}/browse")

    cards = page.locator(".grid-card")
    cards.first.wait_for(state="visible")

    search = page.locator(".vf-search input")
    expect(search).to_have_attribute("autocomplete", "off")
    expect(search).to_have_attribute("spellcheck", "false")

    with page.expect_response(lambda response: "/api/photos/query" in response.url):
        search.fill("definitely-no-such-photo")
        search.press("Enter")

    expect(page.locator("#emptyState")).to_be_visible()
    expect(page.locator("#welcomeState")).to_be_hidden()
    expect(page.locator("#emptyState")).to_contain_text("No photos match")

    with page.expect_response(lambda response: "/api/photos/query" in response.url):
        search.fill("")
        search.press("Enter")

    cards.first.wait_for(state="visible")
    expect(page.locator("#emptyState")).to_be_hidden()
    expect(page.locator("#welcomeState")).to_be_hidden()


def test_clearing_keyword_search_keeps_selected_photo_in_place(live_server, page):
    """Restoring filtered-out photos must not pull focus away from the selection."""
    url = live_server["url"]
    selected_id = live_server["data"]["photos"][3]
    page.goto(f"{url}/browse")

    page.locator(".grid-card").first.wait_for(state="visible")
    page.evaluate("updateThumbSize(400)")

    page.evaluate("VireoFilter.quickSearch('American Robin')")
    page.wait_for_function("() => photos.length === 1")
    selected = page.locator(f'.grid-card[data-id="{selected_id}"]')
    selected.wait_for(state="visible")
    selected.click()

    top_before = page.evaluate(
        """(id) => {
          const card = document.querySelector(`.grid-card[data-id="${id}"]`);
          const container = document.getElementById('gridContainer');
          return card.getBoundingClientRect().top - container.getBoundingClientRect().top;
        }""",
        selected_id,
    )

    page.evaluate("VireoFilter.quickSearch('')")
    page.wait_for_function(
        "(id) => photos.length === 5 && selectedPhotoId === id",
        arg=selected_id,
    )
    page.wait_for_timeout(100)  # allow the anchor-restoration animation frame

    assert page.evaluate("selectedPhotos.size") == 0
    expect(selected).to_have_class("grid-card selected")
    top_after = page.evaluate(
        """(id) => {
          const card = document.querySelector(`.grid-card[data-id="${id}"]`);
          const container = document.getElementById('gridContainer');
          return card.getBoundingClientRect().top - container.getBoundingClientRect().top;
        }""",
        selected_id,
    )
    assert abs(top_after - top_before) < 4


def test_flag_quick_filters_show_picks_and_rejects(live_server, page):
    """Browse keeps always-visible quick filters for picked and rejected photos."""
    url = live_server["url"]
    db = live_server["db"]
    photos = db.get_photos()
    pick_id = photos[0]["id"]
    reject_id = photos[1]["id"]
    db.update_photo_flag(pick_id, "flagged")
    db.update_photo_flag(reject_id, "rejected")

    page.goto(f"{url}/browse")
    page.locator(".grid-card").first.wait_for(state="visible")

    page.click(".vf-filters-btn")
    pick_btn = page.locator('.vf-quick-flags [data-flag="flagged"]')
    reject_btn = page.locator('.vf-quick-flags [data-flag="rejected"]')
    expect(pick_btn).to_be_visible()
    expect(reject_btn).to_be_visible()

    pick_btn.click()
    expect(pick_btn).to_have_class("active")
    expect(page.locator(".grid-card")).to_have_count(1)
    assert page.locator(".grid-card").first.get_attribute("data-id") == str(pick_id)

    # Flags multi-select now: adding Rejected combines into "is one of".
    reject_btn.click()
    expect(page.locator(".grid-card")).to_have_count(2)

    pick_btn.click()
    expect(pick_btn).not_to_have_class("active")
    expect(page.locator(".grid-card")).to_have_count(1)
    assert page.locator(".grid-card").first.get_attribute("data-id") == str(reject_id)

    reject_btn.click()
    expect(page.locator(".grid-card")).to_have_count(5)
