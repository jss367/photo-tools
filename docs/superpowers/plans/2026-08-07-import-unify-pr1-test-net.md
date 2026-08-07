# Import Unification PR 1: Test Net Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the local/remote import test net (parity harness + transport-specific characterization) so the behavior-alignment and refactor PRs that follow land on a net that actually catches one-sided changes.

**Architecture:** Tests-only PR against `vireo/tests/test_import_job.py`. Two halves per the spec (`docs/superpowers/specs/2026-08-06-import-path-unification-design.md`, PR 1): (a) a *generalized* parity harness — the existing one at test line ~8465 only exercises selection payloads on a fresh destination — plus behavioral parity scenarios with seeded destinations and DB-level observables; (b) characterization tests pinning transport-specific semantics the parity harness structurally cannot see.

**Tech Stack:** pytest, existing harness helpers (`FakeRunner`, `_make_card`, `_make_job`, `_remote_archive_for`, `_install_fake_remote_rsync`, `_remote_calls`), monkeypatched `move.py` transport seams.

---

## Context for a zero-context engineer

Read these before starting; everything below assumes them:

- Spec: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md` (especially "PR 1" and the outcome-completeness invariant in the Transport protocol section).
- `vireo/tests/test_import_job.py:15-97` — `FakeRunner` (cancel = add job id to `runner.cancelled_ids`), `_make_job`, `_make_card` (tiny PIL JPEGs; **distinct colors = distinct bytes**; identical colors = byte-identical files; mtime drives the `%Y/%Y-%m-%d` destination folder), `_run_import`, `_photo_rows`.
- `vireo/tests/test_import_job.py:4147-4300` — remote harness. The fake rsync maps NAS paths back onto a local `mount/` dir by swapping the SSH base prefix; `_run_remote_import` **forces `verify_by_hash=True`** (with it off, `remote_unverified` makes both card-safety verdicts `False` and safety assertions pass vacuously). Local parity runners must force it too.
- `vireo/tests/test_import_job.py:8465-8720` — the selection parity harness this plan generalizes. Copy its patterns: per-scenario sibling tmp dirs, whole-dict equality with a mismatch list, a distinctness meta-test, and a local-path expected-outcomes control test.
- `vireo/import_job.py:3044-3068` — `run_import_job` is the only entry point; `params.remote_target` dispatches to `_run_remote_import_job`.
- Run tests from the **repo root**. A single test: `python -m pytest "vireo/tests/test_import_job.py::test_name" -q`. The whole file takes several minutes; individual tests are seconds.
- All work happens on the current branch (`vireo-tech-debt-audit`, already isolated in this Conductor workspace). Commit after every task.

**Characterization-test discipline:** several tasks pin *current* behavior. Write the test with your best-guess assertion, run it, and if reality differs, **update the assertion to reality and document it in the docstring** — do not "fix" the production code in this PR. The one exception: if a run reveals an outright crash, stop and surface it.

---

### Task 1: Generalized parity runners and DB-level observables

**Files:**
- Modify: `vireo/tests/test_import_job.py` (insert after `_selection_observables`, ~line 8523)

- [ ] **Step 1: Write the runners and observables helper**

```python
def _dest_photo_facts(db, dest_root):
    """DB-level import outcome, normalized for local/remote comparison:
    {(folder relpath under dest_root, filename, hash_status)}.
    file_hash presence is implied by hash_status; the hash VALUE is
    excluded because both paths must agree on it via safe_to_format
    assertions instead (comparing values here would double-report)."""
    facts = set()
    for row in _photo_rows(db):
        rel = os.path.relpath(row["folder_path"], str(dest_root))
        facts.add((rel, row["filename"], row["hash_status"]))
    return facts


def _linked_folder_rels(db, dest_root):
    """Folder paths visible in the active workspace, relative to the
    destination root. Twin-folder linking is workspace-scoped, so this is
    where a one-sided _link_duplicate_twin_dirs regression shows up."""
    rows = db.conn.execute(
        """SELECT f.path FROM folders f
           JOIN workspace_folders wf ON wf.folder_id = f.id
           WHERE wf.workspace_id = ?""",
        (db._active_workspace_id,),
    ).fetchall()
    return {os.path.relpath(r["path"], str(dest_root)) for r in rows}


