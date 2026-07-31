import json
import re

from playwright.sync_api import expect


def _suppress_auto_preview(page):
    """Stop the 350ms debounced auto-preview from firing.

    For tests that only care about the Start payload. Their source folders
    don't exist, so a debounced preview that does land completes with zero
    files -- a real, completed preview -- and updateStartGate() correctly
    disables Start with "No files to import". Whether it lands at all is a
    race against how fast Playwright drives the form, so suppress it rather
    than let the machine's speed decide.

    This hooks window.scheduleImportPreview, so it stops working silently if
    a caller is ever changed to reach previewImport() directly. Assert
    #btnStart is enabled immediately before clicking it in tests that rely on
    this, so the failure surfaces as an assertion rather than as a 30s
    actionability timeout with no explanation.
    """
    page.evaluate("() => { window.scheduleImportPreview = () => {}; }")


def test_import_source_browse_button_adds_source_folder(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => ['/tmp/card-a', '/tmp/card-b']")

    browse_btn = page.locator("[data-testid='import-source-browse-btn']")
    expect(browse_btn).to_be_visible()
    browse_btn.click()

    source_list = page.locator("#sourceList")
    expect(source_list).to_contain_text("/tmp/card-a")
    expect(source_list).to_contain_text("/tmp/card-b")


def test_import_source_browse_button_shows_quick_photo_count(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 42,
                total_size: 0,
                type_breakdown: {'.jpg': 42},
                duplicate_count: 0,
                files: [],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
          window.pickDirectory = async () => ['/tmp/card-a'];
        }
        """
    )

    page.locator("[data-testid='import-source-browse-btn']").click()

    source_list = page.locator("#sourceList")
    expect(source_list).to_contain_text("/tmp/card-a")
    expect(source_list.locator(".source-meta")).to_have_text("42 photos")


def test_import_preview_runs_automatically_after_source_selection(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__fullPreviewCalls = 0;
          window.__dupCalls = 0;
          window.__destCalls = 0;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              const body = JSON.parse(init.body || '{}');
              if (body.summary_only) {
                return Promise.resolve(new Response(JSON.stringify({
                  total_count: 2,
                  total_size: 2468,
                  type_breakdown: {'.jpg': 2},
                  duplicate_count: 0,
                  files: [],
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              window.__fullPreviewCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 2,
                total_size: 2468,
                type_breakdown: {'.jpg': 2},
                duplicate_count: 0,
                files: [
                  {
                    path: '/tmp/card-a/IMG_0001.jpg',
                    filename: 'IMG_0001.jpg',
                    subfolder: 'card-a',
                    size: 1234,
                    extension: '.jpg',
                    thumb_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
                  },
                  {
                    path: '/tmp/card-a/IMG_0002.jpg',
                    filename: 'IMG_0002.jpg',
                    subfolder: 'card-a',
                    size: 1234,
                    extension: '.jpg',
                    thumb_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
                  },
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              window.__dupCalls += 1;
              const frame = 'data: ' + JSON.stringify({
                duplicates: ['/tmp/card-a/IMG_0002.jpg'],
                checked: 2,
                total: 2,
              }) + '\\n\\n' + 'data: ' + JSON.stringify({
                done: true,
                duplicate_count: 1,
                checked: 2,
                total: 2,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              window.__destCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({
                folders: [{
                  path: '2026/2026-07-11',
                  full_path: '/archive/2026/2026-07-11',
                  count: 1,
                  exists: false,
                }],
                total_photos: 1,
                total_folders: 1,
                new_folders: 1,
                existing_folders: 0,
                managed_archive: null,
                files: [{
                  path: '/tmp/card-a/IMG_0001.jpg',
                  folder: '2026/2026-07-11',
                  full_folder: '/archive/2026/2026-07-11',
                }],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()

    page.wait_for_function(
        "window.__fullPreviewCalls >= 1 && window.__dupCalls >= 1 && window.__destCalls >= 1"
    )
    expect(page.locator("#previewSummary")).to_contain_text("1 already in your library")
    grid = page.locator("#importPreviewGrid")
    expect(grid).to_be_visible()
    expect(grid).to_contain_text("IMG_0001.jpg")
    expect(grid).to_contain_text("IMG_0002.jpg")
    expect(grid).to_contain_text("Duplicate")
    expect(grid).to_contain_text("To: 2026/2026-07-11")


def test_import_auto_preview_clears_grid_when_selection_becomes_invalid(
    live_server, page
):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__fullPreviewCalls = 0;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              const body = JSON.parse(init.body || '{}');
              if (body.summary_only) {
                return Promise.resolve(new Response(JSON.stringify({
                  total_count: 1,
                  total_size: 1234,
                  type_breakdown: {'.jpg': 1},
                  duplicate_count: 0,
                  files: [],
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              window.__fullPreviewCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 1,
                total_size: 1234,
                type_breakdown: {'.jpg': 1},
                duplicate_count: 0,
                files: [{
                  path: '/tmp/card-a/IMG_0001.jpg',
                  filename: 'IMG_0001.jpg',
                  subfolder: 'card-a',
                  size: 1234,
                  extension: '.jpg',
                  thumb_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
                }],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              const frame = 'data: ' + JSON.stringify({
                done: true, duplicate_count: 0, checked: 1, total: 1,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                folders: [],
                files: [],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.wait_for_function("window.__fullPreviewCalls >= 1")
    expect(page.locator("#importPreviewGrid")).to_be_visible()

    page.locator("#fileTypePreset").select_option("custom")
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.file-ext').forEach(el => { el.checked = false; });
          document.querySelector('.file-ext').dispatchEvent(
            new Event('change', { bubbles: true }));
        }
        """
    )

    expect(page.locator("#importError")).to_contain_text(
        "Choose at least one file extension."
    )
    expect(page.locator("#importPreviewGrid")).to_be_hidden()


def test_import_destination_browse_button_sets_destination(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => '/tmp/archive'")
    page.locator("#modeCopy").check()

    browse_btn = page.locator("[data-testid='import-destination-browse-btn']")
    expect(browse_btn).to_be_visible()
    browse_btn.click()

    expect(page.locator("#destInput")).to_have_value("/tmp/archive")


def test_import_recent_destination_button_selects_saved_path(live_server, page):
    """Saved import destinations remain visible as one-click choices."""
    import config as cfg

    config = cfg.load()
    config["ingest"]["recent_destinations"] = [
        "/Volumes/Photos/Archive",
        "/Volumes/Photos/Trips",
    ]
    cfg.save(config)

    page.goto(f"{live_server['url']}/import")
    page.locator("#modeCopy").check()

    choices = page.locator("[data-testid='recent-destinations']")
    expect(choices).to_be_visible()
    expect(choices).to_contain_text("Archive")
    expect(choices).to_contain_text("Trips")

    page.get_by_role(
        "button", name="Use /Volumes/Photos/Trips"
    ).click()
    expect(page.locator("#destInput")).to_have_value("/Volumes/Photos/Trips")


def test_import_custom_extensions_feed_preview(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__previewBody = null;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              window.__previewBody = JSON.parse(init.body);
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 0,
                total_size: 0,
                type_breakdown: {},
                duplicate_count: 0,
                files: [],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#fileTypePreset").select_option("custom")
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.file-ext').forEach(el => { el.checked = false; });
          document.querySelector('.file-ext[value=".jpg"]').checked = true;
          document.querySelector('.file-ext[value=".nef"]').checked = true;
        }
        """
    )
    page.locator("#btnPreview").click()
    page.wait_for_function("window.__previewBody !== null")

    body = page.evaluate("window.__previewBody")
    assert body["folders"] == ["/tmp/card-a"]
    assert body["file_types"] == [".jpg", ".nef"]


def test_import_preview_passes_verify_by_hash_to_duplicate_check(live_server, page):
    """The preview and the actual import must use the same duplicate mode so
    the counts don't disagree for renamed / metadata-colliding files."""
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__dupBody = null;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 1,
                total_size: 0,
                type_breakdown: {'.jpg': 1},
                duplicate_count: 0,
                files: [{path: '/tmp/card-a/IMG_0001.jpg'}],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              window.__dupBody = JSON.parse(init.body);
              const frame = 'data: ' + JSON.stringify({
                done: true, duplicate_count: 0, checked: 1, total: 1,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#chkSkipDuplicates").check()
    page.locator("#chkVerifyByHash").check()
    page.locator("#btnPreview").click()
    page.wait_for_function("window.__dupBody !== null")

    body = page.evaluate("window.__dupBody")
    assert body["paths"] == ["/tmp/card-a/IMG_0001.jpg"]
    assert body["verify_by_hash"] is True


def test_import_preview_shows_destination_folder_structure(live_server, page):
    """Copy-mode preview surfaces exact destination folder paths and file
    counts beside the folder template, plus a managed-archive callout, wired to
    /api/import/destination-preview. Skipped duplicates are excluded so the
    folder counts match the files that will actually land."""
    url = live_server["url"]
    captured = {}

    def folder_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "files": [
                    {"path": "/tmp/card-a/IMG_0001.jpg"},
                    {"path": "/tmp/card-a/IMG_0002.jpg"},
                ],
            }),
        )

    def check_duplicates(route):
        frame = (
            "data: " + json.dumps({
                "duplicates": ["/tmp/card-a/IMG_0002.jpg"],
                "checked": 2, "total": 2,
            }) + "\n\n"
            + "data: " + json.dumps({
                "done": True, "duplicate_count": 1, "checked": 2, "total": 2,
            }) + "\n\n"
        )
        route.fulfill(
            status=200, content_type="text/event-stream", body=frame,
        )

    def destination_preview(route):
        captured["body"] = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "folders": [
                    {"path": "2026/2026-07-01",
                     "full_path": "/archive/2026/2026-07-01",
                     "count": 1, "exists": False},
                    {"path": "2026/2026-07-02",
                     "full_path": "/archive/2026/2026-07-02",
                     "count": 1, "exists": True},
                ],
                "total_photos": 2,
                "total_folders": 2,
                "new_folders": 1,
                "existing_folders": 1,
                "managed_archive": {"path": "/archive", "photo_count": 1284},
            }),
        )

    page.route("**/api/import/folder-preview", folder_preview)
    page.route("**/api/import/check-duplicates", check_duplicates)
    page.route("**/api/import/destination-preview", destination_preview)
    page.goto(f"{url}/import")

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()

    structure = page.locator("#destStructure")
    expect(structure).to_be_visible()
    expect(structure).to_contain_text(
        "Resulting folders: 2 files split into 2 folders (1 new, 1 existing)"
    )
    expect(page.locator("#destCard #destStructure")).to_be_visible()
    expect(structure.locator("th")).to_have_text(["Exact folder", "Files", "Status"])
    rows = structure.locator("tr")
    expect(rows.nth(1).locator("td")).to_have_text(
        ["/archive/2026/2026-07-01", "1", "new"]
    )
    expect(rows.nth(2).locator("td")).to_have_text(
        ["/archive/2026/2026-07-02", "1", "existing"]
    )
    expect(structure).to_contain_text("Merging into a managed archive at")
    expect(structure).to_contain_text("/archive")
    expect(structure).to_contain_text("1284 photos already cataloged")
    expect(structure).to_contain_text("2026/2026-07-01")
    expect(structure).to_contain_text("new")
    expect(structure).to_contain_text("existing")

    # The structure block is only made visible after destination-preview
    # resolves, so captured["body"] is populated by the time we get here.
    # The skipped duplicate is excluded from the structure preview so the
    # new/existing folder counts reflect the copy set, not every file found.
    assert captured["body"]["exclude_paths"] == ["/tmp/card-a/IMG_0002.jpg"]
    assert captured["body"]["destination"] == "/archive"
    assert captured["body"]["file_types"] == "both"


