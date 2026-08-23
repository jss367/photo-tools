from playwright.sync_api import expect


def test_browse_stacks_collapse_expand_and_select(live_server, page):
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'processed-hawk-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )
    db.save_detections(burst_ids[2], [{
        "box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "confidence": 0.91,
        "category": "animal",
    }], detector_model="test-detector")

    page.goto(f"{live_server['url']}/browse")
    cards = page.locator("#grid > .grid-card")
    expect(cards).to_have_count(5)

    page.locator("#browseStacksToggle").check()
    expect(cards).to_have_count(3)
    expect(page.locator("#filterSummary")).to_contain_text("3 items · 5 photos")
    assert page.evaluate(
        """() => browseStackCoverCompare(
          {id: 1, rating: 0, width: 1, height: 1},
          {id: 2, rating: null, width: 999, height: 999}
        ) < 0"""
    )

    stack_card = page.locator(
        f'.grid-card[data-id="{burst_ids[1]}"]'
    )
    expect(stack_card).to_be_visible()
    badge = stack_card.locator(".browse-stack-badge")
    expect(badge).to_have_text("▦3")
    badge.click()

    tray = page.locator(
        f'.browse-stack-tray[data-stack-cover-id="{burst_ids[1]}"]'
    )
    expect(tray).to_be_visible()
    expect(tray.locator(".browse-stack-member")).to_have_count(3)
    expect(tray).to_contain_text("Burst")
    expect(tray).to_contain_text("hawk1.jpg")
    expect(tray).to_contain_text("hawk2.jpg")
    expect(tray).to_contain_text("hawk3.jpg")
    page.locator("#detBoxToggle").click()
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"] .det-box'
    )).to_be_visible()
    page.evaluate(
        """photoId => {
          window._testOriginalOrientationCheck = window.vireoPhotoHasOrientationEdit;
          window.vireoPhotoHasOrientationEdit = function(id) { return id === photoId; };
          document.dispatchEvent(new CustomEvent('lightbox:renderchanged', {
            detail: {photoIds: [photoId]},
          }));
        }""",
        burst_ids[2],
    )
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"] .det-box'
    )).to_be_hidden()
    page.evaluate(
        """photoId => {
          window.vireoPhotoHasOrientationEdit = window._testOriginalOrientationCheck;
          delete window._testOriginalOrientationCheck;
          document.dispatchEvent(new CustomEvent('lightbox:renderchanged', {
            detail: {photoIds: [photoId]},
          }));
        }""",
        burst_ids[2],
    )
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"] .det-box'
    )).to_be_visible()
    page.locator("#detBoxToggle").click()
    expect(tray.locator(".det-box")).to_have_count(0)
    assert page.evaluate(
        """coverId => {
          var cover = photos.find(function(photo) { return photo.id === coverId; });
          var member = browseStackMembers[String(coverId)].find(function(photo) {
            return photo.id === coverId;
          });
          return cover === member;
        }""",
        burst_ids[1],
    )

    badge.click()
    expect(tray).to_be_hidden()
    assert page.evaluate(
        "coverId => browsePhotoNavigationList(coverId) === photos",
        burst_ids[1],
    )
    badge.click()
    expect(tray).to_be_visible()

    cover_member = tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[1]}"]'
    )
    cover_member.dblclick()
    expect(page.locator("#lightboxFilename")).to_have_text("hawk2.jpg")
    page.locator("[title='Next (→)']").click()
    expect(page.locator("#lightboxFilename")).to_have_text("hawk3.jpg")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "photoId => selectedPhotoId === photoId",
        arg=burst_ids[2],
    )
    expect(page.locator("#detailFilename")).to_have_text("hawk3.jpg")

    hidden_member = tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"]'
    )
    hidden_member.click()
    expect(hidden_member).to_have_class("browse-stack-member selected")
    expect(page.locator("#batchCount")).to_have_text("1 selected")
    expect(page.locator("#detailFilename")).to_have_text("hawk3.jpg")

    other_hidden_member = tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[0]}"]'
    )
    other_hidden_member.click(button="right")
    page.wait_for_function(
        "photoId => selectedPhotoId === photoId",
        arg=burst_ids[0],
    )
    assert page.evaluate(
        "coverId => selectedIndex === photos.findIndex(function(photo) { return photo.id === coverId; })",
        burst_ids[1],
    )
    page.evaluate("() => closeContextMenu()")
    hidden_member.click()
    expect(page.locator("#detailFilename")).to_have_text("hawk3.jpg")

    page.evaluate(
        """coverId => {
          document.querySelector(
            '.browse-stack-tray[data-stack-cover-id="' + coverId + '"]'
          ).dataset.beforeColorRefresh = '1';
        }""",
        burst_ids[1],
    )
    page.evaluate("() => setColorLabel('red')")
    page.wait_for_function(
        "photoId => colorLabels[photoId] === 'red'",
        arg=burst_ids[2],
    )
    expect(tray).not_to_have_attribute("data-before-color-refresh", "1")

    page.locator("#batchBar button", has_text="Export").click()
    expect(page.locator("#exportOverlay")).to_have_class("modal-overlay open")
    expect(page.locator("#exportPreview")).to_contain_text("hawk3")
    page.locator("#exportOverlay button", has_text="Cancel").click()

    other_card_id = live_server["data"]["photos"][3]
    page.locator(f'.grid-card[data-id="{other_card_id}"]').click(
        modifiers=["Meta"]
    )
    tray.get_by_role("button", name="Collapse stack").click()
    multi_collapse_state = page.evaluate(
        """() => {
          var preview = getBrowseShortcutPhoto();
          return {
            selectedPhotoId: selectedPhotoId,
            selectedIds: Array.from(selectedPhotos),
            previewId: preview && preview.photo.id,
            topLevelNavigation: !!preview && preview.navigationPhotos === photos,
          };
        }"""
    )
    assert multi_collapse_state == {
        "selectedPhotoId": burst_ids[2],
        "selectedIds": [burst_ids[2], other_card_id],
        "previewId": burst_ids[1],
        "topLevelNavigation": True,
    }
    badge.click()
    expect(tray).to_be_visible()
    hidden_member.click()

    tray.get_by_role("button", name="Collapse stack").click()
    expect(tray).to_be_hidden()
    page.wait_for_function(
        "coverId => selectedPhotoId === coverId",
        arg=burst_ids[1],
    )
    expect(page.locator("#detailFilename")).to_have_text("hawk2.jpg")
    assert page.evaluate(
        "coverId => browsePhotoNavigationList(coverId) === photos",
        burst_ids[1],
    )
    badge.click()
    expect(tray).to_be_visible()

    tray.get_by_role("button", name="Select all").click()
    expect(page.locator("#batchCount")).to_have_text("3 selected")
    for photo_id in burst_ids:
        expect(
            tray.locator(f'.browse-stack-member[data-id="{photo_id}"]')
        ).to_have_class("browse-stack-member selected")

    tray.get_by_role("button", name="Collapse stack").click()
    expect(tray).to_be_hidden()
    assert page.evaluate(
        """coverId => {
          var preview = getBrowseShortcutPhoto();
          return selectedPhotos.size === 3 && selectedPhotoId === coverId
            && preview.photo.id === coverId && preview.navigationPhotos === photos;
        }""",
        burst_ids[1],
    )
    badge.click()
    expect(tray).to_be_visible()

    page.evaluate("() => setSelectionWildlifeExcluded(true)")
    page.wait_for_function(
        """ids => ids.every(function(id) {
          var photo = findBrowsePhoto(id);
          return photo && photo.wildlife_excluded;
        })""",
        arg=burst_ids,
    )
    expect(tray.locator(".no-wildlife-badge")).to_have_count(3)
    page.evaluate("() => setSelectionWildlifeExcluded(false)")
    expect(tray.locator(".no-wildlife-badge")).to_have_count(0)

    page.evaluate(
        """photoId => document.dispatchEvent(new CustomEvent('lifelist:changed', {
          detail: {species: 'Test species', photoId: photoId},
        }))""",
        burst_ids[0],
    )
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[0]}"] .representative-badge'
    )).to_be_visible()
    page.evaluate(
        """photoId => document.dispatchEvent(new CustomEvent('lifelist:changed', {
          detail: {species: 'Test species', photoId: photoId},
        }))""",
        burst_ids[2],
    )
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[0]}"] .representative-badge'
    )).to_have_count(0)
    expect(tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"] .representative-badge'
    )).to_be_visible()

    page.locator("#batchBar button", has_text="★5").click()
    page.wait_for_function(
        """ids => ids.every(function(id) {
          var photo = findBrowsePhoto(id);
          return photo && photo.rating === 5;
        })""",
        arg=burst_ids,
    )
    expect(page.locator("#ratingMixed")).to_be_hidden()
    page.locator("#batchBar button", has_text="Flag").click()
    page.wait_for_function(
        """ids => ids.every(function(id) {
          var photo = findBrowsePhoto(id);
          return photo && photo.flag === 'flagged';
        })""",
        arg=burst_ids,
    )
    expect(page.locator("#flagMixed")).to_be_hidden()

    page.evaluate(
        """ids => {
          photos.find(function(photo) { return photo.browse_stack; })._similarity = 0.95;
          bestBatchData = {
            best_photo_id: ids[0],
            suggested_reject_ids: ids.slice(1),
          };
          return applyBestBatchPickAndReject();
        }""",
        burst_ids,
    )
    page.wait_for_function(
        """ids => ids.every(function(id, index) {
          var photo = findBrowsePhoto(id);
          return photo && photo.flag === (index === 0 ? 'flagged' : 'rejected');
        })""",
        arg=burst_ids,
    )
    expect(page.locator("#flagMixed")).to_be_visible()
    new_cover = page.locator(f'.grid-card[data-id="{burst_ids[0]}"]')
    expect(new_cover).to_be_visible()
    expect(new_cover.locator(".browse-stack-badge")).to_have_text("▦3")
    expect(page.locator(
        f'.browse-stack-tray[data-stack-cover-id="{burst_ids[0]}"]'
    )).to_be_visible()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_have_count(0)
    assert page.evaluate(
        "coverId => photos.find(function(photo) { return photo.id === coverId; })._similarity",
        burst_ids[0],
    ) == 0.95

    # Stacks is a presentation preference, so it survives a return to Browse.
    page.reload()
    expect(page.locator("#browseStacksToggle")).to_be_checked()
    expect(page.locator("#grid > .grid-card")).to_have_count(3)

    # A collapsed stack has no hydrated member cache. Rejecting its current
    # cover still hydrates that one group and promotes the correct replacement.
    assert page.evaluate("() => Object.keys(browseStackMembers).length") == 0
    page.locator(f'.grid-card[data-id="{burst_ids[0]}"]').click()
    page.evaluate(
        "photoId => setFlagFor(photoId, 'rejected')",
        burst_ids[0],
    )
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[0]}"]')).to_have_count(0)
    page.wait_for_function(
        "photoId => selectedPhotoId === photoId",
        arg=burst_ids[1],
    )
    expect(page.locator("#detailFilename")).to_have_text("hawk2.jpg")

    # Restore the original cover, then demote it while it remains part of a
    # batch. Exact selected IDs stay unchanged while preview maps the now-hidden
    # focused member to the collapsed replacement cover.
    page.evaluate(
        "photoId => setFlagFor(photoId, 'flagged')",
        burst_ids[0],
    )
    expect(page.locator(f'.grid-card[data-id="{burst_ids[0]}"]')).to_be_visible()
    page.evaluate(
        """ids => {
          selectedPhotoId = ids[0];
          selectedIndex = photos.findIndex(function(photo) { return photo.id === ids[0]; });
          selectedPhotos = new Set([ids[0], ids[1]]);
        }""",
        [burst_ids[0], live_server["data"]["photos"][3]],
    )
    page.evaluate(
        "photoId => setFlagFor(photoId, 'rejected')",
        burst_ids[0],
    )
    assert page.evaluate(
        """ids => {
          var active = getActiveSelection();
          var preview = getBrowseShortcutPhoto();
          return selectedPhotoId === ids[0]
            && active.length === 2 && active.includes(ids[0]) && active.includes(ids[1])
            && preview.photo.id !== ids[0] && preview.navigationPhotos === photos;
        }""",
        [burst_ids[0], live_server["data"]["photos"][3]],
    )

    assert page.evaluate(
        """async coverId => {
          var originalSafeFetch = safeFetch;
          var memberLoads = 0;
          delete browseStackMembers[String(coverId)];
          browseStackErrors[String(coverId)] = 'Could not load this stack.';
          expandedBrowseStacks.delete(coverId);
          safeFetch = function(url) {
            if (url === '/api/photos/by-ids') memberLoads++;
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            await toggleBrowseStack(null, coverId);
          } finally {
            safeFetch = originalSafeFetch;
          }
          return memberLoads === 1
            && !!browseStackMembers[String(coverId)]
            && !browseStackErrors[String(coverId)];
        }""",
        burst_ids[1],
    )

    hydration_chunks = page.evaluate(
        """async coverId => {
          var cover = photos.find(function(photo) { return photo.id === coverId; });
          cover.browse_stack.photo_ids = Array(501).fill(coverId);
          delete browseStackMembers[String(coverId)];
          var originalSafeFetch = safeFetch;
          var chunks = [];
          safeFetch = function(url, options) {
            if (url === '/api/photos/by-ids') {
              chunks.push(JSON.parse(options.body).photo_ids.length);
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            await reconcileBrowseStackCovers([coverId]);
          } finally {
            safeFetch = originalSafeFetch;
          }
          return chunks;
        }""",
        burst_ids[1],
    )
    assert hydration_chunks == [500, 1]

    assert page.evaluate(
        """async args => {
          var coverId = args.coverId;
          var oldCover = photos.find(function(photo) { return photo.id === coverId; });
          var oldIndex = photos.indexOf(oldCover);
          var stack = oldCover.browse_stack;
          stack.photo_ids = args.memberIds;
          delete browseStackMembers[String(coverId)];
          expandedBrowseStacks.delete(coverId);
          var releaseExpand;
          var originalSafeFetch = safeFetch;
          safeFetch = function(url) {
            if (url === '/api/photos/by-ids') {
              return new Promise(function(resolve) { releaseExpand = resolve; });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          var pendingExpand = toggleBrowseStack(null, coverId);
          await new Promise(function(resolve) { setTimeout(resolve, 0); });
          oldCover.browse_stack = null;
          var replacement = Object.assign({}, oldCover, {
            id: coverId + 100000,
            browse_stack: stack,
          });
          photos[oldIndex] = replacement;
          releaseExpand({photos: [oldCover]});
          await pendingExpand;
          safeFetch = originalSafeFetch;
          return browseStackMembers[String(coverId)] === undefined;
        }""",
        {"coverId": burst_ids[1], "memberIds": burst_ids},
    )


