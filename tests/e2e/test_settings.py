import re

from playwright.sync_api import expect


def test_settings_links_to_storage_page(live_server, page):
    """Settings makes the dedicated storage page discoverable."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)

    storage_link = page.get_by_role("link", name="Open Storage")
    expect(storage_link).to_be_visible()
    expect(storage_link).to_have_attribute("href", "/storage")


def test_settings_system_info_renders(live_server, page):
    """Settings page loads and displays system info section with real data."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)

    # The system info section should be visible
    system_section = page.locator(".section-title", has_text="System")
    expect(system_section).to_be_visible()

    # /api/system/info populates these fields asynchronously via JS.
    # Allow a generous timeout for the async fetch to complete.
    api_timeout = 30_000

    # Compute device should be populated
    device_name = page.locator("#deviceName")
    expect(device_name).not_to_have_text("-", timeout=api_timeout)


def test_settings_cmd_f_opens_page_text_search(live_server, page):
    """Settings captures find shortcut and highlights page text matches."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)

    page.keyboard.press("Control+F")

    find_panel = page.locator("#settingsFindPanel")
    find_input = page.locator("#settingsFindInput")
    expect(find_panel).to_be_visible()
    expect(find_input).to_be_focused()

    find_input.fill("api")
    expect(page.locator(".settings-find-mark").first).to_be_visible()
    expect(page.locator(".settings-find-mark.active")).to_have_count(1)
    expect(page.locator("#settingsFindStatus")).to_contain_text("of")


def test_settings_text_search_ignores_hidden_update_section(live_server, page):
    """Settings find should not count permanently hidden page text."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)

    page.keyboard.press("Control+F")
    page.locator("#settingsFindInput").fill("Check for Updates")

    expect(page.locator(".settings-find-mark")).to_have_count(0)
    expect(page.locator("#settingsFindStatus")).to_have_text("0 results")


def _wait_for_settings_idle(page):
    """Let the initial config load (and any migration autosave) settle.

    Waits for the explicit ready marker the page flips on after both
    /api/config and /api/workspaces/active/config have been fetched and
    applied, otherwise an interaction can beat loadConfig() to a field
    (and see its value overwritten) or trigger saveWsConfig() before
    _wsOverridesLoaded flips on (a silent no-op that skips the POST the
    test is asserting against). The autosave-pill idle check comes after
    that, since it has no state at all before loads complete and would
    pass immediately on a slow server.
    """
    status = page.locator("#settingsSaveStatus")
    expect(page.locator("#cfgKeywordCase")).to_be_visible()
    expect(page.locator("body")).to_have_attribute(
        "data-settings-ready", "true", timeout=10_000
    )
    expect(status).not_to_have_attribute("data-state", "saving", timeout=10_000)
    return status


def test_settings_autosave_shows_saved_confirmation(live_server, page):
    """Changing a curated field reports Saving… and then a persistent Saved ✓ time."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)
    status = _wait_for_settings_idle(page)

    with page.expect_request(
        lambda r: r.url.endswith("/api/config") and r.method == "POST"
    ):
        page.select_option("#cfgKeywordCase", "title")
        # The write is debounced 500 ms; the pill must already say so.
        expect(status).to_have_attribute("data-state", "saving")
        expect(status).to_have_text("Saving…")

    expect(status).to_have_attribute("data-state", "saved", timeout=10_000)
    expect(status).to_contain_text("Saved ✓")
    # A wall-clock time so the confirmation still means something after the
    # transient flash is gone.
    expect(status).to_have_text(re.compile(r"Saved ✓ \d{1,2}:\d{2}:\d{2}"))

    # The page must agree with what is actually on disk.
    cfg = page.request.get(f"{url}/api/config").json()
    assert cfg["keyword_case"] == "title"


def test_settings_autosave_failure_is_visible(live_server, page):
    """A rejected save is reported in the pill instead of looking like success."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)
    status = _wait_for_settings_idle(page)

    def _fail_post(route, request):
        if request.method == "POST":
            route.fulfill(status=500, content_type="application/json",
                          body='{"error": "disk full"}')
        else:
            route.continue_()

    page.route("**/api/config", _fail_post)
    page.select_option("#cfgKeywordCase", "lower")
    expect(status).to_have_attribute("data-state", "error", timeout=10_000)
    expect(status).to_contain_text("Save failed")

    # The next successful write clears the failure.
    page.unroute("**/api/config", _fail_post)
    page.select_option("#cfgKeywordCase", "title")
    expect(status).to_have_attribute("data-state", "saved", timeout=10_000)
    expect(status).to_contain_text("Saved ✓")
    cfg = page.request.get(f"{url}/api/config").json()
    assert cfg["keyword_case"] == "title"


def test_settings_workspace_override_autosave_shows_saved(live_server, page):
    """The workspace-overrides form reports through the same pill."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)
    status = _wait_for_settings_idle(page)

    checkbox = page.locator("#wsOverride_classification_threshold")
    expect(checkbox).to_be_visible()
    with page.expect_request(
        lambda r: r.url.endswith("/api/workspaces/active/config") and r.method == "POST"
    ):
        checkbox.check()
        expect(status).to_have_attribute("data-state", "saving")

    expect(status).to_have_attribute("data-state", "saved", timeout=10_000)
    expect(status).to_contain_text("Saved ✓")
    overrides = page.request.get(f"{url}/api/workspaces/active/config").json()
    assert "classification_threshold" in overrides


def test_settings_cleared_override_is_not_reported_as_saved(live_server, page):
    """A blank override is dropped from the save; the pill must not claim it saved."""
    url = live_server["url"]
    page.goto(f"{url}/settings", timeout=5000)
    status = _wait_for_settings_idle(page)

    checkbox = page.locator("#wsOverride_grouping_window_seconds")
    checkbox.check()
    expect(status).to_have_attribute("data-state", "saved", timeout=10_000)
    value = page.locator("#wsVal_grouping_window_seconds")
    expect(value).to_have_value("10")

    # Hold the resync GET so we can observe that "Saved" waits for it.
    held = []

    def _hold_get(route, request):
        if request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/workspaces/active/config", _hold_get)
    with page.expect_response(
        lambda r: r.url.endswith("/api/workspaces/active/config")
        and r.request.method == "POST"
    ):
        value.fill("")
        value.dispatch_event("change")
        expect(status).to_have_attribute("data-state", "saving")
    # The POST has completed but the resync GET is held: still not "Saved".
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "resync GET was never requested"
    expect(status).to_have_attribute("data-state", "saving")

    for route in held:
        route.continue_()
    page.unroute("**/api/workspaces/active/config", _hold_get)
    expect(status).to_have_attribute("data-state", "saved", timeout=10_000)
    # The field shows the value actually in effect, not the blank entry.
    expect(value).to_have_value("10")
    overrides = page.request.get(f"{url}/api/workspaces/active/config").json()
    assert overrides["grouping_window_seconds"] == 10
