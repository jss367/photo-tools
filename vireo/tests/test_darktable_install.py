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
    # Path traversal: GitHub resolves ".." on receipt, so this actually serves
    # /attacker/evil/... A raw startswith() on the unnormalised path accepts it.
    "https://github.com/darktable-org/darktable/../../attacker/evil"
    "/releases/download/x/darktable-5.6.0-arm64.dmg",
    # Percent-encoded flavour of the same traversal.
    "https://github.com/darktable-org/darktable/%2e%2e/%2e%2e/attacker/evil"
    "/releases/download/x/darktable-5.6.0-arm64.dmg",
    # Traversal that climbs out *after* satisfying the prefix. The narrowed
    # prefix alone does not catch this one — only normalising the path does.
    "https://github.com/darktable-org/darktable/releases/download/../../../../"
    "attacker/evil/darktable-5.6.0-arm64.dmg",
    "https://github.com/darktable-org/darktable/releases/download/%2e%2e/%2e%2e/"
    "%2e%2e/%2e%2e/attacker/evil/darktable-5.6.0-arm64.dmg",
    # Inside the real repo, but repo *content* rather than a release asset:
    # anyone who can open a PR branch can host a file here.
    "https://github.com/darktable-org/darktable/raw/master/darktable-5.6.0-arm64.dmg",
    # Every real browser_download_url is on github.com; the redirect target we
    # deliberately do not check is release-assets.githubusercontent.com. So this
    # host is unreachable in practice and serves arbitrary user blobs.
    "https://objects.githubusercontent.com/darktable-org/darktable"
    "/releases/download/x/darktable-5.6.0-arm64.dmg",
    # Credentials in the userinfo section must not fool the host check.
    "https://github.com@evil.example.com/darktable-org/darktable"
    "/releases/download/x/darktable-5.6.0-arm64.dmg",
])
def test_select_asset_rejects_untrusted_url(url):
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        if a["name"] == "darktable-5.6.0-arm64.dmg":
            a["browser_download_url"] = url
    assert select_asset(release, "darwin", "arm64") is None


def test_select_asset_accepts_a_real_release_asset_url():
    """The allowlist must not be so tight that it rejects the genuine URL."""
    from darktable_install import select_asset

    asset = select_asset(_release(), "darwin", "arm64")
    assert asset is not None
    assert asset["url"] == (
        "https://github.com/darktable-org/darktable/releases/download/release-5.6.0"
        "/darktable-5.6.0-arm64.dmg"
    )


@pytest.mark.parametrize("field,bad_value", [
    ("size", 1024),
    ("browser_download_url", "https://evil.example.com/x.dmg"),
])
def test_select_asset_skips_a_poisoned_entry_and_keeps_looking(field, bad_value):
    """A rejected candidate must not kill the whole lookup.

    Returning None on the first bad entry hands anyone who can inject one asset
    a reliable DoS on the feature. Skipping cannot accept anything untrusted —
    the rejection already happened — and the digest check is the integrity gate.
    """
    from darktable_install import select_asset

    release = _release()
    poison = {
        "name": "darktable-9.9.9-arm64.dmg",
        "size": 90_000_000,
        "browser_download_url": "https://github.com/darktable-org/darktable"
                                "/releases/download/release-9.9.9/darktable-9.9.9-arm64.dmg",
    }
    # Make exactly one field bad so each rejection branch is exercised alone.
    poison[field] = bad_value
    release["assets"].insert(0, poison)

    asset = select_asset(release, "darwin", "arm64")
    assert asset is not None
    assert asset["name"] == "darktable-5.6.0-arm64.dmg"
    assert asset["size"] == 87094261


def test_select_asset_tolerates_missing_digest():
    from darktable_install import select_asset

    release = _release()
    for a in release["assets"]:
        a.pop("digest", None)
    asset = select_asset(release, "darwin", "arm64")
    assert asset is not None
    assert asset.get("digest") is None


@pytest.mark.parametrize("release", [
    None,
    [],
    "rate limited",
    42,
    {"assets": "nope"},
    {"assets": 5},
    {"assets": [1, 2]},
    {"assets": [{"name": 7}]},
    {"assets": [{"name": "darktable-5.6.0-arm64.dmg", "size": "big"}]},
])
def test_select_asset_tolerates_malformed_payloads(release):
    from darktable_install import select_asset

    assert select_asset(release, "darwin", "arm64") is None


class _StubContractError(BaseException):
    """The request resolve_release() made violated the stub's contract.

    Deliberately a BaseException: resolve_release catches ``Exception`` and
    turns it into "could not reach GitHub", which would mask a downloader that
    silently stopped sending its User-Agent or its SSL context.
    """


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
    """Point resolve_release at a canned response instead of the live API.

    Returns the list of recorded calls. The contract checks here are the only
    thing standing between us and shipping a downloader that 403s in
    production: GitHub rejects requests with no User-Agent, and without the
    certifi context HTTPS fails on stock macOS.
    """
    import urllib.request

    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        if not req.full_url.startswith("https://api.github.com/"):
            raise _StubContractError(f"unexpected URL {req.full_url!r}")
        if context is None:
            raise _StubContractError("must pass the certifi SSL context")
        if not req.get_header("User-agent"):
            raise _StubContractError("GitHub 403s requests with no User-Agent")
        if req.get_header("Accept") != "application/vnd.github+json":
            raise _StubContractError(f"bad Accept header {req.get_header('Accept')!r}")
        calls.append({"url": req.full_url, "timeout": timeout, "context": context})
        return _FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _on_supported_platform(monkeypatch):
    import platform as platform_mod

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_mod, "machine", lambda: "arm64")


