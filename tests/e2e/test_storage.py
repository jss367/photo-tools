from pathlib import Path

from playwright.sync_api import expect


def test_storage_shows_named_categories_and_escaped_file_paths(live_server, page):
    db_path = live_server["db"]._db_path
    backup = Path(f"{db_path}.bak-<img src=x onerror=alert(1)>")
    backup.write_bytes(b"backup" * 1024)
    originals = Path(live_server["app"].config["THUMB_CACHE_DIR"]).parent / "originals"
    originals.mkdir(exist_ok=True)
    (originals / "1.display.jpg").write_bytes(b"render" * 1024)

    page.goto(f"{live_server['url']}/storage")
    expect(page.locator("#storageGrid")).to_contain_text("Catalog backups")
    expect(page.locator("#storageGrid")).to_contain_text("Full-resolution renders")
    expect(page.locator("#storageGrid")).not_to_contain_text("Other Vireo Data")
    details = page.locator("#storageBreakdown details").filter(has_text="Catalog backups")
    details.locator("summary").click()
    expect(details.locator("code").filter(has_text=backup.name)).to_be_visible()
    expect(details.locator(".storage-breakdown-row").filter(has_text=backup.name)).to_contain_text("6 KB")
    expect(details.locator("img")).to_have_count(0)


def test_quota_apply_aborts_when_pending_storage_autosave_fails(
    live_server, page,
):
    """A failed queued autosave must prevent the quota POST and stay editable."""
    posted_payloads = []

    def config_route(route, request):
        if request.method != "POST":
            route.continue_()
            return
        payload = request.post_data_json
        posted_payloads.append(payload)
        if "working_copy_cache_max_mb" in payload:
            route.fulfill(status=200, json={"ok": True})
        else:
            route.fulfill(status=500, json={"error": "autosave failed"})

    page.route("**/api/config", config_route)
    page.goto(f"{live_server['url']}/storage", timeout=5000)

    page.evaluate(
        """async () => {
          document.getElementById('cfgPreviewCacheMaxMb').value = '123';
          saveStorageConfig();
          await commitWorkingCopyLimit(40960, { confirmed: false });
        }"""
    )

    assert posted_payloads
    assert not any(
        "working_copy_cache_max_mb" in payload for payload in posted_payloads
    )
    expect(page.locator("#cfgWorkingCopyCacheMaxGb")).to_be_enabled()
    expect(page.locator("body")).to_contain_text(
        "the working-copy limit was not updated"
    )