def test_stack_metadata_callbacks_follow_promoted_cover(live_server, page):
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'metadata-race-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()

    callback_refreshes = page.evaluate(
        """async ids => {
          var oldCoverId = ids[1];
          var promotedId = ids[0];
          var originalLoadInatStatus = loadInatStatus;
          var originalFetchColorLabels = fetchColorLabels;
          var releaseInat;
          var releaseColors;
          loadInatStatus = function() {
            return new Promise(function(resolve) { releaseInat = resolve; });
          };
          fetchColorLabels = function() {
            return new Promise(function(resolve) { releaseColors = resolve; });
          };
          try {
            await toggleBrowseStack(null, oldCoverId);
            var members = browseStackMembers[String(oldCoverId)];
            members.find(function(photo) { return photo.id === oldCoverId; }).flag = 'rejected';
            members.find(function(photo) { return photo.id === promotedId; }).flag = 'flagged';
            await reconcileBrowseStackCovers([oldCoverId, promotedId]);

            var currentCoverId = browseStackCoverIdForPhoto(oldCoverId);
            var traySelector = '.browse-stack-tray[data-stack-cover-id="'
              + currentCoverId + '"]';
            var tray = document.querySelector(traySelector);
            tray.dataset.beforeInatRefresh = '1';
            releaseInat();
            await new Promise(function(resolve) { setTimeout(resolve, 0); });
            var inatRefreshed = !document.querySelector(traySelector)
              .hasAttribute('data-before-inat-refresh');

            document.querySelector(traySelector).dataset.beforeColorRefresh = '1';
            releaseColors();
            await new Promise(function(resolve) { setTimeout(resolve, 0); });
            var colorsRefreshed = !document.querySelector(traySelector)
              .hasAttribute('data-before-color-refresh');
            return {
              currentCoverId: currentCoverId,
              inatRefreshed: inatRefreshed,
              colorsRefreshed: colorsRefreshed,
            };
          } finally {
            loadInatStatus = originalLoadInatStatus;
            fetchColorLabels = originalFetchColorLabels;
          }
        }""",
        burst_ids,
    )
    assert callback_refreshes == {
        "currentCoverId": burst_ids[0],
        "inatRefreshed": True,
        "colorsRefreshed": True,
    }