def _behavior_observables(result, runner, db, dest_root):
    """Superset of _selection_observables: adds DB-level facts. Excludes
    the same legitimately-divergent keys (photo_ids, folders, errors
    ordering) plus eta fields."""
    obs = _selection_observables(result, runner)
    obs["verified"] = result["verified"]
    obs["cancelled"] = result["cancelled"]
    obs["db_photos"] = _dest_photo_facts(db, dest_root)
    obs["db_linked_folders"] = _linked_folder_rels(db, dest_root)
    return obs


def _run_local_behavior_case(root, monkeypatch, specs, *, seed=None,
                             params_kwargs=None, runner=None,
                             verify_by_hash=True):
    """Local-path runner for behavioral parity scenarios.

    ``seed(dest_root, db_path)`` runs BEFORE the measured import to
    pre-populate/pre-catalog the destination (e.g. by running a prior
    import). ``verify_by_hash=True`` for the same anti-vacuity reason as
    _run_local_selection_case.
    """
    from import_job import ImportParams, run_import_job

    card = _make_card(root, specs)
    dest_root = root / "archive"
    db_path = str(root / "test.db")
    db = Database(db_path)
    if seed is not None:
        seed(dest_root, db_path)
    runner = runner or FakeRunner()
    result = run_import_job(
        _make_job(), runner, db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=str(dest_root),
            verify_by_hash=verify_by_hash, **(params_kwargs or {}),
        ),
    )
    return _behavior_observables(result, runner, db, dest_root)


def _run_remote_behavior_case(root, monkeypatch, specs, *, seed=None,
                              params_kwargs=None, runner=None,
                              verify_by_hash=True):
    """Remote-path runner. Mirrors _run_local_behavior_case's geometry:
    the mount base plays the destination root, and ``seed`` receives it.
    Builds the transport seams itself (rather than _run_remote_import) so
    it can hand ``seed`` the db_path before the measured run."""
    from import_job import ImportParams, run_import_job

    card = _make_card(root, specs)
    ra = _remote_archive_for(root)
    calls = _remote_calls(ra)
    _install_fake_remote_rsync(monkeypatch, calls, verify=None)
    db_path = str(root / "test.db")
    db = Database(db_path)
    if seed is not None:
        seed(Path(ra["mount_base"]), db_path)
    runner = runner or FakeRunner()
    result = run_import_job(
        _make_job(), runner, db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=ra["mount_base"],
            remote_target=ra, verify_by_hash=verify_by_hash,
            **(params_kwargs or {}),
        ),
    )
    return _behavior_observables(result, runner, db, ra["mount_base"])
```

Note `_run_remote_behavior_case` seeds via the *mount*, exactly as a real
NAS twin would appear through SMB. Seeds that need cataloging run a **prior
import through the same path's entry point** (see Task 2 scenarios), so
each side seeds itself the way production would.

- [ ] **Step 2: Smoke-run the helpers with a trivial inline check**

Temporarily add and run:

```python
def test_behavior_harness_smoke(tmp_path, monkeypatch):
    local = _run_local_behavior_case(
        tmp_path / "l", monkeypatch, _PARITY_CARD)
    remote = _run_remote_behavior_case(
        tmp_path / "r", monkeypatch, _PARITY_CARD)
    assert local == remote
```

(Create the two subdirs first — `(tmp_path / "l").mkdir()` etc. if the
helpers don't; match how the selection parity test builds roots.)

Run: `python -m pytest "vireo/tests/test_import_job.py::test_behavior_harness_smoke" -q`
Expected: PASS. If it fails on a key like `db_photos`, inspect whether the
relpath normalization is wrong (fix harness) or the paths genuinely
diverge (record it — that's a finding for the spec's decision table, tell
the user). Watch `verified` specifically: `_selection_observables`
deliberately excluded it, and `_behavior_observables` re-adds it on the
theory that forced `verify_by_hash=True` makes the two paths agree — if
the smoke test fails on that key alone, that exclusion was masking a real
numeric divergence; characterize it, don't hide it again.

- [ ] **Step 3: Keep the smoke test (rename to `test_local_and_remote_agree_on_plain_import`) and commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: generalized local/remote parity harness with DB-level observables"
```

### Task 2: Behavioral parity scenarios + meta-tests

