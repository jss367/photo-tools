"""Process-review controls for rejecting a burst or full encounter."""

import json
import os
import re
import time

from playwright.sync_api import expect


def _write_grouped_pipeline_cache(live_server, photo_ids):
    db = live_server["db"]
    placeholders = ",".join("?" for _ in photo_ids)
    rows = db.conn.execute(
        f"SELECT id, filename, timestamp, flag FROM photos "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        photo_ids,
    ).fetchall()
    photos = [
        {
            "id": row["id"],
            "filename": row["filename"],
            "timestamp": row["timestamp"],
            "label": "REVIEW",
            "quality_composite": 0.5,
            "flag": row["flag"],
            "rating": 0,
        }
        for row in rows
    ]
    ids = [photo["id"] for photo in photos]
    bursts = [
        {"photo_ids": ids[:2], "species_predictions": [], "species_override": None},
        {"photo_ids": ids[2:], "species_predictions": [], "species_override": None},
    ]
    cache = {
        "photos": photos,
        "encounters": [
            {
                "photo_ids": ids,
                "photo_count": len(ids),
                "burst_count": len(bursts),
                "time_range": [photos[0]["timestamp"], photos[-1]["timestamp"]],
                "species": [],
                "species_predictions": [],
                "species_confirmed": False,
                "confirmed_species": None,
                "bursts": bursts,
            }
        ],
        "summary": {
            "total_photos": len(ids),
            "encounter_count": 1,
            "burst_count": len(bursts),
            "keep_count": 0,
            "review_count": len(ids),
            "reject_count": 0,
            "rarity_protected": 0,
        },
    }
    path = os.path.join(
        os.path.dirname(db._db_path),
        f"pipeline_results_ws{db._active_workspace_id}.json",
    )
    with open(path, "w") as cache_file:
        json.dump(cache, cache_file)


def _flags(db, photo_ids):
    placeholders = ",".join("?" for _ in photo_ids)
    rows = db.conn.execute(
        f"SELECT id, flag FROM photos WHERE id IN ({placeholders}) ORDER BY id",
        photo_ids,
    ).fetchall()
    return [row["flag"] for row in rows]