def test_concurrent_stack_hydration_uses_newest_request(live_server, page):
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'concurrent-hydration-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()
    assert page.evaluate("() => Object.keys(browseStackMembers).length") == 0

    hydration_state = page.evaluate(
        """async ids => {
          var oldCoverId = ids[1];
          var promotedId = ids[0];
          var originalSafeFetch = safeFetch;
          var options = {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({photo_ids: ids}),
          };
          var baseline = await originalSafeFetch('/api/photos/by-ids', options);
          var releases = [];
          safeFetch = function(url) {
            if (url === '/api/photos/by-ids') {
              return new Promise(function(resolve) { releases.push(resolve); });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            var first = reconcileBrowseStackCovers([oldCoverId]);
            var second = reconcileBrowseStackCovers([oldCoverId]);
            while (releases.length < 2) {
              await new Promise(function(resolve) { setTimeout(resolve, 0); });
            }

            // The older response arrives first, but the second request was
            // issued after a newer mutation and must remain authoritative.
            releases[0](JSON.parse(JSON.stringify(baseline)));
            await first;
            var keysAfterOlderResponse = Object.keys(browseStackMembers);

            var newerResponse = JSON.parse(JSON.stringify(baseline));
            newerResponse.photos.forEach(function(photo) {
              if (photo.id === oldCoverId) photo.flag = 'rejected';
              if (photo.id === promotedId) photo.flag = 'flagged';
            });
            releases[1](newerResponse);
            await second;
            return {
              keysAfterOlderResponse: keysAfterOlderResponse,
              cacheKeys: Object.keys(browseStackMembers).map(Number),
              currentCoverId: browseStackCoverIdForPhoto(oldCoverId),
              gridHasPromotedCover: photos.some(function(photo) {
                return photo.id === promotedId && !!photo.browse_stack;
              }),
            };
          } finally {
            safeFetch = originalSafeFetch;
          }
        }""",
        burst_ids,
    )
    assert hydration_state == {
        "keysAfterOlderResponse": [],
        "cacheKeys": [burst_ids[0]],
        "currentCoverId": burst_ids[0],
        "gridHasPromotedCover": True,
    }