def test_import_destination_structure_ignores_stale_response(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__resolveDestStructure = null;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                files: [{path: '/tmp/card-a/IMG_0001.jpg'}],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              const frame = 'data: ' + JSON.stringify({
                done: true, duplicate_count: 0, checked: 1, total: 1,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              return new Promise((resolve) => {
                window.__resolveDestStructure = () => resolve(new Response(JSON.stringify({
                  folders: [{
                    path: '2026/07/11',
                    full_path: '/archive/2026/07/11',
                    count: 1,
                    exists: false,
                  }],
                  total_photos: 1,
                  total_folders: 1,
                  new_folders: 1,
                  existing_folders: 0,
                  managed_archive: null,
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              });
            }
            return originalFetch(input, init);
          };
          addSourcePath('/tmp/card-a');
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()
    page.wait_for_function("window.__resolveDestStructure !== null")
    page.locator("#destInput").fill("/new-archive")
    page.evaluate("() => window.__resolveDestStructure()")

    expect(page.locator("#destStructure")).to_be_hidden()


def test_import_destination_structure_hides_on_duplicate_control_toggle(
    live_server, page
):
    url = live_server["url"]

    def folder_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"files": [{"path": "/tmp/card-a/IMG_0001.jpg"}]}),
        )

    def check_duplicates(route):
        frame = (
            "data: " + json.dumps({
                "done": True, "duplicate_count": 0, "checked": 1, "total": 1,
            }) + "\n\n"
        )
        route.fulfill(status=200, content_type="text/event-stream", body=frame)

    def destination_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "folders": [{
                    "path": "2026/07/11",
                    "full_path": "/archive/2026/07/11",
                    "count": 1,
                    "exists": False,
                }],
                "total_photos": 1,
                "total_folders": 1,
                "new_folders": 1,
                "existing_folders": 0,
                "managed_archive": None,
            }),
        )

    page.route("**/api/import/folder-preview", folder_preview)
    page.route("**/api/import/check-duplicates", check_duplicates)
    page.route("**/api/import/destination-preview", destination_preview)
    page.goto(f"{url}/import")

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()
    expect(page.locator("#destStructure")).to_be_visible()

    page.locator("#chkSkipDuplicates").uncheck()
    expect(page.locator("#destStructure")).to_be_hidden()

    page.locator("#btnPreview").click()
    expect(page.locator("#destStructure")).to_be_visible()

    page.locator("#chkVerifyByHash").check()
    expect(page.locator("#destStructure")).to_be_hidden()

    page.locator("#btnPreview").click()
    expect(page.locator("#destStructure")).to_be_visible()

    page.evaluate("() => addSourcePath('/tmp/card-b')")
    expect(page.locator("#destStructure")).to_be_hidden()

    page.locator("#btnPreview").click()
    expect(page.locator("#destStructure")).to_be_visible()

    page.locator("#sourceList .source-item button").first.click()
    expect(page.locator("#destStructure")).to_be_hidden()


def test_import_duplicate_stream_result_ignored_after_controls_change(
    live_server, page
):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__resolveDuplicates = null;
          window.__destinationPreviewCalled = false;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                files: [
                  {path: '/tmp/card-a/IMG_0001.jpg'},
                  {path: '/tmp/card-a/IMG_0002.jpg'},
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              return new Promise((resolve) => {
                window.__resolveDuplicates = () => {
                  const frame = 'data: ' + JSON.stringify({
                    duplicates: ['/tmp/card-a/IMG_0002.jpg'],
                    checked: 2,
                    total: 2,
                  }) + '\\n\\n' + 'data: ' + JSON.stringify({
                    done: true,
                    duplicate_count: 1,
                    checked: 2,
                    total: 2,
                  }) + '\\n\\n';
                  resolve(new Response(frame, {
                    status: 200,
                    headers: {'Content-Type': 'text/event-stream'},
                  }));
                };
              });
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              window.__destinationPreviewCalled = true;
              return Promise.resolve(new Response(JSON.stringify({folders: []}), {
                status: 200,
                headers: {'Content-Type': 'application/json'},
              }));
            }
            return originalFetch(input, init);
          };
          addSourcePath('/tmp/card-a');
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()
    page.wait_for_function("window.__resolveDuplicates !== null")
    page.locator("#fileTypePreset").select_option("custom")
    page.locator("#chkVerifyByHash").check()
    # The control changes schedule a legitimate automatic refresh after the
    # debounce interval. Cancel that future run so this assertion stays scoped
    # to the stale duplicate stream we are releasing, then synchronize on the
    # stale preview's own finally block instead of an arbitrary timeout.
    page.evaluate(
        """() => {
          clearScheduledImportPreview();
          window.__resolveDuplicates();
        }"""
    )
    expect(page.locator("#btnPreview")).to_be_enabled()

    assert page.evaluate("window.__destinationPreviewCalled") is False
    expect(page.locator("#destStructure")).to_be_hidden()


def test_import_preview_older_response_does_not_clobber_newer_summary(
    live_server, page
):
    # If an older previewImport() response arrives after a newer one has
    # already written the summary text, the older run's stale-signature
    # branch used to clear #previewSummary — erasing the newer run's
    # results. The importPreviewSeq guard makes the older run bail
    # without touching summary.
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          // First call to folder-preview stalls forever until we resolve
          // it manually; subsequent calls resolve immediately.
          window.__resolveOldPreview = null;
          window.__folderPreviewCallCount = 0;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              window.__folderPreviewCallCount += 1;
              const payload = new Response(JSON.stringify({
                files: [{path: '/tmp/card-a/IMG_0001.jpg'}],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
              if (window.__folderPreviewCallCount === 1) {
                return new Promise((resolve) => {
                  window.__resolveOldPreview = () => resolve(payload);
                });
              }
              return Promise.resolve(payload);
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              const frame = 'data: ' + JSON.stringify({
                done: true, duplicate_count: 0, checked: 1, total: 1,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                folders: [{
                  path: '2026/07/11',
                  full_path: '/archive/2026/07/11',
                  count: 1,
                  exists: false,
                }],
                total_photos: 1,
                total_folders: 1,
                new_folders: 1,
                existing_folders: 0,
                managed_archive: null,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
          addSourcePath('/tmp/card-a');
        }
        """
    )

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    # Fire preview #1 — its folder-preview fetch will hang.
    page.locator("#btnPreview").click()
    page.wait_for_function("window.__resolveOldPreview !== null")
    # Change a control that would flip the stale-signature branch, then
    # fire preview #2 — it resolves immediately and renders results.
    page.locator("#chkVerifyByHash").check()
    page.locator("#btnPreview").click()
    expect(page.locator("#previewSummary")).to_contain_text("1 to copy")
    # Now let the older, in-flight preview response come back. Its
    # signature no longer matches; the OLD code would clear the summary
    # here, wiping preview #2's rendered result.
    page.evaluate("() => window.__resolveOldPreview()")
    page.wait_for_timeout(100)
    expect(page.locator("#previewSummary")).to_contain_text("1 to copy")


def test_import_dest_structure_invalidation_survives_slow_startup(live_server, page):
    # initImportPage() awaits /api/volumes, /api/config, and
    # /api/workspaces/active before the rest of setup runs. If the
    # destination-structure invalidation listeners are wired after those
    # awaits, a user can render a structure preview and edit destInput
    # while startup is still blocked, leaving the stale structure
    # visible. Wiring the listeners synchronously (before any await)
    # closes that gap.
    url = live_server["url"]
    # Stall /api/volumes forever so initImportPage() never gets past its
    # first await. The listeners must already be installed by the time
    # the page becomes interactive.
    def volumes(route):
        # Never fulfill — playwright's route will hang the request.
        pass

    page.route("**/api/volumes", volumes)
    page.goto(f"{url}/import", wait_until="domcontentloaded")
    # Wait until initImportPage() has at least started so its synchronous
    # prefix (including wireDestStructureInvalidation) has run.
    page.wait_for_function(
        "() => typeof wireDestStructureInvalidation === 'function'"
    )
    # Fake a rendered destination structure — bypass the full preview
    # flow, since /api/volumes is stalled and the button pipeline would
    # await other startup state too. The invalidation contract is:
    # editing any of the wired controls hides #destStructure.
    page.evaluate(
        """
        () => {
          const el = document.getElementById('destStructure');
          el.innerHTML = '<div>fake structure</div>';
          el.style.display = '';
        }
        """
    )
    expect(page.locator("#destStructure")).to_be_visible()
    # Edit destInput — this is one of the controls wireDestStructureInvalidation
    # listens on. If the listener wasn't installed yet, the structure
    # would stay visible.
    page.locator("#destInput").fill("/archive")
    expect(page.locator("#destStructure")).to_be_hidden()


def test_import_copy_start_sends_restored_options(live_server, page):
    url = live_server["url"]
    captured = {}

    def remote_targets(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "rsync_available": True,
                "targets": [{
                    "id": "nas1",
                    "name": "Photo NAS",
                    "user": "photo",
                    "host": "nas.local",
                    "remote_path": "/srv/photos",
                    "mount_path": "/Volumes/photos",
                }],
            }),
        )

    def start_import(route):
        captured["body"] = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "job_id": "import-test",
                "workspace": {"id": 22, "name": "Kenya 2026"},
            }),
        )

    page.route("**/api/remote-targets", remote_targets)
    page.route("**/api/jobs/import-photos", start_import)
    page.goto(f"{url}/import")
    _suppress_auto_preview(page)

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#workspaceNew").check()
    page.locator("#newWorkspaceName").fill("Kenya 2026")
    page.locator("#destMode").select_option("remote:nas1")
    page.locator("#remoteSubpath").fill("2026/kenya")
    page.locator("#fileTypePreset").select_option("custom")
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.file-ext').forEach(el => { el.checked = false; });
          document.querySelector('.file-ext[value=".jpg"]').checked = true;
          document.querySelector('.file-ext[value=".nef"]').checked = true;
        }
        """
    )
    page.locator("#chkSkipDuplicates").uncheck()
    page.locator("#chkVerifyByHash").check()

    # Guards _suppress_auto_preview(): if the debounce ever escapes it, a
    # landed zero-file preview disables Start, and this reports that
    # directly instead of as a bare actionability timeout.
    expect(page.locator("#btnStart")).to_be_enabled()
    page.locator("#btnStart").click()
    expect(page.locator("#progressCard")).to_be_visible()

    body = captured["body"]
    assert body["sources"] == ["/tmp/card-a"]
    assert body["new_workspace_name"] == "Kenya 2026"
    assert body["remote_target_id"] == "nas1"
    assert body["remote_subpath"] == "2026/kenya"
    assert "destination" not in body
    assert body["file_types"] == [".jpg", ".nef"]
    assert body["skip_duplicates"] is False
    assert body["verify_by_hash"] is True
    # The After Import dropdown was untouched, and this is a new-workspace
    # import — the client must omit after_import so the server resolves the
    # default against the newly-created workspace instead of leaking the
    # previously-active workspace's pipeline.default_process_id.
    assert "after_import" not in body


def test_import_start_sends_common_tags_and_gps_location_option(
    live_server, page,
):
    url = live_server["url"]
    captured = {}

    def config_route(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "google_maps_api_key": "configured-for-test",
                "pipeline": {"default_process_id": None},
            }),
        )

    def start_import(route):
        captured["body"] = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"job_id": "import-tags-test"}),
        )

    page.route("**/api/config", config_route)
    page.route("**/api/jobs/import-in-place", start_import)
    page.goto(f"{url}/import")

    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#importTagInput").fill("Kenya trip")
    page.locator("#importTagInput").press("Enter")
    page.locator("#importTagInput").fill("Portfolio")
    page.locator("#btnAddImportTag").click()
    expect(page.locator("#importTagList .import-tag-chip")).to_have_count(2)
    page.locator("#chkLocationFromGps").check()

    page.locator("#btnStart").click()
    expect(page.locator("#progressCard")).to_be_visible()

    assert captured["body"]["tags"] == ["Kenya trip", "Portfolio"]
    assert captured["body"]["location_from_gps"] is True


