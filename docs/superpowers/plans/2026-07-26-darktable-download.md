# Download darktable from Settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dead-end "✗ darktable-cli not found" row in Settings into a working download-and-install affordance.

**Architecture:** A new `vireo/darktable_install.py` resolves the correct asset from the GitHub releases API, downloads it with byte-level progress through the existing `_download_with_resume` helper (extended with progress and cancellation hooks), verifies it against the API's published SHA256 digest, and hands off to the platform's installer. Two Flask routes back it: one returning what would be downloaded, one running it as a JobRunner job with SSE progress. Detection in `develop.py` is fixed first, because without it a successful install still reports "not found".

**Tech Stack:** Python 3 / Flask, `urllib.request`, `hashlib`, SQLite-free (no schema changes), vanilla JS + SSE in `settings.html`, pytest.

**Spec:** `docs/superpowers/specs/2026-07-26-darktable-download-design.md`

---

## Background you need

**Read the spec first.** It records two traps and one reversal that are not obvious from the code:

1. **The AppImage/`realpath` collision.** `find_darktable` calls `os.path.realpath()` deliberately (see its docstring). darktable's AppImage picks which bundled binary to run from `argv[1]` *or* from the basename of the symlink used to invoke it. So a `darktable-cli` symlink pointing at the AppImage gets resolved away by `realpath`, and darktable launches its **GUI** instead of the CLI — which hangs a headless export job until the 120 s timeout. Task 3 solves this via `argv[1]`, which survives `realpath`.
2. **The macOS detection gap.** `find_darktable` never probes `/Applications/darktable.app/`, though `find_dng_converter` probes its Adobe equivalent. A normal `.dmg` install is invisible. This is Task 1 and must land first.
3. **No code-signature check.** darktable does not successfully notarize its macOS builds and its Windows installer is unsigned. A fail-closed `spctl`/Authenticode check would delete every legitimate download. Integrity comes from the GitHub API's `digest` field instead. **Do not add a signature check.**

**Repo conventions:**
- Run tests from the repo root: `python -m pytest vireo/tests/test_develop.py -v`
- `vireo/tests/test_develop.py` imports modules bare (`from develop import ...`) because of the `sys.path.insert` at its top. Follow that.
- Flask routes live inside `create_app` in `vireo/app.py` and are registered with `@app.route`. Imports are function-local by convention in this file.
- Every new route must be added to `vireo/tests/contracts/routes.txt` or a contract test fails.
- Job progress should use `progress_event()` / `failure_event()` from `vireo/job_contract.py` for a consistent payload shape. (`vireo/tests/test_job_contract.py` unit-tests those helpers; it does not enforce their use across `app.py`, so this is convention, not a failing test.)
- **Run `ruff check vireo/ tests/` before every commit.** That is exactly what CI
  runs (`.github/workflows/test.yml:258`), and it is repo-wide — a lint error
  anywhere fails the PR. Task 4 left it red with 6 errors (a `B904` plus 5
  `I001` import-sort errors in new test code), caught only a review round later.
  Linting just the files you touched is not sufficient.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `vireo/develop.py` (modify) | Detection + command construction. Gains `darktable_search_paths()`, `/Applications` probe, AppImage `argv[1]` handling | 1, 3 |
| `vireo/taxonomy.py` (modify) | Gains `byte_callback` / `should_cancel` on `_download_with_resume` | 4 |
| `vireo/darktable_install.py` (**new**) | Release resolution, digest verification, download, platform hand-off. No Flask, no DB — pure functions, easy to test | 5, 6, 7 |
| `vireo/app.py` (modify) | Two new routes + `checked_paths` on the existing status route | 2, 8, 9 |
| `vireo/templates/settings.html` (modify) | Button, confirmation, progress, re-check, Adobe link | 10, 11 |
| `vireo/tests/test_darktable_install.py` (**new**) | Unit tests for the new module | 5, 6, 7 |
| `vireo/tests/test_develop.py` (modify) | Detection + AppImage command tests | 1, 3 |
| `vireo/tests/test_taxonomy.py` (modify) | New downloader hooks | 4 |
| `vireo/tests/test_darktable_api.py` (modify) | New route tests | 2, 8, 9 |
| `vireo/tests/contracts/routes.txt` (modify) | Route contract | 8, 9 |

`darktable_install.py` deliberately holds no Flask or DB code so every function is directly unit-testable without a test client or temp database.

---

## Task 1: Fix darktable detection

Without this, Tasks 8–10 ship a button that downloads 87 MB and still reports "not found".

**Files:**
- Modify: `vireo/develop.py:39-75`
- Test: `vireo/tests/test_develop.py`

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_develop.py`:

```python
def test_find_darktable_finds_macos_app_bundle(monkeypatch):
    """A normal macOS .dmg install is found even though it is not on PATH."""
    from develop import find_darktable

    bundle = "/Applications/darktable.app/Contents/MacOS/darktable-cli"
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("os.path.isfile", lambda p: p == bundle)
    monkeypatch.setattr("os.path.realpath", lambda p: p)

    assert find_darktable("") == bundle


def test_find_darktable_configured_path_wins_over_bundle(tmp_path, monkeypatch):
    """An explicitly configured path takes precedence over the bundle probe."""
    from develop import find_darktable

    configured = tmp_path / "darktable-cli"
    configured.touch()
    monkeypatch.setattr("sys.platform", "darwin")

    assert find_darktable(str(configured)) == str(configured)


def test_darktable_search_paths_matches_find_darktable(monkeypatch):
    """checked_paths cannot drift from what find_darktable actually probes.

    Every candidate reported to the user must be one find_darktable would
    accept; otherwise the "we checked here" message is a lie.
    """
    from develop import darktable_search_paths, find_darktable

    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setattr("sys.platform", "darwin")
    paths = darktable_search_paths()
    assert paths, "expected at least one candidate on darwin"

    for candidate in paths:
        monkeypatch.setattr("os.path.isfile", lambda p, c=candidate: p == c)
        monkeypatch.setattr("os.path.realpath", lambda p: p)
        assert find_darktable("") == candidate
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest vireo/tests/test_develop.py -k "bundle or search_paths" -v`
Expected: FAIL — `ImportError: cannot import name 'darktable_search_paths'` and the bundle test returns `None`.

- [ ] **Step 3: Implement**

In `vireo/develop.py`, add `import sys` to the imports if absent, then insert `darktable_search_paths` immediately before `find_darktable` and rewrite `find_darktable`'s fallback to consume it:

```python
def darktable_search_paths():
    """Locations probed for darktable-cli, in priority order.

    Exposed so the UI can tell the user exactly where we looked when the
    binary is not found, instead of repeating a bare "not found".
    ``find_darktable`` walks this same list, so the two cannot drift.
    """
    candidates = []
    # os.name is checked FIRST, not sys.platform. test_find_darktable_detects_
    # standard_windows_install patches develop.os.name = "nt" while leaving
    # sys.platform as the host's value, so a leading darwin branch would
    # shadow the Windows candidates and break that test on a Mac.
    if os.name == "nt":
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
            base = os.environ.get(env_var)
            if base:
                candidates.extend([
                    os.path.join(base, "darktable", "bin", "darktable-cli.exe"),
                    os.path.join(base, "darktable", "darktable-cli.exe"),
                ])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/darktable.app/Contents/MacOS/darktable-cli",
            os.path.expanduser("~/Applications/darktable.app/Contents/MacOS/darktable-cli"),
        ])
    else:
        # Linux: an AppImage we installed ourselves. Newest mtime wins, since
        # installers are kept and this directory accumulates versions.
        tools_dir = os.path.expanduser("~/.vireo/tools/darktable")
        if os.path.isdir(tools_dir):
            appimages = [
                os.path.join(tools_dir, n)
                for n in os.listdir(tools_dir)
                if n.endswith(".AppImage")
            ]
            appimages.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            candidates.extend(appimages)
    return candidates
```

Then replace the `if os.name == "nt":` block inside `find_darktable` (currently lines 63-74) with:

```python
    for candidate in darktable_search_paths():
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)
    return None