def test_shift_range_from_stack_member_keeps_selection_honest(live_server, page):
    """A Shift-range anchored on an expanded member must not retarget actions.

    Clicking a hidden stack member anchors selectedIndex on the *cover's* grid
    slot. Shift-clicking another card then built a range that contained the
    cover but not the focused member, while the member's card kept rendering
    as selected — so the batch bar, Export and Delete acted on a photo the
    user could see was not the highlighted one.
    """
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    other_ids = live_server["data"]["photos"][3:]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'shift-range-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator("#grid > .grid-card")).to_have_count(3)

    stack_card = page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')
    stack_card.locator(".browse-stack-badge").click()
    tray = page.locator(
        f'.browse-stack-tray[data-stack-cover-id="{burst_ids[1]}"]'
    )
    expect(tray.locator(".browse-stack-member")).to_have_count(3)

    hidden_member = tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"]'
    )
    hidden_member.click()
    expect(hidden_member).to_have_class("browse-stack-member selected")

    page.locator(f'.grid-card[data-id="{other_ids[-1]}"]').click(
        modifiers=["Shift"]
    )

    selection_state = page.evaluate(
        """() => {
          function ids(nodes) {
            return Array.from(new Set(Array.from(nodes).map(function(el) {
              return parseInt(el.dataset.id, 10);
            }))).sort(function(a, b) { return a - b; });
          }
          return {
            active: getActiveSelection().slice().sort(function(a, b) { return a - b; }),
            highlighted: ids(document.querySelectorAll(
              '.grid-card.selected, .browse-stack-member.selected'
            )),
          };
        }"""
    )
    # What is highlighted is exactly what a batch action would target.
    assert selection_state["active"] == selection_state["highlighted"]
    # And the member the user actually clicked is still one of them.
    assert burst_ids[2] in selection_state["active"]
    expect(hidden_member).to_have_class("browse-stack-member selected")
    expect(page.locator("#batchCount")).to_have_text(
        str(len(selection_state["active"])) + " selected"
    )

    # The export modal snapshots the active selection, so the focused member
    # has to survive into the job the user actually confirms.
    page.locator("#batchBar button", has_text="Export").click()
    expect(page.locator("#exportOverlay")).to_have_class("modal-overlay open")
    export_ids = page.evaluate(
        "() => (_exportPhotoIds || getActiveSelection()).slice()"
    )
    assert sorted(export_ids) == selection_state["active"]
    page.locator("#exportOverlay button", has_text="Cancel").click()


