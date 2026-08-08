# Import Unification PR 5b: Run-State Object + Helper Hoist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move both import paths' run-scoped closure state into a shared `_ImportRunState` dataclass and hoist the four byte-identical(-modulo-log-wording) state-manipulating helpers to module level — a purely mechanical, no-behavior-change refactor that unlocks PR 6's phase extraction.

**Architecture:** Second half of the spec's PR 5 split (recorded in the spec). The risk profile is the OPPOSITE of 5a's: no behavior flips, but ~350 one-token edits where a single missed `state.` prefix silently creates a dead function-local (Python won't complain; `_emit`'s ETA would read stale state). The defenses: per-path conversion commits with identical suite counts, and a mandatory grep audit of every migrated name as the review artifact. Log-only wording changes (documented below) are the sole observable deltas.

**Tech Stack:** `dataclasses` (`field(default_factory=...)`); pytest.

---

## Context for a zero-context engineer

- Repo root: `/Users/julius/conductor/workspaces/vireo/nagoya`; branch `import-unify-pr5b-run-state` (checked out, tracks origin/main at the #1438 merge `0e3e32a8`). Run tests from repo root; commit per task. Baseline: `python -m pytest vireo/tests/test_import_job.py -q` → **225 passed, 1 skipped** — this exact count must hold after EVERY task.
- `vireo/import_job.py`: remote `_run_remote_import_job` def 1252; local `run_import_job` def 3180. Line numbers below verified at `0e3e32a8`; re-grep before editing.
- **Nested helpers (current def lines).** Remote: `_probe_dir_case_insensitive` 1325, `_path_under_destination` 1340, `_fold_basename` 1359, `_emit` 1381, `_emit_transfer` 1416, `_discovery_onerror` 1468, `_stop_requested` 1549, `_counts` 1576, `_fail` 1581 (`nonlocal failed` 1582), `_reclassify_landed_failed` 1588 (`nonlocal copied, verified, skipped_duplicate` 1600), `_missing_mount_root` 1662, `_record_checker` 1665, `_do_rsync` 2414, `_rsync_cancelled` 2439. Local: probe 3280, path-under 3295, `_emit` 3322, `_discovery_onerror` 3368, `_stop_requested` 3452, `_counts` 3515, `_fail` 3520 (nonlocal 3521), `_reclassify_landed_failed` 3527 (nonlocal 3539), `_record_checker` 3552, `_src_hash_cached` 4062, `_rehash_dest_or_none` 4392.
- **This PR hoists exactly FOUR helpers**: `_counts`, `_fail`, `_reclassify_landed_failed`, `_record_checker` — the pure state manipulators, byte-identical between paths except two LOG-ONLY wordings. Everything else (emitters, probes, transport bits) stays nested; PR 6 owns those.
- **Migrated state names (18)** — run-scoped, present in BOTH functions, moving into `_ImportRunState`: `copied`, `verified`, `skipped_duplicate`, `unverified_duplicate`, `failed`, `emitted`, `cancelled`, `unsafe_files`, `folder_counts`, `discovery_errors`, `imported_photo_ids`, `linked_dup_dirs`, `dup_link_failed`, `run_dest_folders`, `run_verified_hashes`, `mount_ever_lost`, `wc_source_paths`, `wc_dest_folders`. NOT migrated (stay as locals): `eta`, `checker`, `queued`, `batches`, `destination`, `mount_baseline`, per-batch state (`landed`, `dup_dirs`, `dup_skips`, `mount_lost`, `dest_read_cancelled`, `reclassified_landed_paths`, `to_transfer`, `claimed_basenames`, `queued_src_hashes`, remote transfer vars), and `verified_counted_for_copies` (loop-invariant local — see below).
- **The shadow hazard:** once the bare initializations are deleted, a missed `copied += 1` fails LOUDLY (`UnboundLocalError`); the genuinely SILENT misses are the plain rebinds — `cancelled = True`, `dup_link_failed = True`, `mount_ever_lost = mount_lost` — which quietly create a dead function-local while `state.<name>` keeps its stale value. This is why the audit commands in Task 4 are mandatory, not advisory.
- **Log-only wording changes (the only observable deltas; list in the PR body):**
  1. `_record_checker`'s OSError warning unifies to `"Duplicate-checker record() failed for %s (dest %s): %s"` — the local variant said "after landing at %s", which is wrong for the remote's enqueue-time skip callers (flagged in PR #1433's body for exactly this reconciliation).
  2. `_fail`'s warning keeps its per-path prefix via `state.log_label` (`"Remote import"` / `"Import"`), so log lines stay byte-identical.
- **Carry-overs from 5a's reviews (all no-behavior-change, land in Task 3):**
  1. Booking/rollback pairing: the remote `verified` booking site (`if params.verify_by_hash: verified += 1` — the one in the post-transfer accounting loop at 2542-2543, right after the `copied` booking; NOT 2517, the `remote_verify_files` verification guard, and NOT 2775, the sole remote `update_photo_hash_check` stamp site) switches to `if verified_counted_for_copies:` so increment and decrement share one predicate (PR 7's `attests_bytes` then switches both at once).
  2. `verified_counted_for_copies` moves OUT of the per-batch loop to just before it (it's loop-invariant; today the remote helper at 1588 closes over a name first bound at 1897 inside the loop — works via late binding, needlessly fragile).
  3. Local `reclassified_landed_paths` decl moves up into the per-batch state block (mirroring remote's placement).
  4. `_LandedFile` gains `frozen=True` (entries are never mutated by design — the reclassified set exists precisely to avoid mutation).
- No test monkeypatches or spies target any nested helper (they can't — closures aren't patchable); test-side risk is nil beyond behavior itself.

### Task 0: Sanity

- [ ] `git branch --show-current` → `import-unify-pr5b-run-state`; baseline full file → 225 passed, 1 skipped.

### Task 1: `_ImportRunState` + LOCAL conversion

**Files:** Modify `vireo/import_job.py`.

- [ ] **Step 1:** Add at module level, directly below `_LandedFile`:

```python
@dataclass
class _ImportRunState:
    """Run-scoped mutable state shared by the import batch loop and its
    helpers. One instance per job run; batch-scoped state (``landed``,
    ``dup_skips``, collision maps, …) deliberately stays in function
    locals until the PR 6 phase extraction. ``log_label`` keeps the two
    paths' failure log lines byte-identical after the helper hoist.
    """
    log_label: str
    copied: int = 0
    verified: int = 0
    skipped_duplicate: int = 0
    unverified_duplicate: int = 0
    failed: int = 0
    emitted: int = 0
    cancelled: bool = False
    # None or a mount-root path string; consumers are truthiness-only
    # plus one f-string interpolation of the path into a failure reason.
    mount_ever_lost: str | None = None
    dup_link_failed: bool = False
    unsafe_files: list = field(default_factory=list)
    folder_counts: dict = field(default_factory=dict)
    discovery_errors: list = field(default_factory=list)
    imported_photo_ids: set = field(default_factory=set)
    linked_dup_dirs: set = field(default_factory=set)
    run_dest_folders: dict = field(default_factory=dict)
    run_verified_hashes: dict = field(default_factory=dict)
    wc_source_paths: dict = field(default_factory=dict)
    wc_dest_folders: set = field(default_factory=set)
```

(`from dataclasses import dataclass, field` — extend the existing import.)

- [ ] **Step 2:** LOCAL function: create `state = _ImportRunState(log_label="Import")` where the first migrated name is currently initialized; delete the 18 bare initializations; convert every read/write in the function body AND in the local nested helpers (`_emit` reads `copied`/`folder_counts`; `_counts` mutates `folder_counts`; `_fail` mutates `failed`/`unsafe_files`; `_reclassify_landed_failed` mutates three counters; `_record_checker` mutates the two run maps; `_discovery_onerror` appends `discovery_errors`) to `state.<name>`; delete the two local `nonlocal` lines (3521, 3539). The result-dict construction and `_selection_summary`/`_emit` call sites read `state.<name>`. Do NOT touch the remote function in this task.
- [ ] **Step 3:** `python -c "import sys; sys.path.insert(0, 'vireo'); import import_job"` (syntax/scope smoke), then full file → **225 passed, 1 skipped** (identical). Any delta = a missed or extra conversion; stop and find it.
- [ ] **Step 4:** Commit: `"Local import path: run-scoped state moves into _ImportRunState"`

### Task 2: REMOTE conversion

- [ ] Same procedure for `_run_remote_import_job` with `state = _ImportRunState(log_label="Remote import")`: 18 names, the remote nested helpers (`_emit`, `_emit_transfer` — reads `folder_counts` —, `_counts`, `_fail`, `_reclassify_landed_failed`, `_record_checker`, `_discovery_onerror`), delete `nonlocal` lines 1582/1600. Full file → 225 passed, 1 skipped. Commit: `"Remote import path: run-scoped state moves into _ImportRunState"`

### Task 3: Hoist the four helpers + carry-overs

- [ ] **Step 1:** Add module-level defs (place after `_ImportRunState`); bodies are the current nested ones with `state.` access and explicit params:

```python
def _counts(state, rel):
    return state.folder_counts.setdefault(
        rel, {"copied": 0, "skipped_duplicate": 0, "failed": 0})


def _fail(state, rel, source_file, reason):
    <current body: state.failed += 1; state.unsafe_files.append(...);
     _counts(state, rel)["failed"] += 1;
     log.warning("%s failed for %s: %s", state.log_label, source_file, reason)>


def _reclassify_landed_failed(state, rel, entry, reason,
                              verified_counted_for_copies):
    <current body, switching on entry.origin, decrementing via state,
     gating verified on the parameter, ending with
     _fail(state, rel, entry.dest_path, reason)>


def _record_checker(state, checker, source_file, dest_folder, file_hash):
    <current body; unified OSError warning:
     "Duplicate-checker record() failed for %s (dest %s): %s">
```

Copy the current docstrings, merging where they differ (keep the PR #1113 rationale; drop the stale "after landing" phrasing). Delete all eight nested defs; update every call site (`_counts(rel)` → `_counts(state, rel)`, etc. — grep counts before/after: the number of call sites must not change). CAREFUL with `_fail`'s log line: currently `"Remote import failed for %s: %s"` / `"Import failed for %s: %s"` — the hoisted form `"%s failed for %s: %s", state.log_label` reproduces both byte-for-byte.
- [ ] **Step 2 (carry-overs):** booking-site predicate switch (`if verified_counted_for_copies:` at the remote post-transfer `verified` booking); move both `verified_counted_for_copies` decls out of the batch loops to just before them; move local `reclassified_landed_paths` decl into the per-batch state block; add `frozen=True` to `_LandedFile` (run the suite — if anything mutates entries, that's a finding: revert the freeze, report it, and leave a comment).
- [ ] **Step 3:** Full file → 225 passed, 1 skipped. Commit: `"Hoist the shared state helpers to module level; 5a review carry-overs"`

### Task 4: Audit + verification + PR

- [ ] **Step 1 (the audit — paste results into the PR body):** for each of the 18 migrated names, inside BOTH function ranges, every remaining bare occurrence must be a comment, docstring, keyword argument name, or dict key — never a load/store of a local. Command shape:

```bash
for n in copied verified skipped_duplicate unverified_duplicate failed emitted cancelled unsafe_files folder_counts discovery_errors imported_photo_ids linked_dup_dirs dup_link_failed run_dest_folders run_verified_hashes mount_ever_lost wc_source_paths wc_dest_folders; do
  echo "== $n"; grep -nE "(^|[^.\w\"'])${n}\b" vireo/import_job.py | grep -vE "state\.${n}|def |#|\"|'" ; done
```

Review every hit by eye (e.g. `copied=` keyword args to `_selection_summary` are fine; a bare `cancelled = True` is the bug). Also: `grep -n "nonlocal" vireo/import_job.py` → expect zero hits in the two functions. Belt-and-braces (catches what the grep's comment/quote filters could hide): a `symtable` check asserting none of the 18 names remain locals of either function:

```bash
python3 - <<'PY'
import symtable
src = open('vireo/import_job.py').read()
names = set('copied verified skipped_duplicate unverified_duplicate failed emitted cancelled unsafe_files folder_counts discovery_errors imported_photo_ids linked_dup_dirs dup_link_failed run_dest_folders run_verified_hashes mount_ever_lost wc_source_paths wc_dest_folders'.split())
top = symtable.symtable(src, 'import_job.py', 'exec')
for fn in ('_run_remote_import_job', 'run_import_job'):
    t = top.lookup(fn).get_namespace()
    bad = sorted(n for n in names if n in set(t.get_identifiers())
                 and t.lookup(n).is_local())
    print(fn, 'OK' if not bad else f'LEAKED LOCALS: {bad}')
PY
```

Both lines must print OK.
- [ ] **Step 2:** Full file (225/1) + required CLAUDE.md suite (expect 2077 passed, 14 skipped, 1 known env failure).
- [ ] **Step 3:** Push; `gh pr create --base main --title "Import unification PR 5b: run-state object and helper hoist" --body ...` — body covers: the 5a/5b split pointer; the 18 migrated names; the four hoisted helpers with the two log-wording notes; the four 5a-review carry-overs; the audit output; "no behavior change — suite counts identical at every commit"; notes for PR 6 (the emitters and probes are next; `_reclassify_landed_failed`'s `verified_counted_for_copies` param is the seam PR 7's `attests_bytes` replaces). End with the Claude Code attribution line.
