import hashlib
import json
import os
import subprocess
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


# --------------------------------------------------------------------------
# verify_digest
# --------------------------------------------------------------------------

# sha256(b"hello")
HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_verify_digest_matches(tmp_path):
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    sha = "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    ok, detail = verify_digest(str(f), sha)
    assert ok, detail


def test_verify_digest_mismatch_is_reported_with_both_hashes(tmp_path):
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"tampered")
    ok, detail = verify_digest(str(f), "sha256:" + "0" * 64)
    assert not ok
    assert "0000" in detail          # expected
    assert "expected" in detail.lower()
    # ...and the hash we actually computed, so a support thread can tell a
    # truncated download from a substituted one.
    assert "d121be3103007b41edf96f8262925f8c7d61894afe9a041843b631f69445bc57" in detail


@pytest.mark.parametrize("differ_at", [0, 7, 8, 15, 16, 31, 32, 62, 63])
def test_verify_digest_compares_the_whole_hash_not_a_prefix(tmp_path, differ_at):
    """A near-miss digest must still be rejected.

    The obvious mismatch case (expected 0*64) is caught even by a
    `actual[:8] == want[:8]` comparison, so on its own it does not prove the
    whole hash was checked.  These differ from the true digest in exactly one
    position, spread across its length, so any truncated comparison passes one
    of them and fails this test.
    """
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")

    chars = list(HELLO_SHA256)
    chars[differ_at] = "0" if chars[differ_at] != "0" else "1"
    near_miss = "".join(chars)
    assert near_miss != HELLO_SHA256

    ok, detail = verify_digest(str(f), "sha256:" + near_miss)
    assert not ok, f"accepted a digest differing at index {differ_at}: {detail}"
    assert "mismatch" in detail.lower()


def test_verify_digest_absent_says_so_rather_than_silently_passing(tmp_path):
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), None)
    assert ok
    assert "no digest" in detail.lower()
    # ...and it must not imply a check that did not happen.
    assert "could not be verified" in detail.lower(), detail


def test_verify_digest_success_detail_does_not_overclaim(tmp_path):
    """The detail is shown to the user verbatim.

    A matching digest proves the bytes are what GitHub's API *said* to expect.
    It is not a signature: the digest arrived over the same HTTPS response as
    the download URL, so it shares that response's trust boundary.  The string
    must not let a user believe darktable signed anything.
    """
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), "sha256:" + HELLO_SHA256)

    assert ok
    lower = detail.lower()
    assert "github" in lower, detail
    # It has to say where the expectation came from *and* that no signature was
    # involved, so nobody reads "verified" as "vouched for by darktable".
    assert "no signature" in lower or "does not sign" in lower, detail
    for overclaim in ("authentic", "safe to run", "trusted", "signed by darktable"):
        assert overclaim not in lower, f"{overclaim!r} overclaims: {detail}"


@pytest.mark.parametrize("expected", [
    "sha256:" + HELLO_SHA256,                    # exactly what the API sends today
    "SHA256:" + HELLO_SHA256.upper(),            # algorithm and hex upper-cased
    "sha256:" + HELLO_SHA256.upper(),
    "  sha256:" + HELLO_SHA256 + "  \n",         # stray whitespace
    HELLO_SHA256,                                # bare hash, no algorithm prefix
    "  " + HELLO_SHA256.upper() + "\t",
])
def test_verify_digest_accepts_every_shape_a_correct_sha256_can_arrive_in(tmp_path, expected):
    """`expected` comes from an external API; hex case, padding and the
    presence of the `sha256:` prefix are all cosmetic.  Rejecting a correct
    digest over cosmetics would delete a perfectly good 178MB download."""
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), expected)
    assert ok, detail