def test_resolve_release_strips_the_release_tag_prefix(monkeypatch):
    """version is what the UI shows, so it must be "5.6.0", not "release-5.6.0"."""
    from darktable_install import resolve_release

    _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    _on_supported_platform(monkeypatch)

    info, reason = resolve_release()
    assert reason is None
    assert info["version"] == "5.6.0"
    assert info["name"] == "darktable-5.6.0-arm64.dmg"
    assert info["url"].endswith("/darktable-5.6.0-arm64.dmg")
    assert info["size"] == 87094261


def test_resolve_release_only_strips_a_leading_release_prefix(monkeypatch):
    """str.replace() would also gut a tag that contains "release-" mid-string."""
    from darktable_install import resolve_release

    release = _release()
    release["tag_name"] = "5.6.0-prerelease-2"
    _stub_github(monkeypatch, json.dumps(release).encode("utf-8"))
    _on_supported_platform(monkeypatch)

    info, reason = resolve_release()
    assert reason is None
    assert info["version"] == "5.6.0-prerelease-2"


def test_resolve_release_passes_the_timeout_through(monkeypatch):
    """A hung GitHub must not hang the job thread forever."""
    from darktable_install import resolve_release

    calls = _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    _on_supported_platform(monkeypatch)

    assert resolve_release(timeout=3)[1] is None
    assert calls[0]["timeout"] == 3

    assert resolve_release()[1] is None
    assert calls[1]["timeout"] == 15


def test_resolve_release_reports_network_failure_as_unreachable(monkeypatch):
    """A dead network must degrade to a plain link, never to a traceback —
    and never to "no build for your platform", which is a different fact."""
    import urllib.error
    import urllib.request

    from darktable_install import REASON_UNREACHABLE, resolve_release

    def boom(req, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    _on_supported_platform(monkeypatch)

    info, reason = resolve_release()
    assert info is None
    assert reason == REASON_UNREACHABLE
    assert reason == "Could not reach GitHub to check for a darktable release."


def test_resolve_release_reports_http_errors_as_unreachable(monkeypatch):
    """The 60/hr unauthenticated rate limit is a 403, and it is temporary.
    Telling that user "no build for your platform" sends them away for good."""
    import urllib.error
    import urllib.request

    from darktable_install import REASON_UNREACHABLE, resolve_release

    def rate_limited(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limit exceeded", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rate_limited)
    _on_supported_platform(monkeypatch)

    assert resolve_release() == (None, REASON_UNREACHABLE)


def test_resolve_release_reports_unsupported_platform_distinctly(monkeypatch):
    import platform as platform_mod

    from darktable_install import REASON_NO_PLATFORM_BUILD, resolve_release

    calls = _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    monkeypatch.setattr(sys, "platform", "freebsd14")
    monkeypatch.setattr(platform_mod, "machine", lambda: "amd64")

    info, reason = resolve_release()
    assert info is None
    assert reason == REASON_NO_PLATFORM_BUILD
    assert reason == "No darktable build is published for this platform."
    # No release can ever carry a build for a platform we have no suffix for,
    # so there is nothing to ask GitHub about.
    assert calls == []


def test_resolve_release_reports_a_release_with_no_usable_asset(monkeypatch):
    """Supported platform, GitHub answered — but the build is missing from the
    release. That is neither a network problem nor an unsupported platform."""
    from darktable_install import REASON_NO_USABLE_ASSET, resolve_release

    release = _release()
    release["assets"] = [a for a in release["assets"] if not a["name"].endswith(".dmg")]
    _stub_github(monkeypatch, json.dumps(release).encode("utf-8"))
    _on_supported_platform(monkeypatch)

    info, reason = resolve_release()
    assert info is None
    assert reason == REASON_NO_USABLE_ASSET
    assert reason == (
        "The latest darktable release did not contain a usable build for this platform."
    )


def test_resolve_release_survives_select_asset_blowing_up(monkeypatch):
    """"Never raises" is a contract Task 9's job thread relies on, and it must
    hold for the whole body — not just the part that talks to the network."""
    import darktable_install
    from darktable_install import REASON_UNREACHABLE, resolve_release

    _stub_github(monkeypatch, json.dumps(_release()).encode("utf-8"))
    _on_supported_platform(monkeypatch)

    def explode(*a, **kw):
        raise TypeError("a shape select_asset did not anticipate")

    monkeypatch.setattr(darktable_install, "select_asset", explode)

    assert resolve_release() == (None, REASON_UNREACHABLE)


@pytest.mark.parametrize("payload,expected_reason", [
    # Not a release object at all: a rate-limit body, a captive-portal page or
    # a truncated response. Claiming the release lacked a build would be a lie.
    (b"[]", "unreachable"),
    (b"null", "unreachable"),
    (b'"rate limited"', "unreachable"),
    (b'{"assets": "nope"}', "unreachable"),
    (b"<html>Sign in to the hotel wifi</html>", "unreachable"),
    (b"", "unreachable"),
    # Well-shaped release whose assets list is junk: we did reach GitHub.
    (b'{"assets": [1,2]}', "no_usable"),
])
def test_resolve_release_never_raises_on_a_malformed_body(monkeypatch, payload, expected_reason):
    """Task 9's job calls resolve_release() directly, with no except clause of
    its own, so anything that escapes here crashes the job thread."""
    from darktable_install import REASON_NO_USABLE_ASSET, REASON_UNREACHABLE, resolve_release

    _stub_github(monkeypatch, payload)
    _on_supported_platform(monkeypatch)

    info, reason = resolve_release()
    assert info is None
    assert reason == (
        REASON_UNREACHABLE if expected_reason == "unreachable" else REASON_NO_USABLE_ASSET
    )
