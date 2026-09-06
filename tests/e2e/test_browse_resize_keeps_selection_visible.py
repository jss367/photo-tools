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


def test_browse_resize_keeps_the_most_recently_clicked_card_visible(live_server, page):
    # A cmd-click extends the selection but leaves selectedPhotoId on the
    # earlier detail focus. The card the user just clicked is the one they are
    # looking at, so that is the one the resize has to keep on screen.
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(live_server["url"] + "/browse")

    cards = page.locator(".grid-card")
    cards.first.wait_for(state="visible")
    cards.first.click()
    cards.last.click(modifiers=["ControlOrMeta"])
    last_id = page.evaluate("lastClickedPhotoId")
    assert page.evaluate("selectedPhotoId") != last_id

    wide = page.evaluate("() => gridContainer.clientWidth")
    page.set_viewport_size({"width": 780, "height": 900})
    page.wait_for_function("width => gridContainer.clientWidth < width", arg=wide)
    page.evaluate(SETTLE)

    visibility = page.evaluate(
        """id => {
          const card = document.querySelector(`.grid-card[data-id="${id}"]`);
          const view = document.getElementById('gridContainer').getBoundingClientRect();
          const rect = card.getBoundingClientRect();
          return {fullyVisible: rect.top >= view.top - 1 && rect.bottom <= view.bottom + 1,
                  card: {top: rect.top, bottom: rect.bottom},
                  view: {top: view.top, bottom: view.bottom}};
        }""",
        last_id,
    )
    assert visibility["fullyVisible"] is True, (
        f"the cmd-clicked photo left the grid viewport after the resize: {visibility}"
    )


def test_browse_resize_leaves_a_deliberately_scrolled_away_selection_alone(live_server, page):
    # Selecting a photo and then scrolling somewhere else is a deliberate move.
    # A later resize must not haul the grid back to that selection — the reflow
    # is not what put the card off screen.
    page.set_viewport_size({"width": 780, "height": 900})
    page.goto(live_server["url"] + "/browse")

    cards = page.locator(".grid-card")
    cards.first.wait_for(state="visible")
    cards.first.click()

    page.evaluate("() => { gridContainer.scrollTop = gridContainer.scrollHeight; }")
    page.evaluate(SETTLE)
    scrolled_away = page.evaluate("() => gridContainer.scrollTop")
    assert scrolled_away > 0
    assert page.evaluate(CARD_VISIBILITY)["fullyVisible"] is False

    tall = page.evaluate("() => gridContainer.clientHeight")
    page.set_viewport_size({"width": 780, "height": 700})
    page.wait_for_function("h => gridContainer.clientHeight < h", arg=tall)
    page.evaluate(SETTLE)

    after = page.evaluate("() => gridContainer.scrollTop")
    assert after == scrolled_away, (
        f"the resize scrolled back to the selection ({scrolled_away} → {after})"
    )