def test_import_gps_location_option_explains_missing_api_key(live_server, page):
    url = live_server["url"]

    def config_route(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "google_maps_api_key": "",
                "pipeline": {"default_process_id": None},
            }),
        )

    page.route("**/api/config", config_route)
    page.goto(f"{url}/import")

    expect(page.locator("#chkLocationFromGps")).to_be_disabled()
    expect(page.locator("#locationGpsHint")).to_contain_text(
        "Add a Google Maps API key in Settings"
    )


def test_import_new_workspace_forwards_explicit_after_import(live_server, page):
    """When the user actively picks a saved process for a new-workspace
    import, the client must forward that pick as a process id — only the
    untouched-dropdown case is omitted so the server can apply the new
    workspace's default."""
    url = live_server["url"]
    db = live_server["db"]
    identify_id = next(
        p["id"] for p in db.get_saved_processes() if p["name"] == "Identify birds"
    )
    captured = {}

    def start_import(route):
        captured["body"] = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "job_id": "import-test",
                "workspace": {"id": 23, "name": "Serengeti"},
            }),
        )

    page.route("**/api/jobs/import-photos", start_import)
    page.goto(f"{url}/import")
    _suppress_auto_preview(page)

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#workspaceNew").check()
    page.locator("#newWorkspaceName").fill("Serengeti")
    page.locator("#destInput").fill("/tmp/archive")
    page.locator("#afterImportSelect").select_option(str(identify_id))

    # Guards _suppress_auto_preview(): if the debounce ever escapes it, a
    # landed zero-file preview disables Start, and this reports that
    # directly instead of as a bare actionability timeout.
    expect(page.locator("#btnStart")).to_be_enabled()
    page.locator("#btnStart").click()
    expect(page.locator("#progressCard")).to_be_visible()

    body = captured["body"]
    assert body["new_workspace_name"] == "Serengeti"
    assert body["after_import"] == identify_id


def test_import_new_workspace_shows_target_default_in_after_import_display(
    live_server, page
):
    """When 'New workspace' is picked and the After Import dropdown is
    untouched, the visible label must describe what the server will
    actually do — the global default that the freshly-created workspace
    inherits — rather than leaking the currently-active workspace's
    (possibly-overridden) prefilled selection."""
    url = live_server["url"]
    db = live_server["db"]
    procs_by_name = {p["name"]: p["id"] for p in db.get_saved_processes()}
    identify_id = procs_by_name["Identify birds"]
    quick_look_id = procs_by_name["Quick look"]

    def config_route(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "pipeline": {"default_process_id": identify_id},
            }),
        )

    def active_workspace_route(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": 1,
                "name": "Existing",
                "config_overrides": {
                    "pipeline": {"default_process_id": quick_look_id},
                },
            }),
        )

    page.route("**/api/config", config_route)
    page.route("**/api/workspaces/active", active_workspace_route)
    page.goto(f"{url}/import")

    # Before touching workspaceNew, the dropdown reflects the CURRENT
    # workspace's default (Quick look) — the source of the misleading
    # signal that this fix addresses.
    expect(page.locator("#afterImportSelect")).to_have_value(str(quick_look_id))

    page.locator("#workspaceNew").check()

    # After switching to new-workspace mode, the visible selection swaps
    # to a placeholder that names the GLOBAL default (Identify birds) —
    # matching what the server will actually apply to the freshly-created
    # workspace.
    expect(page.locator("#afterImportSelect")).to_have_value("__hidden_default__")
    placeholder = page.locator("#afterImportHiddenDefault")
    expect(placeholder).to_contain_text("New workspace default")
    expect(placeholder).to_contain_text("Identify birds")

    # Switching back to current-workspace mode without touching the
    # dropdown restores the current workspace's default rather than
    # sticking on the placeholder.
    page.locator("#workspaceCurrent").check()
    expect(page.locator("#afterImportSelect")).to_have_value(str(quick_look_id))


def test_import_browse_button_opens_folder_browser_fallback(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")

    page.locator("[data-testid='import-source-browse-btn']").click()

    browser = page.locator("[data-testid='import-folder-browser']")
    expect(browser).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#folderBrowserTitle")).to_have_text("Select Source Folders")
    expect(page.locator(".folder-browser-panel")).to_have_attribute("role", "dialog")
    expect(page.locator(".folder-browser-panel")).to_have_attribute("aria-modal", "true")
    expect(page.locator(".folder-browser-panel")).to_have_attribute(
        "aria-labelledby", "folderBrowserTitle")


def test_import_folder_browser_shows_recursive_photo_counts(live_server, page):
    """Source picker rows show the recursive count returned for each folder."""
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target === '/api/browse/photo-counts') {
              const body = JSON.parse(init.body || '{}');
              window.__folderCountRequest = body;
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

    page.locator("[data-testid='import-source-browse-btn']").click()

    rows = page.locator("#folderBrowserList .folder-browser-item[data-folder-path]")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator(".folder-browser-count")).to_have_text("1 photo")
    expect(rows.nth(1).locator(".folder-browser-count")).to_have_text("1,234 photos")
    expect(rows.nth(2).locator(".folder-browser-count")).to_be_empty()
    request = page.evaluate("window.__folderCountRequest")
    assert request["paths"] == ["/tmp/card-a", "/tmp/card-b", "/tmp/empty"]
    assert request["file_types"] == "both"


def test_import_folder_browser_selects_multiple_source_folders(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/browse') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                path: '/tmp',
                dirs: [
                  {name: 'card-a', path: '/tmp/card-a'},
                  {name: 'card-b', path: '/tmp/card-b'},
                  {name: 'card-c', path: '/tmp/card-c'},
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("[data-testid='import-source-browse-btn']").click()
    items = page.locator("#folderBrowserList .folder-browser-item[data-folder-path]")
    expect(items).to_have_count(3)

    items.nth(0).click()
    items.nth(2).click(modifiers=["Shift"])

    expect(page.locator("#folderBrowserSelectBtn")).to_have_text("Add 3 Folders")
    page.locator("#folderBrowserSelectBtn").click()

    source_list = page.locator("#sourceList")
    expect(source_list).to_contain_text("/tmp/card-a")
    expect(source_list).to_contain_text("/tmp/card-b")
    expect(source_list).to_contain_text("/tmp/card-c")


def test_import_folder_browser_toggles_discontiguous_source_folders(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/browse') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                path: '/tmp',
                dirs: [
                  {name: 'card-a', path: '/tmp/card-a'},
                  {name: 'card-b', path: '/tmp/card-b'},
                  {name: 'card-c', path: '/tmp/card-c'},
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("[data-testid='import-source-browse-btn']").click()
    items = page.locator("#folderBrowserList .folder-browser-item[data-folder-path]")
    expect(items).to_have_count(3)

    items.nth(0).click()
    items.nth(2).evaluate(
        """el => el.dispatchEvent(new MouseEvent('click', {
          bubbles: true,
          ctrlKey: true,
        }))"""
    )

    expect(page.locator("#folderBrowserSelectBtn")).to_have_text("Add 2 Folders")
    page.locator("#folderBrowserSelectBtn").click()

    source_list = page.locator("#sourceList")
    expect(source_list).to_contain_text("/tmp/card-a")
    expect(source_list).not_to_contain_text("/tmp/card-b")
    expect(source_list).to_contain_text("/tmp/card-c")


def test_import_folder_browser_selects_volumes_from_synthetic_root(live_server, page):
    # The Volumes shortcut renders /api/volumes as a synthetic root with
    # browserPath = ''. Volume rows are selectable, so the Add button must
    # enable once at least one is picked and must submit the selected drives
    # instead of the (empty) synthetic root.
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          Object.defineProperty(navigator, 'userAgent', {
            value: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
            configurable: true,
          });
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/volumes') >= 0) {
              return Promise.resolve(new Response(JSON.stringify([
                {name: 'Volume A', path: '/Volumes/A'},
                {name: 'Volume B', path: '/Volumes/B'},
              ]), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("[data-testid='import-source-browse-btn']").click()
    # Non-Mac branch fetches /api/volumes (stubbed) and renders the drives
    # as selectable rows in a synthetic root with browserPath = ''.
    page.evaluate("async () => { await browseImportFolderTo('__volumes__'); }")

    items = page.locator("#folderBrowserList .folder-browser-item[data-folder-path]")
    expect(items).to_have_count(2)

    select_btn = page.locator("#folderBrowserSelectBtn")
    expect(select_btn).to_be_disabled()

    items.nth(0).evaluate(
        """el => el.dispatchEvent(new MouseEvent('click', {
          bubbles: true,
          ctrlKey: true,
        }))"""
    )
    items.nth(1).evaluate(
        """el => el.dispatchEvent(new MouseEvent('click', {
          bubbles: true,
          ctrlKey: true,
        }))"""
    )

    expect(select_btn).to_be_enabled()
    expect(select_btn).to_have_text("Add 2 Folders")
    select_btn.click()

    source_list = page.locator("#sourceList")
    expect(source_list).to_contain_text("/Volumes/A")
    expect(source_list).to_contain_text("/Volumes/B")


def test_import_folder_browser_disables_select_while_pending(live_server, page):
    # A stale browserPath from a prior fetch used to remain selectable during
    # the next navigation. If the fetch stalled or failed, clicking "Select
    # This Folder" would submit the previous folder. Guard: the button must
    # be disabled while /api/browse is in flight and only re-enable once a
    # real path resolves.
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")
    page.evaluate(
        """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__releaseBrowse = null;
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/browse') === 0) {
              return new Promise((resolve) => {
                window.__releaseBrowse = () => resolve(new Response(
                  JSON.stringify({path: '/tmp/target', dirs: []}),
                  {status: 200, headers: {'Content-Type': 'application/json'}}
                ));
              });
            }
            return originalFetch(input, init);
          };
        }
        """
    )

    page.locator("[data-testid='import-source-browse-btn']").click()

    select_btn = page.locator("#folderBrowserSelectBtn")
    expect(select_btn).to_be_disabled()

    page.evaluate("() => window.__releaseBrowse && window.__releaseBrowse()")
    expect(select_btn).to_be_enabled()


def test_use_staging_as_import_source_forces_copy_mode(live_server, page):
    # Orphaned-staging recovery must copy the staging tree into the archive.
    # In-place would catalog paths that vanish when staging is cleaned up,
    # so useStagingAsImportSource() has to flip the mode to Copy regardless
    # of the page default.
    url = live_server["url"]
    page.goto(f"{url}/import")

    # Default is in_place — sanity check before invoking the recovery helper.
    expect(page.locator("#modeInPlace")).to_be_checked()

    page.evaluate(
        """
        () => useStagingAsImportSource({
          source_root: '/tmp/staging-src',
          inferred_destination: '/tmp/archive-dest',
        })
        """
    )

    expect(page.locator("#modeCopy")).to_be_checked()
    expect(page.locator("#modeInPlace")).not_to_be_checked()
    expect(page.locator("#destInput")).to_have_value("/tmp/archive-dest")
    expect(page.locator("#sourceList")).to_contain_text("/tmp/staging-src")
    # updateImportMode() must have run so the destination card is visible again.
    expect(page.locator("#destCard")).to_be_visible()


def test_import_menu_deep_link_opens_copy_mode_with_source_picker(live_server, page):
    # File > Import Folder... in the native menu routes to
    # /import?mode=copy&pick=source so the two File-menu import commands stay
    # distinct actions. In browser mode pickDirectory() returns null, so the
    # in-page folder browser must open instead of the native dialog.
    url = live_server["url"]
    page.goto(f"{url}/import?mode=copy&pick=source")

    expect(page.locator("#modeCopy")).to_be_checked()
    expect(page.locator("#destCard")).to_be_visible()
    expect(page.locator("[data-testid='import-folder-browser']")).to_have_class(
        re.compile(r"\bopen\b"))
    expect(page.locator("#folderBrowserTitle")).to_have_text("Select Source Folders")
    # pick is a one-shot trigger: it must be stripped from the URL so a manual
    # reload doesn't reopen the picker, while the mode param survives.
    page.wait_for_url(f"{url}/import?mode=copy")


def test_import_folder_browser_escape_closes_modal(live_server, page):
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate("window.pickDirectory = async () => null")

    page.locator("[data-testid='import-source-browse-btn']").click()
    expect(page.locator("[data-testid='import-folder-browser']")).to_have_class(
        re.compile(r"\bopen\b"))
    expect(page.locator(".folder-browser-close")).to_be_focused()

    page.keyboard.press("Escape")

    expect(page.locator("[data-testid='import-folder-browser']")).not_to_have_class(
        re.compile(r"\bopen\b"))


def test_import_destination_structure_hides_when_folder_browser_picks_destination(
    live_server, page
):
    # Selecting a destination via the folder browser assigns destInput.value
    # programmatically — input/change never fire — so the rendered structure
    # preview must be invalidated by the code path itself. Complements the
    # existing DOM-event and addSourcePath coverage with the fallback picker.
    url = live_server["url"]

    def folder_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"files": [{"path": "/tmp/card-a/IMG_0001.jpg"}]}),
        )

    def check_duplicates(route):
        frame = (
            "data: " + json.dumps({
                "done": True, "duplicate_count": 0, "checked": 1, "total": 1,
            }) + "\n\n"
        )
        route.fulfill(status=200, content_type="text/event-stream", body=frame)

    def destination_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "folders": [{
                    "path": "2026/07/11",
                    "full_path": "/archive/2026/07/11",
                    "count": 1,
                    "exists": False,
                }],
                "total_photos": 1,
                "total_folders": 1,
                "new_folders": 1,
                "existing_folders": 0,
                "managed_archive": None,
            }),
        )

    def browse(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/new-archive",
                "dirs": [{"name": "child", "path": "/new-archive/child"}],
            }),
        )

    page.route("**/api/import/folder-preview", folder_preview)
    page.route("**/api/import/check-duplicates", check_duplicates)
    page.route("**/api/import/destination-preview", destination_preview)
    page.route("**/api/browse**", browse)
    page.goto(f"{url}/import")
    # Force the folder-browser fallback (no native picker) so the Select
    # button assigns destInput.value directly.
    page.evaluate("window.pickDirectory = async () => null")

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()
    expect(page.locator("#destStructure")).to_be_visible()

    page.locator("[data-testid='import-destination-browse-btn']").click()
    expect(page.locator("[data-testid='import-folder-browser']")).to_have_class(
        re.compile(r"\bopen\b"))
    page.locator("#folderBrowserSelectBtn").click()

    expect(page.locator("#destInput")).to_have_value("/new-archive")
    expect(page.locator("#destStructure")).to_be_hidden()


