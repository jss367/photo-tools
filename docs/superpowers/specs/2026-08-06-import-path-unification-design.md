# Import path unification — design

**Date:** 2026-08-06
**Status:** Spec-review approved (two passes); amended 2026-08-06 after
external review; awaiting maintainer sign-off before implementation planning
**Scope:** `vireo/import_job.py` (4,700 lines) — merge the hand-mirrored local and
remote import implementations into one orchestrator with a pluggable transport.

## Problem

`vireo/import_job.py` implements the import flow twice:

- `_run_remote_import_job` (lines 1235–3041) — card → NAS over rsync/SSH
- `run_import_job` (lines 3044–4700) — card → local/mounted destination via
  `copy_and_hash_verify`; also the public entry point that dispatches to the
  remote path when `params.remote_target` is set

The fork was deliberate when SSH archives were added (Task 2.7): the remote path
was kept "in a separate function so the local copy path stays byte-for-byte
unchanged." Since then, every import fix has had to be applied twice by hand,
and at least one production regression (the 2026-07-31 duplicate-only rescan
over SMB) shipped because a fix landed on only one path while the mirror test
used different fixture geometry and passed anyway.

A line-by-line phase map (2026-08-06) found:

- ~70% of the two functions is identical or cosmetically different
  (setup, discovery, selection, duplicate preflight, batching, batch guards,
  twin linking, working-copy extraction, finalize/summary).
- The genuine transport divergence is concentrated in three places:
  1. **Collision/basename resolution** — rsync lands files flat by basename
     with `--ignore-existing`, so the remote path must resolve every
     destination name *before* any bytes move and simulate intra-batch
     dedupe in Python (`claimed_basenames`, `queued_src_hashes`,
     `to_transfer`); the local path copies per file and resolves lazily.
  2. **The transfer phase itself** — one rsync per batch (plus per-file
     rsyncs for renamed files) vs an inline `copy_and_hash_verify` per file.
  3. **Verification semantics** — `copy_and_hash_verify` is an independent
     byte check, so the local path always attests the landed hash; the
     remote path attests only under `verify_by_hash` (a network re-read),
     and otherwise reports `safe_to_format = False` via `remote_unverified`.
