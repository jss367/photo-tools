from playwright.sync_api import expect


def test_enter_commits_collection_keyword_and_refreshes_preview(live_server, page):
    """Enter commits a rule value without saving or closing the editor."""
    page.goto(live_server["url"] + "/browse")
    page.get_by_role("button", name="+ New Collection").click()

    modal = page.locator("#collectionModal")
    value_input = modal.locator("#ruleRows input[type='text']")
    expect(value_input).to_be_visible()

    value_input.fill("Red-tailed Hawk")
    value_input.press("Enter")

    expect(value_input).not_to_be_focused()
    expect(modal.locator("#rulePreview")).to_have_text("Matches: 1 photo")
    expect(modal).to_have_class("modal-overlay open")


def test_enter_during_ime_composition_does_not_commit_rule(live_server, page):
    """Enter fired while an IME composition is active must not blur the input."""
    page.goto(live_server["url"] + "/browse")
    page.get_by_role("button", name="+ New Collection").click()

    modal = page.locator("#collectionModal")
    value_input = modal.locator("#ruleRows input[type='text']")
    expect(value_input).to_be_visible()

    value_input.click()
    value_input.evaluate(
        "el => el.dispatchEvent(new KeyboardEvent('keydown',"
        " {key: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true}))"
    )

    expect(value_input).to_be_focused()


def test_enter_commits_timestamp_between_date_input(live_server, page):
    """Enter in a Date/between rule input blurs the field, leaving the editor open."""
    page.goto(live_server["url"] + "/browse")
    page.get_by_role("button", name="+ New Collection").click()

    modal = page.locator("#collectionModal")
    field_select = modal.locator(
        '#ruleRows select:has(option[value="timestamp"])'
    )
    field_select.select_option("timestamp")

    date_input = modal.locator("#ruleRows input[type='date']").first
    expect(date_input).to_be_visible()
    date_input.focus()
    date_input.press("Enter")

    expect(date_input).not_to_be_focused()
    expect(modal).to_have_class("modal-overlay open")


def test_enter_commits_timestamp_recent_days_input(live_server, page):
    """Enter in a Date/recent_days rule input blurs the field, leaving the editor open."""
    page.goto(live_server["url"] + "/browse")
    page.get_by_role("button", name="+ New Collection").click()

    modal = page.locator("#collectionModal")
    modal.locator(
        '#ruleRows select:has(option[value="timestamp"])'
    ).select_option("timestamp")
    modal.locator(
        '#ruleRows select:has(option[value="recent_days"])'
    ).select_option("recent_days")

    number_input = modal.locator("#ruleRows input[type='number']")
    expect(number_input).to_be_visible()
    number_input.fill("14")
    number_input.press("Enter")

    expect(number_input).not_to_be_focused()
    expect(modal).to_have_class("modal-overlay open")