**Files:**
- Modify: `vireo/tests/test_import_job.py` (after Task 1's helpers)

Scenario list, following the `_SELECTION_PARITY_SCENARIOS` pattern —
`(name, card_specs, seed_builder, params_kwargs)`:

- [ ] **Step 1: Write the scenarios**

```python
def _seed_prior_import(specs):
    """Seed by running a full prior import of ``specs`` through the SAME
    path as the measured run — the seed card lives in a sibling dir."""
    def seed_local(dest_root, db_path):
        from import_job import ImportParams, run_import_job
        seed_root = dest_root.parent / "seedcard"
        seed_root.mkdir(exist_ok=True)
        card = _make_card(seed_root, specs, card_name="prior")
        db = Database(db_path)
        run_import_job(
            _make_job("seed-import"), FakeRunner(), db_path,
            db._active_workspace_id,
            ImportParams(sources=[str(card)], destination=str(dest_root),
                         verify_by_hash=True))
    return seed_local
```

(For the remote runner the same builder works: `dest_root` is the mount
base and the seed import runs **locally into the mount** — which is
exactly what "the NAS already holds cataloged photos" looks like from
this machine. Document that in the scenario list comment.)

```python
_TWIN = ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red")

_BEHAVIOR_PARITY_SCENARIOS = [
    # Duplicate skip against a cataloged twin: same file re-imported.
    ("duplicate_skip", [_TWIN], _seed_prior_import([_TWIN]), {}),
    # Basename collision, different bytes: seed cataloged blue DSC_0001,
    # import red DSC_0001 -> suffix copy DSC_0001_1.jpg.
    ("collision_different_bytes", [_TWIN],
     _seed_prior_import([("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0),
                          "blue")]), {}),
    # Crash-recovery adoption: identical bytes already AT the template
    # path but NOT cataloged (plain file drop, no prior import).
    ("adoption_uncataloged_dest_twin", [_TWIN], _seed_file_drop([_TWIN]),
     {}),
    # Mixed batch: one fresh copy + one duplicate of a cataloged twin.
    ("mixed_fresh_and_duplicate",
     [_TWIN, ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green")],
     _seed_prior_import([_TWIN]), {}),
]
```

with the file-drop seeder:

```python
def _seed_file_drop(specs):
    """Seed by writing files at their template destination WITHOUT
    cataloging them (simulates a prior crashed run)."""
    def seed(dest_root, db_path):
        for name, mtime, *color in specs:
            folder = dest_root / mtime.strftime("%Y/%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (16, 16), (color or ["red"])[0]).save(
                str(folder / name))
            ts = mtime.timestamp()
            os.utime(str(folder / name), (ts, ts))
    return seed
```

**Careful:** `_seed_file_drop` must produce *byte-identical* files to the
card's. `_make_card` and this seeder must construct the image the same way
(same size/color/format) — verify by comparing hashes in Step 3's run; if
PIL metadata makes them differ, write the seed by copying the card file
instead (build the card first, then copy).

- [ ] **Step 2: Write the three tests (mirror the selection-parity trio)**

```python
def test_behavior_parity_scenarios_are_distinct():
    names = [n for n, _s, _seed, _p in _BEHAVIOR_PARITY_SCENARIOS]
    assert len(names) == len(set(names))
    keys = {(repr(s), seed.__qualname__.split(".")[0], repr(p))
            for _n, s, seed, p in _BEHAVIOR_PARITY_SCENARIOS}
    assert len(keys) == len(_BEHAVIOR_PARITY_SCENARIOS)


def test_local_and_remote_behavior_results_agree(tmp_path, monkeypatch):
    """CHARACTERIZATION: seeded-destination scenarios must produce the
    same outcome, DB rows included, on both copy paths."""
    mismatches = []
    for name, specs, seed, pkw in _BEHAVIOR_PARITY_SCENARIOS:
        lroot = tmp_path / f"local_{name}"; lroot.mkdir()
        rroot = tmp_path / f"remote_{name}"; rroot.mkdir()
        local = _run_local_behavior_case(
            lroot, monkeypatch, specs, seed=seed, params_kwargs=pkw)
        remote = _run_remote_behavior_case(
            rroot, monkeypatch, specs, seed=seed, params_kwargs=pkw)
        if local != remote:
            mismatches.append((name, local, remote))
    assert not mismatches, "\n".join(
        f"{n}:\n  local ={l}\n  remote={r}" for n, l, r in mismatches)


def test_behavior_parity_scenarios_exercise_their_branches(
        tmp_path, monkeypatch):
    """Positive control: pin each scenario's expected local outcome so a
    branch that stops firing fails here, not silently in parity."""
    seen = {}
    for name, specs, seed, pkw in _BEHAVIOR_PARITY_SCENARIOS:
        root = tmp_path / name; root.mkdir()
        seen[name] = _run_local_behavior_case(
            root, monkeypatch, specs, seed=seed, params_kwargs=pkw)

    assert seen["duplicate_skip"]["skipped_duplicate"] == 1
    assert seen["duplicate_skip"]["copied"] == 0
    assert seen["duplicate_skip"]["safe_to_format"] is True

    assert seen["collision_different_bytes"]["copied"] == 1
    assert any(fn == "DSC_0001_1.jpg"
               for _rel, fn, _hs in
               seen["collision_different_bytes"]["db_photos"])

    assert seen["adoption_uncataloged_dest_twin"]["skipped_duplicate"] == 1
    assert seen["adoption_uncataloged_dest_twin"]["copied"] == 0
    assert seen["adoption_uncataloged_dest_twin"]["safe_to_format"] is True
    # The adopted file gained a photo row.
    assert any(fn == "DSC_0001.jpg"
               for _rel, fn, _hs in
               seen["adoption_uncataloged_dest_twin"]["db_photos"])

    assert seen["mixed_fresh_and_duplicate"]["copied"] == 1
    assert seen["mixed_fresh_and_duplicate"]["skipped_duplicate"] == 1
    assert seen["mixed_fresh_and_duplicate"]["safe_to_format"] is True
```

- [ ] **Step 3: Run all three; reconcile**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "behavior"`
Expected: the control test's assertions may need adjusting to observed
reality (characterization discipline above — e.g. exact `hash_status`
values, or whether adoption re-verification marks `ok`). The parity test
**may genuinely fail** — the spec predicts at least one divergence
(decision 6: remote lacks derived-cache invalidation — invisible here —
but watch for `db_photos`/`verified` splits from decisions 5/7). If a
scenario diverges: move that scenario out of the parity list into a pair
of per-path characterization tests named
`test_local_<scenario>_current_behavior` / `test_remote_<scenario>_current_behavior`,
each with a docstring citing the spec decision that will re-unify them,
and leave a one-line comment in the scenario list. Do NOT delete the
scenario; it returns to the parity list in the PR that fixes the drift.

- [ ] **Step 4: Commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: behavioral local/remote parity scenarios (seeded destinations, DB-level)"
```

### Task 3: Fix mirror-pair geometry A (stuck-twin-hash cancel tests)

**Files:**
- Modify: `vireo/tests/test_import_job.py:10012-10130` (read the exact
  current bodies first; line numbers may drift a few lines)

The pair `test_local_import_cancel_interrupts_stuck_twin_hash` (~10012) /
`test_remote_import_cancel_interrupts_stuck_twin_hash` (~10074) assert the
same conclusion through different branches: the local test parks the FIFO
twin at `archive/old/IMG_0300.jpg` (non-template folder → only the
duplicate-gate twin re-hash can reach it), the remote test at
`mount/2026/2026-01-01/IMG_0300.jpg` (template-shaped → the collision walk
could reach it instead).

- [ ] **Step 1: Align the remote test to the local geometry**

Move the remote test's twin fixture to `mount/old/IMG_0300.jpg` (matching
`archive/old/` on the local side): update the twin file creation path and
whatever catalogs it (the pre-run `scan()` or prior-import call — read the
test body). Keep everything else identical. Add to the docstring:
"Geometry matches the local mirror: the twin lives OFF the template path
so only the duplicate-gate twin re-hash can reach it — a template-shaped
twin would let the collision/adopt walk satisfy this test with the gate
broken."

