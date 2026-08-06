# Import path unification — design

**Date:** 2026-08-06
**Status:** Draft, pending review
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
- Nine divergences have **no transport justification** (§ "Behavior
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
  (`app.py:18328–18522`) is a stretch goal (PR 7), not a requirement.

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

# flush_batch outcome: either a _LandedFile or a per-file failure.
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

    def prepare_folder(self, dest_folder, rel) -> str | None   # error reason
    def enqueue(self, src, dest_folder, dest_basename, src_hash,
                src_size, src_mtime_ns) -> _LandedFile | None
        # local: copy+verify now, return the outcome
        # remote: reserve the name, queue for rsync, return None
    def flush_batch(self, rel, emit_transfer, stop) -> list[_FileOutcome]
        # local: no-op ([]); remote: _remote_mkdir_p + flat rsync + renamed
        # rsyncs + optional remote_verify_files, one outcome per queued file
```

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
  closure at enqueue time.
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

### Entry point

`run_import_job(job, runner, db_path, workspace_id, params)` keeps its exact
signature and remains the only public entry (`app.py:26419` untouched). It
constructs the `Database`, picks the transport from `params.remote_target`,
and runs the single orchestrator. `_run_remote_import_job` is deleted at the
end (PR 6).

## Behavior alignment decisions

The nine non-transport divergences, each resolved deliberately. "Adopt X"
means the other path changes to match.

| # | Divergence | Decision |
|---|---|---|
| 1 | Remote `_emit` never sends the `folders={…}` per-folder snapshot the Import page renders mid-run (local: 3213–3216) | **Bug — adopt local.** Remote imports get the live folder table. |
| 2 | Local dest-under-source batch refusal neither advances `emitted` nor emits progress (3497–3506); remote does both (1676–1691) | **Bug — adopt remote.** A rejected batch must not freeze the progress bar. |
| 3 | Missing-mount-root refusal emits `"{rel}: archive unavailable"` locally (3535) but the generic copied/present string remotely (1710) | **Adopt local.** The specific string is the honest signal; the UI treats `phase` as opaque display text, and no test pins the remote wording for this case. |
| 4 | `landed` 5-tuple (remote) vs 6-tuple (local) with fields transposed | **Structural — `_LandedFile` dataclass** (PR 4). |
| 5 | Remote registers duplicate-accepts with `_record_checker(source_file)` (2008); local never does (3805–3832) | **Adopt remote** (with its optional-args `_record_checker`). Later same-identity files hit the intra-run cache instead of re-hashing twins — strictly less I/O, same outcome. |
| 6 | Remote path has no `pre_scan_hashes` capture, no `_invalidate_derived_caches`, no `_sweep_untracked_previews_for_photos`, no reclassified-path filtering of WC overrides (local: 4142–4155, 4378–4456, 4466–4484) | **Bug — port to remote.** A remote import that replaces bytes at an existing archive path currently leaves stale thumbnails/previews — exactly the failure the local path fixed in PR #1107. |
| 7 | WC identity `(size, mtime_ns)` captured before the copy locally (3893) but after the transfer remotely (2419) | **Adopt local.** The tuple should attest the source at decision time; a source that changes mid-transfer must not look "clean". |
| 8 | Local re-computes the source hash at 3996–3999 instead of reusing `_src_hash_cached()` | **Fix locally.** Pure redundancy; one hash read saved per fresh copy. |
| 9 | Remote rollback open-coded at 8 sites vs local `_reclassify_landed_failed` | **Structural — shared helper on `_ImportRunState`** (PR 4). |

Kept as deliberate (transport-required) differences, expressed through the
protocol rather than duplicated code: transfer sub-progress
(`_emit_transfer`, `phase_*` keys, `"{rel}: transferring"`),
`--ignore-existing`/flat-basename reservation logic, `verify_by_hash`
gating of `verified` counts and `hash_status` stamps, the `remote_unverified`
term in `safe_to_format`, and subprocess-level cancellation probes.

## PR sequence

Each PR runs the full suite in CLAUDE.md plus `vireo/tests/test_import_job.py`,
and goes through the normal PR-agent review cycle.

1. **PR 1 — widen the parity net (tests only).** Extend the
   local/remote parity harness beyond selection observables: scenarios for
   duplicate skip, basename collision, crash-recovery adoption, mount loss
   mid-batch, and stop during a destination read. Fix the two mirror pairs
   whose fixture geometry diverges (twin parked in a non-template folder
   locally vs a template-shaped path remotely at test lines ~10012/10074;
   fresh-copy vs adoption gating at ~10736/10837) so each mirror actually
   exercises the same branch. This is the net everything else lands on.
2. **PR 2 — progress/emit alignment.** Decisions 1, 2, 3. User-visible
   fixes; each with a regression test asserting the event stream.
3. **PR 3 — dedupe/bookkeeping alignment.** Decisions 5, 7, 8, plus port of
   decision 6 (derived-cache invalidation on remote). Separable from PR 2.
   For both PR 2 and PR 3: any change to the duplicate/collision/adopt walk
   must be checked against the hand-mirrored preflight copies at
   `app.py:18300–18550` and either applied there too or recorded as a
   deliberate difference (see Risks).
4. **PR 4 — state consolidation (no behavior change).** `_LandedFile`,
   `_ImportRunState`, shared rollback helper, fold remote adoptions into
   `landed` (collapsing the scan-guard difference), single `_record_checker`.
   The parity suite plus byte-identical result dicts are the check.
5. **PR 5 — extract shared phases (no behavior change).** The
   identical/cosmetic phases (setup, normalization, mount baseline, guards,
   discovery, selection, preflight, batching, batch guards, twin linking, WC
   extraction, finalize) become module-level functions taking
   `(state, params, deps)`; both paths call them. The two functions shrink
   to their genuinely divergent cores.
6. **PR 6 — the merge.** Introduce `_Transport`, `LocalTransport`,
   `RsyncTransport`; one orchestrator batch loop; delete
   `_run_remote_import_job`; repoint the remote tests' monkeypatch seam at
   the transport. This diff is small *because* of PRs 4–5.
7. **PR 7 (stretch) — preflight de-mirror.** Share the collision/adopt walk
   with `/api/import/check-duplicates` and friends (`app.py:18328–18522`),
   removing the third hand-mirrored copy. Only after PR 6 has soaked.

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
  cosmetic could turn out load-bearing. Mitigation: PRs 4–5 are
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
  PR 7 it still has to be handled manually.
- **Comment loss.** The file's comments are an incident ledger (PR numbers,
  Codex review ids). Extraction PRs move comments with their code verbatim;
  a reviewer checklist item is "no rationale comment dropped".