def test_import_folder_template_examples_retire_with_their_source(
    live_server, page,
):
    """Removing the source must clear the folder-template examples.

    Each preset is labelled with an example folder name taken from the files
    being imported. Once those files are gone the example describes nothing —
    and a label that reads "this is what my folder will be called" must never
    outlive the data behind it. Removing the last source hides the structure
    table without re-running the destination preview, so the reset has to live
    on the shared invalidation path, not in the render.
    """
    url = live_server["url"]

    def folder_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"files": [{"path": "/tmp/card-a/IMG_0001.jpg"}]}),
        )

    def check_duplicates(route):
        route.fulfill(
            status=200,
            content_type="text/event-stream",
            body="data: " + json.dumps({
                "done": True, "duplicate_count": 0, "checked": 1, "total": 1,
            }) + "\n\n",
        )

    def destination_preview(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "folders": [
                    {"path": "2026/2026-07-01",
                     "full_path": "/archive/2026/2026-07-01",
                     "count": 1, "exists": False},
                ],
                "total_photos": 1,
                "total_folders": 1,
                "new_folders": 1,
                "existing_folders": 0,
                "managed_archive": None,
                "template_samples": {
                    "samples": {
                        "%Y/%Y-%m-%d": "2026/2026-07-01",
                        "%Y/%m/%d": "2026/07/01",
                        "%Y/%m": "2026/07",
                        "%Y": "2026",
                        "%Y-%m-%d": "2026-07-01",
                    },
                    "sample_date": "2026-07-01T09:00:00",
                    "dated_count": 1,
                },
            }),
        )

    page.route("**/api/import/folder-preview", folder_preview)
    page.route("**/api/import/check-duplicates", check_duplicates)
    page.route("**/api/import/destination-preview", destination_preview)
    page.goto(f"{url}/import")

    preset = page.locator("#folderTemplatePreset")
    # Ships with no example: nothing is known about the files yet.
    expect(preset.locator("option[value='%Y/%Y-%m-%d']")).to_have_text(
        "%Y/%Y-%m-%d")

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#destInput").fill("/archive")
    page.locator("#btnPreview").click()

    expect(page.locator("#destStructure")).to_be_visible()
    # The example is a folder this import really creates — the first row.
    expect(preset.locator("option[value='%Y/%Y-%m-%d']")).to_have_text(
        "%Y/%Y-%m-%d — 2026/2026-07-01")
    expect(preset.locator("option[value='%Y-%m-%d']")).to_have_text(
        "%Y-%m-%d — 2026-07-01")

    # Remove the source the way a user does: the × on the source row.
    page.locator("#sourceList button").last.click()

    expect(page.locator("#destStructure")).to_be_hidden()
    expect(preset.locator("option[value='%Y/%Y-%m-%d']")).to_have_text(
        "%Y/%Y-%m-%d")
    expect(preset.locator("option[value='%Y-%m-%d']")).to_have_text("%Y-%m-%d")


def _stub_preview(page, files, duplicates=None):
    """Stub folder-preview + check-duplicates, and put the page in COPY mode.

    Two things here are load-bearing:
      - #modeInPlace is `checked` by default (import.html:232). Without
        selecting copy mode, previewImport() returns early at the
        `if (!copyMode)` branch, no duplicate stream runs, and per Task 11
        the checkboxes are hidden entirely — every selection test would
        fail for the wrong reason.
      - check-duplicates is SSE, not newline-JSON. The client parses
        `buffer.split('\n\n')` + /^data: (.+)$/m. Frames must be
        `data: {...}\n\n`. Copied from the existing stub above.
    """
    page.locator("#modeCopy").check()
    page.evaluate(
        """
        ([files, dupes]) => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: files.length, total_size: 0,
                type_breakdown: {'.jpg': files.length},
                duplicate_count: 0, files: files,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (t && t.indexOf('/api/import/check-duplicates') === 0) {
              const frame = 'data: ' + JSON.stringify({
                duplicates: dupes, checked: files.length, total: files.length,
              }) + '\\n\\n' + 'data: ' + JSON.stringify({
                done: true, duplicate_count: dupes.length,
                checked: files.length, total: files.length,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            return originalFetch(input, init);
          };
          window.pickDirectory = async () => ['/tmp/card'];
        }
        """,
        [files, duplicates or []],
    )


def _preview(page):
    """Add the stubbed source and wait for the grid to settle."""
    page.locator("[data-testid='import-source-browse-btn']").click()
    page.locator("#btnPreview").click()
    expect(page.locator("#importPreviewGrid")).to_be_visible()


def _files(n, prefix='/tmp/card/DSC_'):
    return [{"path": f"{prefix}{i:04d}.jpg", "filename": f"DSC_{i:04d}.jpg",
             "subfolder": "card", "size": 100, "extension": ".jpg",
             "mtime": 0, "thumb_url": ""} for i in range(n)]


