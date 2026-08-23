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

    page.goto(f"{live_server['url']}/browse")
    cards = page.locator("#grid > .grid-card")
    expect(cards).to_have_count(5)

    page.locator("#browseStacksToggle").check()
    expect(cards).to_have_count(3)
    expect(page.locator("#filterSummary")).to_contain_text("3 items · 5 photos")

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
