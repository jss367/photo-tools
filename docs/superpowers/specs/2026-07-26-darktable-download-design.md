# Download darktable from Settings

**Date:** 2026-07-26
**Status:** Approved (design)

## Problem

The "RAW Development (darktable)" panel in Settings shows two red rows when the
tools are missing:

```
✗ darktable-cli not found        Install darktable or set the path below
✗ Adobe DNG Converter not found  Install it or set the path below
```

Both rows are dead ends. They tell the user something is missing and leave them
to find, download, and install it on their own. `develop.py:217` says as much in
its error text: *"Adobe DNG Converter not found or not configured. You will need
to download it from Adobe."*

There is also a detection gap that makes the darktable row wrong for some users
who have already installed it — see "Detection gap" below.

## Scope

**In scope:** downloading darktable.

**Out of scope:** downloading Adobe DNG Converter. It is proprietary and
EULA-gated, with no stable public URL we can legitimately hotlink. That row gets
a plain "Get it from Adobe ↗" link and nothing more.

**Out of scope:** flatpak detection. A flatpak install has no invokable path; it
requires `flatpak run --command=darktable-cli org.darktable.Darktable`. Supporting
it means `find_darktable` returns a command list instead of a path, and every
caller changes. Worth doing, but separately.

## Source of truth

darktable is GPL and publishes official release assets on GitHub. As of
`release-5.6.0`:

| Asset | Size |
|---|---|
| `darktable-5.6.0-arm64.dmg` | 87 MB |
| `darktable-5.6.0-x86_64.dmg` | 93 MB |
| `darktable-5.6.0-win64.exe` | 142 MB |
| `darktable-5.6.0-woa64.exe` | 107 MB |
| `Darktable-5.6.0-x86_64.AppImage` | 178 MB |
| `Darktable-5.6.0-aarch64.AppImage` | 171 MB |

### Version resolution

Query the GitHub releases API for `latest` at click time, then hard-allowlist the
host (`github.com` / `objects.githubusercontent.com`) and the repo path
(`darktable-org/darktable`).

**Why not pin a version + SHA256** like `scripts/fetch_exiftool.py` does: darktable
releases a few times a year, so a pinned build goes stale between Vireo releases,
and a pinned URL 404s if an asset is ever renamed. Fetching from darktable's own
GitHub releases over HTTPS is the same trust boundary the user gets by clicking
through to darktable.org themselves — this does not lower the bar.

The allowlist is enforced on the `browser_download_url` returned by the API, **not
on redirect targets** — GitHub redirects release assets to
`release-assets.githubusercontent.com`, so an allowlist applied to redirects would
reject every legitimate download.

### Integrity: the API's own SHA256 digest

The GitHub releases API returns a `digest` field per asset, and darktable's
release assets have one:

```
darktable-5.6.0-arm64.dmg  →  sha256:49aec447e891ab481e436b4c0231fc3c8d0001aad220762ae8e765d3bda5d102
```

We hash the downloaded file and compare. This **fails closed**: on mismatch the
job errors and the file is deleted. Unlike a pinned hash this is never stale, and
unlike a code-signature check it is something darktable actually publishes.

**What this proves, stated honestly, because the UI will say it:** the bytes on
disk match what GitHub's API said the asset is. It catches truncation, CDN
corruption, and a wrong-asset mixup. It does **not** prove darktable signed the
build — the digest arrives over the same HTTPS response as the URL, so it shares
that trust boundary. If the `digest` field is absent for an asset, we say
"no digest published — size checked only" rather than silently skipping. That
fallback message is backed by a real check: `download_asset` compares the final
file size to the API's `size` field. (This is a different claim from
`_download_with_resume`'s existing `Content-Length` truncation check, which the
serving host supplies.)

### No code-signature check

An earlier draft specified `spctl --assess` on macOS and Authenticode on Windows.
**That would have deleted every legitimate download.** darktable's macOS build is
not successfully notarized — upstream issue #19295 says notarization "is
implemented but — as it seems — currently not working," it was closed as not
planned, and Homebrew disabled the darktable cask over it. The Windows installer
trips SmartScreen as an unknown publisher. The only signature on `release-5.6.0`
is `darktable-5.6.0.tar.xz.asc`, which covers the **source tarball only**.

So there is no signature to check, and pretending to check one is worse than not
checking. (Also worth recording so nobody re-adds it: `spctl --assess` is a
Gatekeeper *policy* assessment, not a signature-validity check — that would be
`codesign --verify --strict`. They are different questions.)

### Gatekeeper: conditional, not assumed