def test_import_preview_files_are_checked_by_default(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    expect(boxes).to_have_count(3)
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()


def test_import_preview_duplicates_are_unchecked_and_disabled(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    dupe = page.locator(
        f".import-preview-thumb[data-path='{files[1]['path']}'] .thumb-check")
    expect(dupe).not_to_be_checked()
    expect(dupe).to_be_disabled()


def test_import_preview_deselection_survives_a_later_render(live_server, page):
    """A hand-picked deselection must outlive the next render pass.

    renderImportPreviewGrid runs up to three times per preview (files only,
    then with duplicate verdicts, then with destination data). Checkbox state
    is DERIVED from importDeselected on every pass rather than seeded, so a
    late-arriving pass re-computes the same answer instead of stomping the
    user's click. This drives the third pass the way import.html itself does.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    def box(path):
        return page.locator(
            f".import-preview-thumb[data-path='{path}'] .thumb-check")

    box(files[0]["path"]).uncheck()
    expect(box(files[0]["path"])).not_to_be_checked()

    # The duplicate verdict is an eligibility overlay, not user intent: it
    # must never be written into importDeselected.
    assert page.evaluate("() => Array.from(importDeselected)") == [
        files[0]["path"]]

    # Re-render with the same inputs, as the destination-data pass does.
    page.evaluate(
        "([files, dupes]) => renderImportPreviewGrid(files, dupes, null)",
        [files, [files[1]["path"]]],
    )

    expect(box(files[0]["path"])).not_to_be_checked()
    expect(box(files[0]["path"])).to_be_enabled()
    expect(box(files[1]["path"])).not_to_be_checked()
    expect(box(files[1]["path"])).to_be_disabled()
    expect(box(files[2]["path"])).to_be_checked()

    # Black-box proof of the same separation: drop the eligibility overlay
    # and the duplicate comes back checked. If the verdict had been written
    # into importDeselected, it would survive as intent and stay unchecked.
    # (Set the control directly: .uncheck() fires change, which reruns the
    # whole preview and would rebuild state from scratch.)
    page.evaluate(
        "() => { document.getElementById('chkSkipDuplicates').checked ="
        " false; }")
    page.evaluate(
        "([files, dupes]) => renderImportPreviewGrid(files, dupes, null)",
        [files, [files[1]["path"]]],
    )
    expect(box(files[1]["path"])).to_be_checked()
    expect(box(files[1]["path"])).to_be_enabled()
    # ...and the hand-picked deselection is still intent, so it survives.
    expect(box(files[0]["path"])).not_to_be_checked()


def _box(page, path):
    return page.locator(
        f".import-preview-thumb[data-path='{path}'] .thumb-check")


def _drop_skip_duplicates_and_rerender(page, files, dupes):
    """Turn the duplicate overlay off and re-render, without a re-preview.

    Reveals whether a bulk operation wrote duplicate verdicts into
    importDeselected: once the overlay is gone, anything still unchecked is
    intent. Uses a direct property write rather than .uncheck() so no change
    event fires and previewImport() doesn't rebuild the state under us.
    """
    page.evaluate(
        "() => { document.getElementById('chkSkipDuplicates').checked ="
        " false; }")
    page.evaluate(
        "([files, dupes]) => renderImportPreviewGrid(files, dupes, null)",
        [files, dupes],
    )


def test_shift_click_selects_a_contiguous_range(live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(5)
    _stub_preview(page, files)
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(1).click()                          # uncheck index 1
    boxes.nth(3).click(modifiers=["Shift"])       # range 1..3 unchecked
    for i, want in enumerate([True, False, False, False, True]):
        if want:
            expect(boxes.nth(i)).to_be_checked()
        else:
            expect(boxes.nth(i)).not_to_be_checked()


def test_shift_click_range_runs_backwards_too(live_server, page):
    """Anchor after target. The range is min..max, not anchor..target."""
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(5))
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(3).click()                          # anchor at index 3
    boxes.nth(1).click(modifiers=["Shift"])       # range 1..3 unchecked
    for i, want in enumerate([True, False, False, False, True]):
        if want:
            expect(boxes.nth(i)).to_be_checked()
        else:
            expect(boxes.nth(i)).not_to_be_checked()


def test_shift_click_moves_the_anchor(live_server, page):
    """A shift-click re-anchors, so the next range starts where it ended."""
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(5))
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(0).click()                          # anchor 0, uncheck 0
    boxes.nth(4).click(modifiers=["Shift"])       # 0..4 unchecked, anchor 4
    for i in range(5):
        expect(boxes.nth(i)).not_to_be_checked()

    boxes.nth(2).click(modifiers=["Shift"])       # re-check 2..4, not 0..2
    for i, want in enumerate([False, False, True, True, True]):
        if want:
            expect(boxes.nth(i)).to_be_checked()
        else:
            expect(boxes.nth(i)).not_to_be_checked()


def test_shift_click_range_follows_render_order_not_api_order(
        live_server, page):
    """The range is over what the user dragged across, not over the payload.

    renderImportPreviewGrid groups by subfolder and sorts the group keys, so
    render order diverges from the API's file order whenever subfolders don't
    arrive alphabetically. Here the API returns b/ first; the grid shows a/
    first. A range computed from importPreviewedPaths would toggle b/0001 --
    a card the user never dragged across -- and leave a/0001 alone.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(2, prefix='/tmp/card/b/DSC_') + _files(
        2, prefix='/tmp/card/a/DSC_')
    for f in files[:2]:
        f["subfolder"] = "b"
    for f in files[2:]:
        f["subfolder"] = "a"
    _stub_preview(page, files)
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    # Rendered: a/0000, a/0001, b/0000, b/0001.
    assert page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#importPreviewGrid .import-preview-thumb')).map(e => e.dataset.path)"
    ) == [files[2]["path"], files[3]["path"],
          files[0]["path"], files[1]["path"]]

    boxes.nth(0).click()
    boxes.nth(2).click(modifiers=["Shift"])   # a/0000 .. b/0000
    for i, want in enumerate([False, False, False, True]):
        if want:
            expect(boxes.nth(i)).to_be_checked()
        else:
            expect(boxes.nth(i)).not_to_be_checked()


def test_shift_range_does_not_deselect_ineligible_duplicates(
        live_server, page):
    """A range dragged across a skipped duplicate must not record intent.

    While Skip duplicates is on the card is disabled either way, so the DOM
    can't tell the two designs apart — turn the overlay off and look again.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(4)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(0).click()
    boxes.nth(2).click(modifiers=["Shift"])       # range spans the duplicate

    _drop_skip_duplicates_and_rerender(page, files, [files[1]["path"]])
    expect(_box(page, files[1]["path"])).to_be_checked()
    expect(_box(page, files[0]["path"])).not_to_be_checked()
    expect(_box(page, files[2]["path"])).not_to_be_checked()


def test_folder_header_checkbox_toggles_its_subfolder(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.locator(".import-preview-folder-header .folder-check").first.click()
    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).not_to_be_checked()


def test_folder_header_only_toggles_its_own_subfolder(live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = (_files(2, prefix='/tmp/card/a/DSC_')
             + _files(2, prefix='/tmp/card/b/DSC_'))
    for f in files[:2]:
        f["subfolder"] = "a"
    for f in files[2:]:
        f["subfolder"] = "b"
    _stub_preview(page, files)
    _preview(page)

    headers = page.locator(".import-preview-folder-header .folder-check")
    expect(headers).to_have_count(2)
    headers.nth(0).click()
    for f in files[:2]:
        expect(_box(page, f["path"])).not_to_be_checked()
    for f in files[2:]:
        expect(_box(page, f["path"])).to_be_checked()


def test_folder_header_is_indeterminate_on_a_partial_selection(
        live_server, page):
    """The tri-state is the honest readout: some != all, and != none.

    "All" semantics, matching chkSelectAll on pipeline.html: a partial
    selection is dashed and NOT ticked.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    header = page.locator(".import-preview-folder-header .folder-check").first
    expect(header).to_be_checked()
    expect(header).to_have_js_property("indeterminate", False)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(0).click()
    expect(header).not_to_be_checked()
    expect(header).to_have_js_property("indeterminate", True)

    boxes.nth(1).click()
    boxes.nth(2).click()
    expect(header).not_to_be_checked()
    expect(header).to_have_js_property("indeterminate", False)


def test_folder_header_ignores_skipped_duplicates_in_its_tally(
        live_server, page):
    """An ineligible duplicate is not an unselected file.

    Two eligible files both checked plus one skipped duplicate must read as
    "all", not as a partial selection...
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    header = page.locator(".import-preview-folder-header .folder-check").first
    expect(header).to_be_checked()
    expect(header).to_have_js_property("indeterminate", False)

    # ...and with both eligible files deselected it must read as "none". This
    # is the case that actually pins the tally's duplicate filter: with the
    # duplicate counted as a selected file the header would claim a partial
    # selection the user cannot act on.
    _box(page, files[0]["path"]).click()
    _box(page, files[2]["path"]).click()
    expect(header).not_to_be_checked()
    expect(header).to_have_js_property("indeterminate", False)


def test_folder_header_does_not_deselect_ineligible_duplicates(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    page.locator(".import-preview-folder-header .folder-check").first.click()

    _drop_skip_duplicates_and_rerender(page, files, [files[1]["path"]])
    expect(_box(page, files[0]["path"])).not_to_be_checked()
    expect(_box(page, files[1]["path"])).to_be_checked()
    expect(_box(page, files[2]["path"])).not_to_be_checked()


def test_select_all_toggles_every_file_and_reports_the_count(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    expect(page.locator("#selectAllRow")).to_be_visible()
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "3 of 3 selected")

    page.locator("#chkSelectAllImport").uncheck()
    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).not_to_be_checked()
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "0 of 3 selected")

    page.locator("#chkSelectAllImport").check()
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "3 of 3 selected")


def test_select_all_box_reflects_a_partial_selection(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    master = page.locator("#chkSelectAllImport")
    expect(master).to_be_checked()
    expect(master).to_have_js_property("indeterminate", False)

    page.locator(".import-preview-thumb .thumb-check").nth(0).click()
    expect(master).not_to_be_checked()
    expect(master).to_have_js_property("indeterminate", True)


def test_clicking_a_dashed_select_all_selects_the_rest(live_server, page):
    """Tri-state direction, pinned deliberately.

    "All" semantics (chkSelectAll on pipeline.html:1955 uses the same rule),
    so a dashed master is unticked and clicking it completes the selection
    rather than discarding the partial one.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(0).click()
    master = page.locator("#chkSelectAllImport")
    expect(master).to_have_js_property("indeterminate", True)

    master.click()
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()
    expect(master).to_be_checked()
    expect(master).to_have_js_property("indeterminate", False)


def test_select_all_counts_against_the_eligible_files_not_the_total(
        live_server, page):
    """Skipped duplicates are not unselected files.

    Every selectable file on: the master must read as full, not dashed. Its
    denominator is the eligible count, while the readout beside it stays
    "N of <discovered>" to match the summary line above.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    master = page.locator("#chkSelectAllImport")
    expect(master).to_be_checked()
    expect(master).to_have_js_property("indeterminate", False)
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "2 of 3 selected")


def test_bulk_checkboxes_are_disabled_when_nothing_is_selectable(
        live_server, page):
    """All duplicates: a live control that does nothing is a black box."""
    page.goto(f"{live_server['url']}/import")
    files = _files(2)
    _stub_preview(page, files, duplicates=[f["path"] for f in files])
    _preview(page)

    master = page.locator("#chkSelectAllImport")
    header = page.locator(".import-preview-folder-header .folder-check").first
    expect(master).to_be_disabled()
    expect(master).not_to_be_checked()
    expect(header).to_be_disabled()
    expect(header).not_to_be_checked()
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "0 of 2 selected")


def test_select_all_does_not_deselect_ineligible_duplicates(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    page.locator("#chkSelectAllImport").uncheck()

    _drop_skip_duplicates_and_rerender(page, files, [files[1]["path"]])
    expect(_box(page, files[0]["path"])).not_to_be_checked()
    expect(_box(page, files[1]["path"])).to_be_checked()
    expect(_box(page, files[2]["path"])).not_to_be_checked()


def test_a_stale_duplicate_verdict_cannot_disable_a_fresh_card(
        live_server, page):
    """A retained duplicate verdict outlives the render it came from.

    Verdicts only exist once the whole check-duplicates stream drains, so a
    re-preview's FIRST render pass draws every card enabled while the
    previous run's verdicts are the only ones anything could have kept.
    Anything that re-derives checkbox state from a retained set instead of
    from the card turns an unrelated click into a disabled, unchecked box
    with no badge explaining why -- and over SMB with verify_by_hash the
    window between the first pass and the new verdicts is minutes.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)
    expect(_box(page, files[1]["path"])).to_be_disabled()

    # The re-preview's first pass: same files, no verdicts yet.
    page.evaluate("(f) => renderImportPreviewGrid(f, [], null)", files)
    for f in files:
        expect(_box(page, f["path"])).to_be_enabled()
        expect(_box(page, f["path"])).to_be_checked()

    # A click anywhere refreshes every box. The stale verdict must not
    # resurrect and disable a card that carries no badge.
    _box(page, files[0]["path"]).click()
    expect(_box(page, files[1]["path"])).to_be_enabled()
    expect(_box(page, files[1]["path"])).to_be_checked()
    expect(_box(page, files[2]["path"])).to_be_checked()
    expect(page.locator(".import-preview-badge")).to_have_count(0)
    header = page.locator(".import-preview-folder-header .folder-check").first
    expect(header).to_be_enabled()
    expect(header).to_have_js_property("indeterminate", True)


def test_a_stale_duplicate_verdict_cannot_shrink_a_bulk_toggle(
        live_server, page):
    """Same stale window, the bulk paths.

    A bulk toggle that reads a retained verdict set would quietly skip a card
    the renderer drew as an ordinary selectable file: the user hits
    "deselect all" and one box stays ticked with nothing to explain it.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    page.evaluate("(f) => renderImportPreviewGrid(f, [], null)", files)

    page.locator("#chkSelectAllImport").uncheck()
    for f in files:
        expect(_box(page, f["path"])).not_to_be_checked()

    page.locator(".import-preview-folder-header .folder-check").first.check()
    for f in files:
        expect(_box(page, f["path"])).to_be_checked()


def test_a_stale_duplicate_verdict_cannot_kill_the_master_checkbox(
        live_server, page):
    """The counters are the third way a stale verdict reaches the screen.

    An all-duplicate previous run leaves verdicts covering every path.
    Counters reading a retained set report zero eligible over a grid of
    ticked, enabled, badge-free boxes -- which prints "0 of 2 selected" and,
    since fix 6 disables the master at zero eligible, hands the user a dead
    control captioned "every discovered file is a duplicate" while two
    perfectly selectable files sit under it.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(2)
    _stub_preview(page, files, duplicates=[f["path"] for f in files])
    _preview(page)
    expect(page.locator("#chkSelectAllImport")).to_be_disabled()

    # The re-preview's first pass: same files, no verdicts yet.
    page.evaluate("(f) => renderImportPreviewGrid(f, [], null)", files)
    page.evaluate("() => updateImportSelectionUI()")

    master = page.locator("#chkSelectAllImport")
    expect(master).to_be_enabled()
    expect(master).to_be_checked()
    expect(master).to_have_js_property("indeterminate", False)
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "2 of 2 selected")
    expect(page.locator(".import-preview-badge")).to_have_count(0)


def test_folder_checkbox_tooltip_names_its_folder(live_server, page):
    """Two headers, two tooltips -- "this folder" tells the user nothing."""
    page.goto(f"{live_server['url']}/import")
    files = (_files(1, prefix='/tmp/card/a/DSC_')
             + _files(1, prefix='/tmp/card/b/DSC_'))
    files[0]["subfolder"] = "a"
    files[1]["subfolder"] = "b"
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    headers = page.locator(".import-preview-folder-header .folder-check")
    expect(headers.nth(0)).to_have_attribute(
        "title", "Select or deselect every file in a")
    expect(headers.nth(1)).to_have_attribute(
        "title", "Every file in b is a duplicate that will be skipped")


