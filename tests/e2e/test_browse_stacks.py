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

    hidden_member = tray.locator(
        f'.browse-stack-member[data-id="{burst_ids[2]}"]'
    )
    hidden_member.click()
    expect(hidden_member).to_have_class("browse-stack-member selected")
    expect(page.locator("#batchCount")).to_have_text("1 selected")
    expect(page.locator("#detailFilename")).to_have_text("hawk3.jpg")

    tray.get_by_role("button", name="Select all").click()
    expect(page.locator("#batchCount")).to_have_text("3 selected")
    for photo_id in burst_ids:
        expect(
            tray.locator(f'.browse-stack-member[data-id="{photo_id}"]')
        ).to_have_class("browse-stack-member selected")

    page.locator("#batchBar button", has_text="★5").click()
    page.wait_for_function(
        """ids => ids.every(function(id) {
          var photo = findBrowsePhoto(id);
          return photo && photo.rating === 5;
        })""",
        arg=burst_ids,
    )
    page.locator("#batchBar button", has_text="Flag").click()
    page.wait_for_function(
        """ids => ids.every(function(id) {
          var photo = findBrowsePhoto(id);
          return photo && photo.flag === 'flagged';
        })""",
        arg=burst_ids,
    )

    # Stacks is a presentation preference, so it survives a return to Browse.
    page.reload()
    expect(page.locator("#browseStacksToggle")).to_be_checked()
    expect(page.locator("#grid > .grid-card")).to_have_count(3)