def test_stack_edit_during_expansion_outlives_the_pending_response(live_server, page):
    """A stack-wide edit applied while the tray is loading must survive.

    The tray header offers "Select all" the moment it opens, so a user can
    apply Wildlife Exclude to every member before the expansion's
    ``/api/photos/by-ids`` response lands. Those hidden members are in neither
    ``photos`` nor ``browseStackMembers`` yet, so ``findBrowsePhoto()`` cannot
    patch them — and installing the already-in-flight response then repainted
    pre-edit values in the tray and its "No Wildlife" badges, contradicting an
    edit the user had just confirmed, until a full reload.
    """
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'stale-expansion-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()

    state = page.evaluate(
        """async ids => {
          var coverId = ids[1];
          var originalSafeFetch = safeFetch;
          var byIdsOptions = {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({photo_ids: ids}),
          };
          // The payload the in-flight expansion would carry: pre-edit.
          var preEdit = await originalSafeFetch('/api/photos/by-ids', byIdsOptions);
          var release = null;
          safeFetch = function(url) {
            if (url === '/api/photos/by-ids' && !release) {
              return new Promise(function(resolve) { release = resolve; });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            var expansion = toggleBrowseStack(null, coverId);
            while (!release) {
              await new Promise(function(resolve) { setTimeout(resolve, 0); });
            }
            var loadingWhileSelectable = !!document.querySelector(
              '.browse-stack-tray[data-stack-cover-id="' + coverId + '"] .browse-stack-loading'
            );
            selectBrowseStackAll(null, coverId);
            var selected = getActiveSelection().slice().sort(function(a, b) { return a - b; });
            await setSelectionWildlifeExcluded(true);
            // Only now does the response fetched before the edit arrive.
            release(JSON.parse(JSON.stringify(preEdit)));
            await expansion;
            return {
              loadingWhileSelectable: loadingWhileSelectable,
              selected: selected,
              cached: (browseStackMembers[String(coverId)] || []).map(function(photo) {
                return [photo.id, photo.wildlife_excluded ? 1 : 0];
              }).sort(function(a, b) { return a[0] - b[0]; }),
              badges: document.querySelectorAll(
                '.browse-stack-tray[data-stack-cover-id="' + coverId + '"] .no-wildlife-badge'
              ).length,
              error: browseStackErrors[String(coverId)] || null,
            };
          } finally {
            safeFetch = originalSafeFetch;
          }
        }""",
        burst_ids,
    )
    # The edit was reachable while the members were still loading.
    assert state["loadingWhileSelectable"] is True
    assert state["selected"] == sorted(burst_ids)
    assert state["error"] is None
    # Every member — cover and hidden alike — reflects the edit the user made.
    assert state["cached"] == [[pid, 1] for pid in sorted(burst_ids)]
    assert state["badges"] == 3


