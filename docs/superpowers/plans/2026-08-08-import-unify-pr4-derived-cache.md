# Import Unification PR 4: Remote Derived-Cache Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the local import path's derived-cache invalidation (spec decision 6) to the remote path: `raw_companion_invalidations`, `pre_scan_hashes` capture + diff loop, and the `_sweep_untracked_previews_for_photos` call — so a remote import can no longer leave stale thumbnails/previews/working copies.

**Architecture:** Fourth PR of the import-path-unification project (spec: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md`). A 2026-08-08 reachability analysis (below) found the genuinely LIVE remote bug is the **companion geometry**: a JPEG landing beside a pre-cataloged RAW never invalidates the RAW's derived caches (scanner's `_pair_raw_jpeg_companions` only invalidates when an edit recipe transfers — `scanner.py:604-671`), so a stale RAW working copy survives and the deferred WC extraction skips the row. The direct-row diff loop is defense-in-depth on BOTH paths (both `scan()` calls pass `vireo_dir`, so `scanner.py:2525-2538` handles ordinary content changes) — ported for parity and legacy-row robustness, mirroring the local comment's own framing. Neither transport ever replaces bytes in place (local `copy_and_hash_verify` promotes no-overwrite; remote rsync runs `--ignore-existing` + suffix walk), so the geometry sets match.

**Tech Stack:** pytest; existing local tests at `vireo/tests/test_import_job.py:3661-3903` are the templates; remote harness (`_remote_archive_for`, `_install_fake_remote_rsync`).

---

## Context for a zero-context engineer

- Repo root: `/Users/julius/conductor/workspaces/vireo/nagoya`; branch `import-unify-pr4-derived-cache` (checked out, tracks origin/main at the PR-1433 merge `6553b214`). Run tests from repo root. Commit to this branch.
- `vireo/import_job.py`: remote function `_run_remote_import_job` (~1235-3080), local body of `run_import_job` (~3100-4750). You are porting FROM the local catalog block TO the remote one. Line numbers below verified at `6553b214`.
- **Local machinery being ported** (read all of it first):
  - `pre_scan_hashes` capture: 4177-4203 (comment + loop querying each landed path's pre-scan photo row hash).
  - `reclassified_landed_paths` (4243-4253) — local-only; remote does NOT need it for `landed` because remote rollbacks filter `landed` in place (`landed = [e for e in landed if e[0] != dest_path]` at 2627-2629, 2646-2648, 2727-2729, 2745-2747), so surviving `landed` is already the not-reclassified set.
  - `raw_companion_invalidations` (4255-4273 decl; `.add(companion["id"])` at 4354 in the companion accept branch).
  - Post-scan invalidation block 4426-4492: diff loop over `landed` (skip reclassified; skip paths with no pre-scan row; compare `pre_hash` vs copy-time hash; `_invalidate_derived_caches(db, params.vireo_dir, row["id"], thumb_cache_dir=params.thumb_cache_dir)`; collect `invalidated_photo_ids`), then the companion loop 4483-4492.
  - `db.conn.commit()` 4494, then sweep 4496-4504 (`_sweep_untracked_previews_for_photos(db, params.vireo_dir, invalidated_photo_ids)` — runs AFTER the commit, only when non-empty).
  - WC-override fill filtered by reclassified 4506-4532 — remote's fill (2892-2895) already gets this free via the filtered `landed`; nothing to port there.
- **Scanner helpers** (`vireo/scanner.py`): `_invalidate_derived_caches(db, vireo_dir, photo_id, thumb_cache_dir=None)` at 751-871 — deletes thumb/working/display/preview files, NULLs `thumb_path`/`working_copy_*`, deletes `preview_cache` rows for successfully-unlinked sizes; does NOT commit. `_sweep_untracked_previews_for_photos(db, vireo_dir, photo_ids)` at 874-936 — unlinks orphan `previews/{pid}_{size}.jpg` files with no `preview_cache` row; files only, no DB writes.
- **Remote landing sites** (all in `_run_remote_import_job`):
  - Batch-catalog guard 2488; `landed_paths`/`scan_files = landed_paths | set(adopted_paths.keys())` 2489-2494; `scan(...)` 2495-2514; scan-failure rollback 2515-2523.
  - Catalog-stamping loop 2544-2747 (direct-row branch 2552-2657; companion branch 2658-2738 with accept at 2731-2738; not-cataloged fallthrough 2739-2747).
  - Adopted-paths validation pass 2748-2889 (companion sub-branch `is_companion` 2791-2810; failure `continue`s at 2817-2825, 2866-2877, 2878-2888 which do NOT remove the key from `adopted_paths` — the one place remote needs reclassified-style tracking).
  - `db.conn.commit()` 2890; `wc_source_paths` fill 2892-2895.
- **Remote `landed` tuple** (5-tuple, decl 2263): `(dest_path, card_source, src_hash, src_size, src_mtime_ns)` — copy-time hash at `entry[2]`. Local's is a 6-tuple with hash at `entry[1]`. Remote adoptions live in `adopted_paths: {mount_path: (source_file, src_hash)}`, NOT in `landed` — so the pre-scan capture must iterate `scan_files`, not `landed`, or adoption coverage local has (adoptions are inside local `landed`) silently drops.
- **Comparison semantics**: port local's diff comparison EXACTLY (no `EMPTY_FILE_SHA256`/None normalization — local's 4464 doesn't normalize; the degenerate zero-byte spurious-invalidation is a shared harmless quirk both paths will carry into PR 5 identically). Note it in a comment.
- **Local test templates** (`vireo/tests/test_import_job.py`): `test_import_invalidates_derived_caches_on_content_change` (3661-3741; stale row + file deleted out-of-band, new bytes land at same path), `test_import_invalidates_derived_caches_when_pre_row_had_null_hash` (3744-3808), `test_import_invalidates_raw_caches_when_new_jpeg_pairs` (3811-3903; pre-cataloged NEF with seeded stale working copy, new JPEG lands and pairs). Remote harness: `_remote_archive_for` / `_remote_calls` / `_install_fake_remote_rsync` (~4245-4299). The behavior-case runners accept `params_kwargs={"vireo_dir": ..., "thumb_cache_dir": ...}` if convenient, but direct `run_import_job` calls mirroring the local tests' structure are fine and clearer here.
- The dedupe/collision walk is untouched → the app.py preflight mirrors are unaffected (say so in the PR body).

### Task 0: Branch sanity

- [ ] `git branch --show-current` → `import-unify-pr4-derived-cache`; `git log --oneline -1` → `6553b214` or newer. Baseline: `python -m pytest vireo/tests/test_import_job.py -q` → 214 passed, 1 skipped.

### Task 1: Companion invalidation (the live bug) — TDD

**Files:**
- Modify: `vireo/import_job.py` (remote: decl before 2544; adds at 2737 and in 2791-2810; invalidation loop + sweep between 2889 and 2892). Local: comment fix only (see Step 4).
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing test** — `test_remote_import_invalidates_raw_caches_when_new_jpeg_pairs`, a faithful remote mirror of the local test at 3811-3903. Read that test line by line first. Geometry: a real `DSC_0800.NEF` on the MOUNT (not a local archive), cataloged with a bogus `file_hash` and a seeded stale `working_copy_path` file under `vireo_dir/working/`; the card holds a new `DSC_0800.jpg` (same capture date so it lands in the NEF's folder); run through `run_import_job` with `remote_target=ra`, `verify_by_hash=True`, `vireo_dir` set, fake rsync installed. Mirror the local test's assertions: `copied == 1`, the RAW row's `companion_path == "DSC_0800.jpg"`, the stale working-copy bytes do not survive (`working_copy_path` NULLed and/or the seeded file gone — match the local test's exact checks). Keep fixture geometry IDENTICAL to the local test apart from mount-vs-archive (this file's history has a memory of mirror tests with divergent geometry hiding bugs).

- [ ] **Step 2: Verify red.** Run the single test. Expected: FAIL on the stale-working-copy assertion (the pairing happens — `companion_path` set — but the RAW's caches survive untouched, because scanner's pair-merge only invalidates on recipe transfer and the remote path has no `raw_companion_invalidations`). Any other failure shape: STOP, report.

- [ ] **Step 3: Implement the port.** In `_run_remote_import_job`:

(i) Before the stamping loop (~2544), declare, with local's comment adapted (and correct — see Step 4):

```python
            # RAW rows that gained a JPEG companion this batch. The
            # scan's pair-merge only invalidates the RAW's derived
            # caches when an edit recipe transfers, so a RAW whose
            # working copy / thumb / previews were rendered RAW-only
            # keeps serving them after pairing. Collect every such RAW
            # id (transferred AND adopted JPEGs — adoption only proves
            # the JPEG bytes pre-existed on the mount, not that the RAW
            # already carried companion_path) and invalidate below.
            # Mirrors the local path — spec decision 6.
            raw_companion_invalidations = set()
```

(ii) In the companion accept branch, next to `imported_photo_ids.add(companion["id"])` (2737): `raw_companion_invalidations.add(companion["id"])`.

(iii) In the adopted-paths `is_companion` accept flow (2791-2810): after the adopted companion row is accepted (where `imported_photo_ids.add(row_id)` runs for it at 2889-area — read the flow; the add must happen only for entries that survive validation), add `row_id` to `raw_companion_invalidations` when `is_companion` is true.

(iv) Between the validation pass and `db.conn.commit()` (2890): the companion invalidation loop, then commit, then sweep — mirroring local 4483-4504:

```python
            invalidated_photo_ids = set()
            if params.vireo_dir:
                from scanner import _invalidate_derived_caches
                for raw_id in raw_companion_invalidations:
                    _invalidate_derived_caches(
                        db, params.vireo_dir, raw_id,
                        thumb_cache_dir=params.thumb_cache_dir,
                    )
                    invalidated_photo_ids.add(raw_id)
            db.conn.commit()
            if invalidated_photo_ids:
                from scanner import _sweep_untracked_previews_for_photos
                _sweep_untracked_previews_for_photos(
                    db, params.vireo_dir, invalidated_photo_ids,
                )
```

(The existing bare `db.conn.commit()` at 2890 is REPLACED by this sequence; Task 2 extends the same block with the diff loop. `invalidated_photo_ids` is declared here so Task 2 only inserts the diff loop above the companion loop.)

- [ ] **Step 4: Fix the stale LOCAL comment** at 4268-4272: it says "Skip when the JPEG was adopted (`origin == "skipped_duplicate"`)" but the code (4331-4356) deliberately invalidates regardless of origin and says so. Rewrite the 4268-4272 comment to match the code (adoption only proves the JPEG bytes pre-existed, not that the RAW was already paired). One comment, no code change.

- [ ] **Step 5: Verify green + sweep.** The new test passes; `python -m pytest vireo/tests/test_import_job.py -q -k "invalidates or companion or pairs"` all pass; full file → 215 passed, 1 skipped.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "Remote import invalidates RAW derived caches when a new JPEG pairs (spec decision 6)"`

### Task 2: pre_scan_hashes capture + diff loop (defense-in-depth parity) — mirror tests

**Files:**
- Modify: `vireo/import_job.py` (remote: capture after 2494; failed-adopted tracking in the validation pass; diff loop inserted above Task 1's companion loop)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the two remote mirror tests** — `test_remote_import_invalidates_derived_caches_on_content_change` and `test_remote_import_invalidates_derived_caches_when_pre_row_had_null_hash`, faithful mirrors of the local tests at 3661-3741 and 3744-3808 (stale photo row whose file is GONE from the mount → the card lands new/real bytes at the same computed path). Identical geometry apart from mount-vs-archive; `skip_duplicates` setting must match each local template.

- [ ] **Step 2: Run them.** EXPECTED: likely GREEN already — the remote `scan()` call passes `vireo_dir` (2511), and scanner's own `content_identity_changed` invalidation (`scanner.py:2525-2538`) covers the direct-row geometry. If green: these are parity pins, keep them and proceed (the diff loop being ported is defense-in-depth, same as local's own framing at 4177-4189 — legacy rows and codepath changes). If RED: report the failure shape before implementing — it means scanner coverage differs remotely, which is a finding.

- [ ] **Step 3: Implement the capture + diff loop.**

(i) After `scan_files` (2494), before the scan `try:`, capture over **`scan_files`** (NOT `landed` — remote adoptions live outside `landed`, and local's capture covers adoptions because they're inside its `landed`):

```python
            # Pre-scan snapshot of any photo row already cataloged at a
            # path this batch will scan (landed AND adopted). Compared
            # after the scan to invalidate derived caches for rows whose
            # content identity changed. Defense-in-depth next to the
            # scanner's own content_identity_changed invalidation, for
            # rows/codepaths the scanner misses (legacy NULL-hash rows).
            # Mirrors the local path — spec decision 6.
            pre_scan_hashes = {}
            for sp in scan_files:
                row = db.conn.execute(
                    """SELECT p.id, p.file_hash FROM photos p
                       JOIN folders f ON f.id = p.folder_id
                       WHERE f.path = ? AND p.filename = ?""",
                    (os.path.dirname(sp), os.path.basename(sp)),
                ).fetchone()
                if row is not None:
                    pre_scan_hashes[sp] = row["file_hash"]
```

(ii) Failed-adopted tracking: the adopted-validation failure `continue`s (2817-2825, 2866-2877, 2878-2888) leave their key in `adopted_paths`. Add `failed_adopted_paths = set()` (declared next to Task 1's `raw_companion_invalidations`) and `failed_adopted_paths.add(ap)` at each of the three failure sites (read each; `ap` is the loop's mount-path variable). Local needs no analogue for `landed` (its rollbacks keep entries → it filters via `reclassified_landed_paths`; remote's `landed` self-filters) — this set is the remote's one reclassified-style structure, scoped to adoptions.

(iii) The diff loop, inserted ABOVE Task 1's companion loop (inside the same `if params.vireo_dir:`): iterate surviving `landed` entries (hash at `entry[2]`) AND surviving adoptions (`for ap, (adopt_source, a_hash) in adopted_paths.items()` skipping `failed_adopted_paths`; read the actual tuple shape at ~2178/2146 first). For each path: skip if not in `pre_scan_hashes`; compare `pre_scan_hashes[path]` vs the copy-time hash with local's EXACT semantics (no zero-byte normalization — add local's-parity comment noting the shared quirk); re-query the row id; `_invalidate_derived_caches(...)`; collect into `invalidated_photo_ids`. Mirror local 4441-4481's structure and comments.

- [ ] **Step 4: Green + full file.** The two mirror tests still pass; the Task 1 test still passes; full file → 217 passed, 1 skipped.

- [ ] **Step 5: Commit** — `"Remote import: pre-scan hash capture and derived-cache diff loop (spec decision 6)"`

### Task 3: Sweep coverage pair (closes a gap on BOTH paths)

Neither path currently tests `_sweep_untracked_previews_for_photos` from the import flow (an orphan `previews/{pid}_{size}.jpg` with NO `preview_cache` row survives `_invalidate_derived_caches`, which only deletes tracked sizes' files — the sweep exists for exactly these orphans).

- [ ] **Step 1:** Extend ONE existing local invalidation test (the content-change one at 3661) and its new remote mirror: seed an orphan preview file `vireo_dir/previews/{photo_id}_512.jpg` (no `preview_cache` row) alongside the tracked state, and assert after the import that the orphan file is gone. Read `_sweep_untracked_previews_for_photos` (scanner.py 874-936) first to match its filename convention exactly.
- [ ] **Step 2:** Run both; expected: both pass (local sweep wiring exists; remote gained it in Task 1). If the LOCAL one fails, that's a pre-existing local bug — STOP and report before touching production.
- [ ] **Step 3:** Full file → 217 passed, 1 skipped. Commit — `"Test the untracked-preview sweep from both import paths"`

### Task 4: Full verification + PR

- [ ] **Step 1:** Full file (timeout 600000) → 217 passed, 1 skipped.
- [ ] **Step 2:** Required suite (CLAUDE.md list, timeout 600000; known env failure `test_api_exiftool_status_reports_missing` ignored).
- [ ] **Step 3:** Push; `gh pr create --base main --title "Import unification PR 4: remote derived-cache invalidation (spec decision 6)" --body "<BODY>"`. Body: spec/plan links; the live bug (remote JPEG-pairs-RAW left stale RAW caches; why scanner's pair-merge doesn't cover it) with the red test as evidence; the defense-in-depth diff loop with its capture-over-scan_files subtlety and the failed-adopted set; the sweep test pair; the local comment fix; the shared zero-byte comparison quirk noted for PR 5; preflight mirrors unaffected; exact counts. End with the Claude Code attribution line.
