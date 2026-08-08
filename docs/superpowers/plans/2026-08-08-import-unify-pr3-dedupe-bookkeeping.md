# Import Unification PR 3: Dedupe/Bookkeeping Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land spec decisions 5 and 7 (remove the proven-no-op remote `_record_checker(source_file)` accept-branch call; capture working-copy source identity before transfer on the remote path), and resolve decision 8 as a documented no-change (its premise failed code verification).

**Architecture:** Third PR of the import-path-unification project (spec: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md`). Two production changes in `vireo/import_job.py`'s remote function, one spec amendment. Decision 5 is removal-with-tripwire: PR 1's characterization pair proved the call behaviorally dead, so the existing tests must KEEP passing. Decision 7 is TDD with a new working-copy-identity regression test. Decision 8 is a spec correction only: `DuplicateChecker.content_hash` memoizes (`vireo/import_dedup.py:319-327`, `self._hashes`), so with a checker the local copy-site call is a cache hit or a read `copy_and_hash_verify` would otherwise perform itself; with no checker it is read-neutral (`copy_and_hash_verify` does its own standalone `compute_file_hash(src)` when `src_hash is None` — `import_job.py:617-620` — so switching to `_src_hash_cached()` merely moves the read, with a marginal theoretical saving only on the rare collision-walk path that PR 5's shared closure captures anyway). No behavior or I/O bug exists to fix.

**Tech Stack:** pytest; PR 1/2 harness in `vireo/tests/test_import_job.py`.

---

## Context for a zero-context engineer

- Repo root: `/Users/julius/conductor/workspaces/vireo/nagoya`; branch `import-unify-pr3-dedupe-bookkeeping` (checked out, tracks origin/main at the PR 2 merge `e9f82e92`). Commit to it; run tests from repo root.
- The dedupe/collision walk's LOGIC is untouched by this PR — only a dead registration call is removed and stat timing moves. Still, per the spec's PR 2/3 checklist: the hand-mirrored preflight endpoints (`app.py:18300–18550`) mirror the collision WALK, not `_record_checker` or WC identity capture — no sync needed; say so in the PR body.
- Key production sites (verified 2026-08-08, post-PR-2 line numbers):
  - Decision 5 target: `vireo/import_job.py:2031` — `_record_checker(source_file)` in the remote duplicate-accept branch. The helper `_record_checker(source_file, dest_folder=None, file_hash=None)` is defined ~1620s (search `def _record_checker` in the remote body); its other call sites pass all three args: intra-batch different-basename dedup skip (2076), intra-batch same-basename collision skip (2105), adoption (2147), post-transfer (2452). The LOCAL helper (~3440s) takes all three args required and assigns unconditionally.
  - Decision 7 targets: eager `src_hash` computation at 2043-2051 (the natural stat point); `to_transfer.append((source_file, dest_basename, src_hash))` at 2170; the post-transfer `sf.stat()` at 2442-2446 feeding `landed.append((dest_path, str(sf), src_hash, sz, mt))` at 2451; `wc_source_paths[dest_path] = (sf, sz, mt)` fill at 2877-2878.
- PR 1 pins that MUST keep passing unchanged in Task 2: `test_local_renamed_twin_of_accepted_duplicate_current_behavior`, `test_remote_renamed_twin_of_accepted_duplicate_current_behavior`, the `renamed_twin_skip` parity scenario, and the behavior-parity suite.
- Working-copy background for Task 3: when `params.vireo_dir` is set, after each batch the job records `wc_source_paths[dest_path] = (source_path, size, mtime_ns)` and later calls `scanner._extract_working_copies(..., source_paths=wc_source_paths, ...)`. The `(size, mtime_ns)` pair is an identity attestation of the SOURCE file. Local captures it BEFORE the copy; remote captures it AFTER the transfer — so a source that changes mid-transfer looks "clean" remotely. Decision 7 adopts the local timing. Existing WC tests live in the "working copies" section (~test line 1230s) — read one to see how `_extract_working_copies` is spied.

### Task 0: Branch sanity

- [ ] `git branch --show-current` → `import-unify-pr3-dedupe-bookkeeping`; `git log --oneline -1` → `e9f82e92` or newer. Baseline: `python -m pytest vireo/tests/test_import_job.py -q -k "behavior or renamed_twin or agree_on_plain"` → expect 8 passed.

### Task 1: Decision 8 — spec correction, no production change

**Files:**
- Modify: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md` (decision table row 8; PR-sequence PR 3 entry)

- [ ] **Step 1: Verify the premise failure yourself** (do not take this plan's word): read `vireo/import_dedup.py:319-327` (`content_hash` memoizes per path in `self._hashes`), the local copy-site line `src_hash = checker.content_hash(source_file) if checker else None` (search it in the local body, ~4030), and `copy_and_hash_verify` (~574-745, especially the `src_hash is None` branch at ~617-620, which does its own standalone `compute_file_hash(src)`). Confirm: (a) with a checker, any earlier hash computation (duplicate gate fallback, collision walk via `_src_hash_cached`) already populated `self._hashes`, so the copy-site call re-reads nothing — and when nothing computed it earlier, the read it performs is one `copy_and_hash_verify` would otherwise do itself; (b) with no checker, swapping in `_src_hash_cached()` is read-neutral on fresh copies (the standalone read just moves from `copy_and_hash_verify` to the closure), with a saving only when the collision walk already evaluated the closure — a rare path.

- [ ] **Step 2: Amend the spec.** In the decision table, replace row 8's decision cell with:

> **No change — premise disproven (2026-08-08, PR 3).** `DuplicateChecker.content_hash` memoizes per source path (`import_dedup.py:319-327`), so with a checker the copy-site call is a cache hit whenever a hash was computed earlier in the run — and otherwise performs a read `copy_and_hash_verify` would do itself anyway (its `src_hash is None` branch runs a standalone `compute_file_hash(src)`). With no checker, reusing `_src_hash_cached()` is read-neutral: the standalone read merely moves, with a marginal saving only on the rare collision-walk path. No redundant I/O exists; the call-site duplication itself dissolves in PR 5's shared cached-hash closure.

Also update the PR-sequence "PR 3" entry: "Decisions 5, 7, 8" → "Decisions 5 and 7 (8 resolved as a documented no-change — see the decision table)".

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-06-import-path-unification-design.md
git commit -m "Spec: decision 8 premise disproven (content_hash memoizes) - resolved as no-change"
```

### Task 2: Decision 5 — remove the accept-branch `_record_checker(source_file)` call; align the helper

**Files:**
- Modify: `vireo/import_job.py:2031` (the call), `~1620s` (the `_record_checker` helper)
- Test: `vireo/tests/test_import_job.py` (docstring updates only)

This is removal-with-tripwire, not TDD: PR 1's Task 6 proved the call behaviorally unobservable in cataloged-twin geometry (both verify modes — `CatalogIndex.known_hashes` covers the renamed twin) and pinned both paths. The existing tests must keep passing UNCHANGED after the removal; if any test fails, the no-op proof was wrong — STOP, do not adjust assertions, report with the failure.

- [ ] **Step 1: Remove the call.** Delete line 2031 (`_record_checker(source_file)`) in the remote duplicate-accept branch. Update the accept-branch's surrounding comments if any references the registration (read the block ~1990-2032; the comments there discuss `dup_dirs`/`run_dest_folders`, which stay).

- [ ] **Step 2: Align the helper to the local shape.** With the source-only caller gone, every remaining `_record_checker` call site (2076, 2105, 2147, 2452 — re-grep to confirm exactly four) passes all three args. Change the remote helper to match the local one: signature `(source_file, dest_folder, file_hash)` (no defaults), unconditional `run_dest_folders[tok] = dest_folder` / `run_verified_hashes[tok] = file_hash` assignments (drop the `if ... is not None` guards). Update its docstring: remove/adjust any text describing the optional-args form, keep the PR #1113 OSError rationale, and note the shape now matches the local path's helper (spec decision 5, toward PR 5's single helper).

- [ ] **Step 3: Update the tripwire docstrings.** In `test_remote_renamed_twin_of_accepted_duplicate_current_behavior` (and the local twin if it references the call), update the docstring from "a later PR removes that call, and this test is the tripwire" to past tense: the call was removed in PR 3; this test pinned the removal as a no-op. Do NOT change any assertion.

- [ ] **Step 4: Verify the no-op proof holds**

Run: `python -m pytest vireo/tests/test_import_job.py -q -k "renamed_twin or behavior or agree_on_plain"`
Expected: 8 passed with zero assertion changes. Then the remote-heavy sweep: `python -m pytest vireo/tests/test_import_job.py -q` (timeout 600000) — expect 213 passed, 1 skipped, same as the PR 2 baseline.

- [ ] **Step 5: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "Remove the no-op remote _record_checker accept-branch call (spec decision 5)"
```

### Task 3: Decision 7 — capture WC source identity before transfer (remote)

**Files:**
- Modify: `vireo/import_job.py` (per-file loop ~2043; `to_transfer`/`transferred` tuple threading ~2170-2452)
- Test: `vireo/tests/test_import_job.py` (one new test near the working-copies section)

- [ ] **Step 1: Write the failing test.** No remote test passes `vireo_dir` today — this will be the FIRST to exercise the remote WC-extraction gate under the fake-remote harness. Read the LOCAL working-copy tests (~test lines 1780-1816) for the `_extract_working_copies` spy pattern and `vireo_dir` wiring (a `*args, **kwargs` spy style is fine), then adapt:

```python
def test_remote_import_wc_identity_captured_before_transfer(
        tmp_path, monkeypatch):
    """Spec decision 7: the working-copy identity tuple ``(size,
    mtime_ns)`` must attest the SOURCE at decision time. The remote path
    historically stat'd the source AFTER the transfer, so a source that
    changed mid-transfer (card glitch, live folder) still looked clean
    to the working-copy identity check. Mirrors the local path, which
    stats before the copy."""
    import move as _move
    import scanner as _scanner

    ra = _remote_archive_for(tmp_path)
    calls = _remote_calls(ra)
    _install_fake_remote_rsync(monkeypatch, calls, verify=None)
    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    src = card / "DSC_0001.jpg"
    pre_size = src.stat().st_size
    pre_mtime_ns = src.stat().st_mtime_ns

    base_fake = _move._run_rsync_streamed  # the harness fake

    def mutating_rsync(*args, **kw):
        rc = base_fake(*args, **kw)
        # The source changes while/just after the batch is on the wire:
        # append a byte and bump mtime. Decision-time capture must not
        # see this.
        with open(src, "ab") as fh:
            fh.write(b"x")
        os.utime(src, ns=(pre_mtime_ns + 5_000_000_000,
                          pre_mtime_ns + 5_000_000_000))
        return rc

    monkeypatch.setattr(_move, "_run_rsync_streamed", mutating_rsync)

    captured = {}

    def spy_extract(db_arg, vireo_dir, scope, *, source_paths=None, **kw):
        captured["source_paths"] = dict(source_paths or {})
        return None

    monkeypatch.setattr(_scanner, "_extract_working_copies", spy_extract)

    from import_job import ImportParams, run_import_job
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    result = run_import_job(
        _make_job(), FakeRunner(), db_path, db._active_workspace_id,
        ImportParams(sources=[str(card)], destination=ra["mount_base"],
                     remote_target=ra, verify_by_hash=True,
                     vireo_dir=str(tmp_path / "vdir")))

    assert result["copied"] == 1, result
    assert captured, "working-copy extraction never ran"
    [(dest_path, (sf, sz, mt))] = captured["source_paths"].items()
    assert sf == str(src)
    # Identity attests the source BEFORE the mid-transfer mutation.
    assert (sz, mt) == (pre_size, pre_mtime_ns), (sz, mt)
```

ADAPT the spy signature to `_extract_working_copies`' real one (read it in `vireo/scanner.py` — keyword names must match the call in import_job's WC-extraction block, search `_extract_working_copies` there; the call passes `scope=`, `source_paths=`, `cancel_check=`, possibly `thumb_cache_dir=`). Note the transfer VERIFY step: with `verify_by_hash=True`, `remote_verify_files` is the harness fake (returns None = verified) and runs before the stat — the mutation still lands before the current post-transfer stat because the fake rsync mutates synchronously. If red-phase shows `copied == 0` (verification failing on the mutated source — it shouldn't; the fake ignores bytes), read the failure before adjusting anything.

- [ ] **Step 2: Verify red.** Run the single test. Expected: FAIL on the `(sz, mt) == (pre_size, pre_mtime_ns)` assertion, showing the POST-mutation size/mtime (pre_size+1, pre_mtime_ns+5s) — proving today's capture happens after transfer. Any other failure: STOP and report.

- [ ] **Step 3: Implement.** In the remote per-file loop:

(i) Stat at decision time, right after the eager `src_hash` block (2043-2051), inside the same try/except shape the local path uses (stat failure fails the file — matching local, where a source that can't be stat'd before the copy is failed):

```python
            try:
                st = source_file.stat()
                src_size, src_mtime_ns = st.st_size, st.st_mtime_ns
            except OSError as e:
                _fail(rel, source_file, str(e))
                continue
```

Add a comment: captured at decision time, before any bytes move, so a source that changes mid-transfer cannot look clean to the working-copy identity check — mirrors the local path; spec decision 7.

(ii) Thread the pair through the queue. `to_transfer` entries become 5-tuples `(source_file, dest_basename, src_hash, src_size, src_mtime_ns)`; `transferred` entries become 6-tuples `(sf, bn, sh, sz, mt, nas_full)`. Grep EVERY unpack/append of `to_transfer`, `flat`, `renamed`, `transferred` in the remote function and update each — expected sites (re-grep; line numbers drift):
  - `to_transfer.append(...)` (2170)
  - the mount-lost rollback loop over `to_transfer` (~2230s, reason "detached before this file was transferred")
  - the remote-mkdir-failure loop `for sf, _bn, _sh in to_transfer:` (~2301)
  - the `flat` / `renamed` split comprehensions (~2310s)
  - the flat rsync src-list comprehension `[str(sf) for sf, _bn, _sh in flat]` (~2365)
  - the flat `timed_out` and `rc != 0` failure loops over `flat` (~2371, ~2376)
  - the flat-success `transferred.append(...)` and the renamed loop's unpack + `transferred.append(...)`
  - the cancel-gate `if to_transfer and not cancelled:` (no unpack — unchanged)
  - the verify/accounting loop `for sf, bn, src_hash, nas_full in transferred:` (~2407) → add `sz, mt`
(iii) Delete the post-transfer stat block (2442-2446) and use the threaded `sz, mt` in `landed.append((dest_path, str(sf), src_hash, sz, mt))`. The `landed` tuple shape and the `wc_source_paths` fill (2877-2878) are unchanged.

- [ ] **Step 4: Verify green + sweeps.** The new test passes. Then: `python -m pytest vireo/tests/test_import_job.py -q -k "working_cop or wc_ or transfer or stop or renamed"` (the renamed-transfer stop test and transfer-progress tests unpack nothing from these tuples, but they exercise every changed loop). Then the full file (timeout 600000): expect 214 passed, 1 skipped. Any test that pinned `(None, None)` WC identity after a stat race would surface here — read before touching.

- [ ] **Step 5: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "Remote WC identity captured at decision time, before transfer (spec decision 7)"
```

### Task 4: Full verification + PR

- [ ] **Step 1:** Full file: `python -m pytest vireo/tests/test_import_job.py -q` → 214 passed, 1 skipped.
- [ ] **Step 2:** Required suite (CLAUDE.md list, timeout 600000; known env failure `test_api_exiftool_status_reports_missing` ignored).
- [ ] **Step 3:** Push + PR:

```bash
git push -u origin import-unify-pr3-dedupe-bookkeeping
gh pr create --base main --title "Import unification PR 3: dedupe/bookkeeping alignment (spec decisions 5, 7; 8 resolved no-change)" --body "<BODY>"
```

Body covers: spec/plan links; decision 5 = removal of a call PR 1 empirically proved dead (tripwire tests kept passing unchanged; helper signature aligned to local's); decision 7 = WC identity stat moved from post-transfer to decision time with the pair threaded through the transfer queue (user-visible effect: a source file that changes mid-transfer now fails the working-copy identity check instead of silently attesting mutated bytes; stat-failure-at-decision-time now fails the file, matching local); decision 8 = spec corrected, no production change, with the memoization evidence; preflight mirrors unaffected (no walk changes); exact test counts. End with the Claude Code attribution line.