- Ten divergences have **no transport justification** (§ "Behavior
  alignment" below) — they are drift, and several are user-visible bugs.

## Goals

1. One import orchestrator; a fix lands once and applies to both transports.
2. Behavior preserved except where the two paths already disagree — each such
   disagreement resolved deliberately (below), never silently.
3. Every step lands as a reviewable PR with the full import test suite green.
4. The external contract is pinned throughout (§ "Contract pins").

## Non-goals

- No changes to `ImportParams`, the `/api/jobs/import-photos` request shape,
  the job result dict keys, or the remote-target config model.
- No changes to `vireo/import_dedup.py` (`CatalogIndex`/`DuplicateChecker`)
  or `vireo/ingest.py` (`build_destination_path`, `discover_source_files`).
- No new transports (e.g. SFTP) — the seam just has to make a third transport
  *possible*, not deliver one.
- De-duplicating the preflight endpoints' hand-mirrored collision walk
  (`app.py:18328–18522`) is a stretch goal (PR 8), not a requirement.

## Approaches considered

**A. Big-bang rewrite** — write the unified orchestrator + transports in one
PR, delete both functions. Rejected: a ~3,400-line rewrite of a file where
nearly every guard cites a production incident or PR review; the parity test
covers only selection observables, so the net is far too small for one jump.

**B. Incremental extraction (strangler), ending in a transport seam** —
align behavior first, then extract shared phases so both functions call the
same code, then merge the loops around a small transport protocol.
**Chosen:** every intermediate state is shippable and reviewable, the bug
fixes land early and independently, and the final merge diff is small because
by then the two functions are already mostly calls to shared helpers.

**C. Template-method class hierarchy** (`ImportRun` base, local/remote
subclasses). Rejected as the *organizing* principle: Vireo's codebase is
function-oriented, and a two-subclass hierarchy just relocates the fork.
One element survives: run state moves from ~25 closure variables into an
explicit state object, which is what makes function extraction possible.

## Target architecture

All in `vireo/import_job.py` initially (module split can come later; the
refactor is hard enough without also moving files).

### State objects (replace the closures)

```python
@dataclass
class _LandedFile:
    dest_path: str
    verified_hash: str | None   # hash the transport attests is at dest_path
    source_path: str
    origin: str                 # "copied" | "skipped_duplicate" (adoption)
    src_size: int | None
    src_mtime_ns: int | None

# Per-file transport outcomes. Cancellation is deliberately NOT a per-file
# outcome (see the protocol below).
_FileOutcome = _LandedFile | _FailedFile   # _FailedFile: (source_path, reason)

@dataclass
class _ImportRunState:
    # run-scoped counters and ledgers: copied, verified, skipped_duplicate,
    # unverified_duplicate, failed, emitted, cancelled, unsafe_files,
    # folder_counts, imported_photo_ids, linked_dup_dirs, dup_link_failed,
    # run_dest_folders, run_verified_hashes, mount_ever_lost,
    # wc_source_paths, wc_dest_folders, ...
```

This kills the two worst merge hazards outright: the `landed` tuples whose
fields 1 and 2 are *transposed* between the paths (5-tuple remote at line
2428 vs 6-tuple local at 4024), and the remote path's rollback logic
open-coded at 8 sites (each of which must remember `if verify_by_hash:
verified -= 1`) vs the local path's `_reclassify_landed_failed` helper.
Adoptions fold into `landed` with `origin="skipped_duplicate"` (the local
model); the remote-only `adopted_paths` dict disappears, and the per-batch
scan guard becomes the same `if landed:` on both paths because adopted files
are now *in* `landed`.

### Transport protocol

The seam sits exactly where the phase map found the genuine divergence:

```python
class _Transport(Protocol):
    defers_transfer: bool
    # True: every landed file carries a hash the transport verified at the
    # destination. Local: always (copy_and_hash_verify). Remote: only when
    # params.verify_by_hash (rsync alone attests nothing).
    attests_bytes: bool

    def enqueue(self, src, dest_folder, dest_basename, src_hash,
                src_size, src_mtime_ns) -> _FileOutcome | Queued
        # local: copy+verify now — _LandedFile on success, _FailedFile on
        #        a copy/verify error
        # remote: reserve the name and queue for rsync -> Queued
    def flush_batch(self, rel, emit_transfer, stop) -> _BatchResult
        # local: no-op (empty result)
        # remote: _remote_mkdir_p + flat rsync + per-file renamed rsyncs +
        #         optional remote_verify_files

@dataclass
class _BatchResult:
    outcomes: list[_FileOutcome]
    cancelled: bool   # user stop observed mid-transfer

class Queued: ...   # sentinel: bytes not moved yet; outcome arrives at flush
```

Cancellation is a batch-level fact, not a per-file outcome: today a stopped
rsync (`_rsync_cancelled`, line 2320) deliberately produces *no* per-file
failures — queued files stay on the card and partially/fully transferred but
unverified files stay uncataloged for crash-recovery adoption on the next
run (the PR #1425 behavior). `_BatchResult.cancelled` carries that state
back explicitly; the transport never mutates `_ImportRunState`.

Outcome-completeness invariant: when `cancelled` is false, `flush_batch`
returns exactly one outcome per queued file. When `cancelled` is true, it
returns outcomes only for files *conclusively completed before* the
cancellation; every remaining queued file produces neither a failure nor a
landing and is left for next-run adoption. This pins today's asymmetry
between the two rsync shapes: a stop during the flat batch yields no
outcomes for any flat file — the batch shares one exit code, so no per-file
completion can be attributed (line 2350) — while a stop between renamed-file
transfers keeps the outcomes of the per-file rsyncs that already returned
success (lines 2361–2378). Folder
preparation is *not* part of the protocol: the mount guards, local
`os.makedirs`, and folder-status promotion are byte-identical in both paths
today and stay in the orchestrator; the SSH-side `_remote_mkdir_p` (already
inside the transfer phase at line 2276) lives in `RsyncTransport.flush_batch`.

- `LocalTransport` wraps `copy_and_hash_verify`; `RsyncTransport` wraps the
  five existing `move.py` seams (`_ssh_rsh_string`, `rsync_dest_spec`,
  `_remote_mkdir_p`, `_run_rsync_streamed`, `remote_verify_files`) — the
  same five functions `_install_fake_remote_rsync` already monkeypatches,
  so ~75 remote tests move their patch target once, to one place.
- The collision/adopt walk unifies into one shared routine that consults an
  in-flight **reservation map** before the disk. For the local transport the
  map is always empty (copies are immediate), so behavior is unchanged; for
  the remote transport it is `claimed_basenames`/`queued_src_hashes` under a
  single name. Eager-vs-lazy source hashing is expressed as
  `transport.defers_transfer` forcing evaluation of the (shared) cached-hash
  closure at enqueue time; when `defers_transfer` is false the orchestrator
  passes whatever the cache already holds (possibly `None`), preserving the
  local path's lazy hashing — no eager hash is added to the local path.
- Catalog stamping unifies on `_LandedFile.verified_hash`: when present,
  compare the scan row's hash against it and stamp `hash_status='ok'`
  (today's local semantics, and today's remote semantics under
  `verify_by_hash` — with `copy_and_hash_verify` the copy-time hash *is*
  the source content hash, so the two comparisons are the same predicate);
  when absent, keep today's remote-unverified behavior.
- `safe_to_format`'s remote-only `remote_unverified` term generalizes to
  `not transport.attests_bytes` — the honest-signal rule from
  CORE_PHILOSOPHY stays intact for any future transport.
- Cancellation stays orchestrator-owned (`is_cancelled` at batch/file tops,
  `_stop_requested`/`DestReadCancelled` around destination reads); the
  transport owns only subprocess-level cancellation (`cancellation_requested`
  probes inside `_do_rsync`/`_rsync_cancelled`), which is inherently
  transport-specific.

#### PR 7 as-built adaptations (2026-08-10)

The protocol sketch above is the design-phase shape. PR 7's implementation
deliberately adapts it in three ways; each is a considered decision, not
drift:

1. **Verdict-style `enqueue`, not the pure `_FileOutcome | Queued` /
   `_BatchResult` form.** The real per-file middle has FIVE terminal shapes,
   not two: landed, failed, intra-batch duplicate-skip (with its
   `dup_skips` booking), adopted landing, and dest-read-cancelled (which
   breaks the whole batch). Additionally, `_record_checker` needs the RAW
   source hash — un-normalized, i.e. `None` for a remote zero-byte file —
   while `_LandedFile.verified_hash` was normalized to `EMPTY_FILE_SHA256`
   in PR 6b; threading the raw hash through pure value types is a redesign,
   not a move. So `enqueue` mirrors the proven `_duplicate_gate` idiom
   (PR #1423): it mutates `state`/`batch_st` and returns a verdict constant
   (`_ENQ_HANDLED` / `_ENQ_QUEUED` / `_ENQ_CANCELLED`; the caller maps
   CANCELLED → break, same contract as `_GATE_CANCELLED`). The pure
   `_BatchResult` form remains a PR 7b option — it needs a `record_hash`
   field beside `verified_hash` plus a pinned zero-byte
   `run_verified_hashes` asymmetry first.
2. **Two collision/adopt walks retained, one inside each transport** — the
   sketch's single shared walk with a reservation map is deferred. The two
   walks still differ on seven axes: loop shape; reservation map (remote
   consults `claimed_basenames`/`queued_src_hashes`, local has none);
   local's size pre-check before hashing candidates (an I/O difference);
   local's explicit zero-byte adopt branch, which remote can only reach
   checker-less (a live per-transport behavior split); eager vs lazy source
   hashing; `DestReadCancelled` propagation; and output shape. Unifying
   them inside "the merge PR" would hide at least three behavior flips with
   no red-green of their own — and the Non-goals section is explicit that
   the seam must make a third transport POSSIBLE, not unify everything.
   Walk unification is PR 7b, each flip red-green'd individually.
3. **`defers_transfer` is dropped.** It existed only to parameterize the
   shared walk's hashing strategy; with two walks (adaptation 2) it has no
   reader.

`flush_batch` as built also deviates from the sketch: it mutates
`state`/`batch_st` rather than returning a `_BatchResult` (consistent with
adaptation 1); `_emit_transfer` is transport-internal rather than a
parameter; and it deliberately keeps TWO distinct cancel probes — the
watchdog's non-blocking `cancellation_requested` and the loop-boundary
pausing `is_cancelled` — instead of the sketch's single `stop` parameter,
because collapsing them would change when a stop pauses vs merely polls.
The outcome-completeness invariant above holds unchanged (a flat-batch
cancel yields zero outcomes; a renamed-phase cancel keeps prior successes).

**Seam preservation — do not repoint the monkeypatch surface.** All 29
`monkeypatch.setattr(_move, ...)` sites (15 `_run_rsync_streamed`, 9
`_remote_mkdir_p`, 5 `remote_verify_files`; 64 uses of the shared
`_install_fake_remote_rsync` installer) keep working without churn because
`_RsyncTransport` keeps `import move as move_mod` and resolves
`move_mod.<name>(...)` at call time. Call-shape constraints:
`_run_rsync_streamed`'s call shape must stay BYTE-IDENTICAL — four tests
wrap the installed fake and re-call it with the same argument layout — and
`remote_verify_files` must keep its
`(rsync_bin, src_specs, rsync_target, remote, dest_is_dir=…)` shape.

#### PR 7b as-built (2026-08-10)

PR 7b unifies the last hand-mirrored import logic: the two collision/adopt
walks PR 7 adaptation 2 deliberately left inside the transports. Shape:
align-then-extract (the proven PR 6b sequence) — three behavior flips
red-green'd per transport in place, then a provably behavior-neutral
extraction into `_resolve_dest_collision`, then the safety flip
(decision 13) written **once**, in the shared walk. That last commit is the
project's thesis in miniature: the first fix in this file that lands on both
transports by construction rather than by remembering to mirror it.

Disposition of PR 7 adaptation 2's seven divergence axes:

| # | Axis | Disposition |
|---|------|-------------|
| 1 | Loop shape (local: primary special-cased + suffix loop from 1; remote: uniform loop from 0) | Unified on the uniform loop at extraction. Behavior-neutral **after** the semantic flips below. |
| 2 | Reservation map (remote `claimed_basenames`/`ctx.fold_basename`; local none) | Stays a per-transport parameter: `claims=None` on local (branch statically skipped), the batch map on remote. The local-vs-remote ledger difference for a same-basename identical sibling (local adopts the already-copied bytes; remote dup-skips via the claim) is **inherent to deferred transfer**, not a flip — documented, not changed. |
| 3 | Size pre-check before hashing candidates (local yes, remote no) | Ported to remote. Outcome-invisible (same hash ⟹ same size); observable only as fewer `_hash_dest_file` calls — pinned by a call-count test. |
| 4 | Zero-byte adopt (local: explicit primary-only branch; remote: reachable only checker-less) | Unified: `src_size == 0 and cand_size == 0` adopts at ANY candidate position on BOTH transports, red-green. |
| 5 | Eager vs lazy source hashing | Stays per-transport via a `src_hash_fn` zero-arg callable: remote passes `lambda: src_hash` (eager hash already required by the pre-walk `queued_src_hashes` dedup), local passes its memoized lazy closure. Zero-flip. |
| 6 | `DestReadCancelled` propagation (local: raises through to `enqueue`'s handler; remote: caught inline per-candidate) | Unified on propagate-out-of-the-walk; remote `enqueue` gains the same catch local already has. Observable behavior identical — the PR 1 geometry-B cancel pins stay green. |
| 7 | Candidate stat/hash `OSError` (local: fails the file via the outer `except OSError`; remote: advances past the candidate) | Unified on **advance**, red-green on local. An unreadable *candidate* is a destination artifact, not a source problem; failing a healthy source file for it is wrong, and if the destination is genuinely broken the subsequent copy fails with the real error anyway. `DestReadCancelled` subclasses `OSError`, so every new advance-handler re-raises it first — otherwise the wedged-mount cancel pins would be silently swallowed into an advance. |

Two further decisions recorded here:

- **The pure-`_BatchResult` enqueue form (PR 7 adaptation 1's open option)
  is DROPPED, not deferred.** The verdict idiom is now uniform across
  `_duplicate_gate` / `enqueue` / the walk, `_book_adoption` already carries
  the raw-vs-normalized hash split that motivated the deferral, and no third
  transport is on any horizon. YAGNI.
- **Axis-3 test adaptation.** `test_remote_import_cancel_interrupts_stuck_collision_hash`
  simulates a wedged mount with FIFOs at the collision-candidate paths, and a
  FIFO stats as size 0 — so the new size pre-check would advance past it
  without ever reaching the wedged read the test exists to pin. A real wedged
  SMB candidate does not report size 0 (its `stat` blocks or matches), so the
  fixture, not the property, is what the gate invalidates: the test now stubs
  `os.path.getsize` for the two FIFO paths to return the source size, and
  otherwise asserts exactly what it did before.

### Entry point

`run_import_job(job, runner, db_path, workspace_id, params)` keeps its exact
signature and remains the only public entry (`app.py:26419` untouched). It
constructs the `Database`, picks the transport from `params.remote_target`,
and runs the single orchestrator. `_run_remote_import_job` is deleted at the
end (PR 7).

## Behavior alignment decisions

The non-transport divergences, each resolved deliberately (1–9 from the
design phase map; 10 found empirically by PR 1's parity net; 11 found by
the 2026-08-08 extraction phase map). "Adopt X" means the other path
changes to match.

| # | Divergence | Decision |
|---|---|---|
| 1 | Remote `_emit` never sends the `folders={…}` per-folder snapshot the Import page renders mid-run (local: 3213–3216) | **Bug — adopt local.** Remote imports get the live folder table. |
| 2 | Local dest-under-source batch refusal neither advances `emitted` nor emits progress (3497–3506); remote does both (1676–1691) | **Bug — adopt remote.** A rejected batch must not freeze the progress bar. |
| 3 | Missing-mount-root refusal emits `"{rel}: archive unavailable"` locally (3535) but the generic copied/present string remotely (1710) | **Adopt local.** The specific string is the honest signal; the UI treats `phase` as opaque display text, and no test pins the remote wording for this case. |
| 4 | `landed` 5-tuple (remote) vs 6-tuple (local) with fields transposed | **Structural — `_LandedFile` dataclass** (PR 5). |
| 5 | Remote registers duplicate-accepts with `_record_checker(source_file)` (2008); local never does (3805–3832) | **Adopt local — remove the remote call.** The call passes no `dest_folder`, so it never populates `run_dest_folders` and cannot enable the intra-run fast path at 1922; `DuplicateChecker.record()` costs an extra `os.stat` and only registers `_seen_*` identities, whose sole effect is a narrow renamed-twin-of-an-accepted-duplicate edge that the post-import scan already covers. The helper docstring's "mirrors the local path" claim is false at this call site — this is drift, not design. PR 1 adds a characterization test for the renamed-twin scenario on both paths first so the removal is observable. (Reversed 2026-08-06 after external review; the original "strictly less I/O, same outcome" rationale did not survive contact with `import_dedup.py:413`.) *Empirical confirmation 2026-08-07 (PR 1, Task 6): world 1 — the call is behaviorally unobservable in the cataloged-twin geometry in both verify modes, because `CatalogIndex.known_hashes` already covers the renamed twin; the removal is a proven no-op there. Not proven for uncataloged intra-run geometries, which the `run_dest_folders` machinery covers separately.* |
| 6 | Remote path has no `pre_scan_hashes` capture, no `_invalidate_derived_caches`, no `_sweep_untracked_previews_for_photos`, no reclassified-path filtering of WC overrides (local: 4142–4155, 4378–4456, 4466–4484) | **Bug — port to remote.** A remote import that replaces bytes at an existing archive path currently leaves stale thumbnails/previews — exactly the failure the local path fixed in PR #1107. |
| 7 | WC identity `(size, mtime_ns)` captured before the copy locally (3893) but after the transfer remotely (2419) | **Adopt local.** The tuple should attest the source at decision time; a source that changes mid-transfer must not look "clean". |
| 8 | Local re-computes the source hash at 3996–3999 instead of reusing `_src_hash_cached()` | **No change — premise disproven (2026-08-08, PR 3).** `DuplicateChecker.content_hash` memoizes per source path (`import_dedup.py:319-327`), so with a checker the copy-site call is a cache hit whenever a hash was computed earlier in the run — and otherwise performs a read `copy_and_hash_verify` would do itself anyway (its `src_hash is None` branch runs a standalone `compute_file_hash(src)`). With no checker, reusing `_src_hash_cached()` is read-neutral: the standalone read merely moves, with a marginal saving only on the rare collision-walk path. No redundant I/O exists; the call-site duplication itself dissolves in PR 5's shared cached-hash closure. |
| 9 | Remote rollback open-coded at 8 sites vs local `_reclassify_landed_failed` | **Structural — shared helper on `_ImportRunState`** (PR 5). |
| 10 | Adopted (crash-recovery) files get `hash_status='ok'` stamped locally but stay `NULL` remotely — found empirically by PR 1's parity net (2026-08-07): local adoption folds into `landed` and hits the verify stamp; remote adoption lives in `adopted_paths`, whose validation cross-checks bytes but never stamps | **Adopt local, via the PR 5 structural change.** Folding remote adoptions into `landed` with their verified hash makes the stamp fall out of the unified catalog pass; no separate fix PR. Pinned per-path by `test_{local,remote}_adoption_uncataloged_dest_twin_current_behavior`, which flip when PR 5 lands. |
| 11 | Local stamping loop backfills `file_hash` on scan-NULL rows (`update_photo_hash_check(..., "ok", file_hash=verified_hash)`, ~L4511–4514 — NOT the non-backfilling stamp at L4480–4482 just above it, which handles the zero-byte case); remote stamps `"ok"` without backfilling — found by the 2026-08-08 extraction phase map (D4). Related, same loop: a zero-byte normalization-convention split (D2/D3) — remote normalizes `EMPTY_FILE_SHA256` → `None` at hash time with a `read_failed` flag; local compares raw hashes and its `verified_hash` is never `None`. | **RESOLVED 2026-08-09 (PR 6b): adopt local, with the zero-byte exclusion.** Remote backfills `file_hash` only on the scan-NULL re-read-agree path, and only with non-`EMPTY_FILE_SHA256` hashes: `file_hash=(src_hash if src_hash != EMPTY_FILE_SHA256 else None)`. Backfilling `EMPTY_FILE_SHA256` would recreate the collision the scanner's zero-byte NULL convention exists to prevent (scanner nulls empty-file hashes; `empty_hash_needs_repair` would churn repairs). The scan-hash-agrees path needs no backfill — the row already holds the hash (local doesn't backfill there either). For D2/D3, unify by normalizing at `_LandedFile` construction in 6b so both loops see the same convention. |
| 12 | Local has a per-file dest-safety guard (`os.path.samefile` + realpath'd dest-under-source check) before copying; remote has only the folder-level `_batch_preflight` guard | **Port to remote (PR 7, red-green).** The per-file guard is NOT subsumed by the folder guard: it additionally catches `os.path.samefile` identity (hard-links/symlinks to the same inode) and a `dest_file` that is itself a symlink resolving back into the card — realpath'd at file level, where the folder guard only realpaths the folder. On remote, that geometry is exactly the safe_to_format-over-unbacked-bytes hazard the batch guard's own comment describes; today remote has no per-file defense (the collision walk would hash the symlink target — the card file itself — byte-match, and adopt). Porting is a pure tightening: it can only convert an accept into a failure in an already-unsafe geometry; expected existing-suite flips: zero. Red-green in PR 7: a remote import whose `dest_file` is symlinked into the card with `verify_by_hash=True` must yield `failed == 1`, `safe_to_format is False`. HONESTY CONSTRAINT: a source-backed SUFFIX candidate (e.g. a symlink at `name_1.ext` into the card with different bytes at the primary name) is caught by NEITHER guard on EITHER path — that per-candidate gap is pre-existing on local, and this port deliberately preserves parity rather than closing it (closing it is its own behavior change; PR 7b/follow-up). Guard comments must never claim suffix coverage that doesn't exist. |
| 13 | Source-backed SUFFIX candidates (decision 12's KNOWN GAP): both walks hash a suffix candidate that is a symlink/hardlink back into the card, byte-match it against itself, and adopt — `safe_to_format` can go green over card-only bytes. Sibling hole: the free-slot check uses `os.path.exists`, so a DANGLING symlink at a candidate name reads as free and is chosen as the landing slot. | **Close both in PR 7b, once, in the shared walk (red-green per transport).** An existing candidate that is source-backed (`os.path.samefile(source_file, cand)` or realpath-under-any-source) is advanced past with a WARNING — never hashed, never adopted; unlike the primary-name decision-12 guard it does NOT fail the file, because one poisoned entry does not poison sibling slots (the primary guard's geometry — a template resolving into the card — does, and it keeps failing the file at the orchestrator). The free-slot check becomes `not os.path.lexists(cand)`; an `lexists`-but-not-`exists` (dangling) entry is advanced past. KNOWN-REMAINING (documented, out of scope): a candidate hardlinked to a *different* card file has no path- or inode-visible tie to this source (`samefile` false, realpath is its own path) — catching it needs `st_dev` checks against source roots. **Dangling-slot premise corrected (2026-08-10, verified before implementing):** the planning note claimed the local copy "writes through" the dangling link, landing archive bytes at the link target (possibly on the card). It does not. `copy_and_hash_verify` copies to a hidden sibling temp and promotes with a no-overwrite `os.link`, which raises `FileExistsError` on a dangling symlink (the directory entry exists), so today the file is **failed** with the misleading reason `"copy verification failed (destination bytes do not match the source)"`; the hardlinkless fallback's `os.rename` would replace the link itself, never write through it. So the defect is a spurious import failure with a wrong reason, not data loss — still worth closing, and `lexists` closes it, but the red assertion is `failed == 1`, not bytes-on-the-card. |

Kept as deliberate (transport-required) differences, expressed through the
protocol rather than duplicated code: transfer sub-progress
(`_emit_transfer`, `phase_*` keys, `"{rel}: transferring"`),
`--ignore-existing`/flat-basename reservation logic, `verify_by_hash`
gating of `verified` counts and `hash_status` stamps, the `remote_unverified`
term in `safe_to_format`, and subprocess-level cancellation probes.

## PR sequence

Each PR runs the full suite in CLAUDE.md plus `vireo/tests/test_import_job.py`,
and goes through the normal PR-agent review cycle.

1. **PR 1 — widen the test net (tests only).** Two halves:
   - *Parity:* extend the local/remote parity harness beyond selection
     observables — duplicate skip, basename collision, crash-recovery
     adoption, mount loss mid-batch, stop during a destination read. Fix the two mirror
     pairs whose fixture geometry diverges (twin parked in a non-template
     folder locally vs a template-shaped path remotely at test lines
     ~10012/10074; fresh-copy vs adoption gating at ~10736/10837) so each
     mirror actually exercises the same branch.
   - *Transport-specific characterization:* the parity harness forces
     `verify_by_hash=True` (to dodge the vacuous-pass trap), which blinds it
     to remote-only semantics. Pin those directly, asserting DB effects
     (photo rows, folder links, `hash_status`) as well as result dicts and
     event streams: default `verify_by_hash=False` honesty gate; stop
     partway through a flat rsync batch (cancellation, not per-file
     failure; nothing cataloged); stop between renamed-file transfers;
     `remote_verify_files` failure after a successful transfer; and the
     renamed-twin-of-an-accepted-duplicate scenario (decision 5) pinned
     *per path* — and, per the empirical world-1 outcome recorded in the
     decision table (the paths turned out NOT to diverge on it), also
     promoted into the parity list.
   This is the net everything else lands on.
2. **PR 2 — progress/emit alignment.** Decisions 1, 2, 3. User-visible
   fixes; each with a regression test asserting the event stream.
3. **PR 3 — dedupe/bookkeeping alignment.** Decisions 5 and 7 (8 resolved
   as a documented no-change — see the decision table). Separable
   from PR 2. For both PR 2 and PR 3: any change to the
   duplicate/collision/adopt walk must be checked against the hand-mirrored
   preflight copies at `app.py:18300–18550` and either applied there too or
   recorded as a deliberate difference (see Risks).
4. **PR 4 — port derived-cache invalidation to remote.** Decision 6 alone:
   it touches scanning, DB state, previews, thumbnails, and working copies,
   so it gets an isolated PR with DB-level assertions (cache files removed,
   `pre_scan_hashes` comparison, preview sweep) rather than riding along
   with the smaller alignments.
5. **PR 5 — state consolidation (mostly no behavior change).**
   `_LandedFile`, `_ImportRunState`, shared rollback helper, fold remote
   adoptions into `landed` (collapsing the scan-guard difference), single
   `_record_checker`. The parity suite plus byte-identical result dicts
   are the check for everything *except* the single intentional DB-visible
   change from decision 10: folding remote adoptions into `landed` runs
   them through the unified catalog stamp, flipping adopted-photo rows
   from `hash_status=NULL` to `hash_status='ok'` on the remote path (the
   local path already stamps). This is the whole point of the fold — not
   incidental churn — so the `test_{local,remote}_adoption_uncataloged_
   dest_twin_current_behavior` pair must flip in this PR; a "no change"
   verdict there is a regression that silently omitted the fix. All other
   parity scenarios and result-dict fields stay byte-identical.
   *Split 2026-08-08: 5a = `_LandedFile` + fold remote adoptions into
   `landed` + origin-switching rollback (ships three behavior flips
   aligning remote to local — divergence 10's hash_status stamp,
   card-side WC overrides for remote adoptions, local failure
   wording/subjects for adopted-file validation failures); 5b =
   `_record_checker` hoist + `_ImportRunState` (mechanical rename diff,
   kept separate so the flips stay reviewable). Rationale: the fold trio
   is semantically inseparable — the fold requires the `origin` field
   and origin-switching rollback or counters go negative on
   adopted-entry failures.*
6. **PR 6 — extract shared phases (no behavior change).** The
   identical/cosmetic phases (setup, normalization, mount baseline, guards,
   discovery, selection, preflight, batching, batch guards, twin linking, WC
   extraction, finalize) become module-level functions taking
   `(state, params, deps)`; both paths call them. The two functions shrink
   to their genuinely divergent cores.
   *Update 2026-08-08: extraction lands as this PR, minus the stamping
   loop — the 2026-08-08 phase map showed it hides a genuine behavioral
   divergence (11/D4: local-only `file_hash` backfill) plus the zero-byte
   normalization-convention split (D2/D3), so it is carved out into a new
   **PR 6b — stamping alignment + extraction (behavior PR: D4, D2/D3)**
   between PR 6 and PR 7: align per decision 11 first, then extract the
   now-identical loop.*
   *Update 2026-08-09: PR 6b lands as four production commits, each
   individually reviewable: (1) D4 red-green — remote backfills `file_hash`
   on the scan-NULL re-read-agree path with the zero-byte exclusion
   (decision 11, now RESOLVED); (2) D2/D3 — normalize zero-byte
   `verified_hash` to `EMPTY_FILE_SHA256` at `_LandedFile` construction,
   with the one externally-visible flip (remote zero-byte re-import now
   invalidates derived caches, matching local) pinned by its own test;
   (3) D1 + messages — gate stamps on `attests_bytes`
   (≡ `verified_counted_for_copies`, zero-flip) and unify the reason
   strings via a `dest_noun` parameter; (4) mechanical extraction of the
   now-textually-identical loop as `_stamp_landed_and_validate_catalog`.*
7. **PR 7 — the merge.** Introduce `_Transport`, `LocalTransport`,
   `RsyncTransport`; one orchestrator batch loop; delete
   `_run_remote_import_job`; repoint the remote tests' monkeypatch seam at
   the transport. This diff is small *because* of PRs 5–6.
   *Residual asymmetry (recorded by the 2026-08-08 extraction phase map):
   the local-only per-file `same_file`/dest-under-source guard
   (import_job.py L3963–4007) has no remote counterpart — the remote path
   only applies the batch-level dest-under-source guard. PR 7 must decide
   keep-both/port/drop deliberately rather than letting the merge pick a
   winner silently.*
7b. **PR 7b — walk unification + suffix-gap closure.** *In flight
   2026-08-10; plan at
   `docs/superpowers/plans/2026-08-10-import-unify-pr7b-walk-unification.md`.*
   Disposes of PR 7 adaptation 2's seven axes (table above), extracts
   `_resolve_dest_collision`, and closes decision 13's source-backed
   suffix-candidate and dangling-slot holes once, in the shared walk.
8. **PR 8 (stretch) — preflight de-mirror.** Share the collision/adopt walk
   with `/api/import/check-duplicates` and friends (`app.py:18328–18522`),
   removing the third hand-mirrored copy. Only after PR 7 has soaked.

## Contract pins (must not change)

- `run_import_job` signature; `ImportParams` fields; result dict — same 19
  keys, both paths (`discovered, copied, verified, photo_ids,
  photo_fingerprints, source_snapshots, skipped_duplicate,
  unverified_duplicate, unverified_duplicates_only, failed, safe_to_format,
  unsafe_files, folders, cancelled, discovery_errors, files_appeared,
  files_vanished, ok, errors`).
- Runner surface: exactly `set_steps`, `update_step`, `push_event`,
  `is_cancelled`, `cancellation_requested` (the `FakeRunner` double's whole
  API; anything more breaks ~180 tests).
- Phase strings: `"Discovering files"` and `"{rel}: importing"` are pinned by
  tests (`_selection_observables`, the two cancel-on-importing runners);
  `phase_label`/`phase_current`/`phase_total` are pinned by
  `import.html:3719` and the navbar/jobs progress renderers. Step summary
  wordings from `_selection_summary` are pinned byte-for-byte.
- Selection `unsafe_files` sentinel paths and their render order.
- `DestReadCancelled` remains an `OSError` subclass.
- Consumers of `result["photo_ids"]`/`result["ok"]`/`result["cancelled"]`:
  `_chain_after_import` (`app.py:26247`) and `_apply_import_tags`
  (`app.py:8121`).

## Risks

- **Behavior hiding in drift.** Any divergence the phase map classified as
  cosmetic could turn out load-bearing. Mitigation: PRs 5–6 are
  no-behavior-change by construction and reviewed against byte-identical
  result dicts and event streams; the widened parity suite runs first.
- **Parity tests passing vacuously.** The existing suite only avoids this by
  forcing `verify_by_hash=True` on the local side; every new parity scenario
  must keep that guard, and the meta-tests (distinctness + local-path
  expected outcomes) must be extended alongside.
- **Preflight desync.** PRs 2–3 change the walk the preflight endpoints
  mirror by hand. Each such change must grep `app.py:18300–18550` for the
  mirrored copy and update it (or consciously record the difference) —
  this is exactly the failure mode this project exists to end, and until
  PR 8 it still has to be handled manually.
- **Comment loss.** The file's comments are an incident ledger (PR numbers,
  Codex review ids). Extraction PRs move comments with their code verbatim;
  a reviewer checklist item is "no rationale comment dropped".