def test_stack_expansion_response_yields_to_fresher_hydration(live_server, page):
    """An in-flight expansion must not overwrite a post-edit hydration.

    ``reconcileBrowseStackCovers()`` hydrates an uncached stack *after* the
    edit that triggered it, so its cache is strictly fresher than an expansion
    request issued before that edit. The expansion's unconditional write used
    to clobber it and put the pre-edit ratings back in the tray.
    """
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'reconcile-clobber-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()

    state = page.evaluate(
        """async ids => {
          var coverId = ids[1];
          var originalSafeFetch = safeFetch;
          var byIdsOptions = {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({photo_ids: ids}),
          };
          var preEdit = await originalSafeFetch('/api/photos/by-ids', byIdsOptions);
          var held = [];
          safeFetch = function(url) {
            if (url === '/api/photos/by-ids') {
              return new Promise(function(resolve) { held.push(resolve); });
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            var expansion = toggleBrowseStack(null, coverId);
            while (held.length < 1) {
              await new Promise(function(resolve) { setTimeout(resolve, 0); });
            }
            selectBrowseStackAll(null, coverId);
            var rated = batchSetRating(4);
            // batchSetRating -> reconcileBrowseStackCovers issues the second
            // by-ids request, this one after the rating committed.
            while (held.length < 2) {
              await new Promise(function(resolve) { setTimeout(resolve, 0); });
            }
            held[1](await originalSafeFetch('/api/photos/by-ids', byIdsOptions));
            await rated;
            // The pre-edit expansion response finally arrives last.
            held[0](JSON.parse(JSON.stringify(preEdit)));
            await expansion;
            return {
              cached: (browseStackMembers[String(coverId)] || []).map(function(photo) {
                return [photo.id, photo.rating];
              }).sort(function(a, b) { return a[0] - b[0]; }),
              error: browseStackErrors[String(coverId)] || null,
            };
          } finally {
            safeFetch = originalSafeFetch;
          }
        }""",
        burst_ids,
    )
    assert state["error"] is None
    assert state["cached"] == [[pid, 4] for pid in sorted(burst_ids)]


