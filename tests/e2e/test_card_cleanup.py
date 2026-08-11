"""Browser coverage for the dedicated Card Cleanup page."""

import re

from playwright.sync_api import expect


def test_card_cleanup_folder_browser_shows_recursive_photo_counts(
    live_server, page,
):
    """The shared picker shows the same recursive counts as Import."""
    url = live_server["url"]
    page.goto(f"{url}/card-cleanup")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target === '/api/browse/photo-counts') {
              const body = JSON.parse(init.body || '{}');
              window.__cardFolderCountRequest = body;
              return Promise.resolve(new Response(JSON.stringify({
                counts: {
                  '/tmp/card-a': 1,
                  '/tmp/card-b': 1234,
                  '/tmp/empty': 0,
                },
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/browse') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                path: '/tmp',
                dirs: [
                  {name: 'card-a', path: '/tmp/card-a'},
                  {name: 'card-b', path: '/tmp/card-b'},
                  {name: 'empty', path: '/tmp/empty'},
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("[data-testid='card-cleanup-browse-btn']").click()

    browser = page.locator("[data-testid='folder-browser']")
    expect(browser).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#folderBrowserTitle")).to_have_text(
        "Select Card Folder"
    )
    badges = page.locator("#folderBrowserList .folder-browser-count")
    expect(badges).to_have_count(3)
    expect(badges.nth(0)).to_have_text("1 photo")
    expect(badges.nth(1)).to_have_text("1,234 photos")
    expect(badges.nth(2)).to_be_empty()
    request = page.evaluate("window.__cardFolderCountRequest")
    assert request["paths"] == [
        "/tmp/card-a", "/tmp/card-b", "/tmp/empty",
    ]
    assert request["file_types"] == "both"