An earlier draft of this section made an unconditional pre-download warning that
macOS would call the DMG "damaged," with an `xattr -d com.apple.quarantine`
remedy. That was wrong in the other direction, and it is worth recording why.

`com.apple.quarantine` is applied by LaunchServices for browser-style downloads,
not by `urllib`. Measured on macOS 15 (Darwin 25.5.0), a file fetched with
`urllib.request.urlretrieve` carries only `com.apple.provenance`; `xattr -p
com.apple.quarantine` reports *"No such xattr"* and the suggested `xattr -d`
remedy exits 1. Vireo's download runs in the Python sidecar, and
`src-tauri/Entitlements.plist` sets hardened runtime without
`com.apple.security.app-sandbox` or `LSFileQuarantineEnabled`, so there is no
quarantine-propagation path either.

The likely real outcome is therefore **no Gatekeeper dialog at all** — our
download path is quieter than the user fetching the DMG in a browser, precisely
because it bypasses LaunchServices.

Since we cannot promise that for every macOS version, the warning is
**conditional and evidence-based**, never unconditional:

- After download, check for `com.apple.quarantine` on the file.
- Present, and on macOS → show the explanation and the `xattr -d` command.
- Absent → show nothing. Do not warn about a dialog the user will not see.

**Open item for implementation:** confirm the attribute is genuinely absent when
the download runs inside the *packaged* Tauri app, not just under a dev-mode
Python process. The conditional check makes either outcome correct, so this does
not block planning — but the finding should be recorded.

## Per-platform behavior

The three platforms genuinely differ, and the UI does not pretend otherwise.

| Platform | Asset | What Vireo does | What the user does |
|---|---|---|---|
| macOS | `.dmg` | Download, verify SHA256 digest, open the DMG | Drag darktable to Applications, click Re-check |
| Windows | `.exe` | Download, verify SHA256 digest, launch the installer | Click through the installer, click Re-check |
| Linux | `.AppImage` | Download, `chmod +x`, place in `~/.vireo/tools/darktable/`, set `darktable_bin` | Nothing |

On Linux this is a fully managed install, because no installer exists to hand off
to. That asymmetry is stated in the UI rather than papered over.

## Transparency requirements

Per `CORE_PHILOSOPHY.md` ("Show the user what's happening / No black boxes"),
every one of these is a requirement, not a nicety:

1. **Button labels state what actually happens.** macOS/Windows say **"Download
   installer"**, not "Install darktable" — we do not install it. Linux says
   **"Download and set up"**, because there we do.
2. **The confirmation names the exact artifact before any bytes move**: version,
   filename, size, and source host. *"darktable 5.6.0 — `darktable-5.6.0-arm64.dmg`,
   87 MB, from github.com/darktable-org/darktable"*.
3. **Progress reports real bytes**, not a spinner: downloaded / total and current
   phase, via the existing job progress payload.