- [ ] **Step 2: Run both tests**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "cancel_interrupts_stuck_twin_hash"`
Expected: 2 passed. (These use `os.mkfifo` — POSIX-only; on this Mac they
run.) If the remote one now fails, the geometry change exposed a real
branch difference: characterize per the discipline above and flag it in
the final report.

- [ ] **Step 3: Check the sibling pair with the same split**

The `skips_post_loop_mount_probe` pair (~10289 remote / ~10394 local) has
the same geometry split (local `archive/old/`, remote template-shaped).
Apply the same alignment and docstring note.

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "skips_post_loop_mount_probe"`
Expected: all passing.

- [ ] **Step 4: Commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: align remote stuck-twin-hash/mount-probe mirrors to local fixture geometry"
```

### Task 4: Fix mirror-pair geometry B (dest_read_cancel catalog-scan gate)

**Files:**
- Modify: `vireo/tests/test_import_job.py:10701-10945` (read current bodies
  first)

`test_local_import_dest_read_cancel_skips_catalog_scan` (~10736) uses a
fresh copy (gates `if landed:`); `test_remote_import_dest_read_cancel_skips_catalog_scan`
(~10837) pre-writes the mount file so it takes the adoption path (gates
`if landed or adopted_paths:`). Both assert `scan_calls == []` but through
different guard terms. Don't change these two — each is a valid test of
its own branch. **Add the two missing counterparts** so each branch has a
true mirror:

- [ ] **Step 1: Add `test_local_import_dest_read_cancel_skips_catalog_scan_adoption_path`**

Clone the local test, but pre-write the destination file (byte-identical,
at the template path, uncataloged) the way the *remote* test does, so the
local run takes the adoption branch before the `DestReadCancelled` fires.
Reuse the existing test's monkeypatch spy pattern for `_hash_dest_file` /
`scan` (read how the original wires them). Assert `scan_calls == []` and
the adoption was rolled back or ignored exactly as the run reports
(characterization — record actuals).

- [ ] **Step 2: Add `test_remote_import_dest_read_cancel_skips_catalog_scan_fresh_transfer`**

Clone the remote test with NO pre-written mount file (pure fresh-transfer
geometry, mirroring the local original). Assert `scan_calls == []`.

- [ ] **Step 3: Run all four**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "dest_read_cancel_skips_catalog_scan"`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: complete both fixture geometries for the dest-read-cancel scan gate"
```

### Task 5: Characterize stop between renamed-file transfers (coverage gap)

**Files:**
- Modify: `vireo/tests/test_import_job.py` (place near
  `test_remote_import_stop_kills_in_flight_rsync_batch`, ~6788 — read that
  test first; this one follows its shape)

This is the *renamed-transfer* half of the spec's outcome-completeness
invariant: per-file rsyncs that returned success before the stop KEEP
their outcomes; later queued files produce neither failures nor landings.
No existing test covers it (verified 2026-08-07: the only stop-mid-rsync
test kills the flat batch).

- [ ] **Step 1: Write the test**

Geometry: both card files must take the *renamed* path — seed the mount
with same-name different-byte files at the template path so the collision
walk assigns `_1` suffixes, making `flat` empty and `renamed` hold both.

```python
def test_remote_import_stop_between_renamed_transfers_keeps_completed_files(
        tmp_path, monkeypatch):
    """CHARACTERIZATION (spec: outcome-completeness invariant). Renamed
    files transfer one rsync each. A Stop after the first file's rsync
    returned success must keep that file's outcome (verified + cataloged)
    while the not-yet-transferred file produces neither a failure nor a
    landing — it stays on the card for the next run."""
    import shutil

    import move as _move
    from import_job import ImportParams, run_import_job

    ra = _remote_archive_for(tmp_path)
    calls = _remote_calls(ra)
    _install_fake_remote_rsync(monkeypatch, calls, verify=None)

    # Same capture date -> same batch; distinct colors -> distinct bytes.
    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 10, 5, 0), "green"),
    ])
    # Force collisions: different-byte files already at the template path.
    mount_day = Path(ra["mount_base"]) / "2026" / "2026-07-03"
    mount_day.mkdir(parents=True)
    for name in ("DSC_0001.jpg", "DSC_0002.jpg"):
        Image.new("RGB", (16, 16), "blue").save(str(mount_day / name))

    runner = FakeRunner()
    job = _make_job()
    base_fake = _move._run_rsync_streamed  # the Task-harness fake

    state = {"renamed_calls": 0}

    def stop_after_first_renamed(src_path, dest_spec, rsync_flags,
                                 total_files, progress_cb,
                                 rsync_bin="rsync", extra_args=None,
                                 src_specs=None,
                                 src_specs_dest_is_dir=True, **kw):
        assert not src_specs_dest_is_dir, (
            "expected only renamed (file-dest) transfers in this geometry")
        state["renamed_calls"] += 1
        rc = base_fake(src_path, dest_spec, rsync_flags, total_files,
                       progress_cb, rsync_bin=rsync_bin,
                       extra_args=extra_args, src_specs=src_specs,
                       src_specs_dest_is_dir=src_specs_dest_is_dir, **kw)
        # Stop arrives after this file completed.
        runner.cancelled_ids.add(job["id"])
        return rc

    monkeypatch.setattr(_move, "_run_rsync_streamed",
                        stop_after_first_renamed)

    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    result = run_import_job(
        job, runner, db_path, db._active_workspace_id,
        ImportParams(sources=[str(card)], destination=ra["mount_base"],
                     remote_target=ra, verify_by_hash=True))

    # Only the first renamed rsync ran; the loop observed Stop before the
    # second (import_job.py:2361-2364).
    assert state["renamed_calls"] == 1
    assert result["cancelled"] is True
    # The completed file keeps its outcome...
    assert result["copied"] == 1, result
    suffixed = [(fn, hs) for _rel, fn, hs in
                _dest_photo_facts(db, ra["mount_base"])
                if fn.startswith("DSC_0001")]
    assert any(fn == "DSC_0001_1.jpg" for fn, _hs in suffixed), suffixed
    # ...and the un-transferred file is neither failed nor landed.
    assert result["failed"] == 0, result
    assert not any(fn.startswith("DSC_0002_") for _rel, fn, _hs in
                   _dest_photo_facts(db, ra["mount_base"]))
    assert result["safe_to_format"] is False
