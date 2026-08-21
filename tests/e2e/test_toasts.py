from playwright.sync_api import expect


def test_toasts_use_semantic_colors_and_accessibility_roles(live_server, page):
    page.goto(f"{live_server['url']}/browse")
    page.evaluate(
        """() => {
          window.setTheme('vireo-gold');
          showToast('Saved', 'success');
          showToast('For your information', 'info');
          showToast('Needs attention', 'warning');
          showToast('Failed', 'error');
          showToast('Neutral default');
        }"""
    )

    expected = {
        "success": (
            "var(--success)", "rgb(46, 125, 50)", "rgb(255, 255, 255)",
            "status", "polite",
        ),
        "info": (
            "var(--info)", "rgb(79, 109, 122)", "rgb(255, 255, 255)",
            "status", "polite",
        ),
        "warning": (
            "var(--warning)", "rgb(200, 122, 0)", "rgb(0, 0, 0)",
            "status", "polite",
        ),
        "error": (
            "var(--danger)", "rgb(192, 57, 43)", "rgb(255, 255, 255)",
            "alert", "assertive",
        ),
    }
    for kind, (token, background, foreground, role, live) in expected.items():
        toast = page.locator(f'#toastContainer > [data-type="{kind}"]').first
        expect(toast).to_be_visible()
        assert toast.evaluate("el => el.style.background") == token
        assert toast.evaluate("el => getComputedStyle(el).backgroundColor") == background
        assert toast.evaluate("el => getComputedStyle(el).color") == foreground
        assert toast.get_attribute("role") == role
        assert toast.get_attribute("aria-live") == live

    default_toast = page.locator("#toastContainer > div").last
    expect(default_toast).to_have_text("Neutral default")
    assert default_toast.get_attribute("data-type") == "info"
    assert default_toast.evaluate("el => el.style.background") == "var(--info)"


def test_develop_completion_toast_reads_nested_job_result(live_server, page):
    page.goto(f"{live_server['url']}/browse")

    toasts = page.evaluate(
        """async () => {
          const payloads = [
            {
              status: 'completed',
              result: {developed: 2, errors: 0, total: 2},
              errors: []
            },
            {
              status: 'failed',
              result: {developed: 1, errors: 1, total: 2},
              errors: ['robin.jpg: export failed']
            }
          ];
          const original = {
            confirm: window.confirm,
            safeFetch: window.safeFetch,
            safeEventSource: window.safeEventSource,
            showToast: window.showToast
          };
          const seen = [];
          window.confirm = () => true;
          window.safeFetch = async url => url.endsWith('/status')
            ? {available: true}
            : {job_id: 42};
          window.safeEventSource = (_url, callbacks) => {
            callbacks.onComplete(payloads.shift());
            return {close: () => {}};
          };
          window.showToast = (message, type) => seen.push({message, type});
          try {
            await developPhotos([1, 2]);
            await developPhotos([1, 2]);
            return seen.filter(item => item.message.startsWith('Development'));
          } finally {
            Object.assign(window, original);
          }
        }"""
    )

    assert toasts == [
        {
            "message": "Development complete: 2 developed, 0 errors",
            "type": "success",
        },
        {
            "message": "Development failed: 1 developed, 1 errors",
            "type": "error",
        },
    ]