@pytest.mark.parametrize("algorithm,digest", [
    # The *right* hash of the *right* file under a different algorithm.
    ("md5", "5d41402abc4b2a76b9719d911017c592"),
    ("sha1", "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"),
    ("sha512", "9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caadae2dff72519673ca7"
               "2323c3d99ba5c11d7c7acc6e14b8c5da0c4663475c2e5c3adef46f73bcdec043"),
    # 64 hex characters, so it is shaped exactly like a SHA256 and only the
    # algorithm name gives it away.  This is the case that would otherwise be
    # reported as "mismatch — the download was replaced" about an intact file.
    ("blake2s", hashlib.blake2s(b"hello").hexdigest()),
])
def test_verify_digest_rejects_a_digest_from_another_algorithm_by_name(tmp_path, algorithm, digest):
    """Stripping the prefix blindly would compare, say, an MD5 against a
    SHA256 and blame the file.  The detail has to name the algorithm GitHub
    actually sent, otherwise a format change that silently weakens (or
    disables) verification looks identical to a corrupt download."""
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), f"{algorithm}:{digest}")

    assert not ok, f"{algorithm} digest was accepted as if it were SHA256"
    assert "mismatch" not in detail.lower(), f"{detail!r} blames an intact file"
    assert "verif" in detail.lower(), detail
    assert algorithm in detail.lower(), f"{detail!r} does not say GitHub sent a {algorithm} digest"


@pytest.mark.parametrize("expected,label", [
    # Malformed values that can never equal a SHA256 hexdigest.
    ("sha256:" + "z" * 64, "non-hex"),
    ("sha256:" + HELLO_SHA256[:32], "too short"),
    ("sha256:" + HELLO_SHA256 + "ff", "too long"),
    ("sha256:" + "ff" + HELLO_SHA256, "prefixed with junk"),
    ("sha256:", "empty"),
    ({"sha256": HELLO_SHA256}, "not a string"),
])
def test_verify_digest_fails_closed_and_says_why_when_it_cannot_check(tmp_path, expected, label):
    """Fail closed, but with its own sentence.

    An unusable digest is not evidence of tampering, so it must not be reported
    as a mismatch — and it must not pass, because the digest is the only
    integrity check this feature has.  If GitHub ever changes the format this
    breaks loudly on the next release instead of quietly verifying nothing.
    """
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), expected)

    assert not ok, f"{label}: an unverifiable digest must not pass"
    assert "mismatch" not in detail.lower(), f"{label}: {detail!r} blames the file"
    assert "verif" in detail.lower(), f"{label}: {detail!r} does not say it could not verify"


def test_verify_digest_reads_in_chunks_not_all_at_once(tmp_path, monkeypatch):
    """These assets are 87-178MB and Task 7 verifies right after download.
    f.read() with no size would pull the whole file into RAM."""
    import darktable_install
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello" * 1000)

    reads = []
    real_open = open

    class _SpyFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, *args):
            reads.append(args)
            return self._fh.read(*args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def spy_open(path, *args, **kwargs):
        return _SpyFile(real_open(path, *args, **kwargs))

    monkeypatch.setattr(darktable_install, "open", spy_open, raising=False)

    ok, detail = verify_digest(str(f), hashlib.sha256(b"hello" * 1000).hexdigest())

    assert ok, detail
    assert reads, "expected the file to be read"
    assert all(args and isinstance(args[0], int) and args[0] > 0 for args in reads), (
        f"every read() must pass an explicit chunk size, got {reads}"
    )


def test_verify_digest_hashes_the_whole_file_across_chunk_boundaries(tmp_path):
    """A chunked loop that drops or reorders a chunk still 'verifies' small
    test files.  Use a payload several chunks long with distinct content."""
    from darktable_install import verify_digest

    payload = bytes(range(256)) * (5 * 1024)  # 1.25MB, spans >1 chunk at 1MB
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    ok, detail = verify_digest(str(f), "sha256:" + hashlib.sha256(payload).hexdigest())
    assert ok, detail

    # ...and flipping a single byte in the middle must be caught.
    tampered = bytearray(payload)
    tampered[len(tampered) // 2] ^= 0xFF
    f.write_bytes(bytes(tampered))
    ok, detail = verify_digest(str(f), "sha256:" + hashlib.sha256(payload).hexdigest())
    assert not ok
    assert "mismatch" in detail.lower()


def test_verify_digest_reports_an_unreadable_file_instead_of_raising(tmp_path):
    """Task 9's job calls this from a background thread; an OSError escaping
    here would kill the job with no user-facing explanation."""
    from darktable_install import verify_digest

    ok, detail = verify_digest(str(tmp_path / "does-not-exist.bin"), "sha256:" + HELLO_SHA256)
    assert not ok
    assert "read" in detail.lower()


def test_verify_digest_honors_should_cancel_between_chunks(tmp_path):
    """Hashing an ~178MB AppImage takes long enough that a Stop press during
    it must not have to wait for the whole file to finish before the job
    reports cancelled.  A True from should_cancel raises DownloadCancelled so
    the caller (download()) propagates it out to the job runner."""
    import taxonomy
    from darktable_install import verify_digest

    # Big enough to span several chunks (chunk size is 1MB).
    payload = bytes(range(256)) * (5 * 1024)  # 1.25MB
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    with pytest.raises(taxonomy.DownloadCancelled):
        verify_digest(
            str(f),
            "sha256:" + hashlib.sha256(payload).hexdigest(),
            should_cancel=lambda: True,
        )


def test_verify_digest_does_not_call_should_cancel_when_none(tmp_path):
    """The signature change must be backwards compatible: existing callers
    that pass no should_cancel keep working."""
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), "sha256:" + HELLO_SHA256)
    assert ok, detail