```

Leave the configured-path check and the `shutil.which` probe above it untouched.

- [ ] **Step 4: Neutralize the new probe in two pre-existing tests**

`test_find_darktable_returns_none_when_missing` (`test_develop.py:7-12`) and
`test_find_darktable_returns_none_for_bad_configured_path` (`:71-76`) only patch
`shutil.which`. Once the `/Applications` probe exists they will find a real
darktable on any Mac that has one — including this machine after Task 10's manual
verification. That is a genuinely under-specified test, not a weakened assertion:
both intend "nothing is installed anywhere", so make them say it.

Add to each of those two tests:

```python
    import develop
    monkeypatch.setattr(develop, "darktable_search_paths", list)
```

(`test_find_darktable_returns_none_for_bad_configured_path` will need
`monkeypatch` added to its signature if it does not already take it.)

This is the one sanctioned edit to an existing test in this plan. Do not touch
any other assertion.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest vireo/tests/test_develop.py -v`
Expected: PASS, including all pre-existing tests — notably
`test_find_darktable_detects_standard_windows_install`, which the branch ordering
above is written to preserve.

- [ ] **Step 6: Commit**

```bash
git add vireo/develop.py vireo/tests/test_develop.py
git commit -m "fix: detect darktable installed as a macOS app bundle

find_dng_converter probed /Applications for the Adobe app, but
find_darktable only tried PATH and Windows Program Files, so a normal
macOS .dmg install of darktable reported 'not found'.

Adds darktable_search_paths() so the probed locations can also be shown
to the user, with a test asserting the two cannot drift."
```

---

## Task 2: Report the searched locations to the user

**Files:**
- Modify: `vireo/app.py:15910-15930`
- Test: `vireo/tests/test_darktable_api.py`

**Required reading before starting.** Task 1's code review established two
constraints that this task exists to satisfy, and returning
`darktable_search_paths()` raw would violate both:

1. **It omits the two highest-priority probes.** `find_darktable` checks the
   configured path and `shutil.which("darktable-cli")` *before* that list, and
   neither appears in it. A panel saying "we checked here" while silently
   omitting `$PATH` — the probe most likely to explain a Homebrew or distro
   user's miss — answers a cheaper question than the one the user is reading,
   which `CLAUDE.md` forbids.
2. **On Linux it is empty until darktable is installed.** It returns the
   AppImages actually present in `~/.vireo/tools/darktable`, so a fresh Linux box
   gets `[]` — an empty "checked locations" list is *less* informative than the
   bare ✗ this feature exists to fix, on exactly the platform where the download
   matters most.

So **the route composes the user-facing list**; the detector function stays the
detector's concrete candidate list. Every entry the route adds must be something
`find_darktable` genuinely probes, or the claim becomes a lie.

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_darktable_api.py`:

```python
def test_api_darktable_status_reports_checked_paths(app_and_db):
    """The status route says where it looked, so a bare 'not found' can explain itself."""
    app, _ = app_and_db
    client = app.test_client()
    data = client.get('/api/darktable/status').get_json()

    assert 'checked_paths' in data
    assert isinstance(data['checked_paths'], list)
    assert all(isinstance(p, str) for p in data['checked_paths'])


def test_api_darktable_status_checked_paths_mentions_path_probe(app_and_db):
    """$PATH is the probe most likely to explain a miss, so it must be listed.

    find_darktable tries shutil.which() before any filesystem candidate;
    omitting it would make "we checked here" untrue by omission.
    """
    app, _ = app_and_db
    data = app.test_client().get('/api/darktable/status').get_json()

    assert any('PATH' in p for p in data['checked_paths'])


def test_api_darktable_status_checked_paths_never_empty(app_and_db):
    """Never render an empty 'we checked here' list.

    darktable_search_paths() returns [] on a Linux box with no AppImage
    installed — precisely the user this feature targets.
    """
    import develop
    app, _ = app_and_db
    original = develop.darktable_search_paths
    develop.darktable_search_paths = lambda: []
    try:
        data = app.test_client().get('/api/darktable/status').get_json()
    finally:
        develop.darktable_search_paths = original

    assert data['checked_paths'], "checked_paths must never be empty"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_api.py -k checked_paths -v`
Expected: FAIL — `KeyError`/assert on `'checked_paths' in data`.

- [ ] **Step 3: Implement**

In `vireo/app.py`, change the import on line 15913:

```python
        from develop import find_darktable, find_dng_converter, darktable_search_paths
```

Compose the user-facing list before the `return jsonify(...)`:

```python
        # What the user is told we checked must match what find_darktable
        # actually probes: shutil.which first, then the filesystem candidates.
        # darktable_search_paths() covers only the latter and is empty on a
        # Linux box with no AppImage yet, so compose rather than return it raw.
        checked_paths = ["$PATH (darktable-cli)"]
        checked_paths.extend(darktable_search_paths())
        if not sys.platform.startswith(("darwin", "win")) and os.name != "nt":
            tools_dir = os.path.expanduser("~/.vireo/tools/darktable")
            if tools_dir not in checked_paths:
                checked_paths.append(tools_dir)
```

and add the key to the returned dict:

```python
            "output_dir": cfg.get("darktable_output_dir"),
            "checked_paths": checked_paths,
```

Check whether `sys` and `os` are already imported at `app.py` module level; use
whatever is already there rather than adding function-local imports if so.

**If you find a cleaner way to express the Linux condition, take it** — the
requirement is behavioral (the list always names `$PATH`, is never empty, and
names the Linux tools directory on Linux), not a specific expression.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vireo/app.py vireo/tests/test_darktable_api.py
git commit -m "feat: report probed darktable locations from the status route"
```

---

## Task 3: Run darktable-cli from an AppImage

**Files:**
- Modify: `vireo/develop.py:118-136` (`build_command`) and `vireo/develop.py:311` (`subprocess.run`)
- Test: `vireo/tests/test_develop.py`

**Why `argv[1]` and not a symlink:** see "Background" above. A symlink named `darktable-cli` would be resolved away by `os.path.realpath` in `find_darktable`, silently launching the GUI.

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_develop.py`:

```python
def test_build_command_selects_cli_inside_appimage():
    """darktable's AppImage picks its binary from argv[1]; without this we
    would launch the GUI and hang the export job."""
    from develop import build_command

    cmd = build_command(
        "/home/u/.vireo/tools/darktable/Darktable-5.6.0-x86_64.AppImage",
        "in.raw", "out.jpg",
    )
    assert cmd[1] == "darktable-cli"
    assert cmd[2:] == ["in.raw", "out.jpg"]


def test_build_command_plain_binary_unchanged():
    from develop import build_command

    cmd = build_command("/usr/bin/darktable-cli", "in.raw", "out.jpg")
    assert cmd == ["/usr/bin/darktable-cli", "in.raw", "out.jpg"]