def test_expanding_stack_over_500_chunks_by_ids_requests(live_server, page):
    """A stack with more than 500 members must be expandable.

    ``/api/photos/by-ids`` caps each POST at 500 ids, so the expansion path
    used to bail with a permanent ``This stack is too large to expand in
    Browse.`` error on the badge for anything over that. Cover reconciliation
    already chunks in 500-id slices; the expansion path must do the same so
    the tray actually shows every member instead of leaving the stack
    permanently unexpandable.
    """
    db = live_server["db"]
    burst_ids = live_server["data"]["photos"][:3]
    with db.conn:
        db.conn.execute(
            "UPDATE photos SET burst_id = 'over-500-expansion-burst' "
            "WHERE id IN (?, ?, ?)",
            burst_ids,
        )
        db.conn.execute(
            "UPDATE photos SET quality_score = 0.99 WHERE id = ?",
            (burst_ids[1],),
        )

    page.goto(f"{live_server['url']}/browse")
    page.locator("#browseStacksToggle").check()
    expect(page.locator(f'.grid-card[data-id="{burst_ids[1]}"]')).to_be_visible()

    outcome = page.evaluate(
        """async coverId => {
          var cover = photos.find(function(photo) { return photo.id === coverId; });
          // Simulate a stack whose photo_ids list exceeds the /api/photos/by-ids
          // 500-id cap. The server-side ordering is irrelevant to this test;
          // what matters is that the client stops surfacing a permanent
          // "too large" error and chunks its fetches instead.
          cover.browse_stack.photo_ids = Array(501).fill(coverId);
          delete browseStackMembers[String(coverId)];
          delete browseStackErrors[String(coverId)];
          expandedBrowseStacks.delete(coverId);
          var originalSafeFetch = safeFetch;
          var chunks = [];
          safeFetch = function(url, options) {
            if (url === '/api/photos/by-ids') {
              chunks.push(JSON.parse(options.body).photo_ids.length);
              return Promise.resolve({photos: [cover]});
            }
            return originalSafeFetch.apply(this, arguments);
          };
          try {
            await toggleBrowseStack(null, coverId);
          } finally {
            safeFetch = originalSafeFetch;
          }
          return {
            chunks: chunks,
            error: browseStackErrors[String(coverId)] || null,
            cached: !!browseStackMembers[String(coverId)],
            expanded: expandedBrowseStacks.has(coverId),
          };
        }""",
        burst_ids[1],
    )
    assert outcome["chunks"] == [500, 1]
    assert outcome["error"] is None
    assert outcome["cached"] is True
    assert outcome["expanded"] is True
