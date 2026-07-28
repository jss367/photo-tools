import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "darktable_release.json")


def _release():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.mark.parametrize("platform,machine,expected", [
    ("darwin", "arm64",   "darktable-5.6.0-arm64.dmg"),
    ("darwin", "x86_64",  "darktable-5.6.0-x86_64.dmg"),
    ("win32",  "AMD64",   "darktable-5.6.0-win64.exe"),
    ("win32",  "ARM64",   "darktable-5.6.0-woa64.exe"),
    ("linux",  "x86_64",  "Darktable-5.6.0-x86_64.AppImage"),
    ("linux",  "aarch64", "Darktable-5.6.0-aarch64.AppImage"),
])
def test_select_asset_matrix(platform, machine, expected):
    from darktable_install import select_asset

    asset = select_asset(_release(), platform, machine)
    assert asset["name"] == expected


def test_select_asset_never_picks_zsync():
    """The release ships ~300KB .zsync manifests next to the 178MB AppImages.
    A substring match would 'successfully install' a file that is not darktable."""
    from darktable_install import select_asset

    asset = select_asset(_release(), "linux", "x86_64")
    assert not asset["name"].endswith(".zsync")
    assert asset["size"] > 10 * 1024 * 1024


@pytest.mark.parametrize("machine,expected", [
    ("x86_64", "Darktable-5.6.0-x86_64.AppImage"),
    ("aarch64", "Darktable-5.6.0-aarch64.AppImage"),
])
def test_select_asset_skips_zsync_listed_before_the_appimage(machine, expected):
    """Order must not decide the outcome.

    The fixture happens to list each AppImage before its .zsync sibling, which
    lets a substring matcher pass by luck. GitHub makes no ordering promise, so
    re-run the match with the decoys first: a substring matcher then picks the
    ~300KB manifest and this fails.
    """
    from darktable_install import select_asset

    release = _release()
    release["assets"] = sorted(
        release["assets"], key=lambda a: not a["name"].endswith(".zsync")
    )
    assert release["assets"][0]["name"].endswith(".zsync")

    asset = select_asset(release, "linux", machine)
    assert asset is not None
    assert asset["name"] == expected


def test_select_asset_unknown_platform_returns_none():
    from darktable_install import select_asset

    assert select_asset(_release(), "sunos5", "sparc") is None
    assert select_asset(_release(), "linux", "riscv64") is None


def test_select_asset_rejects_implausibly_small_asset():
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        if a["name"] == "darktable-5.6.0-arm64.dmg":
            a["size"] = 1024
    assert select_asset(release, "darwin", "arm64") is None


@pytest.mark.parametrize("url", [
    "https://evil.example.com/darktable-5.6.0-arm64.dmg",
    "https://github.com/attacker/darktable/releases/download/x/darktable-5.6.0-arm64.dmg",
    "http://github.com/darktable-org/darktable/releases/download/x/darktable-5.6.0-arm64.dmg",
])
def test_select_asset_rejects_untrusted_url(url):
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        if a["name"] == "darktable-5.6.0-arm64.dmg":
            a["browser_download_url"] = url
    assert select_asset(release, "darwin", "arm64") is None


def test_select_asset_accepts_objects_githubusercontent():
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        if a["name"] == "darktable-5.6.0-arm64.dmg":
            a["browser_download_url"] = (
                "https://objects.githubusercontent.com/darktable-org/darktable/x.dmg"
            )
    assert select_asset(release, "darwin", "arm64") is not None


def test_select_asset_tolerates_missing_digest():
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        a.pop("digest", None)
    asset = select_asset(release, "darwin", "arm64")
    assert asset is not None
    assert asset.get("digest") is None


class _FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_github(monkeypatch, payload):
    """Point resolve_release at a canned response instead of the live API."""
    import urllib.request

    def fake_urlopen(req, timeout=None, context=None):
        assert req.full_url.startswith("https://api.github.com/"), req.full_url
        return _FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_resolve_release_strips_the_release_tag_prefix(monkeypatch):
    """version is what the UI shows, so it must be "5.6.0", not "release-5.6.0"."""
    import platform as platform_mod

    from darktable_install import resolve_release

    _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_mod, "machine", lambda: "arm64")

    info = resolve_release()
    assert info["version"] == "5.6.0"
    assert info["name"] == "darktable-5.6.0-arm64.dmg"
    assert info["url"].endswith("/darktable-5.6.0-arm64.dmg")
    assert info["size"] == 87094261


def test_resolve_release_returns_none_on_network_failure(monkeypatch):
    """A dead network must degrade to a plain link, never to a traceback."""
    import urllib.error
    import urllib.request

    from darktable_install import resolve_release

    def boom(req, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert resolve_release() is None


def test_resolve_release_returns_none_for_unsupported_platform(monkeypatch):
    import platform as platform_mod

    from darktable_install import resolve_release

    _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    monkeypatch.setattr(sys, "platform", "freebsd14")
    monkeypatch.setattr(platform_mod, "machine", lambda: "amd64")

    assert resolve_release() is None