def test_reject_burst_and_undo_restores_prior_flags(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    db.update_photo_flag(photo_ids[0], "flagged")
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    burst_buttons = page.get_by_test_id("reject-burst")
    expect(burst_buttons).to_have_count(2)

    burst_buttons.first.click()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in burst")
    expect(page.get_by_test_id("reject-burst").first).to_have_attribute(
        "aria-label", "Clear rejects"
    )
    assert _flags(db, photo_ids) == ["rejected", "rejected", "none", "none"]
    expect(
        page.locator(f'.photo-card[data-photo-id="{photo_ids[0]}"] .flag-rejected')
    ).to_have_text("X")

    page.locator("#undoToast .undo-toast-btn").click()

    expect(page.get_by_test_id("reject-burst").first).to_have_attribute(
        "aria-label", "Reject burst"
    )
    expect(page.get_by_text("Restored previous flags for burst", exact=True)).to_be_visible()
    assert _flags(db, photo_ids) == ["flagged", "none", "none", "none"]


def test_reject_and_clear_full_encounter(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    encounter_button = page.get_by_test_id("reject-encounter")
    expect(encounter_button).to_have_attribute("aria-label", "Reject encounter")

    encounter_button.click()

    expect(encounter_button).to_have_attribute("aria-label", "Clear rejects")
    expect(page.locator("#undoMsg")).to_have_text("Rejected 4 photos in encounter")
    assert _flags(db, photo_ids) == ["rejected"] * 4

    encounter_button.click()

    expect(encounter_button).to_have_attribute("aria-label", "Reject encounter")
    expect(page.locator("#undoMsg")).to_have_text(
        "Cleared rejects from 4 photos in encounter"
    )
    assert _flags(db, photo_ids) == ["none"] * 4


def _write_partially_confirmed_pipeline_cache(live_server, photo_ids):
    """Encounter with a confirmed first burst and an unconfirmed second burst."""
    db = live_server["db"]
    placeholders = ",".join("?" for _ in photo_ids)
    rows = db.conn.execute(
        f"SELECT id, filename, timestamp, flag FROM photos "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        photo_ids,
    ).fetchall()
    photos = [
        {
            "id": row["id"],
            "filename": row["filename"],
            "timestamp": row["timestamp"],
            "label": "REVIEW",
            "quality_composite": 0.5,
            "flag": row["flag"],
            "rating": 0,
        }
        for row in rows
    ]
    ids = [photo["id"] for photo in photos]
    bursts = [
        {
            "photo_ids": ids[:2],
            "species_predictions": [],
            "species_override": {"species": "American Robin", "confirmed": True},
        },
        {"photo_ids": ids[2:], "species_predictions": [], "species_override": None},
    ]
    cache = {
        "photos": photos,
        "encounters": [
            {
                "photo_ids": ids,
                "photo_count": len(ids),
                "burst_count": len(bursts),
                "time_range": [photos[0]["timestamp"], photos[-1]["timestamp"]],
                "species": [],
                "species_predictions": [],
                "species_confirmed": False,
                "confirmed_species": None,
                "bursts": bursts,
            }
        ],
        "summary": {
            "total_photos": len(ids),
            "encounter_count": 1,
            "burst_count": len(bursts),
            "keep_count": 0,
            "review_count": len(ids),
            "reject_count": 0,
            "rarity_protected": 0,
        },
    }
    path = os.path.join(
        os.path.dirname(db._db_path),
        f"pipeline_results_ws{db._active_workspace_id}.json",
    )
    with open(path, "w") as cache_file:
        json.dump(cache, cache_file)


def test_encounter_reject_skips_hidden_confirmed_bursts(live_server, page):
    """With `Hide confirmed` active, the encounter-level Reject/Clear button
    must only touch the bursts that are actually rendered — not the ones the
    user hid by confirming their species. Regression for the case where the
    header control read `Clear rejects`/`Reject encounter` off the whole
    photo list, so clicking it could flip flags on invisible photos."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_partially_confirmed_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".burst-strip")).to_have_count(2)

    page.locator("#hideConfirmedBtn").click()
    expect(page.locator(".burst-strip")).to_have_count(1)

    encounter_button = page.get_by_test_id("reject-encounter")
    expect(encounter_button).to_have_attribute("aria-label", "Reject encounter")

    encounter_button.click()

    expect(encounter_button).to_have_attribute("aria-label", "Clear rejects")
    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in encounter")
    # First (confirmed, hidden) burst must be untouched; only the visible
    # unconfirmed burst's photos are rejected.
    assert _flags(db, photo_ids) == ["none", "none", "rejected", "rejected"]

    encounter_button.click()

    expect(encounter_button).to_have_attribute("aria-label", "Reject encounter")
    expect(page.locator("#undoMsg")).to_have_text(
        "Cleared rejects from 2 photos in encounter"
    )
    assert _flags(db, photo_ids) == ["none"] * 4


def _write_mixed_label_pipeline_cache(live_server, photo_ids):
    """Encounter with a single burst whose photos carry mixed labels so
    changing the label filter hides some frames inside the burst."""
    db = live_server["db"]
    placeholders = ",".join("?" for _ in photo_ids)
    rows = db.conn.execute(
        f"SELECT id, filename, timestamp, flag FROM photos "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        photo_ids,
    ).fetchall()
    # First two photos are KEEP, last two are REVIEW. Selecting the REVIEW
    # filter should hide the KEEP frames but still render the burst.
    labels = ["KEEP", "KEEP", "REVIEW", "REVIEW"]
    photos = [
        {
            "id": row["id"],
            "filename": row["filename"],
            "timestamp": row["timestamp"],
            "label": labels[idx],
            "quality_composite": 0.5,
            "flag": row["flag"],
            "rating": 0,
        }
        for idx, row in enumerate(rows)
    ]
    ids = [photo["id"] for photo in photos]
    bursts = [
        {"photo_ids": ids, "species_predictions": [], "species_override": None},
    ]
    cache = {
        "photos": photos,
        "encounters": [
            {
                "photo_ids": ids,
                "photo_count": len(ids),
                "burst_count": len(bursts),
                "time_range": [photos[0]["timestamp"], photos[-1]["timestamp"]],
                "species": [],
                "species_predictions": [],
                "species_confirmed": False,
                "confirmed_species": None,
                "bursts": bursts,
            }
        ],
        "summary": {
            "total_photos": len(ids),
            "encounter_count": 1,
            "burst_count": len(bursts),
            "keep_count": 2,
            "review_count": 2,
            "reject_count": 0,
            "rarity_protected": 0,
        },
    }
    path = os.path.join(
        os.path.dirname(db._db_path),
        f"pipeline_results_ws{db._active_workspace_id}.json",
    )
    with open(path, "w") as cache_file:
        json.dump(cache, cache_file)


def test_burst_reject_respects_active_label_filter(live_server, page):
    """When the Review label filter is active, clicking `Reject burst` must
    only touch photos that pass the filter. Regression for the case where the
    button targeted the raw burst photo list, so it could flip flags on
    KEEP/non-conflict frames hidden by the filter."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_mixed_label_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    page.locator('.filter-btn[data-filter="REVIEW"]').click()
    # Two KEEP frames hide; the two REVIEW frames remain rendered in the burst.
    expect(page.locator(".photo-card")).to_have_count(2)

    burst_button = page.get_by_test_id("reject-burst")
    burst_button.click()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in burst")
    expect(burst_button).to_have_attribute("aria-label", "Clear rejects")
    # The hidden KEEP frames must be untouched; only the visible REVIEW frames
    # are rejected.
    assert _flags(db, photo_ids) == ["none", "none", "rejected", "rejected"]


def test_clear_rejects_reads_live_db_flags(live_server, page):
    """`Clear rejects` must not overwrite a photo the user picked live in
    Browse (another tab) just because the client cache still shows it as
    rejected. Regression: the bulk action derived changedIds and
    previousFlags from pipelineResults.photos[].flag — a client-side cache
    that page-init refreshes on load but that goes stale the moment a
    parallel session mutates flags — so a subsequent "Clear rejects" click
    silently cleared the live pick and Undo restored the stale 'rejected'
    rather than the pick."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    burst_buttons = page.get_by_test_id("reject-burst")
    expect(burst_buttons).to_have_count(2)

    # Reject the first burst: both photos become 'rejected' in the DB and in
    # the in-page pipelineResults cache. The button flips to "Clear rejects".
    burst_buttons.first.click()
    expect(burst_buttons.first).to_have_attribute("aria-label", "Clear rejects")
    assert _flags(db, photo_ids) == ["rejected", "rejected", "none", "none"]

    # Simulate a live pick made in another Browse tab: the DB updates but the
    # already-rendered pipelineResults cache does not. The bulk button still
    # reads "Clear rejects" even though the first photo is now a pick.
    db.update_photo_flag(photo_ids[0], "flagged")
    expect(burst_buttons.first).to_have_attribute("aria-label", "Clear rejects")

    burst_buttons.first.click()

    # Only the truly-rejected photo in the burst is cleared. The live pick
    # is preserved; the second burst is untouched.
    expect(page.locator("#undoMsg")).to_have_text(
        "Cleared rejects from 1 photo in burst"
    )
    assert _flags(db, photo_ids) == ["flagged", "none", "none", "none"]

    page.locator("#undoToast .undo-toast-btn").click()

    expect(
        page.get_by_text("Restored previous flags for burst", exact=True)
    ).to_be_visible()
    # Undo restores what was actually in the DB when the bulk action ran —
    # the second photo goes back to 'rejected', and the live pick stays a
    # pick rather than being clobbered by a stale cached value.
    assert _flags(db, photo_ids) == ["flagged", "rejected", "none", "none"]


def test_burst_reject_blocked_while_encounter_reject_pending(live_server, page):
    """When an encounter reject is still writing, clicking a burst reject
    inside it must be blocked — otherwise both requests snapshot
    previousFlags from the pre-first-action DB read, and the burst's later
    Undo could restore the shared photos to 'none', silently rolling back
    part of the encounter reject."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    burst_buttons = page.get_by_test_id("reject-burst")
    expect(burst_buttons).to_have_count(2)
    encounter_button = page.get_by_test_id("reject-encounter")

    # Hold the encounter's /api/pipeline/group/state request in flight so
    # its bulk action keeps the lock while we try to fire a burst-level
    # bulk action. Only hold the first call — post-release runs and the
    # test cleanup issue further live reads that must pass through.
    held = {}

    def handle_group_state(route):
        if "route" not in held:
            held["route"] = route
            return
        route.continue_()

    page.route("**/api/pipeline/group/state", handle_group_state)

    encounter_button.click()

    deadline = time.time() + 5
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(50)
    assert "route" in held, "expected the encounter group-state read to be held"

    # Clicking a burst reject inside the still-writing encounter must be
    # blocked with a user-visible toast, not silently kick off a second
    # bulk action.
    burst_buttons.first.click()
    expect(
        page.get_by_text(
            "Another bulk reject is still finishing", exact=False
        )
    ).to_be_visible()
    # The blocked burst click must not have altered any DB flags: the
    # encounter's write is still gated on the held read.
    assert _flags(db, photo_ids) == ["none"] * 4

    # Release the encounter read so its bulk write can proceed against the
    # real endpoint (the batch-flag call is not intercepted).
    held["route"].continue_()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 4 photos in encounter")
    assert _flags(db, photo_ids) == ["rejected"] * 4


def test_single_photo_flag_blocked_while_bulk_reject_pending(live_server, page):
    """The shared lightbox/per-photo flag path must honor the same photo-ID
    lock as overlapping bulk actions. Otherwise a pick made while the bulk
    request is paused can be overwritten by the eventual batch write and its
    Undo restores the pre-pick snapshot."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    encounter_button = page.get_by_test_id("reject-encounter")
    held = {}

    def handle_group_state(route):
        if "route" not in held:
            held["route"] = route
            return
        route.continue_()

    page.route("**/api/pipeline/group/state", handle_group_state)
    encounter_button.click()

    deadline = time.time() + 5
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(50)
    assert "route" in held, "expected the encounter group-state read to be held"

    landed = page.evaluate(
        "([photoId]) => window.setFlagFor(photoId, 'flagged')",
        [photo_ids[0]],
    )

    assert landed is False
    expect(
        page.get_by_text(
            "A bulk reject for this photo is still finishing", exact=False
        )
    ).to_be_visible()
    assert _flags(db, photo_ids) == ["none"] * 4

    held["route"].continue_()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 4 photos in encounter")
    assert _flags(db, photo_ids) == ["rejected"] * 4


def test_burst_modal_apply_blocked_while_bulk_reject_pending(live_server, page):
    """The burst review modal writes flags through its own group/apply route,
    so it must consult the shared photo-ID lock independently."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    held = {}

    def handle_group_state(route):
        if "route" not in held:
            held["route"] = route
            return
        route.continue_()

    page.route("**/api/pipeline/group/state", handle_group_state)
    page.get_by_test_id("reject-burst").first.click()

    deadline = time.time() + 5
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(50)
    assert "route" in held, "expected the bulk live-state read to be held"

    # The grid remains interactive while the bulk read is pending. Open the
    # same burst in the modal, make a reject decision, and try to apply it.
    page.locator(f'.photo-card[data-photo-id="{photo_ids[0]}"] img').click()
    expect(page.locator("#grmApplyBtn")).to_be_enabled()
    page.keyboard.press("x")
    expect(page.locator("#grmApplyFlagsChk")).to_be_checked()

    apply_requests = []
    page.on(
        "request",
        lambda request: (
            apply_requests.append(request)
            if "/api/pipeline/group/apply" in request.url
            else None
        ),
    )
    page.locator("#grmApplyBtn").click()

    expect(
        page.get_by_text(
            "A bulk reject for this group is still finishing", exact=False
        )
    ).to_be_visible()
    expect(page.locator("#grmOverlay")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#grmApplyBtn")).to_be_enabled()
    assert apply_requests == []
    assert _flags(db, photo_ids) == ["none"] * 4

    # The native Photo > Pick command must use the page's batchSetFlag helper
    # rather than bypassing the modal through /api/batch/flag. The pending
    # reject stays put and no database write lands while the bulk lock is held.
    page.evaluate("() => nativeMenuSetFlag('flagged')")
    expect(
        page.get_by_text(
            "A bulk reject for the selected photos is still finishing",
            exact=False,
        )
    ).to_be_visible()
    expect(page.locator("#grmCount")).to_have_text(
        "0 picks, 1 rejects, 1 unsorted"
    )
    assert _flags(db, photo_ids) == ["none"] * 4

    held["route"].continue_()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in burst")
    assert _flags(db, photo_ids) == ["rejected", "rejected", "none", "none"]


def test_single_photo_flag_blocked_while_bulk_undo_pending(live_server, page):
    """Undo restores are bulk writes too, so they must retain the photo-ID
    lock until every prior flag has been restored."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    burst_button = page.get_by_test_id("reject-burst").first
    burst_button.click()
    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in burst")
    assert _flags(db, photo_ids) == ["rejected", "rejected", "none", "none"]

    held = {}

    def handle_batch_flag(route):
        if "route" not in held:
            held["route"] = route
            return
        route.continue_()

    page.route("**/api/batch/flag", handle_batch_flag)
    page.locator("#undoToast .undo-toast-btn").click()

    deadline = time.time() + 5
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(50)
    assert "route" in held, "expected the undo batch write to be held"

    page.evaluate(
        "([photoId]) => window.setFlagFor(photoId, 'flagged')",
        [photo_ids[0]],
    )

    expect(
        page.get_by_text(
            "A bulk reject for this photo is still finishing", exact=False
        )
    ).to_be_visible()
    assert _flags(db, photo_ids) == ["rejected", "rejected", "none", "none"]

    held["route"].continue_()

    expect(
        page.get_by_text("Restored previous flags for burst", exact=True)
    ).to_be_visible()
    assert _flags(db, photo_ids) == ["none"] * 4


def test_bulk_reject_surfaces_live_flag_read_failure(live_server, page):
    """A failed live-state read must use the standard request error toast
    instead of silently abandoning the group action."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    page.route(
        "**/api/pipeline/group/state",
        lambda route: route.fulfill(
            status=500,
            json={"error": "Could not read current photo flags"},
        ),
    )

    page.get_by_test_id("reject-burst").first.click()

    expect(
        page.get_by_text("Could not read current photo flags", exact=True)
    ).to_be_visible()
    assert _flags(db, photo_ids) == ["none"] * 4


def test_encounter_reject_respects_active_label_filter(live_server, page):
    """Same guarantee as the burst-level test, but for the encounter-level
    Reject/Clear button: hidden KEEP frames stay untouched when the Review
    filter is active."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_mixed_label_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    page.locator('.filter-btn[data-filter="REVIEW"]').click()
    expect(page.locator(".photo-card")).to_have_count(2)

    encounter_button = page.get_by_test_id("reject-encounter")
    encounter_button.click()

    expect(page.locator("#undoMsg")).to_have_text("Rejected 2 photos in encounter")
    expect(encounter_button).to_have_attribute("aria-label", "Clear rejects")
    assert _flags(db, photo_ids) == ["none", "none", "rejected", "rejected"]

    encounter_button.click()

    expect(encounter_button).to_have_attribute("aria-label", "Reject encounter")
    expect(page.locator("#undoMsg")).to_have_text(
        "Cleared rejects from 2 photos in encounter"
    )
    assert _flags(db, photo_ids) == ["none"] * 4


def test_photo_context_menu_exposes_review_and_organization_actions(
    live_server, page
):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    page.locator(".photo-card[data-photo-id]").first.click(button="right")

    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()
    menu_text = menu.inner_text()
    for label in (
        "Add to Collection",
        "Add Keyword",
        "Open in Lightbox",
        "Open in Browse",
        "Find Similar",
        "Copy Path",
        "Edit Photo",
        "Open in Editor",
        "Develop in darktable",
    ):
        assert label in menu_text
    expect(menu.locator('.vireo-ctx-chip[title="Rate 4"]')).to_have_count(1)
    expect(menu.locator('.vireo-ctx-chip[title="Flag as pick"]')).to_have_count(1)
    expect(menu.locator('.vireo-ctx-chip[title="Reject"]')).to_have_count(1)
    expect(menu.locator('.vireo-ctx-chip[title="Clear flag"]')).to_have_count(1)


def test_lightbox_navigation_follows_visible_filtered_cards(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_mixed_label_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    page.locator('.filter-btn[data-filter="REVIEW"]').click()
    cards = page.locator("#encountersContainer .photo-card[data-photo-id]")
    expect(cards).to_have_count(2)
    visible_ids = [
        int(cards.nth(i).get_attribute("data-photo-id")) for i in range(cards.count())
    ]

    page.evaluate("pid => openPipelineLightbox(pid)", visible_ids[0])
    assert page.evaluate("_lightboxPhotoList.map(photo => photo.id)") == visible_ids
    page.keyboard.press("ArrowRight")
    assert page.evaluate("_lightboxCurrentId") == visible_ids[1]

    page.evaluate("closeLightbox()")
    page.evaluate(
        "pid => setTimeout(function() { openPipelinePhotoEditor(pid); }, 0)",
        visible_ids[0],
    )
    page.wait_for_url(f"**/edit/{visible_ids[0]}")
    assert page.evaluate("window.vireoEditNav.getList()") == visible_ids


def test_native_photo_command_opens_pipeline_lightbox(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    photo_id = photo_ids[0]
    page.evaluate(
        "pid => { pipelineReviewContextPhotoIds = [pid]; nativeMenuOpenLightbox(); }",
        photo_id,
    )
    expect(page.locator("#lightboxOverlay")).to_have_class(re.compile(r"\bactive\b"))
    assert page.evaluate("_lightboxCurrentId") == photo_id


def test_photo_context_menu_adds_photo_to_existing_collection(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)
    collection_id = db.add_collection(
        "Process Review Picks",
        json.dumps([{"field": "photo_ids", "value": []}]),
    )

    page.goto(f"{live_server['url']}/pipeline/review")
    card = page.locator(".photo-card[data-photo-id]").first
    photo_id = int(card.get_attribute("data-photo-id"))
    card.click(button="right")
    page.locator(".vireo-ctx-item", has_text="Add to Collection").click()

    modal = page.locator("#pipelineCollectionModal")
    expect(modal).to_have_class(re.compile(r"\bopen\b"))
    modal.locator(".pipeline-collection-choice", has_text="Process Review Picks").click()
    modal.locator(".modal-btn-primary", has_text="Add").click()
    expect(modal).not_to_have_class(re.compile(r"\bopen\b"))

    members = db.get_collection_photos(collection_id, per_page=100)
    assert [member["id"] for member in members] == [photo_id]


def test_collection_load_failure_clears_native_selection_override(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          window._vireoNativeMenuPhotoIdsOverride = ids.slice(0, 2);
          window.safeFetch = function(url) {
            if (url === '/api/collections') return Promise.reject(new Error('offline'));
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            await addToCollection();
            return {
              override: window._vireoNativeMenuPhotoIdsOverride,
              modalIds: pipelineReviewCollectionPhotoIds.slice(),
              modalOpen: document.getElementById('pipelineCollectionModal').classList.contains('open'),
            };
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {"override": None, "modalIds": [], "modalOpen": False}


def test_latest_collection_load_owns_modal_selection(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          const resolvers = [];
          window.safeFetch = function(url) {
            if (url === '/api/collections') {
              return new Promise(resolve => { resolvers.push(resolve); });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            const first = addToCollection([ids[0]]);
            const secondIds = [ids[1], ids[2]];
            const second = addToCollection(secondIds);
            resolvers[0]([]);
            const firstResult = await first;
            const afterFirst = {
              open: document.getElementById('pipelineCollectionModal').classList.contains('open'),
              ids: pipelineReviewCollectionPhotoIds.slice(),
            };
            resolvers[1]([]);
            const secondResult = await second;
            return {
              firstResult,
              secondResult,
              afterFirst,
              finalIds: pipelineReviewCollectionPhotoIds.slice(),
              title: document.getElementById('pipelineCollectionTitle').textContent,
            };
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {
        "firstResult": False,
        "secondResult": True,
        "afterFirst": {"open": False, "ids": []},
        "finalIds": photo_ids[1:3],
        "title": "Add 2 photos to Collection",
    }


def test_new_collection_submission_is_single_flight_and_retryable(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          let createCalls = 0;
          let addCalls = 0;
          let releaseCreate;
          const createGate = new Promise(resolve => { releaseCreate = resolve; });
          window.safeFetch = async function(url, options) {
            if (url === '/api/collections' && options && options.method === 'POST') {
              createCalls += 1;
              await createGate;
              return {id: 9876};
            }
            if (url === '/api/collections/9876/add-photos') {
              addCalls += 1;
              if (addCalls === 1) throw new Error('temporary add failure');
              return {ok: true};
            }
            return originalSafeFetch.apply(this, arguments);
          };
          pipelineReviewCollectionPhotoIds = ids.slice(0, 2);
          document.getElementById('pipelineCollectionNewName').value = 'Retry Picks';
          document.getElementById('pipelineCollectionModal').classList.add('open');
          try {
            const first = confirmPipelineCollection();
            const duplicate = confirmPipelineCollection();
            const disabledWhilePending = document.getElementById('pipelineCollectionSubmitBtn').disabled;
            releaseCreate();
            const firstResults = await Promise.all([first, duplicate]);
            const retainedId = pipelineReviewCreatedCollection && pipelineReviewCreatedCollection.id;
            const retryResult = await confirmPipelineCollection();
            return {createCalls, addCalls, disabledWhilePending, firstResults, retainedId, retryResult};
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {
        "createCalls": 1,
        "addCalls": 2,
        "disabledWhilePending": True,
        "firstResults": [False, False],
        "retainedId": 9876,
        "retryResult": True,
    }


def test_collection_submission_blocks_close_and_reopen(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          let release;
          window.safeFetch = function(url) {
            if (url === '/api/collections/321/add-photos') {
              return new Promise(resolve => { release = resolve; });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          pipelineReviewCollectionPhotoIds = [ids[0]];
          pipelineReviewSelectedCollectionId = 321;
          pipelineReviewCollections = [{id: 321, name: 'Existing Picks'}];
          document.getElementById('pipelineCollectionModal').classList.add('open');
          try {
            const submission = confirmPipelineCollection();
            const closeResult = hidePipelineCollectionModal();
            const reopenResult = await addToCollection([ids[1]]);
            const pending = {
              closeResult,
              reopenResult,
              open: document.getElementById('pipelineCollectionModal').classList.contains('open'),
              ids: pipelineReviewCollectionPhotoIds.slice(),
              cancelDisabled: document.getElementById('pipelineCollectionCancelBtn').disabled,
            };
            release({ok: true});
            const submitResult = await submission;
            return {
              pending,
              submitResult,
              openAfter: document.getElementById('pipelineCollectionModal').classList.contains('open'),
            };
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {
        "pending": {
            "closeResult": False,
            "reopenResult": False,
            "open": True,
            "ids": [photo_ids[0]],
            "cancelDisabled": True,
        },
        "submitResult": True,
        "openAfter": False,
    }


def test_keyword_submission_blocks_close_and_reopen(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          let release;
          window.safeFetch = function(url) {
            if (url === '/api/batch/keyword') {
              return new Promise(resolve => { release = resolve; });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          pipelineReviewKeywordPhotoIds = [ids[0]];
          document.getElementById('pipelineKeywordInput').value = 'First Keyword';
          document.getElementById('pipelineKeywordModal').classList.add('open');
          try {
            const submission = confirmPipelineKeyword();
            const closeResult = hidePipelineKeywordModal();
            const reopenResult = batchAddKeyword([ids[1]]);
            const pending = {
              closeResult,
              reopenResult,
              open: document.getElementById('pipelineKeywordModal').classList.contains('open'),
              ids: pipelineReviewKeywordPhotoIds.slice(),
              cancelDisabled: document.getElementById('pipelineKeywordCancelBtn').disabled,
            };
            release({ok: true});
            const submitResult = await submission;
            return {
              pending,
              submitResult,
              openAfter: document.getElementById('pipelineKeywordModal').classList.contains('open'),
            };
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {
        "pending": {
            "closeResult": False,
            "reopenResult": False,
            "open": True,
            "ids": [photo_ids[0]],
            "cancelDisabled": True,
        },
        "submitResult": True,
        "openAfter": False,
    }


def test_collection_and_keyword_dialogs_have_separate_selections(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    state = page.evaluate(
        """async ids => {
          const originalSafeFetch = window.safeFetch;
          let releaseCollectionAdd;
          let releaseKeyword;
          let addCallIds = null;
          let keywordCallIds = null;
          window.safeFetch = function(url, options) {
            if (url === '/api/collections') {
              return Promise.resolve([{id: 555, name: 'Reviewer Picks',
                rules: [{field: 'photo_ids', value: []}]}]);
            }
            if (url === '/api/collections/555/add-photos') {
              addCallIds = JSON.parse(options.body).photo_ids.slice();
              return new Promise(resolve => { releaseCollectionAdd = resolve; });
            }
            if (url === '/api/batch/keyword') {
              keywordCallIds = JSON.parse(options.body).photo_ids.slice();
              return new Promise(resolve => { releaseKeyword = resolve; });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            // Open Add to Collection for the first selection and pick a
            // collection so the dialog is fully configured.
            const collectionOpen = await addToCollection([ids[0], ids[1]]);
            pickPipelineCollection(555);
            const afterCollectionOpen = {
              collectionIds: pipelineReviewCollectionPhotoIds.slice(),
              keywordIds: pipelineReviewKeywordPhotoIds.slice(),
            };

            // Open Add Keyword for a different selection while the collection
            // dialog is still open. This must not overwrite the collection
            // dialog's captured photo IDs.
            batchAddKeyword([ids[2], ids[3]]);
            const afterKeywordOpen = {
              collectionIds: pipelineReviewCollectionPhotoIds.slice(),
              keywordIds: pipelineReviewKeywordPhotoIds.slice(),
              collectionOpen:
                document.getElementById('pipelineCollectionModal').classList.contains('open'),
              keywordOpen:
                document.getElementById('pipelineKeywordModal').classList.contains('open'),
            };

            // Closing the keyword dialog must not clear the collection dialog's
            // captured IDs.
            hidePipelineKeywordModal();
            const afterKeywordClose = {
              collectionIds: pipelineReviewCollectionPhotoIds.slice(),
              keywordIds: pipelineReviewKeywordPhotoIds.slice(),
              collectionOpen:
                document.getElementById('pipelineCollectionModal').classList.contains('open'),
            };

            // Reopen the keyword dialog with yet another selection and confirm
            // both dialogs. Each request must carry its own captured IDs.
            batchAddKeyword([ids[3]]);
            document.getElementById('pipelineKeywordInput').value = 'Standalone';
            const submitKeyword = confirmPipelineKeyword();
            const submitCollection = confirmPipelineCollection();
            releaseCollectionAdd({ok: true});
            releaseKeyword({ok: true});
            const [collectionResult, keywordResult] =
              await Promise.all([submitCollection, submitKeyword]);
            return {
              collectionOpen,
              afterCollectionOpen,
              afterKeywordOpen,
              afterKeywordClose,
              addCallIds,
              keywordCallIds,
              collectionResult,
              keywordResult,
            };
          } finally {
            window.safeFetch = originalSafeFetch;
          }
        }""",
        photo_ids,
    )
    assert state == {
        "collectionOpen": True,
        "afterCollectionOpen": {
            "collectionIds": photo_ids[:2],
            "keywordIds": [],
        },
        "afterKeywordOpen": {
            "collectionIds": photo_ids[:2],
            "keywordIds": photo_ids[2:4],
            "collectionOpen": True,
            "keywordOpen": True,
        },
        "afterKeywordClose": {
            "collectionIds": photo_ids[:2],
            "keywordIds": [],
            "collectionOpen": True,
        },
        "addCallIds": photo_ids[:2],
        "keywordCallIds": [photo_ids[3]],
        "collectionResult": True,
        "keywordResult": True,
    }


def test_newest_organization_dialog_is_frontmost_and_owns_escape(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    assert page.evaluate("ids => addToCollection([ids[0]])", photo_ids) is True
    page.evaluate("ids => batchAddKeyword([ids[1]])", photo_ids)

    collection = page.locator("#pipelineCollectionModal")
    keyword = page.locator("#pipelineKeywordModal")
    expect(collection).to_have_class(re.compile(r"\bopen\b"))
    expect(keyword).to_have_class(re.compile(r"\bopen\b"))
    stack = page.evaluate(
        "() => ({"
        "active: pipelineReviewActiveOrganizationModal,"
        "collection: Number(document.getElementById('pipelineCollectionModal').style.zIndex),"
        "keyword: Number(document.getElementById('pipelineKeywordModal').style.zIndex),"
        "})"
    )
    assert stack == {"active": "keyword", "collection": 550, "keyword": 551}

    page.keyboard.press("Escape")
    expect(keyword).not_to_have_class(re.compile(r"\bopen\b"))
    expect(collection).to_have_class(re.compile(r"\bopen\b"))
    assert page.evaluate("pipelineReviewActiveOrganizationModal") == "collection"


def test_photo_context_menu_updates_rating_and_adds_keyword(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    card = page.locator(".photo-card[data-photo-id]").first
    photo_id = int(card.get_attribute("data-photo-id"))
    card.click(button="right")
    page.locator('.vireo-ctx-chip[title="Rate 4"]').click()

    rating = db.conn.execute(
        "SELECT rating FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()["rating"]
    assert rating == 4

    card.click(button="right")
    page.locator(".vireo-ctx-item", has_text="Add Keyword").click()
    modal = page.locator("#pipelineKeywordModal")
    expect(modal).to_have_class(re.compile(r"\bopen\b"))
    modal.locator("#pipelineKeywordInput").fill("Process Review Shortlist")
    modal.locator(".modal-btn-primary", has_text="Add").click()
    expect(modal).not_to_have_class(re.compile(r"\bopen\b"))

    assert "Process Review Shortlist" in {
        keyword["name"] for keyword in db.get_photo_keywords(photo_id)
    }


def test_group_context_menu_preserves_selection_for_collection_action(
    live_server, page
):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)
    collection_id = db.add_collection(
        "Burst Shortlist",
        json.dumps([{"field": "photo_ids", "value": []}]),
    )

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    cards = page.locator("#grmOverlay .grm-card[data-photo-id]")
    expect(cards).to_have_count(2)
    selected_ids = [
        int(cards.nth(0).get_attribute("data-photo-id")),
        int(cards.nth(1).get_attribute("data-photo-id")),
    ]
    # The modal opens with the first card selected. Add the second without
    # toggling the first one off through grmSelect's click-again behavior.
    page.evaluate("id => grmSelect(id, 'toggle')", selected_ids[1])
    cards.nth(1).click(button="right")

    menu = page.locator(".vireo-ctx-menu")
    for label in (
        "Move to Picks",
        "Move to Candidates",
        "Move to Rejects",
        "Remove from Group",
    ):
        expect(menu.locator(".vireo-ctx-item", has_text=label)).to_be_visible()
    menu.locator(".vireo-ctx-item", has_text="Add to Collection").click()

    modal = page.locator("#pipelineCollectionModal")
    expect(modal.locator("#pipelineCollectionTitle")).to_have_text(
        "Add 2 photos to Collection"
    )
    modal.locator(".pipeline-collection-choice", has_text="Burst Shortlist").click()
    modal.locator(".modal-btn-primary", has_text="Add").click()
    expect(modal).not_to_have_class(re.compile(r"\bopen\b"))

    members = db.get_collection_photos(collection_id, per_page=100)
    assert {member["id"] for member in members} == set(selected_ids)


def test_lightbox_over_group_review_suppresses_group_shortcuts(live_server, page):
    """Space/Delete/Backspace pressed inside the shared lightbox must not
    reach the Group Review keydown handler on the hidden overlay beneath."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    expect(page.locator("#grmOverlay")).to_have_class(re.compile(r"\bopen\b"))
    burst_ids = photo_ids[:2]
    expect(page.locator("#grmOverlay .grm-card[data-photo-id]")).to_have_count(2)

    # Open the lightbox above the group review on the first burst photo. The
    # burst overlay stays open beneath; grmState.selected still names the
    # first photo, so a stray Space or Delete would silently move it or drop
    # it from the group.
    page.evaluate(
        "pid => openPipelineLightbox(pid)",
        burst_ids[0],
    )
    expect(page.locator("#lightboxOverlay")).to_have_class(
        re.compile(r"\bactive\b")
    )

    page.locator("#lightboxImg").press("Space")
    page.locator("#lightboxImg").press("Delete")
    page.locator("#lightboxImg").press("Backspace")

    # Group Review's pending zones and touched set must be untouched.
    zones = page.evaluate(
        "() => ({"
        "picks: Array.from(grmState.picks),"
        "rejects: Array.from(grmState.rejects),"
        "removed: Array.from(grmState.removed),"
        "touched: Array.from(grmState.touched || []),"
        "})"
    )
    assert zones == {"picks": [], "rejects": [], "removed": [], "touched": []}


def test_lightbox_browse_uses_state_preserving_navigation_helper(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    page.wait_for_function("grmState && grmState.seeded === true")
    photo_id = photo_ids[0]
    page.evaluate(
        """pid => {
          grmMoveReject();
          window.__lightboxBrowsePhotoId = null;
          window.openInBrowse = id => { window.__lightboxBrowsePhotoId = id; };
          openPipelineLightbox(pid);
        }""",
        photo_id,
    )

    page.locator("#lightboxImg").click(button="right")
    page.locator(".vireo-ctx-item", has_text="Open in Browse").click()
    assert page.evaluate("window.__lightboxBrowsePhotoId") == photo_id
    expect(page.locator("#grmOverlay")).to_have_class(re.compile(r"\bopen\b"))
    assert page.evaluate("grmState.rejects.has(grmState.selected)") is True


def test_similar_photos_overlay_suppresses_group_shortcuts(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    page.wait_for_function("grmState && grmState.seeded === true")
    page.evaluate("findSimilar(grmState.selected)")
    expect(page.locator("#similarOverlay")).to_have_class(re.compile(r"\bactive\b"))

    page.keyboard.press("p")
    page.keyboard.press("x")
    page.keyboard.press("Space")
    page.keyboard.press("Delete")
    page.keyboard.press("Backspace")
    zones = page.evaluate(
        "() => ({"
        "picks: Array.from(grmState.picks),"
        "rejects: Array.from(grmState.rejects),"
        "removed: Array.from(grmState.removed),"
        "touched: Array.from(grmState.touched || []),"
        "})"
    )
    assert zones == {"picks": [], "rejects": [], "removed": [], "touched": []}


def test_lightbox_flag_over_group_review_updates_pending_zones(live_server, page):
    """A flag set from the lightbox above Group Review must route through the
    burst's pending zones, not write the database directly — otherwise the
    burst's Apply overwrites the newer flag with its stale zone snapshot."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    expect(page.locator("#grmOverlay")).to_have_class(re.compile(r"\bopen\b"))
    burst_ids = photo_ids[:2]

    page.evaluate(
        "pid => openPipelineLightbox(pid)",
        burst_ids[0],
    )
    expect(page.locator("#lightboxOverlay")).to_have_class(
        re.compile(r"\bactive\b")
    )

    # Simulate the lightbox flag action for the visible burst photo. The write
    # must land in the burst's pending zones — not the database — so a later
    # Apply from Group Review reflects it instead of clobbering it.
    landed = page.evaluate(
        "pid => window.setFlagFor(pid, 'rejected')",
        burst_ids[0],
    )
    assert landed is not False

    zones = page.evaluate(
        "() => ({"
        "picks: Array.from(grmState.picks),"
        "rejects: Array.from(grmState.rejects),"
        "touched: Array.from(grmState.touched || []),"
        "})"
    )
    assert zones["rejects"] == [burst_ids[0]]
    assert zones["picks"] == []
    assert burst_ids[0] in zones["touched"]
    assert _flags(db, photo_ids) == ["none"] * 4


def test_lightbox_provisional_group_flag_does_not_poison_confirmed_cache(
    live_server, page
):
    """A lightbox flag staged in Group Review must remain visibly pending
    without mutating pipelineResults or the lightbox's confirmed flag cache."""
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    page.wait_for_function("grmState && grmState.seeded === true")
    photo_id = photo_ids[0]
    page.evaluate("pid => openPipelineLightbox(pid)", photo_id)
    expect(page.locator("#lightboxOverlay")).to_have_class(re.compile(r"\bactive\b"))

    page.evaluate("pid => _lbApplyFlag(pid, 'rejected')", photo_id)
    page.wait_for_function("_lbFlagPendingWrites === 0")

    state = page.evaluate(
        "pid => ({"
        "confirmed: _lbConfirmedFlagFor(pid),"
        "cached: pipelineResults.photos.find(p => p.id === pid).flag,"
        "rejected: grmState.rejects.has(pid),"
        "status: document.getElementById('lightboxFlagStatus').textContent,"
        "})",
        photo_id,
    )
    assert state == {
        "confirmed": "none",
        "cached": "none",
        "rejected": True,
        "status": "Rejected",
    }
    assert _flags(db, photo_ids) == ["none"] * 4

    # A later group shortcut supersedes the lightbox choice. Reopening the
    # lightbox must show the current pending zone, not the earlier cached one.
    page.evaluate("closeLightbox(); grmMovePick()")
    page.evaluate("pid => openPipelineLightbox(pid)", photo_id)
    expect(page.locator("#lightboxFlagStatus")).to_have_text("Flagged")
    page.evaluate("closeLightbox(); grmMoveCandidate()")
    page.evaluate("pid => openPipelineLightbox(pid)", photo_id)
    expect(page.locator("#lightboxFlagStatus")).to_have_text("No flag")

    # Discard the group without Apply. The main grid must still reflect the
    # confirmed database/cache value rather than the abandoned pending zone.
    page.evaluate("closeLightbox(); closeGroupReview(); renderResults()")
    expect(
        page.locator(f'.photo-card[data-photo-id="{photo_id}"] .flag-rejected')
    ).to_have_count(0)
    assert _flags(db, photo_ids) == ["none"] * 4


def test_older_persisted_flag_does_not_clear_newer_group_provisional_flag(
    live_server, page
):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    photo_id = photo_ids[0]
    page.evaluate(
        """pid => {
          window.__originalSetFlagFor = window.setFlagFor;
          window.setFlagFor = function() {
            return new Promise(resolve => { window.__resolvePersistedFlag = resolve; });
          };
          openPipelineLightbox(pid);
          _lbApplyFlag(pid, 'flagged');
          closeLightbox();
        }""",
        photo_id,
    )
    page.evaluate("openGroupReview(0, 0)")
    page.wait_for_function("grmState && grmState.seeded === true")
    page.evaluate("grmMoveReject()")
    assert page.evaluate(
        "pid => _lbProvisionalFlags[String(pid)]", photo_id
    ) == "rejected"

    try:
        page.evaluate("window.__resolvePersistedFlag(true)")
        page.wait_for_function("_lbFlagPendingWrites === 0")
        state = page.evaluate(
            "pid => ({"
            "confirmed: _lbConfirmedFlagFor(pid),"
            "provisional: _lbProvisionalFlags[String(pid)],"
            "})",
            photo_id,
        )
        assert state == {
            "confirmed": "flagged",
            "provisional": "rejected",
        }
        page.evaluate("pid => openPipelineLightbox(pid)", photo_id)
        expect(page.locator("#lightboxFlagStatus")).to_have_text("Rejected")
    finally:
        page.evaluate("window.setFlagFor = window.__originalSetFlagFor")


def test_read_only_scope_lightbox_flag_does_not_mutate_cache(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    photo_id = photo_ids[0]
    page.evaluate("reviewScopeMode = 'workspace'")
    page.evaluate("pid => openPipelineLightbox(pid)", photo_id)
    page.evaluate("pid => _lbApplyFlag(pid, 'rejected')", photo_id)
    page.wait_for_function("_lbFlagPendingWrites === 0")

    state = page.evaluate(
        "pid => ({"
        "confirmed: _lbConfirmedFlagFor(pid),"
        "cached: pipelineResults.photos.find(p => p.id === pid).flag,"
        "status: document.getElementById('lightboxFlagStatus').textContent,"
        "})",
        photo_id,
    )
    assert state == {"confirmed": "none", "cached": "none", "status": "No flag"}
    assert _flags(db, photo_ids) == ["none"] * 4


def test_read_only_scope_rating_does_not_mutate_cache_or_database(live_server, page):
    db = live_server["db"]
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    photo_id = photo_ids[0]
    before_cache = page.evaluate(
        "pid => pipelineResults.photos.find(photo => photo.id === pid).rating",
        photo_id,
    )
    before_db = db.conn.execute(
        "SELECT rating FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()["rating"]
    attempted_rating = next(
        value for value in range(6) if value not in {before_cache, before_db}
    )
    result = page.evaluate(
        "args => { reviewScopeMode = 'workspace'; "
        "return setRatingFor(args.photoId, args.rating); }",
        {"photoId": photo_id, "rating": attempted_rating},
    )
    assert result is False
    assert page.evaluate(
        "pid => pipelineResults.photos.find(photo => photo.id === pid).rating",
        photo_id,
    ) == before_cache
    rating = db.conn.execute(
        "SELECT rating FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()["rating"]
    assert rating == before_db


def test_tauri_disables_navigation_when_group_review_is_dirty(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    page.evaluate("openGroupReview(0, 0)")
    page.wait_for_function("grmState && grmState.seeded === true")
    page.evaluate("window.__TAURI_INTERNALS__ = {}; grmMoveReject()")

    card = page.locator("#grmOverlay .grm-card[data-photo-id]").first
    card.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    for label in ("Open in Browse", "Edit Photo"):
        item = menu.locator(".vireo-ctx-item", has_text=label)
        expect(item).to_have_class(re.compile(r"\bvireo-ctx-disabled\b"))
        expect(item).to_have_attribute(
            "title", "Apply or close Group Review before leaving this page"
        )

    page.evaluate("pid => openPipelineLightbox(pid)", photo_ids[0])
    page.locator("#lightboxImg").click(button="right")
    lightbox_browse = page.locator(".vireo-ctx-item", has_text="Open in Browse")
    expect(lightbox_browse).to_have_class(re.compile(r"\bvireo-ctx-disabled\b"))
    expect(lightbox_browse).to_have_attribute(
        "title", "Apply or close Group Review before leaving this page"
    )


def test_tauri_treats_unseeded_group_touches_as_dirty(live_server, page):
    photo_ids = live_server["data"]["photos"][:4]
    _write_grouped_pipeline_cache(live_server, photo_ids)

    page.goto(f"{live_server['url']}/pipeline/review")
    expect(page.locator(".photo-card[data-photo-id]")).to_have_count(4)
    # Model the loading/error window before the live DB snapshot has seeded.
    page.evaluate(
        "window.__TAURI_INTERNALS__ = {}; openGroupReview(0, 0); "
        "grmState.seeded = false; grmMoveReject()"
    )
    assert page.evaluate("grmState.touched.size") == 1

    card = page.locator("#grmOverlay .grm-card[data-photo-id]").first
    card.click(button="right")
    menu = page.locator(".vireo-ctx-menu")
    for label in ("Open in Browse", "Edit Photo"):
        expect(menu.locator(".vireo-ctx-item", has_text=label)).to_have_class(
            re.compile(r"\bvireo-ctx-disabled\b")
        )