def test_a_single_render_pass_produces_a_correct_folder_header(
        live_server, page):
    """One render must be enough.

    #importPreviewGrid ships display:none and previewImport() re-hides it
    before every run, so anything that reads layout to build the header
    tally has to run after the grid is shown again. A later pass would
    paper over a mistake here, but a later pass is not guaranteed: with
    Skip duplicates OFF, the only re-render is the one gated on
    `destData && destData.files`, and renderDestStructure() returns null
    before it even fetches whenever the destination doesn't resolve. That
    leaves the files-only pass as the only one there is, and the user with
    a dead, unticked header over a fully selected folder.

    (With Skip duplicates ON there are always at least two passes -- the
    stream-drained render is unconditional -- so that is NOT the case this
    guards.)

    Copy mode explicitly, since the header checkbox only exists there, and
    the renderer is driven directly so the assertion lands on exactly one
    pass rather than on whichever one previewImport() happens to end with.
    """
    page.goto(f"{live_server['url']}/import")
    # page.goto() returns on `load`, but initImportPage() is still awaiting
    # /api/volumes, /api/config and /api/workspaces/active at that point, and
    # its trailing updateImportMode() calls clearImportPreviewGrid() ->
    # grid.innerHTML = ''. This test drives the renderer directly, so it has
    # no network round trip of its own to hide behind and the wipe lands
    # between the render and the assertions -- the header vanishes and the
    # last expect() fails with "element(s) not found". Measured on this
    # machine: 16/30 without this line, 30/30 with it.
    page.wait_for_load_state("networkidle")
    _suppress_auto_preview(page)
    page.locator("#modeCopy").check()
    expect(page.locator("#importPreviewGrid")).to_be_hidden()
    page.evaluate("(f) => renderImportPreviewGrid(f, [], null)", _files(3))

    header = page.locator(".import-preview-folder-header .folder-check").first
    expect(header).to_be_enabled()
    expect(header).to_be_checked()
    expect(header).to_have_js_property("indeterminate", False)


def test_shift_range_does_not_reach_through_a_hidden_card(live_server, page):
    """The sibling hide-duplicates branch hides cards rather than re-render.

    A range is what the user dragged across on screen, so a card that isn't
    on screen must not be swept up by it. Hidden here the way a CSS filter
    would hide it, since that branch hasn't merged yet.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(4)
    _stub_preview(page, files)
    _preview(page)

    hide = ("(p) => { document.querySelector("
            "`.import-preview-thumb[data-path='${p}']`).style.display = 'X'; }")
    page.evaluate(hide.replace("'X'", "'none'"), files[1]["path"])

    _box(page, files[0]["path"]).click()
    _box(page, files[2]["path"]).click(modifiers=["Shift"])

    page.evaluate(hide.replace("'X'", "''"), files[1]["path"])
    expect(_box(page, files[0]["path"])).not_to_be_checked()
    expect(_box(page, files[1]["path"])).to_be_checked()
    expect(_box(page, files[2]["path"])).not_to_be_checked()
    expect(_box(page, files[3]["path"])).to_be_checked()


def test_duplicate_badge_and_checkbox_do_not_overlap(live_server, page):
    """Both must stay legible: the box says it's off, the badge says why."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    card = page.locator(
        f".import-preview-thumb[data-path='{files[1]['path']}']")
    check = card.locator(".thumb-check")
    badge = card.locator(".import-preview-badge")
    expect(check).to_be_visible()
    expect(badge).to_be_visible()
    expect(badge).to_have_text("Duplicate")

    cb = check.bounding_box()
    bb = badge.bounding_box()
    assert cb["width"] > 0 and bb["width"] > 0
    horizontal_gap = (cb["x"] + cb["width"] <= bb["x"]
                      or bb["x"] + bb["width"] <= cb["x"])
    vertical_gap = (cb["y"] + cb["height"] <= bb["y"]
                    or bb["y"] + bb["height"] <= cb["y"])
    assert horizontal_gap or vertical_gap, (
        f"checkbox {cb} overlaps duplicate badge {bb}")


# --- Task 10: Start gating and the preview lifecycle -----------------------


def test_start_is_disabled_when_all_importable_files_are_unchecked(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.locator("#chkSelectAllImport").click()   # select none
    # Assert the LABEL as well as `disabled`. Start is transiently disabled
    # while a preview runs and while the duplicate stream drains, so
    # `to_be_disabled()` on its own can pass on a state that has nothing to do
    # with the selection and then settle to enabled.
    expect(page.locator("#btnStart")).to_have_text("No files selected")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_stays_enabled_when_every_file_is_a_duplicate(live_server, page):
    """Zero checked, zero eligible — the user must still be able to run the
    import to get the safe-to-format verdict on an already-archived card."""
    page.goto(f"{live_server['url']}/import")
    files = _files(2)
    _stub_preview(page, files, duplicates=[f["path"] for f in files])
    _preview(page)

    # The count settles the timing: _preview() returns on the FIRST render
    # pass, before any verdict has landed, and at that moment both files look
    # eligible. Waiting for "0 files" means the assertion below is made
    # against the all-duplicate state and not the pre-verdict one.
    expect(page.locator("#btnStart")).to_have_text("Start import (0 files)")
    expect(page.locator("#btnStart")).to_be_enabled()


def test_changing_a_source_after_selecting_disables_start(live_server, page):
    """Toggling a signature input must gate Start immediately.

    #chkRecursive is wired into wireDestStructureInvalidation, which fires
    scheduleImportPreview() on a 350ms debounce. Suppress that entirely: if the
    auto-preview starts, updateStartGate checks importPreviewInFlight BEFORE
    staleness and the label reads "Previewing…" instead, making the assertion
    timing-dependent.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    page.evaluate("() => { window.scheduleImportPreview = () => {}; }")
    page.locator("#chkRecursive").click()   # invalidates the signature
    # Label first, like every other gating assertion here: `disabled` alone
    # matches several unrelated states this test doesn't mean.
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_is_disabled_while_a_preview_is_in_flight(live_server, page):
    """The window between clearImportPreviewGrid() and the render is the
    5,000-file hazard: state must not reset to 'no preview run' there."""
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.__release = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return new Promise((res) => { window.__release = res; });
            }
            return f(input, init);
          };
        }"""
    )
    page.locator("#btnPreview").click()
    expect(page.locator("#btnStart")).to_have_text("Previewing…")
    expect(page.locator("#btnStart")).to_be_disabled()
    # The prior selection survives the in-flight window.
    assert page.evaluate("() => importDeselected.size") == 1


def test_start_is_disabled_while_the_duplicate_stream_is_draining(
        live_server, page):
    """Checkbox eligibility is not final until verdicts land, so submitting
    mid-stream would send an include_paths that doesn't match the screen."""
    page.goto(f"{live_server['url']}/import")
    page.locator("#modeCopy").check()
    page.evaluate(
        """(files) => {
          const f = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: files.length, total_size: 0,
                type_breakdown: {'.jpg': files.length},
                duplicate_count: 0, files: files,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (t && t.indexOf('/api/import/check-duplicates') === 0) {
              return new Promise(() => {});   // stream never drains
            }
            return f(input, init);
          };
          window.pickDirectory = async () => ['/tmp/card'];
        }""",
        _files(3),
    )
    _preview(page)
    expect(page.locator("#btnStart")).to_have_text("Checking duplicates…")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_zero_file_preview_disables_start(live_server, page):
    """A completed preview that found nothing is NOT 'no preview run' —
    otherwise later arrivals would be imported unseen.

    _preview() can't be used here: it waits for the grid to become visible
    and a zero-file preview never renders one.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, [])
    page.locator("[data-testid='import-source-browse-btn']").click()
    page.locator("#btnPreview").click()
    expect(page.locator("#previewSummary")).to_have_text(
        "No importable files found.")
    expect(page.locator("#btnStart")).to_have_text("No files to import")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_with_skip_duplicates_off_every_card_is_checked_and_enabled(
        live_server, page):
    """The duplicate stream returns early in this mode, so there are no
    verdicts and the derived-checked rule yields all-on."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    page.locator("#chkSkipDuplicates").uncheck()
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()
        expect(boxes.nth(i)).to_be_enabled()


def test_selected_count_readout(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(4))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    expect(page.locator("#previewSelectedCount")).to_have_text("3 of 4 selected")


def test_a_failed_preview_disables_start(live_server, page):
    """previewImport() clears the grid before it fetches, so a preview that
    throws leaves an EMPTY screen behind a signature that still matches.

    Without an explicit failure state the gate reads "not stale, nothing
    eligible" and re-enables Start over a grid the user can no longer see.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(
                JSON.stringify({error: 'disk went away'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
        }"""
    )
    page.locator("#btnPreview").click()
    expect(page.locator("#importPreviewGrid")).not_to_be_visible()
    # The LABEL, not just `disabled`: previewImport() disables Start the
    # moment it starts, so asserting `disabled` alone passes on the in-flight
    # state and never observes what the failure settles to.
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_a_superseded_preview_does_not_reopen_the_gate(live_server, page):
    """An older run finishing must not clear the newer run's in-flight flag.

    Both runs share the module-level lifecycle flags, so the older run's
    `finally` has to check that it still owns them — otherwise a slow first
    walk completing hands the user an enabled Start while the second walk is
    still running, and the screen is blank.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.__rejects = [];
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return new Promise((res, rej) => { window.__rejects.push(rej); });
            }
            return f(input, init);
          };
          previewImport();
          previewImport();
        }"""
    )
    # Run 1 fails; run 2 is still walking the disk and owns the gate.
    page.evaluate("() => window.__rejects[0](new Error('card ejected'))")
    expect(page.locator("#btnStart")).to_have_text("Previewing…")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_is_disabled_while_an_import_is_running(live_server, page):
    """No second submission while one import is in flight.

    Two distinct states have to close the button: the POST is in flight and
    activeJobId is still null, and then the job id has arrived. Both read
    "Importing…" to the user; only the gate distinguishes them.
    """
    page.goto(f"{live_server['url']}/import")
    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.__resolveStart = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-in-place') === 0) {
              return new Promise((res) => { window.__resolveStart = res; });
            }
            return f(input, init);
          };
          // Keep the job alive: a real stream would complete and re-enable.
          window.EventSource = function () {
            this.close = () => {};
            this.addEventListener = () => {};
          };
          window.scheduleImportPreview = () => {};
        }"""
    )
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#btnStart").click()

    # The POST has not answered yet, so activeJobId is still null.
    expect(page.locator("#btnStart")).to_have_text("Importing…")
    expect(page.locator("#btnStart")).to_be_disabled()

    page.evaluate(
        """() => window.__resolveStart(new Response(
             JSON.stringify({job_id: 'gate-test'}),
             {status: 200, headers: {'Content-Type': 'application/json'}}))"""
    )
    # #progressCard is shown in the same synchronous block that sets
    # activeJobId, so waiting on it means the assertions below observe the
    # job-id state rather than the still-true pending flag.
    expect(page.locator("#progressCard")).to_be_visible()
    expect(page.locator("#btnStart")).to_have_text("Importing…")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_is_re_enabled_when_the_import_finishes(live_server, page):
    """finishJob() owns the end of the import.

    The gate reads activeJobId, so finishJob has to drop it — otherwise the
    button that used to be re-enabled by hand stays shut for the rest of the
    page's life and the user has to reload to import again.
    """
    page.goto(f"{live_server['url']}/import")
    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-in-place') === 0) {
              return Promise.resolve(new Response(
                JSON.stringify({job_id: 'gate-done'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (t && t.indexOf('/api/jobs/gate-done') === 0) {
              return Promise.resolve(new Response(
                JSON.stringify({status: 'completed'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
          window.EventSource = function () {
            this.close = () => {};
            this.addEventListener = (name, fn) => {
              if (name === 'complete') setTimeout(() => fn({}), 0);
            };
          };
          window.scheduleImportPreview = () => {};
        }"""
    )
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#btnStart").click()

    # The job really started: #progressCard is shown in the same synchronous
    # block that sets activeJobId. Without this the assertions below could
    # pass on the never-clicked initial state.
    expect(page.locator("#progressCard")).to_be_visible()
    # ...and finishJob() dropped it again. (The error banner would be the
    # more direct proof that finishJob ran, but initImportPage() calls
    # updateImportMode() -> showError('') after several awaits, so on a busy
    # machine page startup can wipe the banner after the fact.)
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_enabled()