def test_download_passes_should_cancel_through_to_verify_digest(tmp_path, monkeypatch):
    """A cancel that arrives during the ~178MB hash must reach verify_digest,
    not only the network read.  Without threading should_cancel through, the
    Stop button appears dead for the whole verification phase."""
    import darktable_install
    import taxonomy
    from darktable_install import download

    payload = b"z" * 64
    _stub_download(monkeypatch, payload)

    seen = []

    def spy_verify(path, expected, *, should_cancel=None):
        seen.append(should_cancel)
        return True, "ok"

    monkeypatch.setattr(darktable_install, "verify_digest", spy_verify)

    def sentinel_cancel():
        return False

    download(
        _asset(payload), dest_dir=str(tmp_path),
        should_cancel=sentinel_cancel,
    )
    assert seen == [sentinel_cancel], (
        "download() must forward its should_cancel into verify_digest"
    )

    # And a cancel from verify_digest propagates as DownloadCancelled,
    # not swallowed into a RuntimeError that would report as "failed".
    def cancelling_verify(path, expected, *, should_cancel=None):
        raise taxonomy.DownloadCancelled("cancelled during hash")

    monkeypatch.setattr(darktable_install, "verify_digest", cancelling_verify)
    with pytest.raises(taxonomy.DownloadCancelled):
        download(_asset(payload), dest_dir=str(tmp_path))


# --- Task 7: install_dir / is_quarantined / hand_off / download / free_space_bytes ---


def _completed(returncode=0, stderr=""):
    """A stand-in for subprocess.run's return value.

    Returning None here instead (a bare recording lambda) would make any test
    that inspects the exit status pass by accident, so the fake keeps the one
    field the caller reads.
    """
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_install_dir_is_under_vireo_home():
    from darktable_install import install_dir

    assert install_dir().endswith(os.path.join(".vireo", "tools", "darktable"))


def test_install_dir_is_exactly_the_directory_develop_probes(monkeypatch):
    """Downloading anywhere else would 'install' darktable and still report it
    missing: darktable_search_paths only lists AppImages in that one dir."""
    import develop
    from darktable_install import install_dir

    assert install_dir() == develop.darktable_tools_dir()

    # ...by asking develop every call, not by re-deriving the same string: a
    # copy would silently keep the old path the day that one moves, and would
    # ignore a relocated HOME because expansion happened at import time.
    monkeypatch.setattr(develop, "darktable_tools_dir", lambda: "/somewhere/else")
    assert install_dir() == "/somewhere/else"


def test_hand_off_linux_makes_appimage_executable_and_returns_bin_path(tmp_path):
    from darktable_install import hand_off

    appimage = tmp_path / "Darktable-5.6.0-x86_64.AppImage"
    appimage.write_bytes(b"stub")
    appimage.chmod(0o644)

    result = hand_off(str(appimage), platform_name="linux")

    assert os.access(str(appimage), os.X_OK)
    assert result["bin_path"] == str(appimage)
    assert result["action"] == "installed"
    assert result["location"] == str(appimage)


