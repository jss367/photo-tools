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
import ssl
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

_ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com"}
_ALLOWED_PATH_PREFIX = "/darktable-org/darktable/"

# Anything smaller than this is not a darktable build.  Guards against the
# ~300KB .zsync manifests and against a truncated or renamed asset.
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


def _url_is_trusted(url):
    """True only for HTTPS URLs on a GitHub host under the darktable repo.

    Enforced on the API-supplied browser_download_url only.  GitHub redirects
    release downloads to release-assets.githubusercontent.com, so applying
    this to redirect targets would reject every legitimate download.
    """
    try:
        parts = urllib.parse.urlparse(url or "")
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        return False
    return parts.path.startswith(_ALLOWED_PATH_PREFIX)


def select_asset(release, platform_name, machine):
    """Pick the asset matching this platform, or None.

    Returns a dict with name/size/url/digest, or None when this platform has
    no build, the only match is implausibly small, or the URL is untrusted.
    """
    suffix = _ASSET_SUFFIXES.get((platform_name, str(machine).lower()))
    if not suffix:
        log.info("No darktable asset for platform=%s machine=%s", platform_name, machine)
        return None

    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.endswith(suffix):
            continue
        size = asset.get("size", 0)
        if size < _MIN_ASSET_BYTES:
            log.warning("Rejecting %s: %d bytes is too small to be darktable", name, size)
            return None
        url = asset.get("browser_download_url", "")
        if not _url_is_trusted(url):
            log.warning("Rejecting %s: untrusted download URL %r", name, url)
            return None
        return {
            "name": name,
            "size": size,
            "url": url,
            "digest": asset.get("digest"),
        }
    return None


def resolve_release(timeout=15):
    """Fetch the latest release and select this machine's asset.

    Returns {version, name, size, url, digest} or None.  Never raises for
    network problems — the caller turns None into a plain "Get darktable"
    link rather than a dead button.
    """
    import platform as platform_mod
    import sys

    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={
                "User-Agent": "vireo-darktable-install/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except Exception:
        log.warning("Could not reach the GitHub releases API", exc_info=True)
        return None

    asset = select_asset(release, sys.platform, platform_mod.machine())
    if not asset:
        return None
    return {"version": release.get("tag_name", "").replace("release-", ""), **asset}
