from playwright.sync_api import expect


def _set_range(page, selector, value):
    page.locator(selector).evaluate(
        """(el, value) => {
            el.value = String(value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        value,
    )


def test_photo_editor_saves_and_restores_advanced_color(live_server, page):
    url = live_server["url"]
    photo_id = live_server["data"]["photos"][0]
    page.goto(f"{url}/edit/{photo_id}")
    expect(page.locator("#editorFilename")).to_have_text("hawk1.jpg")

    _set_range(page, "#curve_midtonesRange", 62)
    page.locator("#hslColorSelect").select_option("orange")
    _set_range(page, "#hslSaturationRange", 30)
    page.locator("#colorGradeZoneSelect").select_option("shadows")
    _set_range(page, "#colorGradeHueRange", 220)
    _set_range(page, "#colorGradeSaturationRange", 18)

    expect(page.locator("#saveBtn")).to_be_enabled()
    with page.expect_response(f"**/api/photos/{photo_id}/edit-recipe") as response:
        page.locator("#saveBtn").click()
    assert response.value.status == 200
    expect(page.locator("#saveBtn")).to_be_disabled()

    recipe = page.evaluate(
        """async (photoId) => {
            const r = await fetch('/api/photos/' + photoId + '/edit-recipe');
            return (await r.json()).recipe;
        }""",
        photo_id,
    )
    assert recipe["adjustments"]["tone_curve"] == {"midtones": 62.0}
    assert recipe["adjustments"]["hsl"] == {
        "orange": {"saturation": 30.0},
    }
    assert recipe["adjustments"]["color_grading"] == {
        "shadows": {"hue": 220.0, "saturation": 18.0},
    }

    page.reload()
    expect(page.locator("#curve_midtonesRange")).to_have_value("62")
    page.locator("#hslColorSelect").select_option("orange")
    expect(page.locator("#hslSaturationRange")).to_have_value("30")
    expect(page.locator("#colorGradeHueRange")).to_have_value("220")
    expect(page.locator("#colorGradeSaturationRange")).to_have_value("18")


def test_photo_editor_export_saves_current_edits_and_exports_current_photo(
    live_server, page,
):
    url = live_server["url"]
    photo_id = live_server["data"]["photos"][0]
    page.goto(f"{url}/edit/{photo_id}")
    expect(page.locator("#editorFilename")).to_have_text("hawk1.jpg")
    expect(page.locator("#exportBtn")).to_be_enabled()

    _set_range(page, "#exposureRange", 1.2)
    expect(page.locator("#saveBtn")).to_be_enabled()

    with page.expect_response(
        f"**/api/photos/{photo_id}/edit-recipe"
    ) as save_response:
        page.locator("#exportBtn").click()
    assert save_response.value.status == 200
    expect(page.locator("#saveBtn")).to_be_disabled()
    expect(page.locator("#exportOverlay")).to_have_class("modal-overlay open")
    expect(page.locator("#exportPreview")).to_have_text("Preview: hawk1.jpg")

    page.get_by_role("button", name="Browse…", exact=True).click()
    expect(page.locator("#folderBrowser")).to_have_class(
        "folder-browser-overlay open"
    )
    page.keyboard.press("Escape")
    expect(page.locator("#folderBrowser")).not_to_have_class(
        "folder-browser-overlay open"
    )
    expect(page.locator("#exportOverlay")).to_have_class("modal-overlay open")

    page.evaluate(
        """() => {
          window.__editorExportRequest = null;
          const originalSafeFetch = window.safeFetch;
          window.safeFetch = async function(url, options, config) {
            if (url === '/api/jobs/export') {
              window.__editorExportRequest = JSON.parse(options.body);
              return {job_id: 'editor-export-test'};
            }
            return originalSafeFetch(url, options, config);
          };
        }"""
    )
    page.locator("#exportMetadataRating").check()
    page.locator("#exportSubmitBtn").click()
    page.wait_for_function("() => window.__editorExportRequest !== null")
    request = page.evaluate("window.__editorExportRequest")
    assert request["photo_ids"] == [photo_id]
    assert request["destination"] == ""
    assert request["format"] == "jpg"
    assert request["metadata_fields"] == ["rating"]
    expect(page.locator("#exportOverlay")).not_to_have_class("modal-overlay open")


def test_reset_adjustments_preserves_advanced_color(live_server, page):
    url = live_server["url"]
    photo_id = live_server["data"]["photos"][0]
    page.goto(f"{url}/edit/{photo_id}")
    expect(page.locator("#editorFilename")).to_have_text("hawk1.jpg")

    # Basic adjustments the Reset button owns, plus advanced sections that
    # each have their own dedicated reset control.
    _set_range(page, "#exposureRange", 1.2)
    _set_range(page, "#contrastRange", 25)
    _set_range(page, "#curve_midtonesRange", 62)
    page.locator("#hslColorSelect").select_option("orange")
    _set_range(page, "#hslSaturationRange", 30)
    page.locator("#colorGradeZoneSelect").select_option("shadows")
    _set_range(page, "#colorGradeHueRange", 220)
    _set_range(page, "#colorGradeSaturationRange", 18)

    page.locator('button[onclick="resetAdjustments()"]').click()

    # Basic sliders return to neutral, but advanced sections stay intact.
    expect(page.locator("#exposureRange")).to_have_value("0")
    expect(page.locator("#contrastRange")).to_have_value("0")
    expect(page.locator("#curve_midtonesRange")).to_have_value("62")
    page.locator("#hslColorSelect").select_option("orange")
    expect(page.locator("#hslSaturationRange")).to_have_value("30")
    page.locator("#colorGradeZoneSelect").select_option("shadows")
    expect(page.locator("#colorGradeHueRange")).to_have_value("220")
    expect(page.locator("#colorGradeSaturationRange")).to_have_value("18")

    adjustments = page.evaluate("() => editorState.recipe.adjustments || {}")
    assert "exposure" not in adjustments
    assert "contrast" not in adjustments
    assert adjustments.get("tone_curve") == {"midtones": 62.0}
    assert adjustments.get("hsl") == {"orange": {"saturation": 30.0}}
    assert adjustments.get("color_grading") == {
        "shadows": {"hue": 220.0, "saturation": 18.0},
    }