def test_a_completed_preview_replaces_the_previous_selection(live_server, page):
    """A finished preview OWNS the selection.

    Deselections are recorded against the paths a particular preview
    discovered. Carrying them into the next preview would silently exclude
    files from a run the user never made that choice for -- and the boxes on
    screen would agree, because they're derived from the same set.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()
    expect(page.locator("#previewSelectedCount")).to_have_text("2 of 3 selected")

    page.locator("#btnPreview").click()
    expect(page.locator("#previewSelectedCount")).to_have_text("3 of 3 selected")
    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()


def test_removing_the_last_source_gates_start_immediately(live_server, page):
    """Not after the 350ms re-preview debounce.

    Dropping a source changes the preview signature, so the grid that is
    still on screen no longer describes the import. Nothing re-previews here
    (there are no sources left), so if the gate waited for the debounced
    preview to run it would never fire at all.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    expect(page.locator("#btnStart")).to_have_text("Start import (3 files)")

    page.locator("#sourceList .source-item button[title='Remove']").click()
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_is_blocked_when_the_captured_image_list_fails_to_load(
        live_server, page):
    """Snapshot mode: Start stays shut when there is no list behind it.

    The new-images flow imports exactly one frozen list of paths. If that
    list can't be loaded there is nothing to import, and a live Start would
    post a snapshot id the server has already rejected.
    """
    page.goto(f"{live_server['url']}/import?new_images=not-a-snapshot-id")
    expect(page.locator("#newImagesImportSource")).to_contain_text(
        "could not be loaded")
    # newImagesStartBlocked contributes no label, so the default caption is
    # what proves the block came from that flag and not from a stray reason.
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_disabled()


def test_a_mode_round_trip_does_not_leave_start_live_over_an_empty_grid(
        live_server, page):
    """updateImportMode() throws the grid away, so it owes the gate a reset.

    copy -> in place -> copy inside the 350ms re-preview debounce restores
    the exact signature the completed preview captured, so the staleness
    check reports "current" over a grid that no longer exists -- a live
    Start, an invisible grid and a select-all still reading "3 of 3
    selected", all at once. The debounce is stubbed out here so the
    assertion pins the reset to updateImportMode() rather than to the
    re-preview that happens to follow it.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    expect(page.locator("#btnStart")).to_have_text("Start import (3 files)")

    _suppress_auto_preview(page)
    page.locator("#modeInPlace").check()
    page.locator("#modeCopy").check()

    expect(page.locator("#importPreviewGrid")).not_to_be_visible()
    # Back to "no preview run": Start is live, but it now means "import
    # everything", which is what the empty screen beside it says.
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#previewSelectedCount")).to_have_text(
        "0 of 0 selected")


def test_the_start_label_pluralises_a_single_file(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(2))
    _preview(page)

    page.locator(".import-preview-thumb .thumb-check").first.click()
    expect(page.locator("#btnStart")).to_have_text("Start import (1 file)")


# --- Task 10: the gate has to re-OPEN, not just close ---------------------


def test_start_re_opens_after_a_failed_preview_is_retried(live_server, page):
    """importPreviewFailed has exactly one reset, at the top of previewImport.

    Lose it and a single failed preview disables Start for the life of the
    page: the user previews again, gets a complete and current grid, and
    stares at a dead button captioned "Preview again before importing".
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    page.evaluate(
        """() => {
          window.__failPreview = false;
          const f = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (window.__failPreview && t
                && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(
                JSON.stringify({error: 'disk went away'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
        }"""
    )
    _preview(page)

    page.evaluate("() => { window.__failPreview = true; }")
    page.locator("#btnPreview").click()
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")

    page.evaluate("() => { window.__failPreview = false; }")
    page.locator("#btnPreview").click()
    expect(page.locator("#importPreviewGrid")).to_be_visible()
    expect(page.locator("#btnStart")).to_have_text("Start import (3 files)")
    expect(page.locator("#btnStart")).to_be_enabled()


def test_start_re_opens_when_the_import_fails_to_start(live_server, page):
    """A rejected POST has to clear importStartPending.

    Otherwise Start stays disabled reading "Importing…" next to an error
    banner saying the import never started, and only a reload recovers.
    """
    page.goto(f"{live_server['url']}/import")
    page.evaluate(
        """() => {
          const f = window.fetch.bind(window);
          window.__rejectStart = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-in-place') === 0) {
              return new Promise((res, rej) => { window.__rejectStart = rej; });
            }
            return f(input, init);
          };
          window.scheduleImportPreview = () => {};
        }"""
    )
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#btnStart").click()
    expect(page.locator("#btnStart")).to_have_text("Importing…")

    page.evaluate(
        "() => window.__rejectStart(new Error('archive is offline'))")
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_enabled()


def test_previewing_with_no_sources_does_not_latch_start_shut(
        live_server, page):
    """The validation early returns run AFTER in-flight is set.

    previewImport() is awaited here so the assertion lands on the settled
    state rather than on the moment before the early return.
    """
    page.goto(f"{live_server['url']}/import")
    page.evaluate("async () => { await previewImport(); }")

    assert page.evaluate("() => importPreviewInFlight") is False
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_enabled()


def test_previewing_with_no_file_types_does_not_latch_start_shut(
        live_server, page):
    """The second validation early return, same hazard.

    Reachable in copy mode by unchecking the last extension of a custom
    preset, which debounces straight into previewImport().
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    # Set without dispatching change: no debounced re-preview to race with.
    page.evaluate(
        """() => {
          document.getElementById('fileTypePreset').value = 'custom';
          document.querySelectorAll('.file-ext').forEach(
            (el) => { el.checked = false; });
        }"""
    )
    page.evaluate("async () => { await previewImport(); }")

    assert page.evaluate("() => importPreviewInFlight") is False
    # Stale, not "Previewing…": the file types no longer match the grid.
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")
    expect(page.locator("#btnStart")).to_be_disabled()


# --- Task 11: selection is copy-mode only, and the absence is explained ----


def test_in_place_mode_hides_selection_and_explains_why(live_server, page):
    """Hiding the controls is only half the requirement.

    In-place import runs through do_scan(restrict_files=...), which
    vireo/scanner.py only honours alongside restrict_dirs, so per-file
    selection is out of scope for this mode. Omitting the checkboxes
    silently would leave the user unable to tell whether selection is
    missing, broken, or gated behind a setting they haven't found -- the
    same unexplained-control failure the disabled-state work already had to
    fix twice.

    BOTH halves of the note are pinned. The descriptive half alone leaves
    the user knowing selection doesn't apply but not where it does, which
    is the half this whole task exists for; and the pointer has to quote
    the radio's own label, since a user scanning for the note's words has
    to be able to find the control it names.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    page.locator("#modeInPlace").click()
    _preview(page)

    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(0)
    note = page.locator("#selectionUnavailableNote")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Add in place catalogs every file")
    expect(note).to_contain_text("File selection is available in Copy to"
                                 " archive mode.")
    # Not a paraphrase of the radio: the note has to name the control the
    # user will go looking for, exactly as the control names itself.
    expect(page.locator("label", has=page.locator("#modeCopy"))
           ).to_contain_text("Copy to archive")
    expect(page.locator("label", has=page.locator("#modeInPlace"))
           ).to_contain_text("Add in place")
    # The other two selection controls go with them: a folder header that
    # bulk-toggles nothing, and a master checkbox over no boxes.
    expect(page.locator(".import-preview-folder-header .folder-check")
           ).to_have_count(0)
    expect(page.locator("#selectAllRow")).not_to_be_visible()
    # The cards themselves stay -- the preview is still the honest list of
    # what will be catalogued.
    expect(page.locator(".import-preview-thumb")).to_have_count(3)


def test_the_folder_header_still_names_its_folder_without_a_checkbox(
        live_server, page):
    """Dropping the box must not leave an empty or half-empty header.

    The header is a flex row with a 6px gap, so leaving a hidden or
    zero-width checkbox in place would indent the folder name away from
    the grid it labels. The name itself still has to be there: without it
    the group separator would be a blank line.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    page.locator("#modeInPlace").click()
    _preview(page)

    header = page.locator(".import-preview-folder-header")
    expect(header).to_have_text("card (3)")
    expect(page.locator(".import-preview-folder-header > *")).to_have_count(1)


def test_switching_back_to_copy_restores_every_selection_control(
        live_server, page):
    """The re-open direction, which is where these flags usually rot.

    in place -> copy has to bring back the per-file boxes, the folder
    header, and the select-all row, AND retire the note -- a note still
    reading "selection is available when copying files" beside a live set of
    checkboxes is its own black box.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(3)
    expect(page.locator("#selectionUnavailableNote")).not_to_be_visible()

    page.locator("#modeInPlace").click()
    _preview(page)
    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(0)
    expect(page.locator("#selectionUnavailableNote")).to_be_visible()

    page.locator("#modeCopy").click()
    _preview(page)
    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(3)
    expect(page.locator(".import-preview-folder-header .folder-check")
           ).to_have_count(1)
    expect(page.locator("#selectAllRow")).to_be_visible()
    expect(page.locator("#selectionUnavailableNote")).not_to_be_visible()


def test_the_note_retires_on_a_mode_switch_before_any_preview(
        live_server, page):
    """The note is owned by the mode, not by the rendered grid.

    updateImportMode() throws the preview away, so if the note only ever
    updated inside the renderer it would survive a switch to copy mode with
    no grid on screen and contradict the controls the user is about to see.
    """
    page.goto(f"{live_server['url']}/import")
    _suppress_auto_preview(page)
    expect(page.locator("#selectionUnavailableNote")).to_be_visible()

    page.locator("#modeCopy").click()
    expect(page.locator("#selectionUnavailableNote")).not_to_be_visible()

    page.locator("#modeInPlace").click()
    expect(page.locator("#selectionUnavailableNote")).to_be_visible()


