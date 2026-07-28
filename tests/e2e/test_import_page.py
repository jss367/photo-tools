import json
import re

from playwright.sync_api import expect


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


# Stubs a two-file copy-mode preview where IMG_0002.jpg comes back flagged as
# a duplicate. Shared by the tests that exercise the preview grid.
TWO_FILE_PREVIEW_STUB = """
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
              // Tests flip __emptyPreview to simulate a source that comes
              // back with zero importable files (e.g. an empty card or a
              // filter that excludes everything).
              if (window.__emptyPreview) {
                return Promise.resolve(new Response(JSON.stringify({
                  total_count: 0,
                  total_size: 0,
                  type_breakdown: {},
                  duplicate_count: 0,
                  files: [],
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
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
              // Tests flip __noDupes to simulate the next card coming back
              // clean, which is how the preview reaches a completed check
              // with zero duplicates.
              const dupes = window.__noDupes
                ? [] : ['/tmp/card-a/IMG_0002.jpg'];
              const frame = 'data: ' + JSON.stringify({
                duplicates: dupes,
                checked: 2,
                total: 2,
              }) + '\\n\\n' + 'data: ' + JSON.stringify({
                done: true,
                duplicate_count: dupes.length,
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


def run_two_file_preview(live_server, page):
    """Load /import with the stub above and wait for the preview to settle."""
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(TWO_FILE_PREVIEW_STUB)

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()

    page.wait_for_function(
        "window.__fullPreviewCalls >= 1 && window.__dupCalls >= 1 && window.__destCalls >= 1"
    )


# Stubs a four-file copy-mode preview split across two subfolders:
# card-a (both files are duplicates) and card-b (one duplicate, one new).
# The mixed layout is what exercises the "one folder is entirely duplicates
# while another still has content" case for the hide-duplicates filter.
MULTI_FOLDER_MIXED_PREVIEW_STUB = """
        () => {
          const originalFetch = window.fetch.bind(window);
          window.__fullPreviewCalls = 0;
          window.__dupCalls = 0;
          window.__destCalls = 0;
          const THUMB = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
          const FILES = [
            {path: '/tmp/cards/card-a/A1.jpg', filename: 'A1.jpg', subfolder: 'card-a', size: 1234, extension: '.jpg', thumb_url: THUMB},
            {path: '/tmp/cards/card-a/A2.jpg', filename: 'A2.jpg', subfolder: 'card-a', size: 1234, extension: '.jpg', thumb_url: THUMB},
            {path: '/tmp/cards/card-b/B1.jpg', filename: 'B1.jpg', subfolder: 'card-b', size: 1234, extension: '.jpg', thumb_url: THUMB},
            {path: '/tmp/cards/card-b/B2.jpg', filename: 'B2.jpg', subfolder: 'card-b', size: 1234, extension: '.jpg', thumb_url: THUMB},
          ];
          const DUPES = [
            '/tmp/cards/card-a/A1.jpg',
            '/tmp/cards/card-a/A2.jpg',
            '/tmp/cards/card-b/B2.jpg',
          ];
          window.fetch = (input, init) => {
            const target = typeof input === 'string' ? input : input.url;
            if (target && target.indexOf('/api/import/folder-preview') === 0) {
              const body = JSON.parse(init.body || '{}');
              if (body.summary_only) {
                return Promise.resolve(new Response(JSON.stringify({
                  total_count: 4,
                  total_size: 4936,
                  type_breakdown: {'.jpg': 4},
                  duplicate_count: 0,
                  files: [],
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              window.__fullPreviewCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 4,
                total_size: 4936,
                type_breakdown: {'.jpg': 4},
                duplicate_count: 0,
                files: FILES,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              window.__dupCalls += 1;
              const frame = 'data: ' + JSON.stringify({
                duplicates: DUPES, checked: FILES.length, total: FILES.length,
              }) + '\\n\\n' + 'data: ' + JSON.stringify({
                done: true, duplicate_count: DUPES.length, checked: FILES.length, total: FILES.length,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            if (target && target.indexOf('/api/import/destination-preview') === 0) {
              window.__destCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({
                folders: [{path: '2026/2026-07-11', full_path: '/archive/2026/2026-07-11', count: 1, exists: false}],
                total_photos: 1, total_folders: 1, new_folders: 1, existing_folders: 0, managed_archive: null,
                files: [{path: '/tmp/cards/card-b/B1.jpg', folder: '2026/2026-07-11', full_folder: '/archive/2026/2026-07-11'}],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
          };
        }
        """


def run_multi_folder_mixed_preview(live_server, page):
    """Load /import with the multi-folder stub and wait for it to settle."""
    url = live_server["url"]
    page.goto(f"{url}/import")
    page.evaluate(MULTI_FOLDER_MIXED_PREVIEW_STUB)

    page.locator("#modeCopy").check()
    page.locator("#destInput").fill("/archive")
    page.locator("#sourceInput").fill("/tmp/cards")
    page.locator("#btnAddSource").click()

    page.wait_for_function(
        "window.__fullPreviewCalls >= 1 && window.__dupCalls >= 1 && window.__destCalls >= 1"
    )


def test_import_preview_runs_automatically_after_source_selection(live_server, page):
    run_two_file_preview(live_server, page)

    expect(page.locator("#previewSummary")).to_contain_text("1 already in your library")
    grid = page.locator("#importPreviewGrid")
    expect(grid).to_be_visible()
    expect(grid).to_contain_text("IMG_0001.jpg")
    # Duplicate filtering is opt-in, so the automatic preview initially
    # shows both files and marks the duplicate. The dedicated filter test
    # below covers the collapsed state after the checkbox is enabled.
    expect(grid).to_contain_text("IMG_0002.jpg")
    expect(grid).to_contain_text("Duplicate")
    expect(grid).to_contain_text("To: 2026/2026-07-11")


def test_import_preview_hide_duplicates_checkbox_filters_grid(live_server, page):
    run_two_file_preview(live_server, page)

    grid = page.locator("#importPreviewGrid")
    checkbox = page.locator("#chkHideDuplicates")
    # Defaults to off: every discovered file is visible until the user opts in.
    expect(page.locator("#hideDuplicatesRow")).to_be_visible()
    expect(checkbox).not_to_be_checked()
    expect(page.locator("#hideDuplicatesLabel")).to_have_text("Hide duplicates (1)")
    expect(grid).to_contain_text("IMG_0002.jpg")

    calls_before = page.evaluate(
        "[window.__fullPreviewCalls, window.__dupCalls, window.__destCalls]",
    )

    checkbox.check()
    expect(grid).to_contain_text("IMG_0001.jpg")
    expect(grid).not_to_contain_text("IMG_0002.jpg")
    expect(grid).to_contain_text("1 duplicate hidden")
    # The summary still reports the full picture — hiding is a view filter,
    # not a change to what the import will do.
    expect(page.locator("#previewSummary")).to_contain_text("1 already in your library")

    checkbox.uncheck()
    expect(grid).to_contain_text("IMG_0002.jpg")
    expect(grid).to_contain_text("Duplicate")

    # Toggling re-renders from the last preview result and must never
    # re-run discovery, the duplicate check, or the destination preview —
    # on a real card those are the slow calls the filter exists to avoid
    # repeating.
    assert page.evaluate(
        "[window.__fullPreviewCalls, window.__dupCalls, window.__destCalls]",
    ) == calls_before


def test_hide_duplicates_all_duplicate_banner_has_no_phantom_tiles(
    live_server, page,
):
    """When filtering leaves zero pending files, the collapsed banner must
    not claim that zero files are shown below when no tiles follow it."""
    run_two_file_preview(live_server, page)
    page.evaluate(
        """
        () => {
          const file = {
            path: '/tmp/card-a/IMG_0002.jpg',
            filename: 'IMG_0002.jpg',
            subfolder: 'card-a',
          };
          renderImportPreviewGrid(
            [file],
            [file.path],
            [],
          );
        }
        """
    )

    page.locator("#chkHideDuplicates").check()
    banner = page.locator("[data-testid='collapsed-duplicate-preview']")
    expect(banner).to_contain_text("1 duplicate hidden")
    expect(banner).to_contain_text(
        "Nothing else will be imported from this selection."
    )
    expect(banner).not_to_contain_text("0 files to import")
    expect(page.locator("#importPreviewGrid .import-preview-thumb")).to_have_count(0)


def test_hide_duplicates_resets_once_a_check_finds_no_duplicates(
    live_server, page,
):
    """A completed check that finds nothing to hide retires the opt-in.

    Otherwise the checkbox stays silently checked while its row is hidden,
    and the next card that does have duplicates gets filtered without the
    user asking for it this time.
    """
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()
    expect(page.locator("#importPreviewGrid")).not_to_contain_text(
        "IMG_0002.jpg",
    )

    # Next preview comes back clean, so the control has nothing to offer.
    page.evaluate("window.__noDupes = true")
    page.locator("#btnPreview").click()
    expect(page.locator("#hideDuplicatesRow")).to_be_hidden()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()

    # ...so a later duplicate-bearing card starts unfiltered again.
    page.evaluate("window.__noDupes = false")
    page.locator("#btnPreview").click()
    expect(page.locator("#hideDuplicatesRow")).to_be_visible()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()
    expect(page.locator("#importPreviewGrid")).to_contain_text("IMG_0002.jpg")


def test_hide_duplicates_resets_when_dedup_is_turned_off(live_server, page):
    """Turning off "Skip duplicates" settles the picture too: nothing will
    be skipped, so the filter has nothing to hide and the opt-in retires
    rather than lying in wait for the next dedup-enabled preview."""
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()

    page.locator("#chkSkipDuplicates").uncheck()
    expect(page.locator("#previewSummary")).to_contain_text(
        "duplicates will be copied",
    )
    expect(page.locator("#hideDuplicatesRow")).to_be_hidden()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()

    dup_calls = page.evaluate("window.__dupCalls")
    page.locator("#chkSkipDuplicates").check()
    page.wait_for_function(f"window.__dupCalls > {dup_calls}")
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()
    expect(page.locator("#importPreviewGrid")).to_contain_text("IMG_0002.jpg")


def test_hide_duplicates_resets_when_the_last_source_is_removed(
    live_server, page,
):
    """Emptying the source list ends this card's session.

    The row is hidden at that point, so a filter left checked is invisible
    — and the next card added would be filtered on arrival without the
    user opting in for it.
    """
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()

    page.locator("#sourceList button", has_text="×").first.click()
    expect(page.locator("#hideDuplicatesRow")).to_be_hidden()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()

    # A different card arrives and starts unfiltered.
    dup_calls = page.evaluate("window.__dupCalls")
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.wait_for_function(f"window.__dupCalls > {dup_calls}")
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()
    expect(page.locator("#importPreviewGrid")).to_contain_text("IMG_0002.jpg")


def test_hide_duplicates_resets_when_a_preview_settles_with_no_files(
    live_server, page,
):
    """A completed preview that returns zero files settles the picture too.

    The `!files.length` branch hides the row without ever running the
    duplicate check, so a filter left checked is invisible there — and
    the next card that does surface duplicates would be filtered on
    arrival without the user opting in for it.
    """
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()

    # Next preview finds nothing to import (e.g. an empty source card).
    preview_calls = page.evaluate("window.__fullPreviewCalls")
    page.evaluate("window.__emptyPreview = true")
    page.locator("#btnPreview").click()
    page.wait_for_function(f"window.__fullPreviewCalls > {preview_calls}")

    expect(page.locator("#previewSummary")).to_contain_text(
        "No importable files found.",
    )
    expect(page.locator("#hideDuplicatesRow")).to_be_hidden()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()

    # ...so a later duplicate-bearing card starts unfiltered again.
    page.evaluate("window.__emptyPreview = false")
    dup_calls = page.evaluate("window.__dupCalls")
    page.locator("#btnPreview").click()
    page.wait_for_function(f"window.__dupCalls > {dup_calls}")
    expect(page.locator("#hideDuplicatesRow")).to_be_visible()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()
    expect(page.locator("#importPreviewGrid")).to_contain_text("IMG_0002.jpg")


def test_hide_duplicates_resets_when_the_preview_cannot_run(live_server, page):
    """A preview that stops on a validation error is settled as well.

    Clearing every file extension aborts before discovery, so nothing can
    be hidden, and the opt-in must not survive behind the hidden row.
    """
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()

    page.locator("#fileTypePreset").select_option("custom")
    exts = page.locator(".file-ext")
    for i in range(exts.count()):
        exts.nth(i).uncheck()

    expect(page.locator("#importError")).to_contain_text(
        "Choose at least one file extension.",
    )
    expect(page.locator("#hideDuplicatesRow")).to_be_hidden()
    expect(page.locator("#chkHideDuplicates")).not_to_be_checked()

    # Leave a valid selection behind. This test is the only one that puts
    # the page in an unpreviewable state, and the preset is the kind of
    # option later tests assume is sane.
    page.locator("#fileTypePreset").select_option("both")


def test_hide_duplicates_preserves_first_occurrence_from_overlapping_sources(
    live_server, page,
):
    """Overlapping sources yield the same path twice in the preview.

    The `/api/import/check-duplicates` stream flags only the *later*
    occurrence as an intra-import duplicate — the first is the tile that
    will actually be copied and land in the archive. Hiding by path-set
    membership would drop both tiles, silently claiming the to-copy file
    is a duplicate too. Match the summary: hide one tile, keep the other.
    """
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
                  total_count: 2,
                  total_size: 2468,
                  type_breakdown: {'.jpg': 2},
                  duplicate_count: 0,
                  files: [],
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              window.__fullPreviewCalls += 1;
              // Same path arrives twice — mimics scanning /card and
              // /card/DCIM together, the exact overlap that surfaces this
              // per-occurrence bug.
              return Promise.resolve(new Response(JSON.stringify({
                total_count: 2,
                total_size: 2468,
                type_breakdown: {'.jpg': 2},
                duplicate_count: 0,
                files: [
                  {
                    path: '/tmp/card/DCIM/IMG_0001.jpg',
                    filename: 'IMG_0001.jpg',
                    subfolder: 'card/DCIM',
                    size: 1234,
                    extension: '.jpg',
                    thumb_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
                  },
                  {
                    path: '/tmp/card/DCIM/IMG_0001.jpg',
                    filename: 'IMG_0001.jpg',
                    subfolder: 'card/DCIM',
                    size: 1234,
                    extension: '.jpg',
                    thumb_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
                  },
                ],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (target && target.indexOf('/api/import/check-duplicates') === 0) {
              // The real checker only flags the second occurrence as an
              // intra-import duplicate, so the returned list carries the
              // path exactly once.
              const frame = 'data: ' + JSON.stringify({
                duplicates: ['/tmp/card/DCIM/IMG_0001.jpg'],
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
                  path: '/tmp/card/DCIM/IMG_0001.jpg',
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
    page.locator("#sourceInput").fill("/tmp/card")
    page.locator("#btnAddSource").click()
    page.wait_for_function("window.__fullPreviewCalls >= 1")

    grid = page.locator("#importPreviewGrid")
    # Off by default: both occurrences render, one badged Duplicate.
    expect(page.locator("#previewSummary")).to_contain_text(
        "1 already in your library"
    )
    expect(page.locator("#hideDuplicatesLabel")).to_have_text(
        "Hide duplicates (1)"
    )
    expect(grid.locator(".import-preview-thumb")).to_have_count(2)
    expect(grid.locator(".import-preview-thumb.duplicate")).to_have_count(1)

    # Turn on the filter: the second occurrence (the tile the server
    # actually flagged) hides; the first stays, matching what the import
    # will copy. Set-based hiding would erroneously drop both.
    page.locator("#chkHideDuplicates").check()
    expect(grid.locator(".import-preview-thumb")).to_have_count(1)
    expect(grid.locator(".import-preview-thumb.duplicate")).to_have_count(0)
    expect(grid).to_contain_text("1 duplicate hidden")
    # Filter never claims "all X are duplicates" when a to-copy tile
    # survives — that phrasing would contradict the summary line.
    expect(grid).not_to_contain_text("are duplicates")


def test_hide_duplicates_survives_a_repreview_that_still_has_duplicates(
    live_server, page,
):
    """Re-previewing must not silently drop the filter.

    Any import-option change schedules a fresh preview, and each preview
    renders the grid with an empty duplicate list before its check
    finishes. Clearing the opt-in on every zero-count render would flip
    the filter off mid-session and flood the grid back.
    """
    run_two_file_preview(live_server, page)
    page.locator("#chkHideDuplicates").check()

    dup_calls = page.evaluate("window.__dupCalls")
    page.locator("#btnPreview").click()
    page.wait_for_function(f"window.__dupCalls > {dup_calls}")

    expect(page.locator("#chkHideDuplicates")).to_be_checked()
    expect(page.locator("#importPreviewGrid")).not_to_contain_text(
        "IMG_0002.jpg",
    )


def test_hide_duplicates_keeps_all_duplicate_folder_headers_visible(
    live_server, page,
):
    """When one folder of a multi-folder preview is made entirely of
    duplicates, enabling the filter must not silently drop that folder.

    Otherwise the surviving folders' headers own their hidden files, but
    the all-duplicate folder disappears without a trace — the user is
    given no evidence the folder or its duplicate count ever existed. The
    header stays visible as "folder (0) · N duplicates hidden" so the
    hidden count is always accounted for somewhere the user can see.
    """
    run_multi_folder_mixed_preview(live_server, page)

    grid = page.locator("#importPreviewGrid")
    # Off (default): both headers with full counts and all four tiles.
    expect(grid).to_contain_text("card-a (2)")
    expect(grid).to_contain_text("card-b (2)")
    expect(grid).to_contain_text("A1.jpg")
    expect(grid).to_contain_text("A2.jpg")
    expect(grid).to_contain_text("B1.jpg")
    expect(grid).to_contain_text("B2.jpg")

    page.locator("#chkHideDuplicates").check()

    # card-a is entirely duplicates — the header must survive with (0) and
    # the hidden count so the folder never silently vanishes.
    expect(grid).to_contain_text("card-a (0) · 2 duplicates hidden")
    # card-b keeps only its non-duplicate tile plus its hidden count.
    expect(grid).to_contain_text("card-b (1) · 1 duplicate hidden")
    expect(grid).to_contain_text("B1.jpg")
    expect(grid).not_to_contain_text("A1.jpg")
    expect(grid).not_to_contain_text("A2.jpg")
    expect(grid).not_to_contain_text("B2.jpg")

    # The global "all N are duplicates" empty state is reserved for the
    # case where every folder went to zero — a partially filtered preview
    # like this one still has visible tiles, so it must not fire.
    expect(grid).not_to_contain_text("All 4 files are duplicates")

    # Unchecking restores the full multi-folder grid unchanged.
    page.locator("#chkHideDuplicates").uncheck()
    expect(grid).to_contain_text("card-a (2)")
    expect(grid).to_contain_text("card-b (2)")
    expect(grid).to_contain_text("A1.jpg")
    expect(grid).to_contain_text("B2.jpg")


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
    expect(structure).to_contain_text(
        "Files imported here land inside the managed archive rooted at"
    )
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


def test_import_remote_destination_requires_visible_folder_inside_nas(
    live_server, page,
):
    """The remote root alone is not a complete destination. Explain the
    missing folder beside the field and keep Start unavailable until it is
    valid, instead of hiding a tiny error below the thumbnail grid."""
    url = live_server["url"]

    def remote_targets(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "rsync_available": True,
                "ssh_available": True,
                "targets": [{
                    "id": "nas1",
                    "name": "Photo NAS",
                    "user": "photo",
                    "host": "nas.local",
                    "remote_path": "/volume1/Photography",
                    "mount_path": "/Volumes/Photography",
                }],
            }),
        )

    page.route("**/api/remote-targets", remote_targets)
    page.goto(f"{url}/import")
    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#destMode").select_option("remote:nas1")

    inline_error = page.locator("#remoteSubpathError")
    expect(inline_error).to_be_visible()
    expect(inline_error).to_contain_text(
        "Enter the folder inside /volume1/Photography"
    )
    expect(page.locator("#btnStart")).to_be_disabled()
    assert page.evaluate("resolvedCopyDestination()") == ""

    page.locator("#remoteSubpath").fill("/Raw Files/USA")
    expect(inline_error).to_contain_text("Subpath must be relative")
    expect(page.locator("#btnStart")).to_be_disabled()

    page.locator("#remoteSubpath").fill("Raw Files/USA")
    expect(inline_error).to_be_hidden()
    assert page.evaluate("resolvedCopyDestination()") == (
        "/Volumes/Photography/Raw Files/USA"
    )


def test_failed_import_result_offers_preconfigured_retry(live_server, page):
    """A mixed import can retry from its result without rebuilding the form;
    the parent's duplicate-skip setting is preserved, and the parent's
    already-imported photo IDs plus its own job ID are carried on the
    retry request so the after-import chain covers the complete original
    scope."""
    url = live_server["url"]
    captured = {}

    def start_import(route):
        captured["body"] = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"job_id": "import-retry"}),
        )

    page.route("**/api/jobs/import-photos", start_import)
    page.goto(f"{url}/import")
    page.evaluate(
        """
        () => {
          lastFinishedImportJob = {
            id: 'import-original',
            type: 'import',
            status: 'failed',
            config: {
              sources: ['/Volumes/CARD/DCIM'],
              destination: '/Volumes/Photography/Raw Files/USA',
              recursive: true,
              folder_template: '%Y/%Y-%m-%d',
              file_types: 'both',
              skip_duplicates: false,
              verify_by_hash: false,
              trust_likely_duplicates: true,
              after_import: 1,
              tags: ['trip'],
              location_from_gps: true,
              allow_missing_exiftool: false,
              remote_target_id: null,
              remote_subpath: null,
            },
            result: {
              photo_ids: [101, 102, 103],
              failed: 1,
            },
          };
          renderResult({
            discovered: 985,
            copied: 984,
            verified: 984,
            skipped_duplicate: 0,
            failed: 1,
            safe_to_format: false,
            unsafe_files: [{
              path: '/Volumes/CARD/DCIM/DSC_7172.NEF',
              reason: 'copy verification failed',
            }],
            folders: {},
            errors: ['copy verification failed'],
          }, 'failed');
        }
        """
    )

    retry = page.locator("#btnRetryImport")
    expect(retry).to_be_visible()
    expect(retry).to_have_text("Retry 1 failed file")
    retry.click()
    expect(page.locator("#progressCard")).to_be_visible()

    body = captured["body"]
    assert body["sources"] == ["/Volumes/CARD/DCIM"]
    assert body["destination"] == "/Volumes/Photography/Raw Files/USA"
    assert body["folder_template"] == "%Y/%Y-%m-%d"
    # retryBodyFromFinishedJob() preserves the parent's skip_duplicates
    # setting rather than forcing it on, so a parent configured with
    # dedup OFF retries with dedup OFF — otherwise the very file that
    # failed could be silently skipped if it happens to match some
    # unrelated catalog entry. The parent above seeds `false`, so the
    # retry must send `false`.
    assert body["skip_duplicates"] is False
    assert body["after_import"] == 1
    assert body["tags"] == ["trip"]
    # The original run's successful photos must be carried into the retry
    # so the after-import chain covers the complete import scope, not
    # only the newly-recovered file. Without this, the failed original
    # (which skipped chaining because it wasn't OK) plus the retry
    # (which chains on only 1 new photo) silently leave 984 photos
    # unprocessed.
    assert body["carry_photo_ids"] == [101, 102, 103]
    # And it must bind the carry list to the parent so the server can
    # verify each carry ID actually came from that parent — without
    # this binding an API caller could inject arbitrary IDs into the
    # after-import chain scope.
    assert body["parent_import_job_id"] == "import-original"


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

    page.locator("#modeCopy").check()
    page.locator("#sourceInput").fill("/tmp/card-a")
    page.locator("#btnAddSource").click()
    page.locator("#workspaceNew").check()
    page.locator("#newWorkspaceName").fill("Serengeti")
    page.locator("#destInput").fill("/tmp/archive")
    page.locator("#afterImportSelect").select_option(str(identify_id))

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
