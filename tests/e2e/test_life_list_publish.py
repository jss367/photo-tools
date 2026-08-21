import json

from playwright.sync_api import expect


def test_publish_site_defaults_and_preflight_summary(live_server, page):
    requests = []

    def preflight(route):
        body = json.loads(route.request.post_data or "{}")
        requests.append(body)
        include_highlights = body.get("include_highlights", False)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "life_list_species": 2,
                "highlight_buckets": 2 if include_highlights else 0,
                "unidentified_photos": 1 if include_highlights else 0,
                "image_count": 5 if include_highlights else 2,
                "data_file_count": 3,
            }),
        )

    page.route("**/api/jobs/publish-site/preflight", preflight)
    page.goto(f"{live_server['url']}/life-list")
    page.get_by_role("button", name="Publish Site").click()

    expect(page.locator("#publishModal")).to_have_class("modal-overlay open")
    expect(page.locator("#publishLifeList")).to_be_checked()
    expect(page.locator("#publishLifeListPhotos")).to_have_value("1")
    expect(page.locator("#publishHighlights")).not_to_be_checked()
    expect(page.locator("#publishHighlightPhotos")).to_be_disabled()
    expect(page.locator("#publishPreflight")).to_have_text(
        "Will publish 2 Life List species · 2 unique photos · 3 data files."
    )
    expect(page.locator("#publishSubmitBtn")).to_be_enabled()
    assert requests[-1]["photos_per_species"] == 1
    assert requests[-1]["include_highlights"] is False

    page.locator("#publishHighlights").check()
    expect(page.locator("#publishHighlightPhotos")).to_be_enabled()
    expect(page.locator("#publishPreflight")).to_have_text(
        "Will publish 2 Life List species · 2 Highlight categories · "
        "1 unidentified Highlight · 5 unique photos · 3 data files."
    )
    assert requests[-1]["include_highlights"] is True
    assert requests[-1]["limit_per_bucket"] == 3


def test_publish_site_requires_some_content(live_server, page):
    page.goto(f"{live_server['url']}/life-list")
    page.get_by_role("button", name="Publish Site").click()
    expect(page.locator("#publishSubmitBtn")).to_be_enabled()

    page.locator("#publishLifeList").uncheck()

    expect(page.locator("#publishLifeListPhotos")).to_be_disabled()
    expect(page.locator("#publishLocations")).to_be_disabled()
    expect(page.locator("#publishPreflight")).to_have_text(
        "Select Life List, Highlights, or both."
    )
    expect(page.locator("#publishSubmitBtn")).to_be_disabled()
