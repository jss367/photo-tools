from playwright.sync_api import expect

PREVIEW_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='1600' height='1200' "
    "viewBox='0 0 1600 1200'><rect width='1600' height='1200' fill='green'/></svg>"
)


def _open_large_editor_preview(live_server, page):
    page.route(
        "**/photos/*/edit-preview**",
        lambda route: route.fulfill(
            status=200,
            content_type="image/svg+xml",
            body=PREVIEW_SVG,
        ),
    )
    page.set_viewport_size({"width": 1200, "height": 800})
    photo_id = live_server["data"]["photos"][0]
    page.goto(f"{live_server['url']}/edit/{photo_id}")
    expect(page.locator("#editorFilename")).to_have_text("hawk1.jpg")
    page.wait_for_function(
        """() => {
            const img = document.getElementById('editorImg');
            return img && img.naturalWidth === 1600;
        }"""
    )


def test_photo_editor_mouse_drag_pans_at_actual_size(live_server, page):
    """A primary drag at 100% pans the viewport without moving the crop."""
    _open_large_editor_preview(live_server, page)
    page.locator("#actualBtn").click()
    page.wait_for_function(
        """() => {
            const wrap = document.getElementById('editorCanvasWrap');
            const img = document.getElementById('editorImg');
            return wrap.classList.contains('zoom-actual') &&
                img.naturalWidth === 1600;
        }"""
    )
    dimensions = page.evaluate(
        """() => {
            const wrap = document.getElementById('editorCanvasWrap');
            return {
                scrollWidth: wrap.scrollWidth,
                clientWidth: wrap.clientWidth,
                scrollHeight: wrap.scrollHeight,
                clientHeight: wrap.clientHeight,
            };
        }"""
    )
    assert dimensions["scrollWidth"] > dimensions["clientWidth"], dimensions
    assert dimensions["scrollHeight"] > dimensions["clientHeight"], dimensions

    before_crop = page.evaluate("() => JSON.stringify(editorState.recipe.crop)")
    wrap = page.locator("#editorCanvasWrap").bounding_box()
    assert wrap is not None
    start_x = wrap["x"] + min(wrap["width"] - 40, 360)
    start_y = wrap["y"] + min(wrap["height"] - 40, 300)
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x - 140, start_y - 110, steps=4)
    page.mouse.up()

    scroll = page.evaluate(
        """() => {
            const wrap = document.getElementById('editorCanvasWrap');
            return {left: wrap.scrollLeft, top: wrap.scrollTop};
        }"""
    )
    assert scroll["left"] >= 130
    assert scroll["top"] >= 100
    assert page.evaluate("() => JSON.stringify(editorState.recipe.crop)") == before_crop
    assert "panning" not in (
        page.locator("#editorCanvasWrap").get_attribute("class") or ""
    ).split()


def test_photo_editor_actual_size_crop_handle_still_resizes(live_server, page):
    """Crop handles remain resize targets instead of starting a pan at 100%."""
    _open_large_editor_preview(live_server, page)
    page.evaluate(
        """() => {
            setCropField('x', '10');
            setCropField('y', '10');
            setCropField('w', '30');
            setCropField('h', '30');
            setZoomMode('actual');
        }"""
    )
    handle = page.locator(".crop-handle.se")
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    assert box is not None
    before = page.evaluate(
        """() => ({
            width: editorState.recipe.crop.w,
            scrollLeft: document.getElementById('editorCanvasWrap').scrollLeft,
            scrollTop: document.getElementById('editorCanvasWrap').scrollTop,
        })"""
    )
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 30, y, steps=4)
    page.mouse.up()

    after = page.evaluate(
        """() => ({
            width: editorState.recipe.crop.w,
            scrollLeft: document.getElementById('editorCanvasWrap').scrollLeft,
            scrollTop: document.getElementById('editorCanvasWrap').scrollTop,
        })"""
    )
    assert after["width"] > before["width"]
    assert after["scrollLeft"] == before["scrollLeft"]
    assert after["scrollTop"] == before["scrollTop"]


def test_photo_editor_space_drag_overrides_crop_move(live_server, page):
    """Holding Space makes a crop-interior drag pan instead of editing it."""
    _open_large_editor_preview(live_server, page)
    page.evaluate(
        """() => {
            setCropField('x', '10');
            setCropField('y', '10');
            setCropField('w', '60');
            setCropField('h', '60');
        }"""
    )
    before_crop = page.evaluate("() => JSON.stringify(editorState.recipe.crop)")
    crop = page.locator("#editorCropBox").bounding_box()
    assert crop is not None
    x = crop["x"] + crop["width"] / 2
    y = crop["y"] + crop["height"] / 2

    page.keyboard.down("Space")
    expect(page.locator("#editorCanvasWrap")).to_have_class(
        "editor-canvas-wrap space-pan"
    )
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 40, y + 30, steps=4)
    page.mouse.up()
    page.keyboard.up("Space")

    assert page.evaluate("() => JSON.stringify(editorState.recipe.crop)") == before_crop
    assert page.evaluate("() => editorState.spacePan") is False


def test_photo_editor_space_on_focused_button_activates_button(live_server, page):
    """Space on a focused editor button activates the button, not pan mode."""
    _open_large_editor_preview(live_server, page)
    page.locator("#actualBtn").focus()
    page.keyboard.press("Space")
    page.wait_for_function(
        """() => document.getElementById('editorCanvasWrap')
              .classList.contains('zoom-actual')"""
    )
    assert page.evaluate("() => editorState.spacePan") is False
    assert "space-pan" not in (
        page.locator("#editorCanvasWrap").get_attribute("class") or ""
    ).split()
