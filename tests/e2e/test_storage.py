from playwright.sync_api import expect


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