4. **The integrity check reports its result either way** — "SHA256 matches the
   digest published by GitHub" on success, both hashes on mismatch — and says
   what that does and does not prove rather than implying darktable signed it.
   The two OS-trust warnings land at different moments, because the two things
   happen at different moments: the macOS quarantine notice is **post-download and
   conditional** on the attribute actually being present (see "Gatekeeper:
   conditional, not assumed"), while the Windows SmartScreen unknown-publisher
   prompt fires when the *installer launches*, so it is stated **just before
   hand-off**, not at download time.
5. **The landing place is named.** On macOS/Windows: "Downloaded to `<path>` —
   opening the installer." On Linux: "Installed to `<path>` and set the
   darktable-cli path in Settings."
6. **No silent config mutation.** Linux sets `darktable_bin`; the UI says so, and
   the field visibly updates.
7. **A failed re-check explains itself.** If darktable is still not found after
   the hand-off, list the locations that were checked rather than repeating the
   same bare ✗. Nothing today can produce that list — `find_darktable` returns a
   path or `None` and discards its candidate set, and `/api/darktable/status`
   (`app.py:15910-15930`) exposes only the resolved outcome — of its ten fields,
   the darktable-detection ones are `available` / `bin` / `configured_bin`, and
   none is the candidate list. So this requires a real mechanism, specified under
   Components: a `darktable_search_paths()` companion function and a
   `checked_paths` field on the status response. Without this the requirement
   silently becomes a
   `COUNT(*) > 0`-style proxy, which `CLAUDE.md` forbids.
8. **The button is never dead.** If the GitHub API is unreachable, rate-limited,
   or has no asset for this platform/arch, the panel shows the plain
   "Get darktable ↗" link and states why the download option is unavailable.

## Two traps

### The AppImage / realpath collision

`find_darktable` calls `os.path.realpath()` on the configured path
(`develop.py:59`). That is deliberate — the docstring explains that invoking
darktable-cli through the Homebrew cask's symlink dies in `dt_init` because
darktable locates its bundled resources by walking up from `argv[0]`.

darktable's AppImage `AppRun` is multi-binary and selects which binary to run in
one of two ways: `argv[1]` naming the binary, or the basename of `$ARGV0` when
invoked through a symlink. So the obvious approach — symlink
`~/.vireo/tools/darktable/darktable-cli` at the AppImage — would be destroyed by
`realpath`, and Vireo would **launch the darktable GUI instead of the CLI**,
hanging a headless export job.

**Resolution:** point `darktable_bin` at the `.AppImage` itself, and teach
`build_command()` (`develop.py:118-136`) to prepend `darktable-cli` as `argv[1]`
when the binary path ends in `.AppImage`. This survives `realpath`, and it is a
localized change: `build_command` is the only command constructor and has a single
call site at `develop.py:307`.

Separately — and this is a *different* place in the file — set
`APPIMAGE_EXTRACT_AND_RUN=1` as a fallback for distributions without FUSE2.
`build_command` returns a list and never touches the environment, so this belongs
at the `subprocess.run` in `develop_photo` (`develop.py:311`), which today passes
only `**no_window_kwargs()` and no `env`. It gains an `env=` derived from
`os.environ`. Note `develop.py` has two `subprocess.run` sites — line 230 is the
DNG converter and must not be touched.

### Detection gap

`find_dng_converter` probes `/Applications/Adobe DNG Converter.app/...`
(`develop.py:110`), but `find_darktable` does not probe the equivalent darktable
bundle — it only tries `shutil.which("darktable-cli")` and Windows Program Files
(`develop.py:58-75`). A normal macOS `.dmg` install of darktable is therefore
invisible to Vireo, and the panel reports ✗ for a user who has it installed.

This is not optional inside this feature: the macOS hand-off ends with the user
dragging `darktable.app` to `/Applications`, which puts nothing on `PATH`. Without
the fix, a successful download is followed by the same ✗.

`find_darktable` gains, after the `which` probe:
- `/Applications/darktable.app/Contents/MacOS/darktable-cli`
- `~/Applications/darktable.app/Contents/MacOS/darktable-cli`
- `~/.vireo/tools/darktable/*.AppImage`

## Components

### `vireo/darktable_install.py` (new)

One module, four functions, each independently testable:

- `resolve_release()` → `{version, name, size, url, digest}` or `None`. Queries the
  GitHub releases API, selects the asset by `(sys.platform, machine)`, enforces the
  host and repo-path allowlist.

  **Asset matching must be exact-suffix, not substring.** The release also carries
  `Darktable-5.6.0-x86_64.AppImage.zsync` and `…-aarch64.AppImage.zsync` — ~300 KB
  delta-update manifests sitting right next to the 178 MB AppImages. A matcher
  looking for `"AppImage"` anywhere in the name will happily pick the `.zsync` and
  "successfully" install a 300 KB file that is not darktable. Match on the name
  *ending* in the expected extension, and reject any asset whose size is
  implausibly small.

  `digest` is the API's `sha256:…` string, or `None`
  when the API published none — it is the only source of the expected hash, so it
  must be carried through here and echoed on `/api/darktable/install/available`.
- `download_asset(url, dest, progress_cb, should_cancel, expected_size)` → path.
  Resumable and cancellable; verifies the final file size against the API's `size`
  field, which is what backs the "size checked only" message.
- `verify_digest(path, expected)` → `(ok, detail)`. SHA256 the file and compare
  against the API's `digest`. Returns `(True, "no digest published")` when the
  API supplied none.
- `hand_off(path)` → `{action, location, bin_path}`. Opens the DMG, launches the
  EXE, or installs the AppImage. It does **not** write config — it returns
  `bin_path` and the job handler writes `darktable_bin`, so config mutation stays
  in one place alongside the message that reports it (transparency requirement #6).

### `vireo/develop.py` (modified)

- `darktable_search_paths()` (new) returns the ordered list of locations
  `find_darktable` probes on this platform. `find_darktable` is refactored to walk
  that list so the two can never drift. This is what backs transparency
  requirement #7.
- `build_command()` gains the `.AppImage` `argv[1]` prefix described above.

### Download location

All downloads land in `~/.vireo/tools/darktable/`, on every platform — that is
where the `.partial` resume file lives, which filesystem the free-space check
measures, and where the installer is left after hand-off on macOS/Windows (we do
not delete it; the user may want to re-run it). On Linux it is also the final
install location.

Because installers are deliberately kept, this directory accumulates versions over
time. When `find_darktable` falls back to probing
`~/.vireo/tools/darktable/*.AppImage` it picks the **newest mtime**, so detection
is deterministic. On Linux the explicitly written `darktable_bin` takes precedence
anyway; the glob only matters if config was cleared.

### The second "darktable is missing" surface

`platform_support.dependency_readiness()` (`platform_support.py:154-209`) reports
darktable independently, and it is surfaced through `platform_support_info()` at
`app.py:18955` and `app.py:24728`. It is subject to the same transparency rule, so
it must not disagree with the settings panel.

It already calls `find_darktable(config.get("darktable_bin", ""))` at
`platform_support.py:157`, so it **inherits the detection fix for free** — no
change needed, and the two surfaces cannot diverge on availability. The same holds
for the third production caller, inside the develop job at `app.py:25527-25530`:
every consumer resolves darktable through `find_darktable`, so fixing the detector
fixes all of them at once. Its hint text
("Install Darktable or configure darktable-cli under Settings → Paths") stays
accurate, since Settings is exactly where the new button lives.

Adding a download button to the readiness panel is **out of scope**. One entry
point is enough, and the hint already points at it.

### Extending `_download_with_resume`

`taxonomy._download_with_resume` (`taxonomy.py:39-150`) is promoted to a shared
helper rather than duplicated — it already handles `.partial` files, HTTP `Range`
resume, truncation detection, and stall counting, all of which matter for a
90–180 MB download on unreliable wifi.

But it cannot satisfy this spec as written, and that gap is the single largest
piece of work here:

- Its `progress_callback(message)` takes a **status string**, emitted only at
  attempt start, interruption, and completion. It never reports
  `downloaded / total`. Transparency requirement #3 demands real byte counts, and
  the front-end pattern at `settings.html:2776-2820` reads `p.current` / `p.total`
  — reusing it unchanged renders a permanently empty progress bar.
- It has **no cancellation hook** and retries indefinitely with `time.sleep(3)`.
  Vireo job cancellation is cooperative via `runner.is_cancelled(job_id)`
  (`jobs.py:1241`), so "the job is cancellable" is currently false for it.

No existing downloader in the repo emits byte progress, so there is nothing to
copy. (`models.py:731-738` and `models.py:859-863` do pass `current=` / `total=`
through to `app.py:18090`, but those are **file counts**, not bytes.)

**Resolution, chosen to keep the blast radius small:** add two *optional*
keyword-only parameters and leave the existing `progress_callback(message)`
contract untouched, so both current callers (`taxonomy.py:458`, `taxonomy.py:944`)
and the seven existing `test_download_with_resume_*` tests in
`vireo/tests/test_taxonomy.py` — including `test_download_with_resume_callback` —
keep passing without modification.

```python
def _download_with_resume(url, dest_path, progress_callback=None,
                          max_stalled=3, chunk_size=256 * 1024,
                          *, byte_callback=None, should_cancel=None):
```

- `byte_callback(downloaded, total_or_None)` fires from the existing chunk loop
  (`taxonomy.py:97-102`), throttled to at most once per 250 ms so a 178 MB
  AppImage does not flood the SSE ring buffer (`maxsize=200`).
- `should_cancel()` is checked in the same loop and before each retry sleep;
  returning `True` aborts, leaves the `.partial` in place, and raises a
  cancellation error the job handler translates into a cancelled status.

### Routes in `vireo/app.py`

- `GET /api/darktable/install/available` → the resolved release info for the
  confirmation, or `{available: false, reason}` for the fallback link.
- `POST /api/jobs/download-darktable` → JobRunner job, following the
  `api_job_develop` shape at `app.py:25519-25520`.
- `GET /api/darktable/status` (existing, `app.py:15910-15930`) gains a
  `checked_paths` field **composed by the route**, not returned raw from
  `darktable_search_paths()`. The invariant is *every entry names a location
  `find_darktable` genuinely probes* — which is why the route may add entries
  the detector's own list does not contain:
  - `"$PATH (darktable-cli)"`, because `find_darktable` tries `shutil.which`
    before any filesystem candidate. Omitting it would make "we checked here"
    untrue by omission, and it is the probe most likely to explain a Homebrew
    or distro user's miss.
  - the Linux tools directory, because `darktable_search_paths()` returns only
    AppImages *already present* there — so a fresh Linux box would otherwise
    get an empty list, on exactly the platform the download targets.

  Do not "simplify" this to return the detector's list directly; both of the
  above are load-bearing. The configured path stays out of `checked_paths` and
  is surfaced separately by the UI from the existing `configured_bin` field.

Both new routes are added to `vireo/tests/contracts/routes.txt`. The job emits
progress via the shared `progress_event()` / `failure_event()` helpers in
`vireo/job_contract.py`, which `vireo/tests/test_job_contract.py` enforces.

Linux writes `darktable_bin` to the global `~/.vireo/config.json`, not to a
workspace `config_overrides` entry — `config_schema.py:263-266` declares
`darktable_bin` as `scope: "global"`, and an installed binary is a property of the
machine, not of a workspace.

### Front end in `vireo/templates/settings.html`

`loadDarktableStatus()` (`:2166-2187`) additionally calls
`/api/darktable/install/available` and renders either the download button or the
fallback link in the ✗ row. The download flow reuses the model-download pattern
at `:2776-2820`: POST, reveal a progress div, `safeEventSource` on
`/api/jobs/<id>/stream`.

## Data flow

```
loadDarktableStatus()
  → GET /api/darktable/status          (already exists)
  → GET /api/darktable/install/available
      ├─ available  → render "Download installer" / "Download and set up"
      └─ unavailable→ render "Get darktable ↗" + reason

click → confirm(version, name, size, host)
  → POST /api/jobs/download-darktable
      → resolve_release()      (re-resolved server-side; the client value is display only)
      → download_asset()       → byte progress events, cancellable
      → verify_digest()        → mismatch: delete file, error the job
      → hand_off()             → macOS: open dmg | Windows: startfile | Linux: install, return bin_path
      → job handler writes darktable_bin (Linux only) and reports that it did
  → SSE complete → re-run loadDarktableStatus()
```

## Error handling

| Condition | Behavior |
|---|---|
| GitHub API unreachable or rate-limited | `available: false`; panel shows the plain link and the reason |
| No asset matches platform/arch | `available: false` with that reason |
| Insufficient disk space on the `~/.vireo/tools/` filesystem (< 2× asset size) | Refuse before starting, state the requirement and what is free |
| Download interrupted | Resumes from `.partial`; the job is cancellable |
| SHA256 does not match the API's `digest` | Job errors showing both hashes; file deleted |
| Downloaded size does not match the API's `size` | Job errors showing both sizes; file deleted |
| API published no `digest` for the asset | Proceed; state "no digest published — size checked only" |
| Hand-off fails (no installer app, permission denied) | Job errors and names the downloaded file's path so the user can open it manually |
| Still not found after re-check | List the locations that were checked |

## Testing

**New — `vireo/tests/test_darktable_install.py`:**
- asset selection matrix across `(darwin, arm64)`, `(darwin, x86_64)`,
  `(win32, AMD64)`, `(win32, ARM64)`, `(linux, x86_64)`, `(linux, aarch64)`,
  against a recorded releases JSON fixture
- unknown platform/arch → `None`, not a wrong asset
- Linux selection picks `Darktable-5.6.0-x86_64.AppImage`, **never**
  `Darktable-5.6.0-x86_64.AppImage.zsync` — the decoy is in the fixture
- host allowlist rejects a spoofed `browser_download_url`, and accepts a
  legitimate one whose redirect target is `release-assets.githubusercontent.com`
- resume path picks up an existing `.partial`
- `build_command` prepends `darktable-cli` for an `.AppImage` and does not for a
  plain binary
- `verify_digest` returns `(False, detail)` on a hash mismatch and the caller
  deletes the file; returns `(True, "no digest published")` when the fixture's
  asset has no `digest` field

**Extended — `vireo/tests/test_taxonomy.py`:**
- `byte_callback` receives increasing `downloaded` values and the total when
  `Content-Length` is present, and `None` when it is absent
- `should_cancel` returning `True` mid-stream aborts and leaves the `.partial`
- existing `test_download_with_resume_callback` still passes unmodified, proving
  the `progress_callback(message)` contract is intact

**Extended — `vireo/tests/test_darktable_api.py`:**
- `/api/darktable/install/available` response shape
- fallback shape when `resolve_release` raises
- `POST /api/jobs/download-darktable` returns a `job_id`

**Extended — `vireo/tests/test_develop.py`:**
- `find_darktable` locates `/Applications/darktable.app/...` with a monkeypatched
  `os.path.isfile`
- a configured path still wins over the bundle probe
- `darktable_search_paths()` and `find_darktable` probe the same locations in the
  same order, so `checked_paths` can never drift from reality

No test hits the network; `resolve_release` is exercised against a fixture.
`vireo/tests/contracts/api_responses.json` is opt-in and is deliberately not
touched — the new routes only need entries in `routes.txt`.
