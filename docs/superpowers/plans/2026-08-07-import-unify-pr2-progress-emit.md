# Import Unification PR 2: Progress/Emit Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land spec decisions 1–3 — remote progress events carry the per-folder snapshot, the local dest-under-source batch refusal reports progress, and the missing-mount-root refusal emits the honest phase string on both paths.

**Architecture:** First production-code PR of the import-path-unification project (spec: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md`, "Behavior alignment decisions" 1–3). Three small, independent behavior alignments in `vireo/import_job.py`, each TDD'd with an event-stream regression test in `vireo/tests/test_import_job.py`, leaning on the PR 1 parity harness (merged as #1428). None of these touch the duplicate/collision/adopt walk, so the preflight mirrors at `app.py:18300–18550` are unaffected — no sync needed (spec's PR 2/3 checklist item: checked, n/a).

**Tech Stack:** pytest; PR 1 harness (`_run_local_behavior_case`, `_run_remote_behavior_case`, `_behavior_observables`, `FakeRunner`, `_make_card`).

---

## Context for a zero-context engineer

- Repo root: `/Users/julius/conductor/workspaces/vireo/nagoya`; branch `import-unify-pr2-progress-emit` (checked out, tracks origin/main). Commit to it; no new branches. Run tests from repo root.
- `vireo/import_job.py` implements imports twice: `_run_remote_import_job` (≈1235–3041) and the local body of `run_import_job` (≈3044–4700). This PR deliberately changes ONE path per decision to match the other — the point is alignment, so the "mirrors the other path" comments you touch must stay truthful.
- The frontend contract: every progress event's `folders={rel: {copied, skipped_duplicate, failed}}` snapshot drives the Import page's live folder table (`vireo/templates/import.html` renders it whenever `data.folders` is present, ~line 3725). `phase` is opaque display text to the UI. Tests pin `"Discovering files"` and `"{rel}: importing"` — do not touch those.
- PR 1 harness (all in `vireo/tests/test_import_job.py`): `_run_local_behavior_case(root, monkeypatch, specs, *, seed=None, params_kwargs=None, runner=None, verify_by_hash=True)` and `_run_remote_behavior_case(...)` (~8787); `_behavior_observables(result, runner, db, dest_root)` (~8746); `_PARITY_CARD` (~8852); `_BEHAVIOR_PARITY_SCENARIOS` + `test_local_and_remote_behavior_results_agree` + positive-control test below that. `FakeRunner.events` holds `(job_id, kind, data)`; progress payloads are dicts.
- CHARACTERIZATION vs CHANGE: PR 1 pinned current behavior; THIS PR deliberately changes behavior per the spec. Where an existing test pins the OLD behavior, update it — citing the spec decision in the test's docstring/comment — rather than treating the failure as a regression. Every such update must be listed in the PR body.

### Task 0: Branch sanity

- [ ] Run: `git branch --show-current` → expect `import-unify-pr2-progress-emit`; `git log --oneline -1` → expect `2ba3d889` (PR 1 merge) or newer. Run `python -m pytest vireo/tests/test_import_job.py -q -k "behavior or agree_on_plain"` → expect 7 passed (baseline green before any change).

### Task 1: Decision 1 — remote progress events carry the folders snapshot

**Files:**
- Modify: `vireo/import_job.py:1352-1421` (remote `_emit` / `_emit_transfer`), `:1505` area (remote `folder_counts` declaration)
- Test: `vireo/tests/test_import_job.py` (`_behavior_observables` ~8600, plus one new test)

- [ ] **Step 1: Write the failing tests**

(a) Extend `_behavior_observables` — two new keys, so EVERY parity scenario and characterization pair permanently checks the folders contract on both paths:

```python
    # Decision 1 (spec): every progress event must carry the per-folder
    # snapshot the Import page renders. Count the events that don't, and
    # capture the final snapshot for cross-path comparison.
    events = [d for _, kind, d in runner.events if kind == "progress"]
    obs["events_missing_folders"] = sum(
        1 for d in events if "folders" not in d)
    obs["folders_final"] = (
        {rel: dict(c) for rel, c in events[-1]["folders"].items()}
        if events and "folders" in events[-1] else None)
```

(Insert before the `return obs`. Note `_selection_observables` already computed `events` — recompute locally rather than refactoring it.)

(b) New dedicated test, placed after `test_local_and_remote_agree_on_plain_import`:

```python
def test_remote_import_progress_events_carry_folder_snapshots(
        tmp_path, monkeypatch):
    """Spec decision 1: the remote path historically never sent the
    ``folders={...}`` snapshot, so the Import page's live folder table
    stayed empty for remote imports. Every progress event — including
    the transfer sub-progress events — must now carry it, and the final
    snapshot must agree with the result dict's ``folders``."""
    runner = FakeRunner()
    obs = _run_remote_behavior_case(
        tmp_path, monkeypatch, _PARITY_CARD, runner=runner)
    assert obs["events_missing_folders"] == 0, obs["events_missing_folders"]
    # Final snapshot matches the terminal per-folder result.
    assert obs["folders_final"] == {
        "2026/2026-07-03": {"copied": 3, "skipped_duplicate": 0,
                            "failed": 0}}
```

(If the folder rel or counts dict shape differs in practice, fix the EXPECTATION to the local path's actual shape — run the same assertions through `_run_local_behavior_case` to see it — never by weakening the all-events check.)

NOTE — transfer events deliberately NOT asserted here: `_install_fake_remote_rsync`'s fake never calls `progress_cb`, so this harness produces zero `_emit_transfer` events and an `assert transfer_events` would be permanently red.

(c) Instead, extend the EXISTING `test_remote_import_reports_per_file_transfer_progress` (~test line 7133) — its `streaming_rsync` wrapper delegates to the installed fake and then calls `progress_cb` per file, so it produces real transfer events. In its per-event assertion loop, add:

```python
        # Spec decision 1: transfer sub-progress events must also carry
        # the folders snapshot, or the Import page's folder table blanks
        # for the duration of every batch transfer.
        assert "folders" in ev, ev
```

(Adapt the variable name to the test's actual loop; read it first.) Also add the one-line docstring note to `_behavior_observables` that its `folders`-exclusion comment inherited from `_selection_observables` no longer fully applies — `folders_final` is now compared cross-path (rel keys come from `build_destination_path` on both paths, so equality holds).

- [ ] **Step 2: Run to verify red**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "behavior or agree_on_plain or folder_snapshots or per_file_transfer_progress"`
Expected: the new dedicated test FAILS (`events_missing_folders > 0`), `test_local_and_remote_behavior_results_agree` + the plain-import baseline FAIL on the new observable keys (remote missing folders), and `test_remote_import_reports_per_file_transfer_progress` FAILS on the new `"folders" in ev` assertion. The adoption/renamed-twin characterization pairs may also fail on the new keys — that's the same red, listed now, fixed by the same production change.

- [ ] **Step 3: Implement**

In `vireo/import_job.py`, remote function:

(i) Move the `folder_counts = {}` declaration (currently ~line 1505, inside the ledger block below `_emit`) to just above the remote `_emit` definition (~1356), with the local path's rationale comment adapted:

```python
    # Live per-folder counters, mutated by the copy loop via _counts() and
    # snapshotted onto every progress event so the Import page can render
    # truthful per-folder progress mid-run. Declared before _emit so the
    # discovery-phase emits see an empty-but-present dict. Mirrors the
    # local path.
    folder_counts = {}
```

Delete the old declaration line (keep any surrounding ledger comments intact; if the old line carries its own comment, move/merge it).

(ii) In remote `_emit` (~1378–1383), add the snapshot to the pushed event, exactly like the local `_emit` (3207–3218):

```python
        runner.push_event(
            job["id"], "progress",
            progress_event(
                phase, current, total, current_file,
                # Snapshot (counts dicts mutate as the loop advances; SSE
                # consumers must see the state at emit time). Mirrors the
                # local path — spec decision 1.
                folders={
                    rel: dict(counts) for rel, counts in folder_counts.items()
                },
                **eta_fields,
            ),
        )
```

(iii) In `_emit_transfer` (~1413–1420), add the same `folders={...}` kwarg to its `progress_event(...)` call, with a comment: the Import page re-renders the folder table from each event, so a transfer event without the snapshot would blank the table for the whole batch transfer.

- [ ] **Step 4: Run to verify green**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "behavior or agree_on_plain or folder_snapshots or current_behavior or per_file_transfer_progress"`
Expected: ALL pass (parity restored: both paths now emit folders everywhere, including transfer sub-progress events).

- [ ] **Step 5: Sweep for stale pins and commit**

Run the remote-heavy selections most likely to inspect event payloads: `python -m pytest vireo/tests/test_import_job.py -q` (full file, ~30s). Fix any test asserting exact remote event-dict key sets (update with a spec-decision-1 comment).

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "Remote import progress events carry the per-folder snapshot (spec decision 1)"
```

### Task 2: Decision 2 — local dest-under-source refusal reports progress

**Files:**
- Modify: `vireo/import_job.py:3497-3506` (local batch guard)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Find the remote twin test**

`grep -n "resolves inside a source directory" vireo/tests/test_import_job.py` — read the remote-path test(s) asserting the batch refusal (from PR #1113) to mirror their assertion style. Also grep `vireo/import_job.py` to confirm the local guard still sits at ~3497 (`if _path_under_any_source(dest_folder):` with NO `emitted += 1` and NO `_emit`).

- [ ] **Step 2: Write the failing test**

```python
def test_local_import_dest_under_source_refusal_reports_progress(
        tmp_path, monkeypatch):
    """Spec decision 2: a batch refused because dest_folder resolves
    inside a source directory must advance ``emitted`` and emit the
    batch-summary phase, exactly like the remote guard
    (import_job.py:1676-1691). Historically the local guard did neither,
    freezing the progress bar at the last pre-refusal value while the
    whole batch quietly failed."""
    from import_job import ImportParams, run_import_job

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 10, 5, 0), "green"),
    ])
    runner = FakeRunner()
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    # Destination = the card itself: the %Y/%Y-%m-%d dest_folder resolves
    # under the source root, tripping the batch-level guard (the
    # /api/jobs/import-photos route refuses this shape up front, but the
    # job-level guard is the backstop this test pins).
    result = run_import_job(
        _make_job(), runner, db_path, db._active_workspace_id,
        ImportParams(sources=[str(card)], destination=str(card),
                     verify_by_hash=True))

    assert result["failed"] == 2, result
    assert result["safe_to_format"] is False, result
    events = [d for _, kind, d in runner.events if kind == "progress"]
    # The refusal advances the bar over the whole rejected batch...
    assert any(d["current"] == 2 and d["total"] == 2 for d in events), events
    # ...with the same batch-summary phase string the remote path emits.
    assert any(d["phase"].endswith("0 copied · 0 already present")
               for d in events), [d["phase"] for d in events]
```

- [ ] **Step 3: Run to verify red**

Run: `python -m pytest "vireo/tests/test_import_job.py::test_local_import_dest_under_source_refusal_reports_progress" -q`
Expected: FAIL on the `current == 2 and total == 2` assertion (only the "Discovering files" 0/0 emit exists). If it instead fails EARLIER (e.g. `failed != 2` — meaning the guard didn't trip), stop and re-read the guard geometry; do not proceed on a test that isn't red for the right reason.

- [ ] **Step 4: Implement**

Align `vireo/import_job.py:3497-3506` to the remote guard's shape (1676–1691): add `emitted += 1` inside the loop and the batch-summary `_emit` after it:

```python
        if _path_under_any_source(dest_folder):
            for source_file in batch:
                # Count these as emitted so the progress bar reflects the
                # rejected batch instead of freezing at the last copied
                # file. Mirrors the remote guard — spec decision 2.
                emitted += 1
                _fail(
                    rel, source_file,
                    "destination folder resolves inside a source directory "
                    "(dest_folder would be created under the card being "
                    "imported); formatting the card would erase the archive "
                    "copy",
                )
            _emit(
                f"{rel}: {_counts(rel)['copied']} copied · "
                f"{_counts(rel)['skipped_duplicate']} already present",
                emitted, queued,
            )
            continue
```

(The `_fail` reason string is unchanged — byte-identical to today's, which is also the remote path's.)

- [ ] **Step 5: Run to verify green + no collateral**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "dest_under_source or source_directory or behavior"`
Then the selection-parity block (`-k "selection"`) — `emitted`/`copy_totals` feed `_selection_observables`, so confirm nothing pinned the frozen behavior. Expected: all pass; if a test pinned the old freeze, update it with a decision-2 comment and list it in the PR body.

- [ ] **Step 6: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "Local dest-under-source batch refusal reports progress (spec decision 2)"
```

### Task 3: Decision 3 — missing-mount-root refusal emits "archive unavailable" on both paths

**Files:**
- Modify: `vireo/import_job.py:1710-1714` (remote guard's `_emit`)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Find the local twin test and any stale remote pins**

`grep -n "archive unavailable" vireo/tests/test_import_job.py vireo/import_job.py` — read the local missing-mount-root test to mirror its setup (how it makes `_missing_archive_mount_root` return a path — likely monkeypatching `pipeline_job._missing_archive_mount_root`; the remote wrapper `_missing_mount_root` (~1597) calls the same function, imported at run time, so the same monkeypatch works). Also grep for tests asserting the remote guard's CURRENT emit (`already present` in a mount-root context) — those pins must be updated, not fought.

- [ ] **Step 2: Write the failing test**

```python
def test_remote_import_missing_mount_root_emits_archive_unavailable(
        tmp_path, monkeypatch):
    """Spec decision 3: the missing-mount-root batch refusal must emit
    the specific ``"{rel}: archive unavailable"`` phase (the local
    path's honest signal) instead of the generic copied/present summary
    the remote path historically reused for this failure."""
    import pipeline_job

    ra = _remote_archive_for(tmp_path)
    calls = _remote_calls(ra)
    _install_fake_remote_rsync(monkeypatch, calls, verify=None)
    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    monkeypatch.setattr(
        pipeline_job, "_missing_archive_mount_root",
        lambda destination: "/Volumes/GoneShare")

    from import_job import ImportParams, run_import_job
    runner = FakeRunner()
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    result = run_import_job(
        _make_job(), runner, db_path, db._active_workspace_id,
        ImportParams(sources=[str(card)], destination=ra["mount_base"],
                     remote_target=ra, verify_by_hash=True))

    assert result["failed"] == 1, result
    assert "is not available" in result["unsafe_files"][0]["reason"]
    phases = [d["phase"] for _, kind, d in runner.events
              if kind == "progress"]
    assert any(p.endswith("archive unavailable") for p in phases), phases
    assert calls["rsync"] == []
```

(Adapt the monkeypatch mechanics to whatever the existing local test actually does — Step 1's read wins over this sketch; the assertions stay.)

- [ ] **Step 3: Run to verify red**

Run: `python -m pytest "vireo/tests/test_import_job.py::test_remote_import_missing_mount_root_emits_archive_unavailable" -q`
Expected: FAIL on the `archive unavailable` phase assertion, with the generic `"… copied · … already present"` string visible in the failure output (proving the guard fired and only the string differs).

- [ ] **Step 4: Implement**

Replace the remote guard's `_emit` at `vireo/import_job.py:1710-1714`:

```python
            _emit(
                f"{rel}: archive unavailable", emitted, queued,
            )
```

Add a one-line comment above it: `# Specific refusal phase — mirrors the local path; spec decision 3.`

- [ ] **Step 5: Run green + sweep**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "mount_root or archive_unavailable or missing"` then the full file. Update any stale pins found in Step 1 (decision-3 comment; list in PR body).

- [ ] **Step 6: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "Remote missing-mount-root refusal emits archive-unavailable phase (spec decision 3)"
```

### Task 4: Full verification + PR

- [ ] **Step 1:** `python -m pytest vireo/tests/test_import_job.py -q` (600000ms timeout) — all pass (1 known macOS case-insensitivity skip).
- [ ] **Step 2:** Required suite per CLAUDE.md: `python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -q` (600000ms; known env failure to ignore: `test_api_exiftool_status_reports_missing`).
- [ ] **Step 3:** Push and open the PR:

```bash
git push -u origin import-unify-pr2-progress-emit
gh pr create --base main --title "Import unification PR 2: progress/emit alignment (spec decisions 1-3)" --body "..."
```

Body must cover: link to spec + this plan; the three decisions with their user-visible effect (remote imports get the live folder table; rejected batches no longer freeze the progress bar; honest "archive unavailable" phase on both paths); first production-code PR of the project — production diff is small and confined to the two emit sites and two batch guards; the preflight mirrors at `app.py:18300–18550` are unaffected (no dup-walk changes); every pre-existing test whose pins were updated, each tied to its decision; exact test counts from Steps 1–2. End with the standard Claude Code attribution line.
