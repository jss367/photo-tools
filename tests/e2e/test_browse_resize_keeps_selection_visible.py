"""The photo the user clicked stays on screen when the window is resized."""

CARD_VISIBILITY = """
() => {
  const card = document.querySelector('.grid-card.selected');
  if (!card) return null;
  const view = document.getElementById('gridContainer').getBoundingClientRect();
  const rect = card.getBoundingClientRect();
  return {
    fullyVisible: rect.top >= view.top - 1 && rect.bottom <= view.bottom + 1,
    card: {top: rect.top, bottom: rect.bottom},
    view: {top: view.top, bottom: view.bottom},
  };
}
"""

# The reflow lands in the same frame as the resize, but Playwright's
# set_viewport_size returns before the page has rendered it. Wait for the
# narrower grid, then let a frame pass so the correction has been applied.
SETTLE = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def test_browse_keeps_clicked_photo_visible_when_window_resizes(live_server, page):
    # The grid is an `auto-fill` grid, so narrowing the window drops the column
    # count and reflows every card onto a different row. Without a correction
    # the photo the user clicked scrolls out from under them.
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(live_server["url"] + "/browse")

    cards = page.locator(".grid-card")
    cards.first.wait_for(state="visible")
    cards.last.click()
    assert page.evaluate("selectedPhotoId") is not None
    assert page.evaluate(CARD_VISIBILITY)["fullyVisible"] is True

    wide_columns = page.evaluate("() => gridContainer.clientWidth")
    page.set_viewport_size({"width": 780, "height": 900})
    page.wait_for_function(
        "width => gridContainer.clientWidth < width", arg=wide_columns
    )
    page.evaluate(SETTLE)

    visibility = page.evaluate(CARD_VISIBILITY)
    assert visibility["fullyVisible"] is True, (
        f"the clicked photo left the grid viewport after the resize: {visibility}"
    )
