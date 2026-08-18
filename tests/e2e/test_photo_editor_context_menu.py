import re

from playwright.sync_api import expect


def _open_editor(live_server, page):
    photo_id = live_server["data"]["photos"][0]
    page.goto(f"{live_server['url']}/edit/{photo_id}")
    expect(page.locator("#editorFilename")).to_have_text("hawk1.jpg")
    page.wait_for_function("() => !editorState.loading")


def test_photo_editor_right_click_opens_useful_menu(live_server, page):
    _open_editor(live_server, page)

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})

    menu = page.locator(".vireo-ctx-menu")
    expect(menu).to_be_visible()
    for label in (
        "Save Changes",
        "Revert to Saved",
        "Show Saved Version",
        "Fit to Window",
        "View at 100%",
        "Auto Tone",
        "Edit Crop",
        "Full Frame",
        "Rotate Left",
        "Rotate Right",
        "Flip Horizontal",
        "Flip Vertical",
        "Copy Edit Settings",
        "Reset All Edits",
        "Export…",
        "Send to iNaturalist",
    ):
        expect(menu.get_by_text(label, exact=True)).to_be_attached()

    expect(menu.get_by_text("Save Changes", exact=True)).to_have_class(
        re.compile(r"vireo-ctx-disabled")
    )


def test_photo_editor_context_transform_updates_recipe(live_server, page):
    _open_editor(live_server, page)

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    page.locator(".vireo-ctx-menu").get_by_text("Rotate Right", exact=True).click()

    expect(page.locator(".vireo-ctx-menu")).to_be_hidden()
    assert page.evaluate("() => editorState.recipe.rotation") == 90
    expect(page.locator("#saveBtn")).to_be_enabled()

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    save_item = page.locator(".vireo-ctx-menu").get_by_text("Save Changes", exact=True)
    expect(save_item).not_to_have_class(re.compile(r"vireo-ctx-disabled"))


def test_photo_editor_context_revert_restores_saved_recipe(live_server, page):
    _open_editor(live_server, page)
    page.evaluate("() => rotateRecipe(90)")
    assert page.evaluate("() => isEditorDirty()") is True

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    page.locator(".vireo-ctx-menu").get_by_text("Revert to Saved", exact=True).click()

    page.wait_for_function("() => !isEditorDirty()")
    assert page.evaluate("() => Number(editorState.recipe.rotation || 0)") == 0
    expect(page.locator("#saveBtn")).to_be_disabled()


def test_photo_editor_keeps_native_menu_for_inputs(live_server, page):
    _open_editor(live_server, page)

    page.locator("#cropX").click(button="right")

    expect(page.locator(".vireo-ctx-menu")).to_have_count(0)


def test_photo_editor_export_opens_standard_export_for_current_photo(
    live_server, page
):
    _open_editor(live_server, page)
    photo_id = page.evaluate("() => editorState.photoId")

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    page.locator(".vireo-ctx-menu").get_by_text("Export…", exact=True).click()

    page.wait_for_url(re.compile(r"/browse\?photo_id=\d+$"))
    expect(page.locator("#exportOverlay")).to_have_class("modal-overlay open")
    assert page.evaluate("() => getActiveSelection()") == [photo_id]
    expect(page.locator("#exportSubmitBtn")).to_have_text("Export 1 photo")


def test_photo_editor_context_sends_current_photo_to_inaturalist(
    live_server, page
):
    _open_editor(live_server, page)
    photo_id = page.evaluate("() => editorState.photoId")
    page.evaluate(
        """() => {
          window.__inatTarget = null;
          window.submitToInat = function(id) { window.__inatTarget = id; };
        }"""
    )

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    page.locator(".vireo-ctx-menu").get_by_text(
        "Send to iNaturalist", exact=True
    ).click()

    assert page.evaluate("() => window.__inatTarget") == photo_id


def test_photo_editor_export_actions_require_saved_changes(live_server, page):
    _open_editor(live_server, page)
    page.evaluate("() => rotateRecipe(90)")

    page.locator("#editorCanvasWrap").click(button="right", position={"x": 20, "y": 20})
    menu = page.locator(".vireo-ctx-menu")
    for label in ("Export…", "Send to iNaturalist"):
        item = menu.get_by_text(label, exact=True)
        expect(item).to_have_class(re.compile(r"vireo-ctx-disabled"))
        expect(item).to_have_attribute("title", re.compile("Save changes"))