def test_in_place_start_label_carries_no_file_count(live_server, page):
    """The label's count is guarded by copyMode and must stay that way.

    In-place mode sends no include_paths, so "Start import (3 files)" would
    be a promise the request doesn't make: the scan catalogues whatever is
    on disk when it runs, not the three cards on screen.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    page.locator("#modeInPlace").click()
    _preview(page)

    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_enabled()


def _stub_snapshot_import(page, files):
    """Drive the new-images (snapshot) flow, the THIRD selection state.

    Routed at the network layer rather than by patching window.fetch:
    initNewImagesImport() runs from the page's own load handler, so a
    fetch stub installed after goto() would arrive too late.
    """
    page.route(
        "**/api/workspaces/active/new-images/snapshot/42",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "snapshot_id": 42, "file_count": len(files),
                "folder_paths": ["/tmp/card"],
            })),
    )
    page.route(
        "**/api/import/new-images-preview",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "total_count": len(files), "unavailable_count": 0,
                "files": files,
            })),
    )


def test_snapshot_mode_hides_selection_and_says_what_it_will_import(
        live_server, page):
    """Snapshot mode is not in-place mode, and the note must not claim it is.

    Both mode radios are DISABLED here, so pointing at Copy to archive
    would name a control the user cannot reach, and "every file it finds in
    your source folders" is wrong twice over: this import posts a
    snapshot_id and adds exactly the frozen list that was captured, which
    is a subset of what those folders hold.
    """
    page.goto(f"{live_server['url']}/import")  # warm the app before routing
    _stub_snapshot_import(page, _files(3))
    page.goto(f"{live_server['url']}/import?new_images=42")
    expect(page.locator("#importPreviewGrid")).to_be_visible()

    expect(page.locator(".import-preview-thumb")).to_have_count(3)
    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(0)
    expect(page.locator("#selectAllRow")).not_to_be_visible()
    note = page.locator("#selectionUnavailableNote")
    expect(note).to_be_visible()
    expect(note).to_contain_text("adds exactly the captured list")
    # Both halves again: "what it adds" and "what happens to the files".
    # The summary line above happens to repeat the second one today, but a
    # note that only half-explains itself shouldn't depend on that.
    expect(note).to_contain_text("leaving the originals where they are")
    expect(note).not_to_contain_text("Copy to archive")
    expect(note).not_to_contain_text("your source folders")
    # The disabled radios are what make naming Copy to archive wrong here.
    expect(page.locator("#modeCopy")).to_be_disabled()
    expect(page.locator("#modeInPlace")).to_be_disabled()


def test_snapshot_mode_withholds_selection_even_if_the_mode_radio_flips(
        live_server, page):
    """importSelectionEnabled()'s snapshot clause is not decoration.

    Today it is belt-and-braces: activateNewImagesImport() ticks in-place
    and DISABLES both radios, so "copy is checked" should be unreachable,
    and updateImportMode() re-ticks in-place on every call besides. But the
    snapshot import posts a snapshot_id against a frozen server-side list
    and carries no include_paths at all, so if the radio ever comes back the
    controls must not follow it -- checkboxes over a list the request cannot
    narrow are a lie. Driven through the renderer directly because
    updateImportMode() would undo the radio before the guard was reached.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_snapshot_import(page, files)
    page.goto(f"{live_server['url']}/import?new_images=42")
    expect(page.locator("#importPreviewGrid")).to_be_visible()

    page.evaluate(
        """(files) => {
          document.getElementById('modeCopy').disabled = false;
          document.getElementById('modeCopy').checked = true;
          renderImportPreviewGrid(files, [], null);
          updateImportSelectionUI();
        }""",
        files,
    )

    expect(page.locator(".import-preview-thumb")).to_have_count(3)
    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(0)
    expect(page.locator(".import-preview-folder-header .folder-check")
           ).to_have_count(0)
    expect(page.locator("#selectAllRow")).not_to_be_visible()
    expect(page.locator("#selectionUnavailableNote")).to_be_visible()


def _capture_start(page):
    """Intercept the import job POST and stash its parsed body.

    Matches both job routes, so a test can assert on what in-place mode
    sends as well as what copy mode does. EventSource is stubbed out
    because watchJob() opens one against the fake job id the moment the
    POST resolves.
    """
    page.evaluate(
        """() => {
          const f = window.fetch;
          window.__body = null;
          window.__url = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-') === 0) {
              window.__url = t;
              window.__body = JSON.parse(init.body);
              return Promise.resolve(new Response(
                JSON.stringify({job_id: 'import-x'}), {status: 200,
                headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
          window.EventSource = function () {
            this.close = () => {};
            this.addEventListener = () => {};
          };
        }"""
    )


def _await_duplicate_verdicts(page, count):
    """Block until the check-duplicates stream has actually landed.

    _preview() returns on the files-only render; the duplicate stream is
    still draining behind it. Every assertion about include_paths turns on
    whether a card carries the duplicate class by the time Start is clicked,
    so waiting for the badge -- not for _preview() -- is what makes these
    tests deterministic.
    """
    expect(page.locator(".import-preview-thumb.duplicate")).to_have_count(count)


def test_start_import_sends_include_paths_with_duplicates_retained(
        live_server, page):
    """include_paths keeps a file the UI shows as an unchecked duplicate --
    the job needs it to count the duplicate and keep the ledger balanced."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[2]["path"]])
    _capture_start(page)
    # Fill the destination BEFORE previewing. #destInput is wired into
    # wireDestStructureInvalidation, so filling it after selecting schedules a
    # re-preview whose success path resets importDeselected -- the selection
    # assertions would then flake.
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    _await_duplicate_verdicts(page, 1)
    page.locator(".import-preview-thumb .thumb-check").first.click()
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert set(body["include_paths"]) == {files[1]["path"], files[2]["path"]}
    assert body["previewed_count"] == 3
    assert body["checked_count"] == 1


def test_a_duplicate_unchecked_before_verdicts_still_reaches_the_job(
        live_server, page):
    """At first render verdicts have not arrived, so duplicate cards are still
    enabled and clickable. A click there writes the path into importDeselected
    and nothing removes it -- the eligibleDeselections filter is what stops
    that path being dropped from include_paths, where it would land in no
    ledger bucket and falsely report a fully-archived card as unsafe to
    format.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[2]["path"]])
    page.locator("#destInput").fill("/tmp/archive")   # before preview -- see above
    _preview(page)
    _await_duplicate_verdicts(page, 1)
    _capture_start(page)
    # Simulate the pre-verdict click: the path IS a duplicate, but the user
    # unchecked it before the stream landed, so it sits in importDeselected.
    page.evaluate("(p) => importDeselected.add(p)", files[2]["path"])
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert files[2]["path"] in body["include_paths"]
    # And it is not silently counted as a file the user chose to copy.
    assert body["checked_count"] == 2


def test_in_place_start_sends_no_selection_fields(live_server, page):
    """The in-place route ignores these fields, but sending them is still a
    lie: do_scan() catalogues whatever is on disk when it runs, so a request
    carrying a file list promises a narrowing that never happens.

    The `importPreviewCapturedSignature !== null` guard is NOT what protects
    this. previewImport() captures the signature before its `if (!copyMode)`
    return, so an in-place preview leaves it set; only the copy-mode branch
    keeps the fields out. Hence the wait for the in-place summary below --
    without it the assertion would pass against a null signature and prove
    nothing.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _capture_start(page)
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    expect(page.locator("#btnStart")).to_have_text("Start import (3 files)")

    page.locator("#modeInPlace").check()
    # The debounced in-place preview has completed and captured a signature.
    expect(page.locator("#previewSummary")).to_have_text(
        "3 files found · originals will stay in place")
    page.locator("#btnStart").click()

    assert page.evaluate("() => window.__url").endswith("/import-in-place")
    body = page.evaluate("() => window.__body")
    assert "include_paths" not in body
    assert "previewed_count" not in body
    assert "checked_count" not in body


def test_the_selection_payload_follows_a_re_preview(live_server, page):
    """A completed preview owns the selection, and the payload has to hear
    about it in both directions: a deselection made before the re-preview is
    gone, and one made after it counts."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files)
    _capture_start(page)
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").nth(1).click()
    expect(page.locator("#previewSelectedCount")).to_have_text("2 of 3 selected")

    page.locator("#btnPreview").click()
    expect(page.locator("#previewSelectedCount")).to_have_text("3 of 3 selected")
    page.locator(".import-preview-thumb .thumb-check").nth(0).click()
    expect(page.locator("#previewSelectedCount")).to_have_text("2 of 3 selected")
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert set(body["include_paths"]) == {files[1]["path"], files[2]["path"]}
    assert body["previewed_count"] == 3
    assert body["checked_count"] == 2


def test_a_fully_duplicate_card_sends_every_path_and_zero_checked(
        live_server, page):
    """Nothing is copied, so checked_count is 0 -- but every path still has to
    reach the job, because it is the copy loop that counts a skipped duplicate
    and only a balanced ledger yields "safe to format"."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[f["path"] for f in files])
    _capture_start(page)
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    _await_duplicate_verdicts(page, 3)
    expect(page.locator("#btnStart")).to_have_text("Start import (0 files)")
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert set(body["include_paths"]) == {f["path"] for f in files}
    assert body["previewed_count"] == 3
    assert body["checked_count"] == 0


def test_a_file_discovered_under_two_sources_is_counted_once(
        live_server, page):
    """/api/import/folder-preview appends per source with no cross-source
    dedup, so nested sources (/card and /card/DCIM) emit the same file twice
    and the grid draws two cards for it. Both counts must be counts of FILES,
    not of cards: the route rejects checked_count > len(set(include_paths)),
    and one file the user ticked once cannot be two files selected.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(2)
    twin = dict(files[1], subfolder="DCIM")
    _stub_preview(page, files + [twin])
    _capture_start(page)
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    expect(page.locator(".import-preview-thumb")).to_have_count(3)
    # Both readouts are counts of files too. "3 of 3 selected" over an import
    # that copies two files is the same lie as the payload's, and the button
    # is the last thing the user reads before committing to the run.
    expect(page.locator("#previewSelectedCount")).to_have_text("2 of 2 selected")
    expect(page.locator("#btnStart")).to_have_text("Start import (2 files)")
    # And the master checkbox, which compares the two counts rather than
    # printing them. Card-counted eligibility (3) against a file-counted
    # checked (2) leaves it DASHED over a fully-selected preview -- a partial
    # selection the user would go hunting for and never find.
    master = page.locator("#chkSelectAllImport")
    expect(master).to_be_checked()
    expect(master).to_have_js_property("indeterminate", False)
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert set(body["include_paths"]) == {f["path"] for f in files}
    assert body["previewed_count"] == 2
    assert body["checked_count"] == 2
    # The route's own invariant, asserted the way it is enforced.
    assert body["checked_count"] <= len(set(body["include_paths"]))


def test_copy_mode_with_no_preview_run_sends_no_selection_fields(
        live_server, page):
    """The first of updateStartGate()'s four preview states, and it is
    reachable: a mode switch leaves importPreviewCapturedSignature null and
    Start live, so a click before the 350ms debounce lands here. There is no
    file list to send -- the import copies everything, which is what the
    screen says -- and sending one anyway means include_paths: [], which the
    route rejects outright ("include_paths must be a non-empty list").
    """
    page.goto(f"{live_server['url']}/import")
    _suppress_auto_preview(page)
    _capture_start(page)
    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card")
    page.locator("#btnAddSource").click()
    page.locator("#destInput").fill("/tmp/archive")
    # No preview has run, so no count belongs on the button either.
    expect(page.locator("#btnStart")).to_have_text("Start import")
    expect(page.locator("#btnStart")).to_be_enabled()
    page.locator("#btnStart").click()

    # Really the copy route -- otherwise this would pass for the in-place
    # reason and prove nothing about the guard.
    assert page.evaluate("() => window.__url").endswith("/import-photos")
    body = page.evaluate("() => window.__body")
    assert "include_paths" not in body
    assert "previewed_count" not in body
    assert "checked_count" not in body


def test_snapshot_start_sends_no_selection_fields(live_server, page):
    """The snapshot import posts a snapshot_id against a frozen server-side
    list; there is nothing for include_paths to narrow, and the page draws no
    checkboxes to build one from.

    Asserted directly rather than left to ride on the in-place test: the two
    share only the `copyMode` predicate, and a change that split them would
    take this case with it silently. The signature assertion is the point --
    new-images-preview captures one, so `!== null` is true here and the
    copy-mode placement is the only thing keeping the fields out.
    """
    page.goto(f"{live_server['url']}/import")  # warm the app before routing
    _stub_snapshot_import(page, _files(3))
    page.goto(f"{live_server['url']}/import?new_images=42")
    expect(page.locator("#importPreviewGrid")).to_be_visible()
    _capture_start(page)
    assert page.evaluate("() => importPreviewCapturedSignature !== null")

    expect(page.locator("#btnStart")).to_be_enabled()
    page.locator("#btnStart").click()

    assert page.evaluate("() => window.__url").endswith("/import-in-place")
    body = page.evaluate("() => window.__body")
    assert body["source_snapshot_id"] == 42
    assert "include_paths" not in body
    assert "previewed_count" not in body
    assert "checked_count" not in body