def test_hand_off_macos_opens_dmg_and_sets_no_bin_path(tmp_path, monkeypatch):
    """macOS cannot know the final path — the user drags the app themselves."""
    import darktable_install
    from darktable_install import hand_off

    calls = []
    monkeypatch.setattr(darktable_install.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _completed(0))

    dmg = tmp_path / "darktable-5.6.0-arm64.dmg"
    dmg.write_bytes(b"stub")
    result = hand_off(str(dmg), platform_name="darwin")

    assert calls, "expected the DMG to be opened"
    assert list(calls[0][0]) == ["open", str(dmg)]
    assert result["bin_path"] is None
    assert result["action"] == "opened-installer"
    assert result["location"] == str(dmg)


def test_hand_off_windows_launches_the_installer_and_sets_no_bin_path(tmp_path, monkeypatch):
    import darktable_install
    from darktable_install import hand_off

    opened = []
    monkeypatch.setattr(darktable_install.os, "startfile",
                        lambda p: opened.append(p), raising=False)

    exe = tmp_path / "darktable-5.6.0-win64.exe"
    exe.write_bytes(b"stub")
    result = hand_off(str(exe), platform_name="win32")

    assert opened == [str(exe)]
    assert result["bin_path"] is None
    assert result["action"] == "opened-installer"


def test_hand_off_says_so_when_the_installer_could_not_be_opened(tmp_path, monkeypatch):
    """A corrupt DMG makes `open` fail. Reporting "opened-installer" anyway
    would leave the user waiting for a window that never appears."""
    import darktable_install
    from darktable_install import hand_off

    monkeypatch.setattr(darktable_install.subprocess, "run",
                        lambda *a, **k: _completed(1, "no mountable file systems"))

    dmg = tmp_path / "darktable-5.6.0-arm64.dmg"
    dmg.write_bytes(b"stub")

    with pytest.raises(RuntimeError) as exc:
        hand_off(str(dmg), platform_name="darwin")
    # The download is still on disk, so the message must name it.
    assert str(dmg) in str(exc.value)


def test_hand_off_default_platform_agrees_with_develop(tmp_path, monkeypatch):
    """With no override, hand_off must branch the same way darktable_search_paths
    does — otherwise we chmod a file nothing probes, or open an AppImage."""
    import darktable_install
    import develop
    from darktable_install import hand_off

    monkeypatch.setattr(darktable_install.subprocess, "run", lambda *a, **k: _completed(0))
    monkeypatch.setattr(darktable_install.os, "startfile", lambda p: None, raising=False)

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"stub")
    artifact.chmod(0o644)

    result = hand_off(str(artifact))

    if develop.darktable_uses_tools_dir():
        assert result["bin_path"] == str(artifact)
        assert os.access(str(artifact), os.X_OK)
    else:
        assert result["bin_path"] is None
        assert not os.access(str(artifact), os.X_OK)


def test_is_quarantined_false_for_plain_file(tmp_path):
    """Warn about Gatekeeper only when the attribute is really present.

    urllib downloads are not quarantined (LaunchServices applies that, not
    urllib), so an unconditional warning would scare users about a dialog
    they will never see.
    """
    from darktable_install import is_quarantined

    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    assert is_quarantined(str(f)) is False