```

**Trap:** the batch cancel check at the top of the *file* loop and the
duplicate-gate hashing of the pre-seeded collision files both run before
transfer; setting `cancelled_ids` inside the rsync fake (not before the
run) is what keeps them un-cancelled. Also `_stop_requested` feeds
`_hash_dest_file` — since the collision hashing happens before the stop
flag flips, it is unaffected. If the run turns out to route these files
through the flat batch instead (i.e. `dest_basename == source name`),
re-read `import_job.py:2286-2293` and fix the seeding, not the assertion.

- [ ] **Step 2: Run it**

Run: `python -m pytest "vireo/tests/test_import_job.py::test_remote_import_stop_between_renamed_transfers_keeps_completed_files" -q`
Expected: PASS. If an assertion fails, apply characterization discipline:
verify the observed behavior against `import_job.py:2361-2382` before
changing the assertion, and record any surprise in the docstring.

- [ ] **Step 3: Commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: pin renamed-transfer stop semantics (outcome-completeness invariant)"
```

### Task 6: Characterize decision 5 (renamed twin of an accepted duplicate), per path

**Files:**
- Modify: `vireo/tests/test_import_job.py` (place after Task 2's scenarios)

Spec decision 5 removes the remote-only `_record_checker(source_file)`
call at `import_job.py:2008` in PR 3. These tests pin the *current*
behavior of each path in the geometry where that call could matter, so
the removal is an observable, deliberate change (or a proven no-op).

Geometry: a cataloged twin of `X.jpg`; the measured card holds `X.jpg`
(accepted as duplicate of the twin) and `Y.jpg` with **identical bytes but
a different name** (the renamed twin). Whether the two paths diverge
depends on whether `Y` can only match via the checker's `_seen_*` sets
(populated by the 2008 call) or also via the catalog index. Do not guess —
run both and record.

- [ ] **Step 1: Write both tests with best-guess assertions**

```python
def _renamed_twin_case_specs():
    twin = ("X.jpg", datetime(2026, 7, 3, 10, 0, 0), "red")
    card = [twin, ("Y.jpg", datetime(2026, 7, 3, 10, 0, 0), "red")]
    return twin, card


def test_local_renamed_twin_of_accepted_duplicate_current_behavior(
        tmp_path, monkeypatch):
    """CHARACTERIZATION for spec decision 5 (local half). The local path
    does NOT register accepted duplicates with the checker."""
    twin, card = _renamed_twin_case_specs()
    obs = _run_local_behavior_case(
        tmp_path, monkeypatch, card, seed=_seed_prior_import([twin]))
    # Best guess: Y still matches through the catalog's hash index, so
    # both files skip. RECORD ACTUALS — if Y copies instead, assert that
    # and say so in the docstring.
    assert obs["skipped_duplicate"] == 2, obs
    assert obs["copied"] == 0, obs


def test_remote_renamed_twin_of_accepted_duplicate_current_behavior(
        tmp_path, monkeypatch):
    """CHARACTERIZATION for spec decision 5 (remote half). The remote path
    registers accepted duplicates via _record_checker(source_file) at
    import_job.py:2008; PR 3 removes that call, and this test is the
    tripwire that makes the removal visible."""
    twin, card = _renamed_twin_case_specs()
    obs = _run_remote_behavior_case(
        tmp_path, monkeypatch, card, seed=_seed_prior_import([twin]))
    assert obs["skipped_duplicate"] == 2, obs
    assert obs["copied"] == 0, obs
```

- [ ] **Step 2: Run, record actuals, and write down the verdict**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "renamed_twin_of_accepted_duplicate"`

Three possible worlds — handle explicitly:
1. **Both skip both files** (catalog hash index covers Y): the 2008 call
   is behaviorally dead in this geometry. Note that in both docstrings —
   it strengthens the removal case. Try ONE variation before concluding:
   pass `verify_by_hash=False` (a dedicated runner kwarg — do NOT put it
   in `params_kwargs`, which would collide with the runner's own
   argument) on a copied pair of tests (`_no_verify` suffix) — key-based
   matching is where `_seen_keys` could matter. Keep whichever variant
   differentiates; drop the other.
2. **Paths differ** (e.g. local copies Y, remote skips it): pin each
   side's actual numbers with a docstring cross-reference to decision 5.
   This is the expected-by-spec outcome.
3. **Both copy Y**: record it; the 2008 call is fully dead weight and
   PR 3's removal needs no behavior test beyond these.

- [ ] **Step 3: Commit**

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: pin renamed-twin-of-accepted-duplicate behavior per path (spec decision 5)"
```

### Task 7: Audit + top-up existing transport-specific coverage

**Files:**
- Modify (only if gaps found): `vireo/tests/test_import_job.py`

The spec lists three more characterization targets that largely exist
already. Verify rather than duplicate:

- [ ] **Step 1: Confirm the flat-batch stop pin**

Read `test_remote_import_stop_kills_in_flight_rsync_batch` (~6788). It
already asserts cancelled/failed==0/copied==0/safe_to_format False. Gap
check: it does NOT assert the DB is row-free for the interrupted batch.
Add one line (`assert _photo_rows(db) == []` — the test already holds
`db`) if absent.

- [ ] **Step 2: Confirm the verify-failure and no-verify pins**

Read `test_remote_import_verify_failure_fails_specific_file` (~4504) and
the `enable verify_by_hash` assertion near ~4416
(`test_remote_import_no_verify_*` cluster, ~4380-4470 and ~4668). Confirm
between them they pin: default `verify_by_hash=False` → `safe_to_format`
False with the `"enable verify_by_hash for remote verification"` reason;
`remote_verify_files` failure after a successful transfer → that file
fails, others land. Add only what's missing, including a
`hash_status`/DB assertion if neither has one.

- [ ] **Step 3: Run the touched tests, commit (or note "no gaps" in the PR body)**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "stop_kills_in_flight or verify_failure_fails_specific or no_verify"`

```bash
git add vireo/tests/test_import_job.py
git commit -m "test: DB-level assertions for existing transport characterization pins"
```

### Task 8: Full-file run and PR

- [ ] **Step 1: Run the whole test file**

Run: `python -m pytest vireo/tests/test_import_job.py -q`
Expected: all pass (a few minutes). Known machine quirk (memory): 4
pre-existing failures exist in the *wider* `vireo/tests` suite — none of
them in this file; anything red here is yours.

- [ ] **Step 2: Run the CLAUDE.md required suite**

Run: `python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -q`
Expected: pass, modulo the known `test_api_exiftool_status_reports_missing`
environment failure (pre-existing on machines with exiftool installed).

- [ ] **Step 3: Create the PR**

```bash
gh pr create --base main --title "Import unification PR 1: widen the local/remote test net" --body "..."
```

Body must include: link to the spec file, the parity-scenario list, which
mirror pairs were geometry-aligned, the decision-5 verdict from Task 6
Step 2 (which of the three worlds), any divergences moved out of the
parity list in Task 2 Step 3, and full test results. Also note that the
spec's "mount loss mid-batch" and "stop during a destination read" parity
items are delivered as geometry-aligned mirror pairs (Tasks 3–4) rather
than dict-comparison scenarios — they need FIFOs/cancel timing that don't
fit the harness — so a spec-vs-PR reviewer doesn't flag them as missing. Per CLAUDE.md, review
feedback lands on this same branch.
