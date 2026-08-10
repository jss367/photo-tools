# Import Unification PR 7b: Walk Unification + Suffix-Gap Closure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two per-transport collision/adopt walks in `vireo/import_job.py` with one shared `_resolve_dest_collision`, then close the source-backed suffix-candidate data-loss gap ONCE in the shared walk — the first fix that demonstrably lands once for both transports.

**Architecture:** Align-then-extract, the proven PR 6b shape. Three behavior alignments land first, each red-green'd per transport in place (zero-byte adoption at any candidate position; remote size pre-check; local advance-on-candidate-error). Then the walk extraction is provably behavior-neutral. Then the safety flip — rejecting source-backed and dangling-symlink candidates — is written exactly once, in the shared walk.

**Tech Stack:** Python/Flask repo; pytest; the PR 1 parity harness in `vireo/tests/test_import_job.py`.

**Spec:** `docs/superpowers/specs/2026-08-06-import-path-unification-design.md` (decision rows 1–12; this PR adds row 13 and the PR 7b as-built section).

**Branch:** `import-unify-pr7b-walk-unification` (off `origin/main` at `95ac3461`).

---

## Context the implementer must load first

Read, in this order:

1. Spec decision row 12 and the "PR 7 as-built adaptations" section of the spec — they define the seven walk-divergence axes and the honesty constraint on the suffix gap.
2. `vireo/import_job.py:3154-3336` — `_LocalTransport` (docstring + `enqueue`; the walk is lines 3220–3280).
3. `vireo/import_job.py:3463-3648` — `_RsyncTransport.enqueue` (the walk is lines 3529–3637).
4. `vireo/import_job.py:1012-1075` — `_reject_source_backed_dest` (the decision-12 guard and its KNOWN GAP paragraph).
5. `vireo/import_job.py:3091-3151` — `_book_copied` / `_book_adoption` / `_landed_copied` (booking conventions: `verified_hash` ledger-normalized, `record_hash` raw).
6. `vireo/import_job.py:729-795` — `_fail`, `_record_checker`.
7. `vireo/import_dedup.py:418-430` — `DuplicateChecker.record()` returns `()` for zero-byte files. **Consequence used throughout this plan:** the checker never stores anything for a zero-byte source, so `record_hash` at zero-byte adopts is observably moot; only `claimed_basenames`/`queued_src_hashes` (raw-`src_hash` convention, `None` ⟺ checker'd zero-byte) matter.
8. `vireo/import_job.py:4080-4116` — the orchestrator's per-file sequence: `_duplicate_gate` → `_reject_source_backed_dest` (primary name only) → `transport.enqueue`.

## The seven axes, and this plan's disposition of each

| # | Axis | Disposition |
|---|------|-------------|
| 1 | Loop shape (local: primary-special-cased + suffix loop from 1; remote: uniform loop from 0) | Unified on the uniform loop at extraction (Task 5). Behavior-neutral **after** Tasks 2–4 align semantics. |
| 2 | Reservation map (remote `claimed_basenames`/`ctx.fold_basename`; local none) | Stays a per-transport parameter: `claims=None` on local (branch statically skipped), the batch map on remote. The local-vs-remote ledger difference for a same-basename identical sibling (local adopts the already-copied bytes; remote dup-skips via the claim) is **inherent to deferred transfer**, not a flip — documented in the spec, not changed. |
| 3 | Size pre-check before hashing candidates (local yes, remote no) | Ported to remote (Task 3). Outcome-invisible (same hash ⟹ same size); observable only as fewer `_hash_dest_file` calls — pinned by a call-count test. |
| 4 | Zero-byte adopt (local: explicit primary-only branch; remote: reachable only checker-less) | Unified: `src_size == 0 and cand_size == 0` adopts at ANY candidate position on BOTH transports (Task 2, red-green). |
| 5 | Eager vs lazy source hashing | Stays per-transport via a `src_hash_fn` zero-arg callable: remote passes `lambda: src_hash` (eager hash already required by the pre-walk `queued_src_hashes` dedup), local passes its memoized lazy closure. Zero-flip. |
| 6 | `DestReadCancelled` propagation (local: raises through to `enqueue`'s handler; remote: caught inline per-candidate) | Unified on propagate-out-of-the-walk; remote `enqueue` gains the same catch local already has (sets `state.cancelled` + `batch_st.dest_read_cancelled`, returns `_ENQ_CANCELLED`). Observable behavior identical — the PR 1 geometry-B cancel pins must stay green untouched. |
| 7 | Candidate stat/hash `OSError` (local: fails the file via the outer `except OSError`; remote: advances past the candidate) | Unified on **advance** (Task 4, red-green on local). Rationale: an unreadable *candidate* is a destination artifact, not a source problem; failing a healthy source file for it is wrong, and if the destination is genuinely broken the subsequent copy fails with the real error anyway. |

**Also in scope (Task 6, the finale flip):** the decision-12 KNOWN GAP — a source-backed suffix candidate is adopted today by both walks (hash the card bytes against themselves → match → `skipped_duplicate` → `safe_to_format` can go green over bytes that exist only on the card). Plus a sibling hole found during planning: the free-slot check `not os.path.exists(candidate)` treats a **dangling symlink** as free. Both are closed once, in the shared walk.

> **CORRECTED DURING IMPLEMENTATION (2026-08-10).** This plan predicted that the local copy **writes through** the dangling symlink, landing "archive" bytes at the link target (including onto the card). That was checked before implementing and is false: `copy_and_hash_verify` copies to a hidden sibling temp and promotes with a no-overwrite `os.link`, which raises `FileExistsError` on a dangling symlink, and the hardlinkless fallback's `os.rename` replaces the link rather than following it. What actually happens today is that a healthy card file **fails** with the misleading reason "copy verification failed (destination bytes do not match the source)". The hole is real and `lexists` still closes it, but the RED expectation is `failed == 1`, not bytes-on-the-card. See spec decision row 13. The prediction is left in place below rather than rewritten, so the record of what was assumed stays honest.

**Explicitly out of scope (record in spec row 13 as known-remaining):**
- Device-level identity: a candidate that is a *hardlink to a different card file* is caught by neither `samefile(source_file, …)` nor the realpath-under-source check. Closing it would need `st_dev` comparison against source roots; not done here, documented honestly.
- The orchestrator-level decision-12 primary guard is NOT relitigated: a source-backed PRIMARY dest still **fails the file** (its geometry — template resolving back into the card — poisons every suffix too). The new walk-level rule applies to candidates the walk probes, and it **advances** (imports the file at a safe slot) rather than failing, because a single symlinked/hardlinked entry does not poison sibling slots.
- The pure-`_BatchResult` enqueue form is **DROPPED**, not deferred (spec note in Task 0): the verdict idiom is now uniform across `_duplicate_gate`/`enqueue`/the walk, `_book_adoption` already carries the raw-vs-normalized hash split, and no third transport is on any horizon. YAGNI.

## File structure

- Modify: `vireo/import_job.py` (walk extraction + flips; all changes inside the transport classes, `_reject_source_backed_dest`'s docstring, and one new module-level function + two verdict constants near `_book_adoption`).
- Modify: `vireo/tests/test_import_job.py` (new red-green pairs + keep-pins; zero churn to existing tests is a hard acceptance criterion for Task 5).
- Modify: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md` (row 13, PR 7b as-built section).

Baseline: `python -m pytest vireo/tests/test_import_job.py -q` → **233 passed, 1 skipped** at branch base. Every task ends with this suite green (plus its own additions).

---

### Task 0: Spec amendments

**Files:**
- Modify: `docs/superpowers/specs/2026-08-06-import-path-unification-design.md`

- [ ] **Step 1:** Add decision-table row 13:

> *(The row as shipped differs — see spec decision row 13, which carries the corrected dangling-symlink model.)*
>
> | 13 | Source-backed SUFFIX candidates (decision 12's KNOWN GAP): both walks hash a suffix candidate that is a symlink/hardlink back into the card, byte-match it against itself, and adopt — `safe_to_format` can go green over card-only bytes. Sibling hole: the free-slot check uses `os.path.exists`, so a DANGLING symlink reads as free and the local copy writes through it (archive bytes land at the link target, including on the card). | **Close both in PR 7b, once, in the shared walk (red-green per transport).** An existing candidate that is source-backed (`os.path.samefile(source_file, cand)` or realpath-under-any-source) is advanced past with a WARNING — never hashed, never adopted; unlike the primary-name decision-12 guard it does NOT fail the file, because one poisoned entry does not poison sibling slots (the primary guard's geometry — a template resolving into the card — does, and it keeps failing the file at the orchestrator). The free-slot check becomes `not os.path.lexists(cand)`; an `lexists`-but-not-`exists` (dangling) entry is advanced past. KNOWN-REMAINING (documented, out of scope): a candidate hardlinked to a *different* card file has no path- or inode-visible tie to this source (`samefile` false, realpath is its own path) — catching it needs `st_dev` checks against source roots. |

- [ ] **Step 2:** Add a "PR 7b as-built (2026-08-10)" subsection after the PR 7 as-built adaptations, recording: the seven-axis disposition table above (verbatim is fine), the inherent same-basename-sibling ledger difference (axis 2), and this decision: **the pure-`_BatchResult` option from PR 7 adaptation 1 is dropped, not deferred** — verdict idiom now uniform, raw/normalized hash split lives in `_book_adoption`, YAGNI.
- [ ] **Step 3:** Update the PR-sequence section: PR 7b is now in flight with this plan's filename.
- [ ] **Step 4:** Commit: `Spec: decision 13 (suffix-candidate + dangling-slot closure), PR 7b as-built dispositions`

### Task 1: Coverage audit + keep-pins

The flips in Tasks 2–4 and the extraction in Task 5 must not silently change neighboring behavior. Verify each **keep-behavior** below has an existing pin; add a pin ONLY where missing. Search `vireo/tests/test_import_job.py` (grep the cited markers).

- [ ] **Step 1:** Audit for existing coverage of:
  1. Local suffix crash-recovery adopt (PR #1107 geometry: primary differs, `name_1.ext` has this source's bytes → adopt, no `name_2.ext`). Grep: `_1` / `crash` / `suffix`.
  2. Remote claimed-basename branches (FIX 2): same-basename different-bytes sibling advances to a suffix; same-basename same-bytes sibling dup-skips (checker'd). Grep: `claimed` / `collide`.
  3. Remote `queued_src_hashes` intra-batch dedup incl. the checker-less no-skip contract (PR #1113). Grep: `skip_duplicates=False`.
  4. Cancel-mid-candidate-hash on both transports (PR 1 geometry B: stop during `_hash_dest_file` → file neither copied nor failed, batch stops touching the mount). Grep: `DestReadCancelled` / `dest_read_cancelled`.
  5. Local checker-less zero-byte primary twin adopt AND remote checker-less zero-byte primary twin adopt (both should adopt today — `EMPTY == EMPTY`). This is the Task 2 anchor: the checker-LESS cases must be pinned green BEFORE the checker'd flip.
- [ ] **Step 2:** For each behavior with no pin, write one focused test asserting TODAY's behavior. Expected: all new pins pass immediately.
- [ ] **Step 3:** Run `python -m pytest vireo/tests/test_import_job.py -q` → all green.
- [ ] **Step 4:** Commit: `test: keep-pins for walk behaviors preserved across PR 7b`

### Task 2 (Flip A): zero-byte twins adopt at any candidate position, both transports

Today: local adopts a zero-byte twin ONLY at the primary name (explicit branch, `import_job.py:3222-3230`); at a suffix position a checker'd zero-byte source never matches (`checker.content_hash` → `None`) and lands at the next free suffix. Remote has no explicit branch at all: a checker'd zero-byte source (`src_hash=None`) never matches any on-disk hash, so a zero-byte twin at the primary name gets a pointless suffix copy. Unified rule: `src_size == 0 and cand_size == 0` → adopt with `verified_hash=EMPTY_FILE_SHA256, record_hash=EMPTY_FILE_SHA256` (matches local's shipped primary branch; observably equivalent to remote's reachable checker-less case; `record()` returns `()` for zero-byte so the checker maps are untouched either way). On remote, the adopt claims the folded candidate basename with the RAW `src_hash` (i.e. `None` when checker'd) — `None == None` in the claimed-compare is safe because zero-byte ⟺ `None` under a checker, and all zero-byte files are identical.

**Files:** Modify `vireo/import_job.py` (both `enqueue` walks), Test `vireo/tests/test_import_job.py`.

- [ ] **Step 1 (RED):** `test_remote_zero_byte_twin_at_primary_adopts_with_checker` — remote import, `skip_duplicates=True`, mount dest folder already contains zero-byte `IMG_0001.NEF`, card has zero-byte `IMG_0001.NEF`. Assert: `skipped_duplicate == 1`, `copied == 0`, no `IMG_0001_1.NEF` transferred (fake-rsync file list empty for this file), landed entry carries `verified_hash == EMPTY_FILE_SHA256`. Run: expect FAIL (today it queues `IMG_0001_1.NEF`).
- [ ] **Step 2 (RED):** `test_local_zero_byte_suffix_candidate_adopts_with_checker` — local import, `skip_duplicates=True`, dest has non-empty `IMG_0001.NEF` (different bytes) and zero-byte `IMG_0001_1.NEF`; card has zero-byte `IMG_0001.NEF`. Assert: adoption of `IMG_0001_1.NEF` (`skipped_duplicate == 1`), no `IMG_0001_2.NEF` created. Run: expect FAIL (today it copies to `_2`).
- [ ] **Step 3 (GREEN, local):** In `_LocalTransport.enqueue`'s suffix loop, before the `cand_size == src_size` hash compare, insert the zero-byte branch mirroring the primary one: `if src_size == 0 and cand_size == 0: adopted_dest = (candidate, EMPTY_FILE_SHA256); break`. (The primary branch at 3222 already conforms.)
- [ ] **Step 4 (GREEN, remote):** In `_RsyncTransport.enqueue`'s candidate loop, inside the `os.path.exists(cand_mount)` branch, BEFORE hashing: get `cand_size` via `os.path.getsize` (wrap `OSError` → treat as size-mismatch/advance, `counter += 1; continue` — note this is also the Task 3 shape); `if src_size == 0 and cand_size == 0:` → claim the folded candidate with raw `src_hash`, call `_book_adoption(..., verified_hash=EMPTY_FILE_SHA256, record_hash=EMPTY_FILE_SHA256, ...)`, `adopted = True; break`. Keep the rationale comment brief and cite `record()`-returns-`()`.
- [ ] **Step 5:** Full file run → 233 + new all green (checker-less anchors from Task 1 must be untouched).
- [ ] **Step 6:** Commit: `Zero-byte twins adopt at any candidate position on both transports (PR 7b flip A)`

### Task 3 (Flip B): remote size pre-check before candidate hashing

Local gates candidate hashing on `getsize(cand) == src_size`; remote hashes every existing candidate — pure wasted mount I/O (memory: the archive is SMB over Tailscale; count round trips). Outcome-invisible; pin via call counting.

- [ ] **Step 1 (RED):** `test_remote_size_mismatched_candidate_not_hashed` — remote import, mount dest folder has `IMG_0001.NEF` with DIFFERENT size than the card's `IMG_0001.NEF`. Monkeypatch `import_job._hash_dest_file` with a counting wrapper. Assert: the mismatched candidate's path is never hashed AND the file still lands at `IMG_0001_1.NEF`. Run: expect FAIL on the call-count assertion.
- [ ] **Step 2 (GREEN):** In the remote walk's exists-branch (already reshaped by Task 2 Step 4 to fetch `cand_size`): only call `_hash_dest_file` when `cand_size == src_size`; on mismatch `counter += 1; continue`. `getsize` `OSError` → advance (remote's existing hash-`OSError`-advance semantics, one probe earlier).
- [ ] **Step 3:** Full file run green. Note for Task 5: after this task the remote exists-branch shape is `cand_size → zero-byte adopt → size gate → hash → compare`, i.e. structurally identical to local's.
- [ ] **Step 4:** Commit: `Remote walk gains local's size pre-check before hashing candidates (PR 7b flip B)`

### Task 4 (Flip C): local candidate stat/hash errors advance instead of failing the file

Today on local: primary `os.path.getsize(dest_file)` is unguarded and a candidate `_hash_dest_file` `OSError` propagates — both hit the outer `except OSError` and **fail the source file**. Remote advances past the sick candidate. Unify on advance (rationale in the axis table).

- [ ] **Step 1 (RED):** `test_local_unreadable_primary_candidate_advances` — local import; dest `IMG_0001.NEF` exists; monkeypatch `os.path.getsize` to raise `OSError` for that exact path (delegate otherwise). Assert: file lands at `IMG_0001_1.NEF`, `failed == 0`, `copied == 1`. Expect FAIL (today `failed == 1`).
- [ ] **Step 2 (RED):** `test_local_unhashable_same_size_candidate_advances` — dest `IMG_0001.NEF` same size as source but monkeypatch `import_job._hash_dest_file` to raise `OSError` for that path (delegate otherwise). Assert: lands at `IMG_0001_1.NEF`, `failed == 0`. Expect FAIL.
- [ ] **Step 3 (GREEN):** In `_LocalTransport.enqueue`: (a) wrap the primary `getsize` in `try/except OSError` → on error skip the twin/zero-byte compare and fall into the suffix loop (do NOT fail); (b) wrap the primary-twin `_hash_dest_file` compare the same way (`except OSError: dest_hash = None`, no adopt); (c) in the suffix loop wrap `_hash_dest_file` with `except OSError: cand_hash = None` (existing `cand_hash is not None` gate then advances). **`DestReadCancelled` must NOT be caught by these handlers** — it subclasses… verify: check `DestReadCancelled`'s base class; if it subclasses `OSError`, catch it first and re-raise. Write this check into the test: the geometry-B cancel pins must stay green.
- [ ] **Step 4:** Full file run green. Commit: `Local walk advances past unreadable candidates instead of failing the file (PR 7b flip C)`

### Task 5: Extract the shared walk — `_resolve_dest_collision`

After Tasks 2–4 the two walks are semantically aligned modulo the dispositions table. Extract ONE module-level function; both `enqueue`s call it. This task is **behavior-neutral**: zero changes to any existing test, full suite count identical.

**Files:** Modify `vireo/import_job.py` (new constants + function near `_book_adoption`; both `enqueue` bodies).

- [ ] **Step 1:** Add verdict constants + function (final shape; adjust only to match post-Task-4 reality):

```python
_WALK_HANDLED = "handled"   # adoption or intra-batch dup-skip fully booked
_WALK_PLACED = "placed"     # free slot chosen; caller copies/queues it


def _resolve_dest_collision(state, batch_st, ctx, *, source_file, rel,
                            checker, src_hash_fn, src_size, src_mtime_ns,
                            claims, stop_requested):
    """Walk primary name + numeric suffixes to an adoption, an intra-batch
    duplicate skip, or a free destination basename (PR 7b, spec row 13).

    ``src_hash_fn``: zero-arg callable returning the RAW source hash
    (memoized-lazy on the local transport, precomputed on rsync; ``None``
    means checker'd zero-byte). ``claims``: the rsync transport's
    batch-scoped ``claimed_basenames`` reservation map (``None`` on local,
    where the filesystem itself is the reservation — bytes land inside
    ``enqueue``). ``DestReadCancelled`` and source-read ``OSError``
    propagate to the caller's existing handlers.

    Returns ``(_WALK_HANDLED, None)`` or ``(_WALK_PLACED, dest_basename)``.
    """
    stem, suffix = os.path.splitext(source_file.name)
    counter = 0
    while True:
        candidate = (source_file.name if counter == 0
                     else f"{stem}_{counter}{suffix}")
        cand_path = os.path.join(batch_st.dest_folder, candidate)
        if claims is not None:
            candidate_key = ctx.fold_basename(candidate)
            if candidate_key in claims:
                if checker is not None and claims[candidate_key] == src_hash_fn():
                    state.skipped_duplicate += 1
                    _counts(state, rel)["skipped_duplicate"] += 1
                    batch_st.dup_skips.append((source_file, False))
                    _record_checker(state, checker, source_file,
                                    batch_st.dest_folder, src_hash_fn())
                    return _WALK_HANDLED, None
                counter += 1
                continue
        if os.path.exists(cand_path):
            try:
                cand_size = os.path.getsize(cand_path)
            except OSError:
                counter += 1
                continue
            if src_size == 0 and cand_size == 0:
                _adopt = (cand_path, EMPTY_FILE_SHA256, EMPTY_FILE_SHA256)
            elif cand_size == src_size:
                try:
                    cand_hash = _hash_dest_file(cand_path, stop_requested)
                except OSError:
                    cand_hash = None
                src_h = src_hash_fn()
                if (cand_hash is not None and src_h is not None
                        and cand_hash == src_h):
                    _adopt = (cand_path, src_h, src_h)
                else:
                    _adopt = None
            else:
                _adopt = None
            if _adopt is not None:
                dest_path, verified_hash, record_hash = _adopt
                if claims is not None:
                    claims[ctx.fold_basename(candidate)] = src_hash_fn()
                _book_adoption(
                    state, batch_st, checker, rel=rel,
                    source_file=source_file, dest_path=dest_path,
                    verified_hash=verified_hash, record_hash=record_hash,
                    src_size=src_size, src_mtime_ns=src_mtime_ns,
                )
                return _WALK_HANDLED, None
            counter += 1
            continue
        return _WALK_PLACED, candidate
```

  Carry over (condensed) the load-bearing comments: the PR #1107 suffix-adopt rationale, the FIX 2 claimed-basename rationale, the adoption-rides-`landed`-not-`dup_skips` pointer (that one lives on `_book_adoption`'s docstring already — pointer suffices), and the `None == None` zero-byte claimed-compare note.

- [ ] **Step 2:** Rewrite `_LocalTransport.enqueue`: keep stat + `_src_hash_cached`; replace the primary-check + suffix loop with `verdict, basename = _resolve_dest_collision(..., src_hash_fn=_src_hash_cached, claims=None, ...)`; `_WALK_HANDLED` → `return _ENQ_HANDLED`; `_WALK_PLACED` → `dest_file = os.path.join(dest_folder, basename)` then the UNCHANGED copy tail (`src_hash = checker.content_hash(...) if checker is not None else None` — do NOT reuse the cache; `copy_and_hash_verify` semantics stay verbatim). The outer `try/except DestReadCancelled / OSError` handlers stay exactly as they are (the walk deliberately propagates both).
- [ ] **Step 3:** Rewrite `_RsyncTransport.enqueue`: keep stat + eager hash + `queued_src_hashes` dedup; replace the candidate loop with a `try: verdict, basename = _resolve_dest_collision(..., src_hash_fn=lambda: src_hash, claims=batch_st.claimed_basenames, ...) except DestReadCancelled: state.cancelled = True; batch_st.dest_read_cancelled = True; return _ENQ_CANCELLED`; `_WALK_HANDLED` → `return _ENQ_HANDLED`; `_WALK_PLACED` → the UNCHANGED tail (claim placed basename, populate `queued_src_hashes`, append `to_transfer`, `return _ENQ_QUEUED`). Delete the now-dead `adopted` flag and post-loop `state.cancelled` check.
- [ ] **Step 4:** Equivalence audit, PR 7 style: for each deleted walk line, show it verbatim (or itemize the adjustment and which Task 2–4 flip / axis-disposition covers it) in the shared function. Confirm axis-2 divergence intentionally preserved: local passes `claims=None`.
- [ ] **Step 5:** `python -m pytest vireo/tests/test_import_job.py -q` → count identical to end of Task 4, **zero test-file edits in this task**.
- [ ] **Step 6:** Commit: `Extract the shared collision/adopt walk — _resolve_dest_collision (PR 7b)`

### Task 6 (Flip D, the finale): close the source-backed-candidate and dangling-slot holes once

The payoff commit: the walk now exists once, so the safety fix is written once and covers both transports by construction.

- [ ] **Step 1 (RED ×3):**
  1. `test_local_source_backed_suffix_candidate_not_adopted` — dest has `IMG_0001.NEF` (different bytes) and `IMG_0001_1.NEF` = **symlink to the card's** `IMG_0001.NEF`. Today: adopted (`skipped_duplicate == 1` — the data-loss shape: format the card, lose the bytes). Assert instead: `copied == 1`, `skipped_duplicate == 0`, real bytes at `IMG_0001_2.NEF`, a WARNING mentioning the candidate path was logged (use `caplog`).
  2. `test_remote_source_backed_suffix_candidate_not_adopted` — same geometry on the fake mount (mount dir contains the symlink into the card dir). Assert: file queued/transferred as `IMG_0001_2.NEF`, `skipped_duplicate == 0`.
  3. `test_local_dangling_symlink_slot_not_written_through` — dest has `IMG_0001.NEF` (different bytes) and `IMG_0001_1.NEF` = dangling symlink pointing at a nonexistent path INSIDE the card dir. Today: `os.path.exists` false → treated as free → copy writes through the link, bytes land on the card. Assert instead: real file at `IMG_0001_2.NEF`, the card-side target still does not exist, link untouched. **[CORRECTED — see the note in "Also in scope" above: there is no write-through. Today the file FAILS with a misleading "copy verification failed" reason, so the RED expectation is `failed == 1`. Shipped as `test_local_dangling_symlink_slot_is_not_a_free_slot`; the post-fix expectation — the walk advances to the next suffix — is unchanged.]**
  Run all three: expect FAIL with today's adoption/write-through shapes.
- [ ] **Step 2 (GREEN):** In `_resolve_dest_collision` replace the exists-branch opening with:

```python
        if os.path.lexists(cand_path):
            if not os.path.exists(cand_path):
                # Dangling symlink: not adoptable, and landing here would
                # write THROUGH the link (bytes at the target, e.g. on the
                # card) while cataloging cand_path. Never a free slot.
                counter += 1
                continue
            if _candidate_source_backed(ctx, source_file, cand_path):
                log.warning(
                    "%s: destination candidate %s resolves to source "
                    "media; skipping it (never adopt bytes the card "
                    "still owns)", state.log_label, cand_path,
                )
                counter += 1
                continue
            ...  # existing size/zero-byte/hash logic unchanged
        else:
            return _WALK_PLACED, candidate
```

  with a small helper next to `_reject_source_backed_dest` reusing its two probes (`samefile` with the normalized-path `OSError` fallback, then `ctx.path_under_any_source` on the candidate — verify that method realpaths its argument; if not, pass `os.path.realpath(cand_path)`). Do not duplicate the guard's probe bodies — extract/share them if that's cleaner than a second copy.
- [ ] **Step 3:** Update honesty text, all three sites: `_reject_source_backed_dest`'s KNOWN GAP paragraph (gap now closed in the walk — point at `_candidate_source_backed`; state the known-remaining hardlink-to-other-card-file case), the orchestrator comment at `import_job.py:4093-4099`, and the batch-guard comment near `import_job.py:848-852`. Spec row 13 → mark resolved with test names.
- [ ] **Step 4:** Full file run → all green. Commit: `Close the source-backed suffix-candidate and dangling-slot holes in the shared walk (spec decision 13)`

### Task 7: Full verification + PR

- [ ] **Step 1:** `python -m pytest vireo/tests/test_import_job.py -q` — record exact counts (expect 233 + ~9 new).
- [ ] **Step 2:** Required suite: `python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -q` — expect 2077+ passed; the single known env-only failure `test_api_exiftool_status_reports_missing` is acceptable, anything else is not.
- [ ] **Step 3:** `git push -u origin import-unify-pr7b-walk-unification`; `gh pr create --base main` — title: `Import unification PR 7b: one collision walk, and the suffix-candidate data-loss gap closed once`. Body: spec/plan links; the axis-disposition table; flips A–D each with its red-green test names; the write-through discovery; the dropped `_BatchResult` note; exact test counts; end with the `🤖 Generated with [Claude Code](https://claude.com/claude-code)` line. Commits carry `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
