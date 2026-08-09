# Import Unification PR 6: Phase Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract every now-identical phase of the two import functions into shared module-level functions (six suite-green commits, least-risky first), shrinking both functions to their transport cores — plus record two newly-found divergences in the spec.

**Architecture:** A fresh side-by-side phase map (2026-08-08, embedded below per task) verified — by comment-stripped diff — that after PRs 2-5b the following are byte-identical or one-token-parameterizable: destination context + predicates, `_stop_requested`, `_emit` (the transfer-keys clear is a proven no-op locally), discovery→selection→preflight→batching (78-line empty diff), all six batch-loop guards (three narration-string deltas), the duplicate gate, the post-loop rollback (modulo the remote `to_transfer` hook), the catalog scan/pre-scan/diff-loop/sweep/WC-fill, twin linking, WC extraction, and the entire finalize tail (one `remote_unverified` boolean). Deliberately NOT extracted here: the collision/adopt walk and transfer phase (PR 7's transport cores) and the **stamping loop** — it hides a genuine behavioral divergence (D4: local-only `file_hash` backfill) plus a zero-byte normalization-convention split (D2/D3), so it gets its own align-then-extract PR after a spec decision. This plan records D4 and the local-only per-file dest-under-source guard (L3963-4007, no remote counterpart) in the spec.

**Tech Stack:** dataclasses; pytest; the `_BEHAVIOR_PARITY_SCENARIOS` matrix as the net.

---

## Context for a zero-context engineer

- Repo root: `/Users/julius/conductor/workspaces/vireo/nagoya`; branch `import-unify-pr6-phase-extraction` (tracks origin/main at the PR-5b merge `a4463005`). Run tests from repo root; commit per task. Baseline: `python -m pytest vireo/tests/test_import_job.py -q` → **225 passed, 1 skipped** — must hold after EVERY task.
- `vireo/import_job.py` (4856 lines): remote `_run_remote_import_job` def 1356 (body 1376-3212); local `run_import_job` def 3215 (body 3222-4856). `_ImportRunState` at ~592; hoisted helpers `_counts`/`_fail`/`_reclassify_landed_failed`/`_record_checker` at ~622-694. Line refs below verified at `a4463005`; re-grep before editing.
- **Extraction style:** each shared function is module-level, takes `state` (and later `batch_st`) plus explicit deps; the two function bodies replace the extracted block with one call. MOVE comments with their code verbatim — the file's comments are an incident ledger (PR numbers, Codex review ids); a reviewer checklist item on every PR in this project is "no rationale comment dropped". Where the two paths' comments differ, keep the more accurate one and note the merge in the commit message.
- **DB-setup asymmetry is correct layering, not drift:** `run_import_job` is the entry point (constructs `Database`, sets workspace, dispatches at 3234-3240); the remote fn receives `db`. Keep. Likewise keep the function-level `from pipeline_job import ...` (breaks an import cycle — do NOT hoist to module scope; extracted functions may do their own function-level import or receive the symbols).
- **The per-file-loop control-flow hazard (Task 6):** the duplicate gate block contains 6 `continue` / 2 `break` in total, of which 4 continues and exactly 1 break target the ENCLOSING per-file loop (the rest belong to the inner twin loop, which stays inside the extracted function); the one enclosing break is the cancellation exit. Extraction returns a verdict enum; the CALLER maps it — and mapping the post-`DestReadCancelled` `break` to `continue` would silently reintroduce the wedged-mount pin PR #1423 fixed. The parity matrix + the dest-read-cancel pins are the net; run them by name in that task.
- Preflight mirrors in app.py: unaffected (no walk changes).

### Task 0: Sanity
- [ ] Branch check; baseline 225 passed, 1 skipped.

### Task 1: Spec amendments (docs-only)
- [ ] In `docs/superpowers/specs/2026-08-06-import-path-unification-design.md`:
  1. Add decision-table row 11: *Local stamping loop backfills `file_hash` on scan-NULL rows (`update_photo_hash_check(..., "ok", file_hash=verified_hash)`, ~L4511-4514 — NOT the non-backfilling stamp at L4480-4482 just above it); remote stamps `"ok"` without backfilling — found by the 2026-08-08 extraction phase map (D4).* Decision cell: **Deferred to the stamping align-then-extract PR (6b): adopt local (backfill on both paths) unless review finds a reason the remote's NULL is load-bearing; until then the stamping loops stay per-function.** Also note D2/D3 there (zero-byte normalization convention split: remote normalizes `EMPTY_FILE_SHA256`→None with a `read_failed` flag; local compares raw and its `verified_hash` is never None — unify by normalizing at `_LandedFile` construction in 6b).
  2. Add to the PR-7 notes (or a "residual asymmetries" list under the PR sequence): the local-only per-file `same_file`/dest-under-source guard (L3963-4007) has no remote counterpart; PR 7 must decide keep-both/port/drop deliberately.
  3. Update the PR-sequence PR-6 entry: extraction lands as this PR; the stamping loop moves to a new "PR 6b — stamping alignment + extraction (behavior PR: D4, D2/D3)".
- [ ] Commit: `"Spec: divergence 11 (file_hash backfill), zero-byte convention split, residual per-file guard asymmetry; PR 6b carved out"`

### Task 2: Pure-identical hoists, no parameters (map commit 1)
- [ ] Extract, delete-and-call, verbatim (all verified byte-identical by stripped diff):
  - `_make_stop_check(runner, job)` from R1645-1656 / L3476-3487 (or inline the 1-line lambda at both `_hash_dest_file`-adjacent call sites — implementer's choice; keep the docstring either way, it cites the pause-aware distinction).
  - `_build_destination_context(db, params)` → returns a small `_DestContext` (namedtuple or frozen dataclass): `destination`, `mount_baseline`, `path_under_any_source`, `path_under_destination`, `fold_basename` — from R1390-1452 / L3257-3338 (normalize → baseline → guard → probe/predicates → fold). ORDERING IS LOAD-BEARING (comments R1394-1407/L3263-3285: baseline captured after normalization, before discovery — one function preserves it by construction). `fold_basename` is unused on local — free and harmless.
  - Delete the remote `_missing_mount_root` wrapper (R1719-1720); both paths call `_missing_archive_mount_root(context.destination)` directly (the symbol comes from the function-level pipeline_job import — pass it or re-import; keep it working on both).
- [ ] Full file → 225/1. Commit: `"Extract destination context and stop-check; drop the mount-root wrapper"`

### Task 3: Emitter + plan (map commit 2)
- [ ] `_make_emitter(job, runner, state, eta)` → returns `_emit`, from R1483-1516 / L3355-3395: ONE body clearing `_IMPORT_ETA_PROGRESS_KEYS + _IMPORT_TRANSFER_PROGRESS_KEYS` unconditionally. The transfer-keys clear is a PROVEN no-op on local: the keys are only ever written by the remote-only `_emit_transfer` (import_job.py:1534-1536) and never seeded by JobRunner (jobs.py:552/626/1042); push_event mirroring can't introduce them locally since local events never carry them. Put that proof in a comment. `_emit_transfer` stays nested in the remote fn (transport core).
- [ ] `_plan_import(db, params, emit, state)` → `_ImportPlan` namedtuple `(files, discovered, source_snapshots, include_paths, queued, deselected, vanished_paths, appeared, checker, timestamps, batches)` from R1566-1643 / L3397-3474 (78-line empty diff). `_discovery_onerror` becomes nested inside it (touches only `state.discovery_errors` + log). ORDERING: `_capture_source_snapshots` before `_apply_selection` (retry-signature comments R1579-1594/L3410-3417 — move them).
- [ ] Full file → 225/1. Commit: `"Extract the shared emitter and import planning phase"`

### Task 4: Finalize (map commit 3)
- [ ] `_finalize_import(job, runner, db, state, params, *, discovered, include_paths, source_snapshots, deselected, vanished_paths, appeared, remote_unverified=False) -> dict` from R3078-3212 / L4705-4856: status/summary/update_step, discovery-error unsafe entries, partial_scope, the `remote_unverified` append (R3118-3123, runs only when the flag is True), unverified_duplicate entry, `_append_selection_unsafe`, `safe_to_format` (+`and not remote_unverified`), `unverified_duplicates_only` (same), result dict, return. Local calls with the default; remote passes `remote_unverified=not params.verify_by_hash` — READ the current remote computation at ~3118 first and reproduce exactly (it also checks `discovered > 0` for the unsafe append — keep the exact predicate).
- [ ] Full file → 225/1. Commit: `"Extract the shared import finalizer"`

### Task 5: Narration-string alignment (map commit 4 — strings only, ~10 lines)
- [ ] Unify the three remaining reason-string deltas (all pure narration; emits/counters identical since PR 2):
  1. Sticky-guard reason (R1751-1752 vs L3575-3576): adopt the REMOTE wording ("archive claim" covers adoptions).
  2. Stale-mount reason (R1836-1837 vs L3673-3674): adopt the LOCAL wording (names the actual failure mode).
  3. Per-file mount-lost reason (R1963-1965 vs L3781-3783): merge: "detached while this batch was in progress (the directory persists but the share is gone, so neither further writes nor a duplicate match against it can be trusted)". Grep tests for all six old strings first — no assertion pins exist (verified: the mount-detach pins assert "local shadow"/"detached before", untouched here), but TWO test COMMENTS quote the old per-file wordings (test lines ~11584, ~11658) — update them to the merged wording.
- [ ] Full file → 225/1. Commit: `"Align the three remaining batch-guard narration strings"`

### Task 6: `_ImportBatchState` + guards + rollback (map commit 5)
- [ ] Add module-level `_ImportBatchState` (after `_ImportRunState`): fields `rel`, `dest_folder` (set post-preflight), `landed: list`, `dup_dirs: set`, `dup_skips: list`, `reclassified_landed_paths: set`, `mount_lost: str | None = None`, `dest_read_cancelled: bool = False`, plus remote-only `to_transfer: list`, `claimed_basenames: dict`, `queued_src_hashes: dict` (default factories; comment the remote-only trio as unused-on-local until PR 7). Convert both per-batch blocks to one `batch_st = _ImportBatchState(rel=rel, dest_folder="")`-style construction (mechanical rename, same discipline as 5b — the symtable/grep audit applies to the nine names within the batch loops).
- [ ] Extract `_batch_preflight(state, emit, db, *, rel, batch, queued, ctx, missing_root_check) -> str | None` (returns `dest_folder` or None→caller `continue`s) from R1743-1880 / L3567-3721: sticky refusal → dest-under-source → missing-mount-root → stale-mount → makedirs → folder-status promotion, IN THAT ORDER (the 2026-07-30 incident ordering — extract as ONE function, never five; move the incident comments). Remote's `ssh_dest` compute stays behind in the remote fn (transport).
- [ ] Extract `_rollback_on_mount_loss(state, batch_st, rel, verified_counted_for_copies, extra_rollback=None)` from R2314-2369 / L4196-4247: final probe stays at the call site; the function does dup_skips rollback → `extra_rollback()` if provided (remote passes the `to_transfer` closure) → landed rollback via `_reclassify_landed_failed` → `state.mount_ever_lost = batch_st.mount_lost` LAST. Ordering comment required.
- [ ] Full file → 225/1; also `-k "mount_detach or dest_read_cancel or behavior"` and report. Commit: `"Batch state object; extract batch preflight and mount-loss rollback"`

### Task 7: Duplicate gate + catalog tail (map commit 6)
- [ ] `_duplicate_gate(state, batch_st, *, source_file, rel, checker, db, params, ctx, stop_requested) -> verdict` from R1973-2118 / L3793-3961 (verified identical; 4 non-semantic hunks). Verdict: a tiny enum or module constants `_GATE_SKIPPED` (caller `continue`s), `_GATE_PROCEED`, `_GATE_CANCELLED` (caller `break`s — BOTH `DestReadCancelled` exits map here; see the control-flow hazard in Context). Both callers' mappings must be structurally identical — write them as the same three-line block.
- [ ] Extract the catalog-tail quartet (all verified identical): `_catalog_scan_and_prescan(state, batch_st, db, params, scan, destination, dest_folder, rel)` (gate + landed_paths + pre-scan capture + scan call + scan-failure rollback + `_invalidate_new_images`) from R2602-2658 / L4265-4332; `_invalidate_changed_and_sweep(state, batch_st, db, params, raw_companion_invalidations)` (diff loop + companion invalidation + commit + sweep) from R2936-3011 / L4548-4613; `_fill_wc_overrides(state, batch_st, params, dest_folder)` from R3012-3033 / L4622-4648 — NOTE: these three blocks are SEMANTICALLY identical but differ in loop shape (remote iterates `landed_paths`/builds a `changed_candidates` comprehension; local iterates `landed` with alias variables) — adopt the LOCAL form in the shared function and say so in the commit message; the parity matrix is the check. Twin-linking and WC-extract ARE byte-identical; `_link_twins_and_emit(state, batch_st, db, workspace_id, emit, rel, queued)` from R3034-3057 / L4649-4673 + the deferred `_extract_working_copies` block R3059-3077 / L4674-4704 as `_extract_deferred_working_copies(state, params, runner, job)`. The STAMPING LOOP (R2687-2935 / L4352-4546, incl. local `_rehash_dest_or_none`) STAYS IN EACH FUNCTION — it is PR 6b's align-then-extract; leave a one-line comment at each pointing at spec decision 11.
- [ ] Full file → 225/1; run the FULL parity matrix + pins by name: `-k "behavior or agree_on_plain or adoption or renamed_twin or zero_byte or dest_read_cancel or mount_detach or pairs"` — report exact count. Commit: `"Extract the duplicate gate and catalog tail; stamping loops stay for PR 6b"`

### Task 8: Audit + verification + PR
- [ ] Function-size accounting for the PR body: `wc -l` equivalents of both function bodies before (1836/1634 lines) and after; the diff `--stat` net.
- [ ] Symtable/grep audit for the batch-state names (same discipline as 5b; recursive variant).
- [ ] Full file (225/1) + required CLAUDE.md suite (2077/14/1 known env failure).
- [ ] Push; PR: title `"Import unification PR 6: extract the shared import phases"`, base main. Body: spec/plan links; the fresh-map methodology (stripped-diff verification); the extraction inventory with line counts; the proven-no-op transfer-keys note; the three narration alignments; what deliberately STAYS (stamping loops → PR 6b with divergence 11; collision/adopt walk + transfer → PR 7; local-only per-file guard recorded for PR 7); suite counts identical at every commit; the audit artifacts. End with the Claude Code attribution line.

## As built (erratum, 2026-08-09)

Two deltas between this plan and what merged in PR #1444; the merged code is
the source of truth.

- **Seven commits, not six.** The goal line says "six suite-green commits";
  seven production commits landed — the Task 6 batch-state/preflight/rollback
  work split into two commits (narration alignment landed separately from the
  batch-state extraction) so each stayed individually reviewable.
- **As-built signatures differ from the Task 6 sketch:**
  - `_rollback_on_mount_loss` takes no `rel` parameter — it reads
    `batch_st.rel` instead.
  - `_batch_preflight` returns `dest_folder` (or `None` → caller `continue`s)
    and takes a `missing_root_check` parameter.