def test_build_command_appimage_keeps_style_and_width():
    """Optional args must land after the positional args, not before."""
    from develop import build_command

    cmd = build_command("/x/D.AppImage", "in.raw", "out.jpg", style="Wild", width=2048)
    assert cmd == ["/x/D.AppImage", "darktable-cli", "in.raw", "out.jpg",
                   "--style", "Wild", "--width", "2048"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_develop.py -k build_command -v`
Expected: FAIL — `cmd[1]` is `"in.raw"`, not `"darktable-cli"`.

- [ ] **Step 3: Implement**

Replace the body of `build_command` in `vireo/develop.py` (line 131):

```python
    cmd = [darktable_bin]
    if darktable_bin.endswith(".AppImage"):
        # darktable ships a multi-binary AppImage whose AppRun selects the
        # binary from argv[1]. The alternative (a symlink named darktable-cli)
        # does not survive find_darktable's os.path.realpath, which would
        # silently launch the GUI and hang the job.
        cmd.append("darktable-cli")
    cmd.extend([input_path, output_path])
```

Leave the `style` / `width` `extend` calls that follow unchanged.

- [ ] **Step 4: Set the FUSE fallback at the call site**

`build_command` returns a list and never touches the environment, so this is a separate change. In `vireo/develop.py`, replace the `subprocess.run` at line 311 (inside `develop_photo`):

```python
        # AppImages need FUSE2 to self-mount; many current distros ship only
        # FUSE3. This makes the AppImage extract to a temp dir instead of
        # failing outright. Harmless for non-AppImage binaries.
        env = {**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"}
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            env=env, **no_window_kwargs()
        )
```

**Do not touch the `subprocess.run` at line 230** — that one is the Adobe DNG Converter.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest vireo/tests/test_develop.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vireo/develop.py vireo/tests/test_develop.py
git commit -m "feat: invoke darktable-cli inside an AppImage via argv[1]

darktable's AppImage AppRun selects its binary from argv[1] or from the
basename of the symlink used to invoke it. The symlink route does not
survive find_darktable's os.path.realpath, which would launch the GUI
and hang a headless export until the 120s timeout."
```

---

## Task 4: Byte progress and cancellation in the shared downloader

**Files:**
- Modify: `vireo/taxonomy.py:39-150`
- Test: `vireo/tests/test_taxonomy.py`

The existing `progress_callback(message)` contract stays **exactly** as-is so the seven current `test_download_with_resume_*` tests and both callers (`taxonomy.py:458`, `taxonomy.py:944`) keep working untouched. Two keyword-only params are added.

- [ ] **Step 1: Write the failing tests**

**First read `_start_test_server` (`vireo/tests/test_taxonomy.py:539-548`) and one
existing `test_download_with_resume_*` test.** These tests do not mock `urlopen` —
they spin a real `http.server` on a random port. Reuse that harness; do not
introduce a second mocking style.

Append to `vireo/tests/test_taxonomy.py`:

```python
def test_download_with_resume_reports_bytes(tmp_path):
    """byte_callback gets real byte counts — this is what the progress bar reads."""
    import http.server
    from taxonomy import _download_with_resume

    payload = b"x" * 500_000

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server, port = _start_test_server(Handler)
    try:
        seen = []
        dest = tmp_path / "out.bin"
        # chunk_size small enough to force several loop iterations
        _download_with_resume(
            f"http://127.0.0.1:{port}/x", str(dest), chunk_size=4096,
            byte_callback=lambda done, total: seen.append((done, total)),
        )
    finally:
        server.shutdown()

    assert seen, "byte_callback was never called"
    assert seen[-1][0] == len(payload)
    assert seen[-1][1] == len(payload)          # from Content-Length
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_download_with_resume_cancels_midstream(tmp_path):
    """should_cancel aborts and keeps a non-empty .partial for resume."""
    import http.server
    from taxonomy import DownloadCancelled, _download_with_resume

    payload = b"x" * 500_000

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server, port = _start_test_server(Handler)
    calls = {"n": 0}

    def cancel_after_two_chunks():
        calls["n"] += 1
        return calls["n"] > 2

    try:
        dest = tmp_path / "out.bin"
        with pytest.raises(DownloadCancelled):
            _download_with_resume(
                f"http://127.0.0.1:{port}/x", str(dest), chunk_size=4096,
                should_cancel=cancel_after_two_chunks,
            )
    finally:
        server.shutdown()

    assert not dest.exists()
    partial = tmp_path / "out.bin.partial"
    assert partial.exists()
    # Non-empty proves we cancelled mid-stream, not before the first read —
    # open(..., "wb") would create a 0-byte file either way.
    assert partial.stat().st_size > 0
```

`_start_test_server(handler_class, port=0)` returns `(server, port)`
(`test_taxonomy.py:543-548`); the URL is built from the port, as above. Add
`import pytest` at the top of the file if it is not already there.

**These two tests are NOT sufficient — proven by mutation, not guessed.** During
implementation, forcing `progress_base = downloaded_before` always (i.e. exactly
the "bar past 100%" bug this step spends a paragraph warning about) passed both
tests above. Four more are required, each closing a mutation that otherwise
survives the whole suite:

- `..._bytes_reset_when_server_ignores_range` — a server that ignores `Range` and
  returns 200 on the retry. Assert the reported total never exceeds the real file
  size and progress restarts from 0 rather than continuing from
  `downloaded_before`.
- **Pin `except DownloadCancelled: raise`** — pass a `progress_callback`, assert
  no message contains `"retrying"`, and assert the server was hit exactly once.
  Without it, deleting that clause still passes: the backoff cancel check catches
  the exception one beat later, but the user is then told the *connection
  dropped* after pressing Cancel.
- **Pin the backoff cancel check** — a 500 handler plus a `should_cancel` that
  flips after the first attempt. Assert it raises well under the 3 s backoff and
  hit the server once. Without it, reverting to `time.sleep(3)` still passes,
  masked by the re-raise. **Each of these two gaps hides the other's removal** —
  the weakest possible state.
- **Pin the throttle interval, not just its existence.** Extract it as a private
  keyword-only `_emit_interval=0.25` param; assert `_emit_interval=0` fires ~once
  per chunk and a huge value fires exactly once. Otherwise setting the interval to
  `1e9` passes every test while the real 178 MB progress bar never moves until
  completion — the exact failure Task 10 exists to prevent.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_taxonomy.py -k "reports_bytes or cancels" -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'byte_callback'`.

- [ ] **Step 3: Implement**

Change the signature at `vireo/taxonomy.py:39`:

```python
def _download_with_resume(url, dest_path, progress_callback=None,
                          max_stalled=3, chunk_size=256 * 1024,
                          *, byte_callback=None, should_cancel=None):
```

Add to the docstring's Args:

```
        byte_callback: optional ``callback(downloaded, total_or_None)`` for
            byte-level progress.  Throttled to ~4 Hz by the caller's clock.
        should_cancel: optional ``callback() -> bool``.  When it returns True
            the download aborts, leaving the ``.partial`` file for resume.
```

Add a cancellation sentinel near the top of the module:

```python
class DownloadCancelled(Exception):
    """Raised when should_cancel() asked us to stop."""
```

Replace the chunk loop (lines 96-102):

```python
                with open(partial_path, mode) as f:
                    last_emit = 0.0
                    while True:
                        if should_cancel is not None and should_cancel():
                            raise DownloadCancelled("Download cancelled")
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        if byte_callback is not None:
                            now = time.monotonic()
                            if now - last_emit >= 0.25:
                                last_emit = now
                                byte_callback(progress_base + received, expected_total)
                    if byte_callback is not None:
                        byte_callback(progress_base + received, expected_total)
```

`progress_base` and `expected_total` are computed next to `expected_bytes`
(line 92). **This is subtler than it looks.** `mode` is `"wb"` whenever the
status is not 206 (line 94), which *truncates* the partial file — but
`downloaded_before` is deliberately not reset, because it is the stall-detection
baseline (see the comment at lines 87-89). So on a server that ignores `Range`,
`downloaded_before + received` would over-report and drive the bar past 100%.
`test_download_with_resume_server_ignores_range` (`test_taxonomy.py:742`) proves
that branch is live. Use a separate variable for display:

```python
                # Bytes already on disk that we are keeping. Distinct from
                # downloaded_before, which stays put as the stall baseline even
                # when mode == "wb" truncates the partial.
                progress_base = downloaded_before if resp.status == 206 else 0
                expected_total = (
                    expected_bytes + progress_base
                    if expected_bytes is not None
                    else None
                )
```

Throttling to 4 Hz matters: the SSE subscriber queue is `maxsize=200` (`jobs.py:898`), and an unthrottled 178 MB download would drop events.

Finally, make cancellation escape the retry loop — in the `except Exception as e:` handler at line 111, re-raise immediately before any retry bookkeeping:

```python
        except DownloadCancelled:
            raise
        except Exception as e:
```

And check before sleeping, so a cancel during backoff is honoured — replace `time.sleep(3)` at line 139:

```python
            for _ in range(6):
                if should_cancel is not None and should_cancel():
                    raise DownloadCancelled("Download cancelled")
                time.sleep(0.5)
            continue
```

- [ ] **Step 4: Run the full taxonomy suite**

Run: `python -m pytest vireo/tests/test_taxonomy.py -v`
Expected: PASS, **including all seven pre-existing `test_download_with_resume_*` tests unmodified**. If any of them changed behaviour, the contract was broken — revert and rethink rather than editing those tests.

- [ ] **Step 5: Commit**

```bash
git add vireo/taxonomy.py vireo/tests/test_taxonomy.py
git commit -m "feat: byte-level progress and cancellation in _download_with_resume

Adds optional keyword-only byte_callback and should_cancel. The existing
progress_callback(message) contract is untouched, so both current callers
and the seven existing tests keep working unmodified.

Byte events are throttled to 4 Hz because the SSE subscriber queue is
bounded at 200."
```

---

## Task 5: Resolve the right release asset

**Files:**
- Create: `vireo/darktable_install.py`
- Create: `vireo/tests/test_darktable_install.py`
- Create: `vireo/tests/fixtures/darktable_release.json`

- [ ] **Step 1: Create the fixture**

`vireo/tests/fixtures/` does not exist yet — create the directory (no
`__init__.py` needed). Save a trimmed real API response to
`vireo/tests/fixtures/darktable_release.json`. It **must** include the `.zsync`
decoys — they are the point of one of the tests:

```json
{
  "tag_name": "release-5.6.0",
  "assets": [
    {"name": "Darktable-5.6.0-aarch64.AppImage", "size": 170895880,
     "digest": "sha256:147943bd2eedc33c8d31eb3e6b87b591ac9ca285d00282b2655d8d19caecfca0",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/Darktable-5.6.0-aarch64.AppImage"},
    {"name": "Darktable-5.6.0-aarch64.AppImage.zsync", "size": 292297,
     "digest": "sha256:52a2b5da0dd55c984f3d4c6e77bbd1119b4561cf17c80146a09c9e498ae56da4",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/Darktable-5.6.0-aarch64.AppImage.zsync"},
    {"name": "darktable-5.6.0-arm64.dmg", "size": 87094261,
     "digest": "sha256:49aec447e891ab481e436b4c0231fc3c8d0001aad220762ae8e765d3bda5d102",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/darktable-5.6.0-arm64.dmg"},
    {"name": "darktable-5.6.0-x86_64.dmg", "size": 92795715,
     "digest": "sha256:24c83655af0d81c2f8cb78b97531a03bb6a650349b7fd49c1679080db675cbcb",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/darktable-5.6.0-x86_64.dmg"},
    {"name": "darktable-5.6.0-win64.exe", "size": 141543364,
     "digest": "sha256:b42989195dfff44540c0b767b407987329ca99853612304cbbf14c48d1d3f803",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/darktable-5.6.0-win64.exe"},
    {"name": "darktable-5.6.0-woa64.exe", "size": 106943215,
     "digest": "sha256:b7737d54d6ee007816ae0a1fad3ca3677588735e1432887a917bc55f818f5268",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/darktable-5.6.0-woa64.exe"},
    {"name": "Darktable-5.6.0-x86_64.AppImage", "size": 177752568,
     "digest": "sha256:cbad7bf4be2607e1725db156d73c799d267a79fc29a572c3136a5deb9c9be948",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/Darktable-5.6.0-x86_64.AppImage"},
    {"name": "Darktable-5.6.0-x86_64.AppImage.zsync", "size": 304013,
     "digest": "sha256:35b038a00c6a8a73bf1b7e2a9ad1d05db471d59ec3feec9edb3df594aee715b9",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/Darktable-5.6.0-x86_64.AppImage.zsync"},
    {"name": "darktable-5.6.0.tar.xz", "size": 8179036,
     "digest": "sha256:157d6d3847af8afcabe78944454786f73a886e08a504b4bd6114c2065fe006e4",
     "browser_download_url": "https://github.com/darktable-org/darktable/releases/download/release-5.6.0/darktable-5.6.0.tar.xz"}
  ]
}
```

- [ ] **Step 2: Write the failing tests**

Create `vireo/tests/test_darktable_install.py`:

```python
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


def test_select_asset_skips_zsync_listed_before_the_appimage():
    """THE test above is vacuous on its own — verified by mutation.

    The fixture lists each AppImage before its .zsync sibling, and
    select_asset returns on the first suffix match, so a substring matcher
    still returns the real AppImage and the test above passes. GitHub makes
    no ordering promise, so re-sort the assets to put the decoys first.
    """
    from darktable_install import select_asset

    release = _release()
    release["assets"].sort(key=lambda a: not a["name"].endswith(".zsync"))
    asset = select_asset(release, "linux", "x86_64")
    assert asset["name"] == "Darktable-5.6.0-x86_64.AppImage"


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
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_install.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'darktable_install'`.

- [ ] **Step 4: Implement**

Create `vireo/darktable_install.py`:

```python
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
import os
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
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_install.py -v`
Expected: PASS (all parametrized cases).

- [ ] **Step 6: Commit**

```bash
git add vireo/darktable_install.py vireo/tests/test_darktable_install.py vireo/tests/fixtures/darktable_release.json
git commit -m "feat: resolve the darktable release asset for this platform

Exact-suffix matching, not substring: the release ships ~300KB .zsync
delta manifests whose names contain 'AppImage', and a substring matcher
would 'successfully install' one of those instead of darktable.

Download URLs are allowlisted to GitHub hosts under darktable-org/darktable."
```

---

## Task 6: Verify the download

**Files:**
- Modify: `vireo/darktable_install.py`
- Test: `vireo/tests/test_darktable_install.py`

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_darktable_install.py`:

```python
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


def test_verify_digest_absent_says_so_rather_than_silently_passing(tmp_path):
    from darktable_install import verify_digest

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok, detail = verify_digest(str(f), None)
    assert ok
    assert "no digest" in detail.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_install.py -k verify_digest -v`
Expected: FAIL — `ImportError: cannot import name 'verify_digest'`.

- [ ] **Step 3: Implement**

Add to `vireo/darktable_install.py` (and `import hashlib` at the top):

```python
def verify_digest(path, expected):
    """Compare the file's SHA256 against the digest published by the API.

    Returns (ok, human_readable_detail).  The detail is shown to the user
    verbatim, so it must be honest about what was and was not checked: a
    matching digest proves the bytes are what GitHub's API said they are,
    not that darktable signed them.
    """
    if not expected:
        return True, "No digest published by GitHub — size checked only"

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    want = expected.split(":", 1)[-1].strip().lower()

    if actual == want:
        return True, f"SHA256 matches the digest published by GitHub ({actual[:16]}…)"
    return False, f"SHA256 mismatch — expected {want}, got {actual}"
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_install.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vireo/darktable_install.py vireo/tests/test_darktable_install.py
git commit -m "feat: verify downloaded darktable against the API's SHA256 digest"
```

---

## Task 7: Download and hand off to the platform installer

**Files:**
- Modify: `vireo/darktable_install.py`
- Test: `vireo/tests/test_darktable_install.py`

### OPEN QUESTION — decide this before writing the Linux path

Task 3's code review surfaced a real performance problem that this task is the
right place to solve.

**The problem.** Task 3 sets `APPIMAGE_EXTRACT_AND_RUN=1` when invoking an
AppImage (gated on `_is_appimage`, so only AppImage users pay). But when that
variable is set, the AppImage type-2 runtime does not probe FUSE first — it
*unconditionally* unpacks the whole squashfs to a temp dir, runs, and deletes.
darktable's AppImage is ~178 MB, and the develop job calls `develop_photo` once
per photo in a plain loop (`app.py`). So a 200-photo develop job on Linux
extracts and deletes 178 MB **two hundred times**, and that overhead counts
against the 120 s per-photo timeout.

**The proposed alternative:** run `./X.AppImage --appimage-extract` **once at
install time** here in Task 7, and point `darktable_bin` at
`squashfs-root/usr/bin/darktable-cli`. If it works it is strictly better — that
layout is what darktable's walk-up-from-`argv[0]` resource lookup expects, it
survives `realpath`, it needs no `argv[1]` prefix, no FUSE at any version, and
costs nothing per export. The trade is ~500 MB of disk, once.

**Why it is not already decided:** darktable's `AppRun` does real setup before
exec'ing — it sets `CAMLIBS`, `IOLIBS`, `GIO_EXTRA_MODULES`, and sources
`apprun-hooks/linuxdeploy-plugin-gtk.sh`. The extracted `darktable-cli` may not
run correctly without that environment, depending on whether linuxdeploy patched
RPATH. **This is an empirical question that must be tested on real Linux** — it
cannot be settled by reading.

**What to do:** test it on Linux before implementing the Linux branch. If the
extracted binary works, take it and simplify (Task 3's `argv[1]` prefix and env
var become dead code for our own installs, though keep them for user-configured
AppImages). If it does not, keep the current approach and note the per-photo cost
in the plan so it is a known limitation rather than a surprise.

**Do not silently pick one.** If you cannot test on Linux, say so and leave the
current approach in place with the cost documented.

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_darktable_install.py`:

```python
def test_install_dir_is_under_vireo_home():
    from darktable_install import install_dir

    assert install_dir().endswith(os.path.join(".vireo", "tools", "darktable"))


def test_hand_off_linux_makes_appimage_executable_and_returns_bin_path(tmp_path, monkeypatch):
    from darktable_install import hand_off

    appimage = tmp_path / "Darktable-5.6.0-x86_64.AppImage"
    appimage.write_bytes(b"stub")
    appimage.chmod(0o644)

    result = hand_off(str(appimage), platform_name="linux")

    assert os.access(str(appimage), os.X_OK)
    assert result["bin_path"] == str(appimage)
    assert result["action"] == "installed"


def test_hand_off_macos_opens_dmg_and_sets_no_bin_path(tmp_path, monkeypatch):
    """macOS cannot know the final path — the user drags the app themselves."""
    import darktable_install
    from darktable_install import hand_off

    calls = []
    monkeypatch.setattr(darktable_install.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)

    dmg = tmp_path / "darktable-5.6.0-arm64.dmg"
    dmg.write_bytes(b"stub")
    result = hand_off(str(dmg), platform_name="darwin")

    assert calls, "expected the DMG to be opened"
    assert result["bin_path"] is None
    assert result["action"] == "opened-installer"


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_install.py -k "hand_off or install_dir or quarantined" -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement**

Add to `vireo/darktable_install.py` (and `import subprocess`, `import sys`, `import stat` at the top):

```python
def install_dir():
    """Where downloads land, on every platform.

    Also where the .partial resume file lives and which filesystem the
    free-space check measures.  Installers are kept after hand-off — the
    user may want to re-run them.
    """
    return os.path.expanduser(os.path.join("~", ".vireo", "tools", "darktable"))


def is_quarantined(path):
    """True if macOS tagged the file with com.apple.quarantine.

    Measured behaviour: urllib downloads are NOT quarantined (LaunchServices
    applies that attribute for browser-style downloads), so this is normally
    False and the Gatekeeper warning stays hidden.  Checked rather than
    assumed in either direction.
    """
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", path],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def hand_off(path, platform_name=None):
    """Hand the downloaded artifact to the platform.

    Returns {action, location, bin_path}.  Does NOT write config: bin_path is
    returned so the job handler writes darktable_bin in one place, next to the
    message that tells the user it happened.
    """
    platform_name = platform_name or sys.platform

    if platform_name == "linux":
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return {"action": "installed", "location": path, "bin_path": path}

    if platform_name == "darwin":
        subprocess.run(["open", path], timeout=30)
    else:
        os.startfile(path)  # noqa: F821 - Windows only

    # The user chooses where the app lands, so we cannot know bin_path here.
    # Detection (Task 1) finds it on the next re-check.
    return {"action": "opened-installer", "location": path, "bin_path": None}


def download(asset, dest_dir=None, byte_callback=None, should_cancel=None):
    """Download one resolved asset, verify it, and return (path, detail).

    Raises RuntimeError on a size or digest mismatch, deleting the bad file:
    a wrong artifact must never be handed to an installer.
    """
    from taxonomy import _download_with_resume

    # NOTE from Task 4: should_cancel is checked BEFORE the first read, so an
    # immediately-cancelled download leaves a 0-byte .partial. Do not treat an
    # empty .partial as a corrupt-download signal — it is a normal resume state.
    dest_dir = dest_dir or install_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, asset["name"])

    _download_with_resume(
        asset["url"], dest,
        byte_callback=byte_callback,
        should_cancel=should_cancel,
    )

    actual_size = os.path.getsize(dest)
    if asset.get("size") and actual_size != asset["size"]:
        os.remove(dest)
        raise RuntimeError(
            f"Size mismatch — expected {asset['size']} bytes, got {actual_size}"
        )

    ok, detail = verify_digest(dest, asset.get("digest"))
    if not ok:
        os.remove(dest)
        raise RuntimeError(detail)
    return dest, detail


def free_space_bytes(path):
    """Bytes free on the filesystem holding path (creating it if needed)."""
    os.makedirs(path, exist_ok=True)
    st = os.statvfs(path) if hasattr(os, "statvfs") else None
    if st is None:
        import shutil
        return shutil.disk_usage(path).free
    return st.f_bavail * st.f_frsize
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_install.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vireo/darktable_install.py vireo/tests/test_darktable_install.py
git commit -m "feat: download, verify, and hand off darktable to the OS installer

hand_off returns bin_path rather than writing config, so the single
config mutation lives with the message that reports it."
```

---

## Task 8: The "what would be downloaded" route

**Files:**
- Modify: `vireo/app.py` (add after `api_darktable_status`, ~line 15931)
- Modify: `vireo/tests/contracts/routes.txt`
- Test: `vireo/tests/test_darktable_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_darktable_api.py`. **The release cache added in Step 3
is module-level state that leaks between tests** — a cached success would make the
fallback test see a stale value instead of the raising stub. Reset it in every
test that touches this route:

```python
@pytest.fixture(autouse=True)
def _clear_release_cache():
    import darktable_install
    darktable_install._release_cache.update(at=0.0, value=None)
    yield
    darktable_install._release_cache.update(at=0.0, value=None)
```

Add `import pytest` to the file if it is not already there. Then:

**CONTRACT CHANGE from Task 5's review.** `resolve_release()` returns a **2-tuple
`(release_or_None, reason_or_None)`**, not a bare value. Bare `None` conflated
four distinct outcomes — unsupported platform, no matching asset, asset rejected
as suspicious, and network failure — so this route would have told a user behind
a proxy or over GitHub's 60/hr rate limit *"No darktable build is published for
this platform."* That is false, and it is the exact failure `CLAUDE.md` forbids.

`darktable_install` exports the three reason strings as constants
(`REASON_UNREACHABLE`, `REASON_NO_PLATFORM_BUILD`, `REASON_NO_USABLE_ASSET`) —
compare against those, do not duplicate the literals. `resolve_release` also
never raises now, so **the route's `except` clause is not a fallback you can rely
on** — the reason string is.

```python
def test_api_darktable_install_available_shape(app_and_db, monkeypatch):
    """The confirmation needs version, name, size, url and digest."""
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release", lambda: ({
        "version": "5.6.0",
        "name": "darktable-5.6.0-arm64.dmg",
        "size": 87094261,
        "url": "https://github.com/darktable-org/darktable/releases/download/x/d.dmg",
        "digest": "sha256:" + "a" * 64,
    }, None))

    data = app.test_client().get('/api/darktable/install/available').get_json()
    assert data['available'] is True
    assert data['version'] == "5.6.0"
    assert data['size'] == 87094261
    assert data['digest'].startswith("sha256:")


def test_api_darktable_install_available_reports_network_failure_honestly(app_and_db, monkeypatch):
    """A user behind a proxy must not be told their platform is unsupported."""
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_UNREACHABLE))

    resp = app.test_client().get('/api/darktable/install/available')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['available'] is False
    assert data['reason'] == darktable_install.REASON_UNREACHABLE
    assert 'platform' not in data['reason'].lower()


def test_api_darktable_install_available_handles_unsupported_platform(app_and_db, monkeypatch):
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_NO_PLATFORM_BUILD))

    data = app.test_client().get('/api/darktable/install/available').get_json()
    assert data['available'] is False
    assert data['reason'] == darktable_install.REASON_NO_PLATFORM_BUILD


def test_api_darktable_install_available_survives_an_unexpected_raise(app_and_db, monkeypatch):
    """Belt and braces: resolve_release should never raise, but if it does the
    route must still 200 with a reason rather than 500 into a dead button."""
    import darktable_install
    app, _ = app_and_db

    def boom():
        raise OSError("network unreachable")
    monkeypatch.setattr(darktable_install, "resolve_release", boom)

    resp = app.test_client().get('/api/darktable/install/available')
    assert resp.status_code == 200
    assert resp.get_json()['available'] is False
    assert resp.get_json()['reason']
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_api.py -k install_available -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

In `vireo/app.py`, immediately after `api_darktable_status` ends (line 15930):

```python
    @app.route("/api/darktable/install/available")
    def api_darktable_install_available():
        """What a download would fetch, for the confirmation dialog.

        Always 200: when this cannot answer, the panel shows a plain
        "Get darktable" link and the reason, rather than a dead button.
        """
        import darktable_install

        try:
            release, reason = darktable_install.resolve_release()
        except Exception as e:
            # resolve_release is documented never to raise; this is belt and
            # braces so an unexpected bug still degrades to a link, not a 500.
            log.warning("darktable release lookup failed: %s", e)
            return jsonify({
                "available": False,
                "reason": darktable_install.REASON_UNREACHABLE,
            })

        if not release:
            # Pass the reason through verbatim. Do NOT substitute a generic
            # message: "we could not reach GitHub" and "no build exists for
            # your platform" are different facts and users act on them
            # differently.
            return jsonify({"available": False, "reason": reason})

        return jsonify({"available": True, **release})
```

Add a short in-process cache in `darktable_install.py` so repeated Settings loads
do not burn the unauthenticated GitHub rate limit (60 requests/hour per IP) and
degrade to the fallback link:

```python
_release_cache = {"at": 0.0, "value": None}
_RELEASE_CACHE_SECS = 600


def resolve_release_cached():
    """resolve_release() with a short TTL, returning the same 2-tuple.

    Only successes are cached. A transient outage or a rate-limit reply must
    not pin the fallback link for ten minutes — and caching a failure would
    also freeze its reason string, so a user who fixed their network would
    keep being told GitHub is unreachable.
    """
    now = time.monotonic()
    if _release_cache["value"] is not None and now - _release_cache["at"] < _RELEASE_CACHE_SECS:
        return _release_cache["value"], None
    release, reason = resolve_release()
    if release is not None:
        _release_cache.update(at=now, value=release)
    return release, reason
```

Add `import time` at module level (it is not currently imported).

Call `resolve_release_cached()` from the route above. Failures are deliberately
not cached, so a transient outage does not pin the fallback link for ten minutes.
**Task 9's job must still call `resolve_release()` directly** — it re-resolves
server-side so the URL it downloads is never a stale client-supplied value.

- [ ] **Step 4: Add the route to the contract**

Insert into `vireo/tests/contracts/routes.txt` in sorted position (next to line 38's `GET /api/darktable/status`), matching the file's existing column alignment:

```
GET          /api/darktable/install/available
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vireo/app.py vireo/tests/test_darktable_api.py vireo/tests/contracts/routes.txt
git commit -m "feat: add /api/darktable/install/available

Always returns 200 so the UI degrades to a plain link with a stated
reason instead of showing a button that cannot work."
```

---

## Task 9: The download job

**Files:**
- Modify: `vireo/app.py` (add near the other download jobs, after `api_job_download_taxonomy` ~line 18262)
- Modify: `vireo/tests/contracts/routes.txt`
- Test: `vireo/tests/test_darktable_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `vireo/tests/test_darktable_api.py`:

```python
def test_api_job_download_darktable_returns_job_id(app_and_db, monkeypatch):
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release", lambda: ({
        "version": "5.6.0", "name": "d.dmg", "size": 100,
        "url": "https://github.com/darktable-org/darktable/releases/download/x/d.dmg",
        "digest": None,
    }, None))
    monkeypatch.setattr(darktable_install, "free_space_bytes", lambda p: 10 ** 12)
    monkeypatch.setattr(darktable_install, "download", lambda *a, **k: ("/tmp/d.dmg", "ok"))
    monkeypatch.setattr(darktable_install, "hand_off",
                        lambda p, **k: {"action": "opened-installer",
                                        "location": p, "bin_path": None})
    monkeypatch.setattr(darktable_install, "is_quarantined", lambda p: False)

    client = app.test_client()
    resp = client.post('/api/jobs/download-darktable')
    assert resp.status_code == 200
    job_id = resp.get_json()['job_id']

    # Wait for the job thread to finish INSIDE the test. Returning early lets
    # it run after monkeypatch teardown, where it would call the real download
    # and hit the network.
    from wait import wait_for_job_via_client
    job = wait_for_job_via_client(client, job_id)
    assert job['status'] == 'completed'


def test_api_job_download_darktable_refuses_without_disk_space(app_and_db, monkeypatch):
    """Refuse before downloading 87MB, and say what is needed vs free."""
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release", lambda: ({
        "version": "5.6.0", "name": "d.dmg", "size": 87094261,
        "url": "https://github.com/darktable-org/darktable/releases/download/x/d.dmg",
        "digest": None,
    }, None))
    monkeypatch.setattr(darktable_install, "free_space_bytes", lambda p: 1024)

    resp = app.test_client().post('/api/jobs/download-darktable')
    assert resp.status_code == 400
    assert 'space' in resp.get_json()['error'].lower()


def test_api_job_download_darktable_400_when_unavailable(app_and_db, monkeypatch):
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_NO_PLATFORM_BUILD))

    resp = app.test_client().post('/api/jobs/download-darktable')
    assert resp.status_code == 400
    # The 400 body must carry the specific reason, not a generic message.
    assert resp.get_json()['error'] == darktable_install.REASON_NO_PLATFORM_BUILD
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest vireo/tests/test_darktable_api.py -k job_download_darktable -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

In `vireo/app.py`, after `api_job_download_taxonomy`:

```python
    @app.route("/api/jobs/download-darktable", methods=["POST"])
    def api_job_download_darktable():
        import darktable_install
        from job_contract import progress_event

        runner = app._job_runner
        active_ws = _get_db()._active_workspace_id

        try:
            asset, reason = darktable_install.resolve_release()
        except Exception:
            asset, reason = None, darktable_install.REASON_UNREACHABLE
        if not asset:
            # Surface the specific reason. resolve_release distinguishes
            # "could not reach GitHub" from "no build for your platform";
            # collapsing them here would re-introduce the lie Task 5 fixed.
            return json_error(reason or darktable_install.REASON_UNREACHABLE)

        # Refuse before spending 87MB, and say what is needed vs what is free.
        target = darktable_install.install_dir()
        free = darktable_install.free_space_bytes(target)
        needed = (asset.get("size") or 0) * 2
        if free < needed:
            return json_error(
                f"Not enough disk space: {needed // (1024*1024)} MB needed in "
                f"{target}, {free // (1024*1024)} MB free."
            )

        def work(job):
            # NOTE from Task 4: byte_callback runs on the download thread
            # inside the write loop. Keep it cheap — a queue put, never a DB
            # write or anything that can block the transfer.
            def on_bytes(done, total):
                runner.push_event(job["id"], "progress", progress_event(
                    phase="Downloading darktable",
                    current=done,
                    total=total or asset.get("size") or 0,
                    current_file=asset["name"],
                ))

            path, verify_detail = darktable_install.download(
                asset,
                byte_callback=on_bytes,
                should_cancel=lambda: runner.is_cancelled(job["id"]),
            )

            # total=0 on purpose: the UI treats a non-zero total as a byte
            # count and would render "Verifying: 0 of 0 MB" instead of the
            # verification detail.
            runner.push_event(job["id"], "progress", progress_event(
                phase="Verifying", current=0, total=0, current_file=verify_detail,
            ))

            result = darktable_install.hand_off(path)

            # Single place config is mutated, next to the message reporting it.
            config_written = False
            if result.get("bin_path"):
                import config as cfg
                cfg.set("darktable_bin", result["bin_path"])
                config_written = True

            return {
                "version": asset["version"],
                "downloaded_to": path,
                "verified": verify_detail,
                "action": result["action"],
                "bin_path": result.get("bin_path"),
                "config_written": config_written,
                "quarantined": darktable_install.is_quarantined(path),
            }

        job_id = runner.start("download-darktable", work, workspace_id=active_ws)
        return jsonify({"job_id": job_id})
```

- [ ] **Step 4: Add the route to the contract**

Add to `vireo/tests/contracts/routes.txt` next to the other download jobs (line ~258):

```
POST         /api/jobs/download-darktable
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest vireo/tests/test_darktable_api.py vireo/tests/test_job_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vireo/app.py vireo/tests/test_darktable_api.py vireo/tests/contracts/routes.txt
git commit -m "feat: add the download-darktable job

Refuses up front when disk space is short rather than failing at 90%.
Reports byte-level progress and honours job cancellation."
```

---

## Task 10: The Settings UI

**Files:**
- Modify: `vireo/templates/settings.html:2166-2187` (`loadDarktableStatus`)

No unit test — this is inline template JS with no test harness in this repo. Verified manually in Step 4 and by the e2e suite.

- [ ] **Step 1: Rewrite `loadDarktableStatus`**

Replace lines 2166-2187 of `vireo/templates/settings.html`:

```javascript
async function loadDarktableStatus() {
  try {
    var data = await safeFetch('/api/darktable/status', {}, { toast: false });
    var el = document.getElementById('darktableStatus');
    var needsGetOption = false;
    if (data.available) {
      el.innerHTML = '<span style="color:var(--accent);font-size:13px;">&#10003; darktable-cli found</span>' +
        '<span style="color:var(--text-dim);font-size:12px;margin-left:8px;">' + escapeHtml(data.bin) + '</span>';
    } else {
      el.innerHTML = '<span style="color:var(--danger);font-size:13px;">&#10007; darktable-cli not found</span>' +
        '<span style="color:var(--text-dim);font-size:12px;margin-left:8px;">Install darktable or set the path below</span>' +
        '<div id="darktableGet" style="margin-top:6px;"></div>' +
        '<div id="darktableProgress" style="display:none;margin-top:8px;">' +
        '  <div style="background:var(--bg-tertiary);border-radius:3px;height:6px;overflow:hidden;">' +
        '    <div id="dtProgressFill" style="background:var(--accent);height:100%;width:0%;"></div>' +
        '  </div>' +
        '  <div id="dtProgressText" style="font-size:12px;color:var(--text-dim);margin-top:4px;"></div>' +
        '</div>';
      // The user's OWN configured path is the highest-priority probe and the
      // one they are most likely asking about. find_darktable falls through
      // silently when darktable_bin points at a path that no longer exists
      // (develop.py), and checked_paths deliberately does not include it — so
      // say it explicitly, or the panel never answers the actual question.
      if (data.configured_bin) {
        el.innerHTML += '<div style="color:var(--danger);font-size:12px;margin-top:6px;">' +
          'Configured path not found: ' + escapeHtml(data.configured_bin) + '</div>';
      }
      // Say where we looked, so a bare ✗ can explain itself.
      // 'Checked:' not 'Checked PATH and:' — element 0 of checked_paths is
      // already "$PATH (darktable-cli)" (Task 2 composes it in), so the older
      // prefix named PATH twice and implied the rest were additional to it.
      // Task 2 also guarantees the list is never empty, so no else branch.
      if (data.checked_paths && data.checked_paths.length) {
        el.innerHTML += '<div style="font-size:11px;color:var(--text-ghost);margin-top:6px;">' +
          'Checked: ' + data.checked_paths.map(escapeHtml).join(', ') + '</div>';
      }
      needsGetOption = true;
    }
    if (data.auto_convert_dng) {
      if (data.dng_available) {
        el.innerHTML += '<div style="color:var(--accent);font-size:13px;margin-top:4px;">&#10003; Adobe DNG Converter found' +
          '<span style="color:var(--text-dim);font-size:12px;margin-left:8px;">' + escapeHtml(data.dng_bin) + '</span></div>';
      } else {
        el.innerHTML += '<div style="color:var(--danger);font-size:13px;margin-top:4px;">&#10007; Adobe DNG Converter not found' +
          '<span style="color:var(--text-dim);font-size:12px;margin-left:8px;">Install it or set the path below &mdash; ' +
          '<a href="https://helpx.adobe.com/camera-raw/digital-negative.html" target="_blank" rel="noopener" ' +
          'style="color:var(--accent);">Get it from Adobe &#8599;</a></span></div>';
      }
    }
    // MUST run after the DNG block. That block does `el.innerHTML +=`, which
    // re-parses the subtree and detaches any node captured earlier — so
    // rendering the button before it would write into a dead node and the
    // button would never appear. darktable_auto_convert_dng defaults to true
    // (config.py:66), so this is the default path, not an edge case.
    if (needsGetOption) await renderDarktableGetOption();
  } catch(e) {}
}

// The button must say what it actually does. On macOS/Windows we hand off to
// the OS installer and the user finishes the job, so it says "Download
// installer" — never "Install darktable".
async function renderDarktableGetOption() {
  var host = document.getElementById('darktableGet');
  if (!host) return;
  var info;
  try {
    info = await safeFetch('/api/darktable/install/available', {}, { toast: false });
  } catch(e) {
    info = { available: false, reason: 'Could not check for a darktable release.' };
  }

  if (!info.available) {
    host.innerHTML = '<a href="https://www.darktable.org/install/" target="_blank" rel="noopener" ' +
      'style="color:var(--accent);font-size:13px;">Get darktable &#8599;</a>' +
      '<span style="color:var(--text-ghost);font-size:11px;margin-left:8px;">' +
      escapeHtml(info.reason || '') + '</span>';
    return;
  }

  var isLinux = /Linux/.test(navigator.userAgent) && !/Android/.test(navigator.userAgent);
  var label = isLinux ? 'Download and set up' : 'Download installer';
  window._dtAsset = info;
  host.innerHTML = '<button class="btn" onclick="downloadDarktable()">' + label + '</button>' +
    '<span style="color:var(--text-dim);font-size:12px;margin-left:8px;">' +
    'darktable ' + escapeHtml(info.version) + ' &mdash; ' + escapeHtml(info.name) + ', ' +
    Math.round(info.size / 1048576) + ' MB, from github.com/darktable-org</span>';
}

async function downloadDarktable() {
  var a = window._dtAsset || {};
  var isLinux = /Linux/.test(navigator.userAgent) && !/Android/.test(navigator.userAgent);
  var what = isLinux
    ? 'Vireo will download it and set the darktable-cli path for you.'
    : 'Vireo will download the installer and open it. You finish the install.';
  if (!confirm('Download darktable ' + a.version + '?\n\n' +
               a.name + ' (' + Math.round(a.size / 1048576) + ' MB)\n' +
               'From: github.com/darktable-org/darktable\n\n' + what)) return;

  document.getElementById('darktableGet').style.display = 'none';
  var wrap = document.getElementById('darktableProgress');
  var fill = document.getElementById('dtProgressFill');
  var text = document.getElementById('dtProgressText');
  wrap.style.display = 'block';
  text.textContent = 'Starting...';

  // The route 400s on insufficient disk space. Without this guard the panel
  // sits on "Starting..." forever with the button hidden.
  var resp;
  try {
    resp = await safeFetch('/api/jobs/download-darktable', { method: 'POST' });
  } catch(e) {
    wrap.style.display = 'none';
    document.getElementById('darktableGet').style.display = '';
    return;
  }

  safeEventSource('/api/jobs/' + resp.job_id + '/stream', {
    // NOTE from Task 4: p.current can move BACKWARDS. When a server ignores
    // Range, the retry truncates the .partial and restarts from 0, so the bar
    // must tolerate a decreasing current rather than assuming monotonicity.
    // That is honest — the bytes really were discarded — so do not clamp it.
    onProgress: function(p) {
      if (p.total) {
        fill.style.width = Math.round((p.current / p.total) * 100) + '%';
        text.textContent = p.phase + ': ' +
          Math.round(p.current / 1048576) + ' of ' + Math.round(p.total / 1048576) + ' MB';
      } else {
        text.textContent = p.phase + (p.current_file ? ': ' + p.current_file : '');
      }
    },
    onComplete: function(r) {
      // Job FAILURES arrive here with status !== 'completed' — not via
      // onError, which only fires on EventSource connection loss and is
      // called with no arguments. A digest mismatch lands here; rendering it
      // as a success with an empty message would be exactly the black box
      // CORE_PHILOSOPHY.md forbids. Same shape as downloadModel (:2800).
      if (r && r.status === 'cancelled') {
        // jobs.py:526 sets status 'cancelled' with empty errors and no
        // failure, so this must be handled before the failure branch or a
        // user-initiated cancel would read as "Download failed".
        fill.style.width = '0%';
        text.innerHTML = 'Download cancelled.' +
          '<br><button class="btn" style="margin-top:6px;" onclick="loadDarktableStatus()">Try again</button>';
        return;
      }
      if (!r || r.status !== 'completed') {
        var why = (r && r.failure && r.failure.message) ||
                  ((r && r.errors) || []).join(', ') || 'Download failed';
        fill.style.width = '0%';
        text.innerHTML = '<span style="color:var(--danger);">' + escapeHtml(why) + '</span>' +
          '<br><button class="btn" style="margin-top:6px;" onclick="loadDarktableStatus()">Try again</button>';
        return;
      }
      fill.style.width = '100%';
      var res = r.result || {};
      var lines = [];
      if (res.verified) lines.push(res.verified);
      if (res.config_written) {
        lines.push('Installed to ' + res.bin_path + ' and set the darktable-cli path in Settings.');
      } else if (res.downloaded_to) {
        lines.push('Downloaded to ' + res.downloaded_to + ' — opening the installer. ' +
                   'Drag darktable to Applications, then click Re-check.');
      }
      // Only warn about Gatekeeper if the file really is quarantined.
      if (res.quarantined) {
        lines.push('macOS quarantined this download. darktable does not notarize its ' +
                   'macOS builds, so you may see "damaged". To clear it, run: ' +
                   'xattr -d com.apple.quarantine ' + res.downloaded_to);
      }
      text.innerHTML = lines.map(escapeHtml).join('<br>') +
        '<br><button class="btn" style="margin-top:6px;" onclick="loadDarktableStatus()">Re-check</button>';
    },
    // safeEventSource calls onError with NO arguments (_navbar.html:10241),
    // and only for connection loss. Do not try to read an error off it.
    onError: function() {
      text.innerHTML = '<span style="color:var(--danger);">Lost connection to the ' +
        'download job. It may still be running.</span>' +
        '<br><button class="btn" style="margin-top:6px;" onclick="loadDarktableStatus()">Re-check</button>';
    }
  });
}
```

- [ ] **Step 2: Confirm the SSE completion payload shape**

Read `safeEventSource` at `vireo/templates/_navbar.html:10225` and one existing consumer (`downloadModel`, `settings.html:2776-2820`) to confirm whether `onComplete` receives the job envelope or the result directly. **Adjust `res` in `onComplete` to match** — do not guess.

- [ ] **Step 3: Run the app and drive the flow**

```bash
python vireo/app.py --db ~/.vireo/vireo.db --port 8080
```

Open Settings → RAW Development. With darktable not installed, confirm:
- the ✗ row shows a "Download installer" button plus version/size/source
- the "Checked:" line lists real paths
- clicking shows the confirmation with the exact filename and size
- the progress bar advances with real MB counts
- on completion the installer opens and "Re-check" appears
- no Gatekeeper text appears unless the file is genuinely quarantined

- [ ] **Step 4: Verify the fallback path**

Temporarily make `resolve_release` raise, reload, and confirm the panel shows the plain "Get darktable ↗" link with a reason — **not** a dead button. Revert the change.

- [ ] **Step 5: Commit**

```bash
git add vireo/templates/settings.html
git commit -m "feat: offer a darktable download from the RAW Development panel

Button labels state what actually happens: 'Download installer' on
macOS/Windows where the user finishes the install, 'Download and set up'
on Linux where we complete it. The panel lists the locations checked,
shows real byte progress, and falls back to a plain link with a stated
reason when the release cannot be resolved."
```

---

## Task 11: Full verification

- [ ] **Step 1: Run the repo's required suite**

```bash
python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py \
  vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py \
  vireo/tests/test_darktable_api.py vireo/tests/test_config.py -v
```

- [ ] **Step 2: Run the suites this work touched**

```bash
python -m pytest vireo/tests/test_develop.py vireo/tests/test_darktable_install.py \
  vireo/tests/test_taxonomy.py vireo/tests/test_job_contract.py \
  vireo/tests/test_platform_support.py -v
```

Expected: all pass. **Note:** per the local baseline there are ~4 pre-existing failures in the wider `vireo/tests` suite unrelated to this work — confirm any failure you see is one of those before treating it as a regression, and do not "fix" a test by weakening its assertion.

- [ ] **Step 3: Use the verify skill**

Invoke the `verify` skill to drive the flow end-to-end in the real app rather than relying on tests alone.

- [ ] **Step 4: Open the PR**

```bash
gh pr create --base main --title "Offer a darktable download from Settings" --body "$(cat <<'EOF'
## What

Turns the dead-end "✗ darktable-cli not found" row in Settings → RAW Development
into a working download, with byte-level progress and an OS installer hand-off.

Design: `docs/superpowers/specs/2026-07-26-darktable-download-design.md`

## Notable decisions

- **darktable only.** Adobe DNG Converter is proprietary and EULA-gated, so that
  row gets a "Get it from Adobe ↗" link and nothing more.
- **Latest release resolved from the GitHub API** at click time rather than a
  pinned version, with integrity verified against the API's own SHA256 digest.
- **No code-signature check.** darktable does not successfully notarize its macOS
  builds (upstream #19295) and its Windows installer is unsigned — a fail-closed
  signature check would have deleted every legitimate download.
- **Gatekeeper warning is conditional** on `com.apple.quarantine` actually being
  present. urllib downloads are not quarantined, so it normally stays hidden.

## Bugs fixed along the way

- `find_darktable` never probed `/Applications/darktable.app`, so a normal macOS
  install reported "not found" (`find_dng_converter` already probed its Adobe
  equivalent). Without this, a successful download still ended in ✗.
- Asset matching is exact-suffix: the release ships ~300KB `.zsync` manifests
  whose names contain "AppImage", and a substring matcher would have installed
  one of those instead of darktable.

## Test results

<!-- paste the output of Task 11 Steps 1-2 here -->
EOF
)"
```

---

## Out of scope (recorded so it is not silently dropped)

- **flatpak detection** — needs `find_darktable` to return a command list rather
  than a path, changing every caller. Separate change.
- **A download button on the dependency-readiness panel.** It reads the same
  `find_darktable`, so it inherits the detection fix and cannot disagree about
  availability; its hint already points at Settings.
- **Confirming quarantine behaviour inside the packaged Tauri app.** The
  conditional check makes either outcome correct, but the finding should be
  recorded when someone next builds a release.
