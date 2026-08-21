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
