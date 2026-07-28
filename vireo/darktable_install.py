"""Download and install darktable from its official GitHub releases.

darktable is GPL and publishes signed-by-nobody but digest-bearing release
assets on GitHub.  We resolve the latest release at request time rather than
pinning a version, and verify integrity against the SHA256 digest the GitHub
API publishes alongside each asset.

There is deliberately no code-signature check: darktable does not successfully
notarize its macOS builds (upstream issue #19295) and its Windows installer is
unsigned, so a fail-closed signature check would reject every legitimate
download.  See docs/superpowers/specs/2026-07-26-darktable-download-design.md.
"""

import json
import logging
import platform
import posixpath
import ssl
import sys
import urllib.parse
import urllib.request

import certifi

log = logging.getLogger(__name__)

# Use certifi's CA bundle so HTTPS works on macOS without running
# Install Certificates.command — same convention as taxonomy.py:35,
# labels.py:17, model_verify.py:29 and places.py:57. Without it this
# silently degrades to "Could not reach GitHub" on affected installs.
_ssl_ctx = ssl.create_default_context(cafile=certifi.where())

RELEASES_API = "https://api.github.com/repos/darktable-org/darktable/releases/latest"

_ALLOWED_HOSTS = {"github.com"}
# Every real browser_download_url is
# https://github.com/darktable-org/darktable/releases/download/<tag>/<file>.
# Constraining to the release-download path (not just the repo) means a URL
# pointing at repo *content* — /raw/master/evil.sh — cannot pass.
_ALLOWED_PATH_PREFIX = "/darktable-org/darktable/releases/download/"

# Anything smaller than this is not a darktable build.  This compares the size
# the GitHub API *reports*, so it filters out the small siblings listed next to
# the real builds (the ~300KB .zsync manifests, the 195-byte .asc signature) and
# an asset renamed to look like a build.  It cannot detect a truncated download —
# that is the digest check's job.
_MIN_ASSET_BYTES = 10 * 1024 * 1024

# (sys.platform, platform.machine()) -> required asset-name suffix.
# Exact suffixes, never substrings: "Darktable-5.6.0-x86_64.AppImage.zsync"
# contains "AppImage" but is a 300KB delta manifest, not the application.
_ASSET_SUFFIXES = {
    ("darwin", "arm64"): "-arm64.dmg",
    ("darwin", "x86_64"): "-x86_64.dmg",
    ("win32", "amd64"): "-win64.exe",
    ("win32", "arm64"): "-woa64.exe",
    ("linux", "x86_64"): "-x86_64.AppImage",
    ("linux", "aarch64"): "-aarch64.AppImage",
}

# User-facing failure sentences returned by resolve_release().  These are shown
# verbatim next to a plain "Get darktable" link, so each one must state the
# fact the user needs in order to act: retry later, or stop looking.
REASON_UNREACHABLE = "Could not reach GitHub to check for a darktable release."
REASON_NO_PLATFORM_BUILD = "No darktable build is published for this platform."
REASON_NO_USABLE_ASSET = (
    "The latest darktable release did not contain a usable build for this platform."
)


def _url_is_trusted(url):
    """True only for HTTPS release-asset URLs under the darktable repo.

    Enforced on the API-supplied browser_download_url only.  GitHub redirects
    release downloads to release-assets.githubusercontent.com, so applying
    this to redirect targets would reject every legitimate download.

    The path is percent-decoded and normalised before the prefix test: GitHub
    resolves ".." on receipt, so a raw startswith() would accept
    /darktable-org/darktable/../../attacker/evil/releases/download/x/evil.exe.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if (parts.hostname or "").lower() not in _ALLOWED_HOSTS:
        return False
    path = posixpath.normpath(urllib.parse.unquote(parts.path)).lower()
    return path.startswith(_ALLOWED_PATH_PREFIX)


def select_asset(release, platform_name, machine):
    """Pick the asset matching this platform, or None.

    Returns a dict with name/size/url/digest, or None when this platform has
    no build and when every asset whose name matches this platform's suffix was
    rejected as implausibly small or as having an untrusted URL.  A rejected
    candidate is skipped, not fatal: a single poisoned entry ahead of the
    genuine asset must not take the feature down.

    Tolerates a malformed release payload (anything that is not a dict, or an
    "assets" list holding non-dict entries) by returning None.
    """
    if not isinstance(release, dict):
        return None

    suffix = _ASSET_SUFFIXES.get((platform_name, str(machine).lower()))
    if not suffix:
        log.info("No darktable asset for platform=%s machine=%s", platform_name, machine)
        return None

    assets = release.get("assets")
    if not isinstance(assets, list):
        return None

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str) or not name.endswith(suffix):
            continue
        size = asset.get("size")
        if not isinstance(size, int) or size < _MIN_ASSET_BYTES:
            log.warning("Rejecting %s: %r bytes is too small to be darktable", name, size)
            continue
        url = asset.get("browser_download_url", "")
        if not _url_is_trusted(url):
            log.warning("Rejecting %s: untrusted download URL %r", name, url)
            continue
        return {
            "name": name,
            "size": size,
            "url": url,
            "digest": asset.get("digest"),
        }
    return None


def resolve_release(timeout=15):
    """Fetch the latest release and select this machine's asset.

    Returns ``(release, None)`` on success, or ``(None, reason)`` where reason
    is a user-facing sentence explaining which failure occurred.  The caller
    shows that sentence next to a plain "Get darktable" link, so it must be
    true and specific: "no build for your platform" and "we could not reach
    GitHub" are different facts and users act on them differently.

    Never raises.
    """
    machine = platform.machine()
    if (sys.platform, str(machine).lower()) not in _ASSET_SUFFIXES:
        log.info("No darktable build for platform=%s machine=%s", sys.platform, machine)
        return None, REASON_NO_PLATFORM_BUILD

    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={
                # GitHub answers 403 to requests with no User-Agent.
                "User-Agent": "vireo-darktable-install/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
            # A rate-limit body, a captive-portal login page or a truncated
            # response parses fine but is not a release.  Saying "no usable
            # build in the latest release" here would be a lie.
            raise ValueError("GitHub returned something that is not a release object")
        asset = select_asset(release, sys.platform, machine)
    except Exception:
        log.warning("Could not reach the GitHub releases API", exc_info=True)
        return None, REASON_UNREACHABLE

    if not asset:
        return None, REASON_NO_USABLE_ASSET

    tag = release.get("tag_name")
    version = tag.removeprefix("release-") if isinstance(tag, str) else ""
    return {"version": version, **asset}, None