def test_is_quarantined_true_when_xattr_reports_the_attribute(tmp_path, monkeypatch):
    import darktable_install
    from darktable_install import is_quarantined

    monkeypatch.setattr(darktable_install.sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(darktable_install.subprocess, "run",
                        lambda *a, **k: seen.append(a[0]) or _completed(0, ""))

    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    assert is_quarantined(str(f)) is True
    assert seen == [["xattr", "-p", "com.apple.quarantine", str(f)]]


def test_is_quarantined_false_when_xattr_is_missing(tmp_path, monkeypatch):
    """Never raise: a missing tool must not fail an otherwise good install."""
    import darktable_install
    from darktable_install import is_quarantined

    monkeypatch.setattr(darktable_install.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("xattr")

    monkeypatch.setattr(darktable_install.subprocess, "run", boom)

    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    assert is_quarantined(str(f)) is False


def test_is_quarantined_does_not_shell_out_off_macos(tmp_path, monkeypatch):
    import darktable_install
    from darktable_install import is_quarantined

    monkeypatch.setattr(darktable_install.sys, "platform", "linux")

    # Recorded, not raised: is_quarantined swallows every exception, so a stub
    # that raises would be absorbed and the test would pass with the check gone.
    seen = []
    monkeypatch.setattr(darktable_install.subprocess, "run",
                        lambda *a, **k: seen.append(a) or _completed(0))

    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    assert is_quarantined(str(f)) is False
    assert seen == [], "xattr is macOS-only; do not run it elsewhere"


def _asset(payload, **overrides):
    asset = {
        "name": "darktable-5.6.0-arm64.dmg",
        "url": "https://github.com/darktable-org/darktable/releases/download/"
               "release-5.6.0/darktable-5.6.0-arm64.dmg",
        "size": len(payload),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    asset.update(overrides)
    return asset


def _stub_download(monkeypatch, payload=b"", *, record=None, raises=None):
    """Replace the real network download. No test may touch the network."""
    import taxonomy

    def fake(url, dest_path, **kwargs):
        if record is not None:
            record.append({"url": url, "dest_path": dest_path, **kwargs})
        if raises is not None:
            # Mirror the real function: a cancelled download leaves its
            # .partial behind (0 bytes when cancelled before the first read).
            open(dest_path + ".partial", "wb").close()
            raise raises
        with open(dest_path, "wb") as f:
            f.write(payload)
        return dest_path

    monkeypatch.setattr(taxonomy, "_download_with_resume", fake)


def test_download_writes_the_asset_and_returns_the_verification_detail(tmp_path, monkeypatch):
    from darktable_install import download

    payload = b"darktable" * 100
    _stub_download(monkeypatch, payload)

    path, detail = download(_asset(payload), dest_dir=str(tmp_path))

    assert path == str(tmp_path / "darktable-5.6.0-arm64.dmg")
    assert open(path, "rb").read() == payload
    assert "SHA256 matches" in detail


def test_download_defaults_to_install_dir_and_creates_it(tmp_path, monkeypatch):
    import darktable_install
    from darktable_install import download

    payload = b"x" * 64
    _stub_download(monkeypatch, payload)
    target = tmp_path / "nested" / "tools" / "darktable"
    monkeypatch.setattr(darktable_install, "install_dir", lambda: str(target))

    path, _detail = download(_asset(payload))

    assert path == str(target / "darktable-5.6.0-arm64.dmg")
    assert os.path.isfile(path)


def test_download_deletes_the_file_and_raises_on_a_size_mismatch(tmp_path, monkeypatch):
    """A wrong-length artifact must never reach an installer."""
    from darktable_install import download

    payload = b"short"
    _stub_download(monkeypatch, payload)

    with pytest.raises(RuntimeError) as exc:
        download(_asset(payload, size=999999), dest_dir=str(tmp_path))

    assert "size mismatch" in str(exc.value).lower()
    assert not os.path.exists(str(tmp_path / "darktable-5.6.0-arm64.dmg")), (
        "the bad download must be deleted, not left for the installer"
    )


def test_download_deletes_the_file_and_raises_on_a_digest_mismatch(tmp_path, monkeypatch):
    from darktable_install import download

    payload = b"y" * 32
    _stub_download(monkeypatch, payload)
    wrong = "sha256:" + hashlib.sha256(b"something else").hexdigest()

    with pytest.raises(RuntimeError) as exc:
        download(_asset(payload, digest=wrong), dest_dir=str(tmp_path))

    assert "mismatch" in str(exc.value).lower()
    assert not os.path.exists(str(tmp_path / "darktable-5.6.0-arm64.dmg")), (
        "the bad download must be deleted, not left for the installer"
    )


def test_download_returns_the_unverified_detail_verbatim(tmp_path, monkeypatch):
    """verify_digest returns ok=True for a *missing* digest too, so download
    must pass its sentence through rather than inventing 'verified' wording."""
    from darktable_install import download, verify_digest

    payload = b"z" * 16
    _stub_download(monkeypatch, payload)

    path, detail = download(_asset(payload, digest=None), dest_dir=str(tmp_path))

    assert detail == verify_digest(path, None)[1]
    assert "could not be verified" in detail
    assert "matches" not in detail


def test_download_forwards_progress_and_cancellation_hooks(tmp_path, monkeypatch):
    """byte_callback and should_cancel are keyword-only on _download_with_resume;
    dropping either silently kills the progress bar and the Cancel button."""
    from darktable_install import download

    payload = b"w" * 8
    record = []
    _stub_download(monkeypatch, payload, record=record)

    def on_bytes(done, total):
        pass

    def cancelled():
        return False

    download(_asset(payload), dest_dir=str(tmp_path),
             byte_callback=on_bytes, should_cancel=cancelled)

    assert len(record) == 1
    assert record[0]["url"] == _asset(payload)["url"]
    assert record[0]["byte_callback"] is on_bytes
    assert record[0]["should_cancel"] is cancelled


def test_download_propagates_cancellation_and_keeps_the_empty_partial(tmp_path, monkeypatch):
    """should_cancel is checked before the first read, so an immediately
    cancelled download leaves a 0-byte .partial. That is a normal resume
    state, not corruption: keep it and let the cancel surface as itself."""
    import taxonomy
    from darktable_install import download

    _stub_download(monkeypatch, raises=taxonomy.DownloadCancelled("Download cancelled"))

    with pytest.raises(taxonomy.DownloadCancelled):
        download(_asset(b"q" * 8), dest_dir=str(tmp_path), should_cancel=lambda: True)

    partial = tmp_path / "darktable-5.6.0-arm64.dmg.partial"
    assert partial.exists(), "the .partial must survive for resume"
    assert partial.stat().st_size == 0
    assert not (tmp_path / "darktable-5.6.0-arm64.dmg").exists()


def test_free_space_bytes_creates_the_directory_and_reports_free_not_total(
    tmp_path, monkeypatch
):
    import collections

    import darktable_install

    usage = collections.namedtuple("usage", "total used free")
    seen = []
    monkeypatch.setattr(darktable_install.shutil, "disk_usage",
                        lambda p: seen.append(p) or usage(total=100, used=70, free=30))

    target = tmp_path / "made" / "here"
    assert darktable_install.free_space_bytes(str(target)) == 30
    assert os.path.isdir(str(target))
    assert seen == [str(target)]


def test_free_space_bytes_returns_a_real_number_on_this_filesystem(tmp_path):
    from darktable_install import free_space_bytes

    assert free_space_bytes(str(tmp_path / "sub")) > 0


def test_download_refuses_an_asset_name_that_escapes_the_directory(tmp_path, monkeypatch):
    """The name is API-supplied. Joining it unchecked would let one write
    outside the tools dir — every other field from that response is already
    constrained, and this one must be too."""
    from darktable_install import download

    payload = b"p" * 8
    called = []
    _stub_download(monkeypatch, payload, record=called)

    with pytest.raises(RuntimeError) as exc:
        download(_asset(payload, name="../escaped.dmg"), dest_dir=str(tmp_path / "dl"))

    assert "suspicious" in str(exc.value).lower()
    assert called == [], "nothing should be fetched for a rejected name"
    assert not os.path.exists(str(tmp_path / "escaped.dmg"))


def test_hand_off_does_not_write_config(tmp_path, monkeypatch):
    """bin_path is *returned* so the job handler writes darktable_bin in one
    place, next to the message that tells the user it happened."""
    import config as cfg
    from darktable_install import hand_off

    def boom(*a, **k):
        raise AssertionError("hand_off must not write config")

    monkeypatch.setattr(cfg, "set", boom)
    monkeypatch.setattr(cfg, "save", boom)

    appimage = tmp_path / "Darktable-5.6.0-x86_64.AppImage"
    appimage.write_bytes(b"stub")
    result = hand_off(str(appimage), platform_name="linux")
    assert result["bin_path"] == str(appimage)
