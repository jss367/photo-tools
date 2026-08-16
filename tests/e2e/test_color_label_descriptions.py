from playwright.sync_api import expect


def test_right_click_color_adds_workspace_description(live_server, page):
    """A color dot opens the description editor and the saved text becomes its tooltip."""
    page.goto(f"{live_server['url']}/browse")
    first = page.locator(".grid-card").first
    first.wait_for(state="visible")
    first.click()

    red = page.locator('#detailColors [data-color="red"]')
    expect(red).to_be_visible()
    expect(red).to_have_attribute("title", "Red (6) · Right-click to add a description")
    red.click(button="right")

    modal = page.locator("#colorLabelDescriptionModal")
    expect(modal).to_have_class("modal-overlay open")
    expect(page.locator("#colorLabelDescriptionHeading")).to_have_text(
        "Red label description"
    )
    field = page.locator("#colorLabelDescriptionInput")
    field.fill("Reptiles")
    page.locator("#colorLabelDescriptionSave").click()

    expect(modal).not_to_have_class("modal-overlay open")
    expect(red).to_have_attribute(
        "title", "Red (6) — Reptiles · Right-click to edit"
    )
    expect(red).to_have_attribute("aria-label", "Red label: Reptiles")

    # The same workspace meaning follows the color into other color-label UI.
    quick_red = page.locator('.vf-quick-colors [data-color="red"]')
    expect(quick_red).to_have_attribute(
        "title", "Red label — Reptiles · Right-click to edit"
    )

    response = page.request.get(f"{live_server['url']}/api/color-label-descriptions")
    assert response.ok
    assert response.json() == {"red": "Reptiles"}


def test_color_description_can_be_removed_and_right_click_does_not_filter(
    live_server, page
):
    """Opening or clearing the editor never toggles the clicked color filter."""
    page.request.put(
        f"{live_server['url']}/api/color-label-descriptions/blue",
        data={"description": "Waterbirds"},
    )
    page.goto(f"{live_server['url']}/browse")
    page.locator(".grid-card").first.wait_for(state="visible")
    blue = page.locator('.vf-quick-colors [data-color="blue"]')
    expect(blue).to_have_attribute(
        "title", "Blue label — Waterbirds · Right-click to edit"
    )

    blue.click(button="right")
    assert "active" not in (blue.get_attribute("class") or "").split()
    field = page.locator("#colorLabelDescriptionInput")
    expect(field).to_have_value("Waterbirds")
    field.fill("")
    page.locator("#colorLabelDescriptionSave").click()

    expect(blue).to_have_attribute(
        "title", "Blue label · Right-click to add a description"
    )
    assert page.request.get(
        f"{live_server['url']}/api/color-label-descriptions"
    ).json() == {}
