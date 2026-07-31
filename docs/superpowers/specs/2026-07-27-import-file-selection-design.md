# Per-file selection in the import preview

**Date:** 2026-07-27
**Branch:** `import-file-checkboxes`

## Problem

The import page renders a per-file preview grid that looks selectable but isn't.
`previewImport()` (`vireo/templates/import.html:2234`) fetches every discovered
file and `renderImportPreviewGrid()` (`import.html:1927`) draws a thumbnail card
per file — filename, destination path, and a "Duplicate" badge on files the
preflight flagged. The cards have no checkbox and no click handler. Import is
controlled at folder granularity only: the whole folder goes in, or it doesn't.

Two use cases are unserved:

1. **Deselect junk** — drop a handful of test shots or accidental captures from
   an otherwise-good folder.
2. **Cherry-pick** — keep a small subset of a large card dump.

There is a second, independent defect. `startImport()` (`import.html:2401`) never
reads preview state; it posts folder paths and `run_import_job` re-discovers
files from disk (`vireo/import_job.py:2113-2116`). The preview can therefore say
"1,147 to copy" and the job can copy a different number, with no acknowledgement
that the two disagreed. Files added to the source folder between Preview and
Start are imported without ever having been shown to the user.

This violates the "no black boxes" rule in `CORE_PHILOSOPHY.md`: a screen that
displays 1,147 thumbnails and then quietly ignores all of them is a status
display that does not mean what users read it as.

## What already exists

- **The UI pattern.** `vireo/templates/pipeline.html:1687-1965` implements per-file
  checkboxes against the same preview response shape: a `_previewSelected` map,
  `.thumb-check` per card, folder-header checkboxes, select-all with indeterminate
  state, and an "N of M selected" readout.
- **Shift-range selection.** `browse.html:4757` (`selectPhoto`) and
  `review.html:2201-2245` both implement anchor-based shift-click range selection.
- **Backend exclusion (copy mode).** `ingest()` accepts `skip_paths` and filters
  discovered files against it (`vireo/ingest.py:444-445`). The legacy
  `/api/jobs/import-full` (`vireo/app.py:22220`) accepts `exclude_paths`
  (`app.py:22231`) — but the import page does not use that route.
- **Backend restriction (in-place mode).** `/api/jobs/import-in-place` already
  restricts its scan to an explicit file set via `do_scan(..., restrict_files=,
  restrict_dirs=)` (`app.py:23382-23383`). Snapshot mode builds
  `restricted_files` per source at `app.py:23343-23346`; folder mode leaves it
  `None`.
- **Per-file duplicate verdicts.** `/api/import/check-duplicates`
  (`app.py:17630-17693`) streams the exact set of paths `ingest` will skip. The
  frontend drains it into a function-local array, greys out the matching cards,
  and throws the list away (`import.html:2314-2364`).
- **Preview invalidation.** `importPreviewSignature()` (`import.html:2101`) already
  hashes exactly the inputs that determine the file set: sources, snapshot id,
  mode, file types, recursive, and the three duplicate flags.
- **Path-admission precedent.** The in-place snapshot route re-validates every
  frozen path against the workspace's registered roots at enqueue time
  (`app.py:23042-23085`), explicitly so that "a stale/crafted snapshot can never
  use Import as a path-admission escape hatch."
- **Card-safety guard.** `safe_to_format` requires `(copied + skipped_duplicate)
  == discovered` (`import_job.py:3312-3320`), and `partial_scope`
  (`import_job.py:3291-3303`) forces it false when `recursive` or `file_types`
  narrows the walk.

The missing pieces are checkboxes, a selection state object, and an inclusion
field threaded from the page through to the copy-mode job paths.

## Scope: copy mode only

Import spans four execution paths. This spec covers the two copy-mode ones and
explicitly defers the in-place ones:

| Path | Machinery | In scope |
|---|---|---|
| Local copy | `run_import_job` (`import_job.py:1970`) | yes |
| Remote copy (SSH) | `_run_remote_import_job` (`import_job.py:523`, dispatched at 1987) | yes |
| In-place folder | `do_scan(restrict_files=, restrict_dirs=)` | **no** |
| In-place snapshot | same, with a frozen path set | **no** |

The two copy paths share a shape — both are `ImportParams`-driven, both have their
own discovery loop and their own `discovered = len(files)` (2117 and 646
respectively) — so the same filter applies to each. Both must be wired: the page
reaches the remote path whenever `body.remote_target_id` is set
(`import.html:2443`), which `/api/jobs/import-photos` resolves via
`_resolve_remote_archive_target` (`app.py:3071`) into the `remote_target` passed
to `ImportParams`. A remote import that ignored `include_paths` would rsync every
deselected file.

In-place is deferred because it needs a change to the core scanner, not just
wiring. `restrict_files` is only honored inside the `if restrict_dirs is not None:`
branch of `vireo/scanner.py:1618-1663`; the full-walk `else` branch at 1665 never
reads it, and the docstring at 1477-1480 says so ("When provided alongside
`restrict_dirs`"). Passing `restrict_files` alone is a silent no-op that would
import every deselected file. The obvious workaround — also populating
`restrict_dirs` — is worse: it drives
`register_restrict_dirs_as_roots=(snapshot_paths_by_root is None)`
(`app.py:23387`), and `scanner.py:1796-1850` then registers only those leaf
directories as workspace roots, leaving the user's chosen source folder
`is_root=0` and unlinked. Making in-place work correctly means teaching the
scanner to honor `restrict_files` standalone — a riskier edit to shared code, on
the path where the two motivating use cases least apply (in-place targets photos
already sitting in organized folders, not card dumps).

**In in-place mode the selection controls are hidden, not shown and ignored.**
Rendering checkboxes that silently do nothing is the same black-box failure this
spec exists to fix, and would be worse than not having the feature. See §2.

## Design

### 1. Selection state

**User intent and duplicate eligibility are separate structures.** This is the
single most important decision in the spec, and the one an implementer is most
likely to get wrong by reflex.

- `importDeselected` — a `Set` of paths the user **explicitly unchecked**. This is
  the only record of user intent, and the only thing that feeds `include_paths`.
- Duplicate verdicts — a display and eligibility overlay from the preflight
  stream. Never written into `importDeselected`.

Checkbox state is *derived*, never seeded:

```js
checked  = !importDeselected.has(path) && !(isDuplicate && skipDuplicates)
disabled = isDuplicate && skipDuplicates
```

Because the default state is "absent from `importDeselected`", everything renders
checked with no seeding pass at all. That eliminates a class of bugs outright:

- **No seeding race.** `renderImportPreviewGrid()` has four call sites and runs up
  to three times per preview (`import.html:2290`, then `2310` in-place or `2357`
  and `2364` in copy mode). Duplicate verdicts don't exist at first render, but
  since the renderer recomputes `checked` from current state each time rather than
  seeding it, late-arriving verdicts simply change the derived value.
- **No stomping.** A file the user unchecks while the duplicate stream is still
  draining stays unchecked, because their click went into `importDeselected` and
  the duplicate overlay never touches it. (For a path that later turns out to be a
  duplicate, the click is discarded at wire time — see the eligibility filter
  below.)
- **Survives re-render.** The `hide-duplicates-checkbox` branch re-renders from
  cached inputs via `rerenderImportPreviewGrid()`; with intent held separately, a
  hand-picked selection cannot be reset by a view toggle.

Note `available === false` is emitted only by `/api/import/new-images-preview`
(snapshot mode), which is out of scope per *Scope* — so it is unreachable in copy
mode and needs no special handling here.

**`include_paths` is not the set of checked boxes.** It is:

```js
// A deselection only counts if the file was eligible to import in the first
// place. Duplicates are not, so a click on one is discarded here.
eligibleDeselections = skipDuplicates
  ? importDeselected.difference(duplicatePaths)
  : importDeselected
include_paths = previewedPaths - eligibleDeselections
```

The `difference(duplicatePaths)` step is not cosmetic. At the first render
(`import.html:2290`) verdicts have not arrived, so duplicate cards are still
enabled and clickable; a user unchecking one — or catching it in a shift-range —
would write it into `importDeselected`, and nothing later removes it. Without the
filter that path is subtracted from `include_paths`, never reaches the checker,
lands in no ledger bucket, and produces exactly the false "the card still holds the
only copies of them" accusation about a file the archive demonstrably already has.
Filtering at wire time is stateless and covers the window regardless of when the
click landed.

Duplicates therefore **stay in `include_paths`** even though they render unchecked.
This is deliberate and load-bearing — see §5, where excluding them breaks
`safe_to_format` and produces a false accusation against the user. The job already
owns duplicate policy via `skip_duplicates`; `include_paths` must not
double-implement it. The rule is *"everything previewed, minus what the user
actively unchecked"* — a duplicate the user never clicked was not deselected by
them.

The visible count readout is the checkbox count (what will actually be copied),
which is a *subset* of `include_paths`. These two numbers legitimately differ; §2
covers what the user is shown.

**Preview state is four-valued.** "Never previewed", "previewed then invalidated",
and "a preview is running right now" are different situations and must not
collapse:

| State | `startImport()` behavior |
|---|---|
| **No preview run** | `include_paths` omitted → import everything (current behavior) |
| **Preview current** | send `include_paths` + `previewed_count` |
| **Preview stale** — `importPreviewSignature()` differs from the one captured at preview time | **Start disabled**, labelled *"Preview again before importing"* |
| **Preview in flight** | **Start disabled**, labelled *"Previewing…"* |

Discarding a stale selection and silently importing everything is unsafe: the
re-preview is debounced and automatic (`scheduleImportPreview()` at
`import.html:1898`, wired to every signature input by
`wireDestStructureInvalidation` at 731-751), so a user who picks 100 of 5,000
files, toggles a file-type box, and clicks Start before the re-preview lands would
copy all 5,000. Having expressed an intent is not the same as having expressed
none, and the difference is thousands of unwanted files.

**The in-flight state is a distinct hazard, not a rounding of the stale one.**
`previewImport()` calls `clearImportPreviewGrid()` at `import.html:2238` — *before*
the folder-preview fetch, which is the slow disk walk. If selection state were
reset there, the page would sit in "No preview run → import everything" for the
entire walk, and the stale check could not save it because the captured signature
matches the current UI. That is the same 5,000-file failure displaced by a few
hundred milliseconds.

So: **the previous preview's `previewedPaths`, `importDeselected`, and captured
signature are retained untouched until the new render completes.** Selection state
is replaced on success, not cleared on start. The remaining exits from
`previewImport()` must be explicit about which state they leave behind:

| Exit | Resulting state |
|---|---|
| zero files returned (`import.html:2286-2289`) | **preview current, with an empty file set; Start disabled** |
| fetch throws (`2366-2372`) | previous state retained; Start disabled, stale |
| superseded by `importPreviewSeq` or a signature change (`2281`, `2350`, `2363`) | previous state retained; the newer run owns the transition |

A zero-file preview is a *completed* preview that found nothing importable — not
an absence of one. Treating it as "no preview run" would omit `include_paths` and
re-open the unseen-import hole: files landing on the card after that preview would
be imported without ever having been shown.

### 2. Grid interaction

Each `.import-preview-thumb` gains an `<input type="checkbox" class="thumb-check">`.
A global select-all/none sits with the summary.

Each `.import-preview-folder-header` gains a checkbox that toggles its subfolder
and reflects indeterminate state. Note this header is currently built with
`header.textContent = ...` (`import.html:1954`), so it must be restructured into
child elements — appending to it is not sufficient.

**Shift-range.** An `importSelectionAnchor` holds the last-clicked path.
Shift-click sets every card between anchor and target to the target's new value.
The range runs over **visible render order** — the flattened, subfolder-grouped
order actually in the DOM, after any active filter. Ranging over the unfiltered
list would toggle cards the user cannot see. Pattern follows `review.html:2201-2245`.

Every user-driven toggle (individual, folder, select-all, shift-range) adds the
affected paths to — or removes them from — `importDeselected`. Disabled cards are
skipped by folder and select-all toggles; they cannot be deselected because they
are already ineligible.

**Duplicates.** While `Skip duplicates` is on, duplicate cards render unchecked and
`disabled`, badge reading `Duplicate — skipped`. They are already excluded by that
global toggle, so drawing a checked box next to them would promise an import that
will not happen.

With `Skip duplicates` **off**, the duplicate stream does not run at all — the page
returns early at `import.html:2303` and renders the grid with an empty duplicate
list. So there are no verdicts, every card is checked and enabled, and the summary
reads "duplicates will be copied". No special handling is required for this case;
the derived-`checked` rule in §1 produces it naturally.

Because `chkSkipDuplicates` is part of `importPreviewSignature()`, toggling it
invalidates the preview and disables Start until a re-preview completes. A
hand-picked selection is lost across that toggle, which is accepted: the flag
changes which files are eligible at all.

**Start gating.** `#btnStart` is disabled whenever eligibility is unsettled or the
selection cannot be trusted:

- while a preview is in flight (§1), labelled *"Previewing…"*
- while the preview is stale (§1), labelled *"Preview again before importing"*
- while the duplicate stream is still draining — checkbox state is not yet final,
  so submitting mid-stream would send an `include_paths` that doesn't match what
  the user is looking at
- **when the checked count is zero *and at least one file was eligible*,**
  labelled *"No files selected"*

That last one is the real guard against "the user deselected everything," not the
empty-`include_paths` rejection in §3. Because duplicates remain in
`include_paths`, deselecting all 90 importable files on a 100-file card still
yields a *non-empty* list of 10 duplicate paths, which passes shape validation and
runs an import that copies nothing. The checked count is the number that reflects
what the user actually sees and intends; §3's empty-list `400` is a backstop for
malformed clients, not the primary check.

**The eligibility qualifier is load-bearing.** A card whose files are *all* already
archived renders every card unchecked and disabled, so the checked count is zero
through no choice of the user's. Blocking Start there would remove a capability
that exists today: running the import to have those duplicates verified and receive
the safe-to-format verdict — the whole point of pointing Vireo at a card you think
is already backed up. So the gate is `eligibleCount > 0 && checkedCount == 0`.
"Nothing is checked because nothing can be" and "the user unchecked everything" are
different situations with opposite correct behaviors.

**One owner for the disabled state.** `#btnStart.disabled` is currently written
unconditionally from seven places (`import.html:2477`, `2496`, `2535`, `2776`,
`2810`, `2817`, `2842`) — notably `finishJob` at 2535 re-enables it after every
import. Adding four more gating conditions as scattered assignments will race with
those. All of them must route through a single `updateStartGate()` that recomputes
from state and owns both `disabled` and the label.

**Readouts.** A `#previewSelectedCount` element shows `"1,147 of 1,234 selected"`,
counting *checked* cards — the files that will actually be copied, not the larger
`include_paths` set. `#btnStart` relabels to `Start import (1,147 files)` using the
same number. The user is never shown the `include_paths` count; it is an internal
accounting detail (§5), and surfacing a number larger than the checkboxes would be
its own black box.

**In-place mode.** Per *Scope*, all selection affordances — per-card checkboxes,
folder-header checkboxes, select-all, and `#previewSelectedCount` — are hidden
when the mode is in-place or a snapshot. The grid instead carries a single line:

> File selection is available when copying files. In-place import catalogs every
> file in the folder.

Hiding the controls without saying why would leave the user unable to tell whether
selection is missing, broken, or gated behind a setting they haven't found. The
note states what the import *will* do, not merely what is absent.

Switching modes after selecting is already safe: `mode` is part of
`importPreviewSignature()` (`import.html:2106`), so the preview invalidates and
the selection is discarded rather than carried across as stale state.

### 3. Wire protocol

`startImport()` adds three fields to the `/api/jobs/import-photos` POST body only.
`/api/jobs/import-in-place` is unchanged, and the page never sends either field to
it (per *Scope*).

- `include_paths` — `previewedPaths - importDeselected` per §1. **Not** the checked
  boxes: duplicates are present here even though they render unchecked.
- `previewed_count` — an integer: **the number of *unique* paths in the preview
  endpoint's response**, not the response's array length and not the number of
  cards visible in the grid.
- `checked_count` — an integer: the number of cards rendered checked, i.e. the
  files the user was told would be copied.

`previewed_count` must be a unique count because `/api/import/folder-preview`
appends per source folder with no cross-source dedup (`app.py:17500-17506`), and
`total_count` is a plain `len(all_files)`. Nested sources — adding both `/card` and
`/card/DCIM` — emit the same file twice, which would inflate the array length above
the true set size and skew every figure derived from it. Counting the deduplicated
set of `path` values keeps it aligned with `previewedPaths`, `include_paths`, and
`discovered_paths`, all of which are sets.

It also must not be the *displayed* count: those differ once the sibling
`hide-duplicates-checkbox` filter is active, and every hidden duplicate would
inflate the files-appeared figure in §6.

`checked_count` exists because `len(include_paths)` cannot serve as the selected
count — that set deliberately contains unchecked duplicates (§1). Without it the
job cannot report back the same number the UI promised the user, which is the
contract this spec is built on. The server recovers the user-deselected count as
`previewed_count - len(include_paths)`; §5 uses that for the `unsafe_files`
message and `checked_count` for the summary line.

**Known imprecision.** `checked_count` is a snapshot of what the preview promised,
and reality can move underneath it in one direction: a file shown as
"Duplicate — skipped" whose archive twin is deleted between preview and import
stays in `include_paths` (correctly) and *is* copied, so the summary can read "500
selected · 501 copied". This is harmless for card safety — the extra file is
archived, not stranded — but it is a real gap in the "what was checked is exactly
what was imported" contract, and the copy should not be written to imply the two
can never differ.

Three states for `include_paths`:

| State | Meaning |
|---|---|
| key absent | no preview run → import everything (current behavior, unchanged) |
| non-empty list | import exactly these paths |
| empty list | rejected — see below |

A stale preview is **not** one of these states; Start is disabled instead (§1), so
no request is issued at all.

An absent key and an empty list must not collapse into the same case. Absent means
"no opinion"; empty means the client is malformed, since §2 disables Start at zero
*checked* files well before `include_paths` could empty out. The empty-list `400`
is a backstop, not the user-facing guard — see §2 for why the checked count is the
number that actually catches "I deselected everything."

**String form.** `include_paths` carries the client's original path strings
unmodified, matching `ingest()`'s existing `skip_paths` convention. Path resolution
is used only for the containment check in §4 — the stored set must still match
raw `discover_source_files` output by string equality, or the filter matches
nothing and imports zero files.

### 4. Backend validation

`include_paths` and `previewed_count` are untrusted client input in the same class
as the in-place snapshot's frozen path list. All validation is **server-side**; the
client-side checks in §3 are conveniences, not the enforcement point.

**Shape validation**, before anything else, each failure → `400`:

- `include_paths` is a list of non-empty strings (reject non-list, non-string
  members, empty strings, and `null`)
- `include_paths` is non-empty when present
- `previewed_count` and `checked_count` are non-negative integers, with
  `checked_count <= len(include_paths)` — the tighter bound is the correct one,
  since every checked file is by construction in the job's scope; comparing against
  `previewed_count` would admit a client claiming more checked files than it sent
- **the three fields are all-or-nothing.** Requiring the counts whenever
  `include_paths` is present is not sufficient — the converse matters too. A body
  carrying `previewed_count` without `include_paths` would fabricate a
  "files added after preview" figure in §6 and then evaluate
  `include_paths - discovered_paths` against `None`, killing the job with a
  `TypeError`. Reject any partial combination.
- **booleans are rejected for both counts.** `isinstance(True, int)` is `True` in
  Python, so a naive integer check accepts `{"previewed_count": true}` as `1`.
  Test `type(v) is int` or exclude `bool` explicitly.
- `include_paths` is **deduplicated** before any length comparison, and
  `len(set(include_paths)) <= previewed_count` — the selection cannot exceed what
  was previewed. Deduping matters beyond tidiness: §5 derives the deselected count
  from `previewed_count - len(include_paths)`, so a client repeating paths would
  otherwise shrink or invert that figure.

**Containment validation.** Each path must live under one of the request's
already-validated `sources` directories, checked with `os.path.commonpath`. Any
path that does not → `400`. Import must not become a path-admission escape hatch
because a client asked for a file outside the folders the user actually chose.

`os.path.commonpath` **raises** `ValueError` on a mix of absolute and relative
paths rather than returning a verdict, so a client sending a relative string would
produce a `500` instead of the specified `400`. Wrap the call and treat the
exception as a containment failure.

**Containment is lexical: `os.path.normpath` on both sides, never `realpath`.**
This is a deliberate choice with a documented limit, not an oversight.

The threat `include_paths` introduces is a client naming files the user never
chose. Lexical containment catches exactly that: `/src/../etc/passwd` collapses to
`/etc/passwd` and fails `commonpath` against `/src`.

What it does *not* catch is a symlink inside a source pointing outside it. That is
correct behavior here, because it is what happens today: `discover_source_files`
walks with `followlinks` off but still returns symlinked *files*, the preview
endpoint applies no containment filter, and `ingest()` copies them. A user who
points Vireo at a folder containing a link has already chosen to import through it.
Adding `realpath` would newly reject imports that work today, for no gain against
the actual threat.

Resolving symlinks is also unworkable in this position. The server holds only
`sources` and a path list, so a symlinked file inside a source is indistinguishable
from a crafted path pointing outside one — there is no rule that admits the first
and rejects the second. And "drop it and continue" is worse than either: a silently
removed path becomes an invisible deselection, inflating the §5 deselected count
and breaking the promise that a checked file is either imported or accounted for.

So: every containment failure is a `400`, uniformly, and symlinked files inside a
source pass because lexically they are inside it. The policy is stated here so a
future reader does not "fix" it into a regression.

### 5. Job execution

`ImportParams` (`vireo/import_job.py:202`) gains three fields, matching the three
in §3:

```python
include_paths: set | None = None
previewed_count: int | None = None
checked_count: int | None = None
```

The counts are transport for values the job cannot reconstruct: drift is computed
against `discovered_paths`, which only exists inside the job body, and
`checked_count` cannot be derived from `include_paths` because that set contains
unchecked duplicates (§3). `previewed_count` additionally gates the deselection
verdict condition below, so it is load-bearing for card safety, not just reporting.

**Do not persist `include_paths` into `job_config`** (`app.py:23883`). The other
params are recorded there by convention, but there is no re-run-from-config path
today and a 5,000-entry path list would bloat the job row for nothing.

**Both copy paths must be wired.** `run_import_job` delegates to
`_run_remote_import_job` (`import_job.py:523`) at line 1987 whenever
`params.remote_target` is set, and that function has its own duplicated discovery
loop and its own `discovered = len(files)` at `import_job.py:646`, with its own
verdict blocks at 1924-1933 and 1934-1943. The same filter goes in both, at the
same relative position. Wiring only `run_import_job` would leave remote imports
rsyncing every deselected file.

In `run_import_job` the filter goes **after** `discovered = len(files)`
(`import_job.py:2117`) and **before** `DuplicateChecker.prepare()`
(`import_job.py:2124`, inside the `if params.skip_duplicates:` block opening at
2120); nothing between 2117 and 2124 reads `files`. The equivalent position in
`_run_remote_import_job` is immediately after line 646, with its own
`checker.prepare(files)` at 653.

```python
if params.include_paths is not None:
    files = [f for f in files if str(f) in params.include_paths]
```

This position is load-bearing in both directions:

- **After `discovered`** — `discovered` keeps meaning "files actually on the
  card." Filtering above it would shrink `discovered`, satisfy
  `(copied + skipped_duplicate) == discovered`, and report **safe to format** on a
  card whose deselected originals have not been copied anywhere. That is card
  formatting data loss. Placed after, the equality fails on a deselection in the
  ordinary case — but **not in every case**, which is why deselection also gets an
  explicit verdict condition below rather than being left to fall out of the
  arithmetic.
- **Before `prepare()`** — the dedup preflight does not hash files nobody selected.

**Why duplicates must stay in `include_paths`.** `import_job.py:2150` states the
ledger invariant: *"Every discovered file ends in exactly one terminal bucket."*
`skipped_duplicate` is only ever incremented inside the copy loop, which iterates
the filtered `files` list. A file removed by the filter therefore lands in **no**
bucket, and `copied + skipped_duplicate` silently falls short of `discovered`.

If the UI excluded duplicates from `include_paths` — the reflexive
"`include_paths` = the checked boxes" implementation — the ordinary case would
regress:

| 100 files, 10 already in the archive | discovered | copied | skipped_dup | verdict |
|---|---|---|---|---|
| today | 100 | 90 | 10 | ✅ safe to format |
| duplicates excluded from `include_paths` | 100 | 90 | **0** | ❌ unsafe, blames the user |

The user would be told not to format a card that is genuinely fully archived, with
an `unsafe_files` entry accusing them of deselecting ten files they never touched.
Keeping duplicates in `include_paths` lets the checker see them, skip them, and
count them, so the invariant holds:

- deselect nothing → `include_paths` = 100 → 90 copied + 10 skipped = 100 → **safe**
- deselect 20 → `include_paths` = 80 → 70 copied + 10 skipped = 80 ≠ 100 →
  **unsafe**, correctly attributing the gap to the 20 deselected files

**The card-safety warning must explain itself.** Keeping `discovered` honest means
any deselection correctly flips `safe_to_format` to false — but as the code stands
that surfaces as a bare red "Do NOT format the card yet" pill with no reason
given. `unsafe_files` gets no entry for deselected files, and `renderResult` hides
the list entirely when it is empty (`import.html:2633-2634`), so the user sees a
scary warning next to a summary reading "1,234 discovered · 500 copied · 0
duplicates skipped · 0 failed" and no way to connect the two.

An `unsafe_files` entry is therefore required whenever files were deselected, in
**both** copy paths. Entries are `{path, reason}` dicts rendered as
`li.textContent = u.path + ' — ' + u.reason` (`import.html:2636-2638`), so the
entry must be that pair and carries no markup. Follow the existing aggregate-entry
convention (`{"path": "Likely duplicates", ...}` at `import_job.py:3305`):

```python
if params.include_paths is not None and params.previewed_count is not None:
    deselected = params.previewed_count - len(params.include_paths)
    if deselected > 0:
        unsafe_files.append({
            "path": "Deselected files",
            "reason": f"{deselected} files you deselected were not copied",
        })
```

Earlier drafts appended *"— the card still holds the only copies of them"*. That is
false whenever a deselected file is byte-identical to a selected one, which is a
common reason to deselect: the archive does receive those bytes. Asserting it is
the same class of false accusation this section eliminates for catalog duplicates.
The shorter wording is true in every case.

**These entries attribute the gap; they do not fully explain it.** The red pill is
caused by `discovered - (copied + skipped_duplicate)`, while the entries derive
from `previewed_count` and the drift sets — the two coincide only in simple cases.
Deselect X, have X vanish, and have a new file Y arrive: `vanished` is empty (X was
never in `include_paths`), `appeared` clamps to zero, and the only line rendered
reads "1 files you deselected were not copied" — numerically equal to the gap, so
it presents as complete while the actually-unimported file is Y. The implementation
must not claim exhaustiveness in its copy; a fully-attributed ledger would have to
derive from the gap directly rather than from these three signals.

Deriving the count from `previewed_count - len(include_paths)` rather than from
`discovered` is what keeps duplicates and newly-appeared files out of it: both
inflate `discovered`, neither was deselected by the user. `include_paths` is
deduplicated at validation (§4) so a client repeating a path cannot inflate the
figure.

**Files appearing after the preview also need an entry**, for the same reason.
They flip `safe_to_format` false on their own — `discovered` grows while the
filtered `files` list does not — but `deselected` is zero in that case, so the
block above appends nothing and the user gets the bare red pill this section spends
a paragraph prohibiting:

```python
if appeared > 0:
    unsafe_files.append({
        "path": "Files added after preview",
        "reason": f"at least {appeared} files arrived after your preview and "
                  "were not imported — re-preview to include them",
    })
```

This is the same signal §6 reports; routing it through `unsafe_files` as well is
what connects it to the formatting warning it causes.

**Scope note:** these entries close the bare-pill paths *this spec introduces*. Two
pre-existing ones remain and are not in scope — `partial_scope` (`recursive=False`
or a narrowed `file_types`) can produce `safe_to_format=False` with an empty
`unsafe_files`, and the remote path appends its `<remote>` entry only when
`discovered > 0` (`import_job.py:1911-1915`). The tests below assert the new paths,
not universal coverage.

**A vanished in-scope file must force `safe_to_format` false.** The existing
equality cannot catch this on its own. If 100 files are previewed and one
disappears before the job runs, discovery finds 99, `discovered` is 99, the filter
keeps 99, and `copied + skipped_duplicate == 99 == discovered` — a clean **safe to
format** verdict on a card where a file the user asked for was never archived.

**Deselection needs its own condition; the ledger equality does not reliably catch
it.** The equality compares `discovered` against what was processed, and both
shrink together when a deselected file also disappears from the card:

- preview finds 100, user deselects X → `include_paths` = 99
- X vanishes before the job runs
- discovery finds 99, all of them in `include_paths`, all processed
- `copied + skipped_duplicate == 99 == discovered` — equality **holds**
- `vanished_paths` is empty, because X was never in `include_paths`

Both verdict blocks then pass, and the green branch sets
`unsafe.style.display = 'none'` (`import.html:2611`), hiding the "1 file
deselected" entry that was computed. The user is told *"every file is verified in
the archive"* about an import where one file deliberately wasn't.

So `deselected == 0` becomes an explicit condition, where
`deselected = previewed_count - len(include_paths)`. This is the same value §5
already computes for the `unsafe_files` entry; it just has to gate the verdict too
rather than only annotate it.

The general lesson, having now produced two data-loss paths this way: **every
safety-relevant intent in this feature must be its own condition.** Relying on the
ledger equality to catch things emergently works until two errors cancel.

**`not vanished_paths` and `deselected == 0` must both be added to *two* condition
blocks per path, not one.**
Each copy path computes two verdicts with nearly identical conditions:

| Path | `safe_to_format` | `unverified_duplicates_only` |
|---|---|---|
| local | `import_job.py:3312-3320` | `3321-3329` |
| remote | `1924-1933` | `1934-1943` |

Every other condition this spec relies on — deselection, files appearing — breaks
`(copied + skipped_duplicate) == discovered`, which **both** blocks require, so
they are covered incidentally. `vanished_paths` is the only new condition
independent of that equality, so patching `safe_to_format` alone leaves it live in
the second block.

That is a data-loss path, not a cosmetic gap. `unverified_duplicates_only` renders
as an amber pill reading *"Import complete — keep the card until likely duplicates
are verified"* (`import.html:2613`), which asserts that duplicate verification is
the **sole** remaining blocker. With `trust_likely_duplicates` on and one in-scope
file gone at job time, `safe_to_format` correctly goes false but the amber pill
appears — and its stated remedy is to re-run with `verify_by_hash`. On that re-run
the vanished file is absent from the fresh preview too, so `include_paths ==
discovered_paths`, everything verifies, and the pill goes **green** over a card
whose missing file was never archived.

With that covered, the condition and its entry:

```python
if vanished_paths:
    unsafe_files.append({
        "path": "Files missing at import time",
        "reason": f"{len(vanished_paths)} files were in scope but had "
                  "disappeared from the source when the import ran",
    })
```

Conservatism is right here even though a vanished file is, by definition, no longer
on the card. The job cannot distinguish "the user deleted it" from a read error or
failing media — and in the latter case the bytes may still be physically present
and recoverable until a format destroys them. Declaring a card safe to erase is the
most destructive assertion this feature makes; it should fail closed. This matches
how `partial_scope` and `unverified_duplicate` already behave.

Note every `unsafe_files` entry is also mirrored into `result["errors"]`
(`import_job.py:1965`, `3353`), so this wording appears in two places.

Without it, the warning is exactly the black box `CORE_PHILOSOPHY.md` prohibits:
technically correct, and unreadable as to why.

**Progress runs on the selected workload; safety accounting stays on the full
card.** These are different denominators and conflating them breaks one or the
other. `_emit(..., emitted, discovered)` currently uses `discovered`, so a
half-deselected import would run to completion with the bar stalled near 50% — a
finished job that looks hung. Progress must instead use `len(files)` *after* the
filter, the work actually queued.

**Both copy paths again.** The `_emit` call sites are `import_job.py:2348` and
`3200` locally, and **806, 830, 904, 1847** in `_run_remote_import_job`; the step
summaries are `3245` and **1879**. Wiring only the local set leaves remote imports
with the stalled bar.

`discovered` continues to back `safe_to_format`, and the step summary should name
both so the difference is legible rather than mysterious. The selected figure comes
from `checked_count` (§3), **not** `len(include_paths)` — that set contains
unchecked duplicates and would overstate what the user chose:

> 500 selected of 1,234 discovered · 500 copied · 0 failed

### 6. Drift reporting

`discovered_paths` is snapshotted from the discovery result **before** the §5
filter runs — computing it afterwards would make files-appeared structurally zero.
Both signals are skipped entirely when `previewed_count is None` (no preview ran),
which also avoids arithmetic against the field's declared default.

The naive `discovered_paths - include_paths` is **not** drift — it is dominated by
files the user deliberately deselected. Reporting it as "files were added to the
source folder" would state something false on every ordinary deselection, which is
precisely the failure mode this spec opens by citing. The two honest signals are:

- **Files appeared.** `max(0, len(discovered_paths) - previewed_count)`. An integer
  suffices — identifying *which* files appeared would require shipping the full
  previewed path set, and the actionable message ("re-preview") is the same either
  way.

  This is a **net delta, and therefore a lower bound**, not an exact count. If 5
  files appear and 3 vanish it reports 2; if more vanish than appear it would go
  negative, hence the clamp. The wording must not overclaim, and the message is
  phrased "at least N". Computing an exact figure would require shipping the
  previewed path set, which §Out of scope rejects for the same reason as above.
- **Files vanished.** `include_paths - discovered_paths`, an exact path set: files
  that were **in scope** and are no longer on disk. The wording matters —
  `include_paths` contains unchecked duplicates (§1), so this set can include a
  file the user was shown as *not* being copied. Calling these "files you selected"
  would be false for exactly those paths. A *deselected* file that vanished is
  invisible to both signals, which is acceptable since the user had already
  declined to import it.

Both are carried in the job result and surfaced on the result card:

> At least 20 files were added to the source folder after your preview and were
> not imported. Re-preview to include them.

> 3 files were in scope for this import but had disappeared from the source when
> it ran.

The vanished set also forces `safe_to_format` false; see §5 for why that has to be
a separate condition rather than falling out of the ledger equality.

Drift reporting is copy-mode only, following *Scope*. The in-place route never
materializes a discovered-path set — `do_scan` does not return one, and its result
dict reports `discovered` as the *indexed* count rather than a card enumeration
(`app.py:23520-23528`) — so there is nothing to compare against there.

Drift is **reported, not blocked**. Refusing to start on a folder that is still
receiving files would make import unusable during a card transfer. Reporting keeps
the preview grid an honest contract — what was checked is exactly what was
imported — while naming anything that diverged.

This also repairs the pre-existing preview/job mismatch described in *Problem*,
independent of checkboxes.

## Testing

**Backend** (`vireo/tests/test_jobs_api.py`, new `vireo/tests/test_import_job.py` cases):

- include-path filtering selects exactly the requested files
- absent key imports everything; empty list is rejected
- **shape validation**: non-list, non-string members, empty strings, negative or
  missing `previewed_count`, and `len(include_paths) > previewed_count` each `400`
- path-escape rejection → `400`: `../` traversal, sibling directory, absolute path
  outside every source
- **a symlinked file inside a source still imports successfully** — lexical
  containment admits it, matching today's `ingest()` behavior; this is the
  regression guard against someone "hardening" §4 with `realpath`
- a source directory itself reached through a symlink still imports successfully
- **booleans are rejected for `previewed_count` / `checked_count`** — `{"previewed_count": true}`
  must `400`, not be coerced to `1`
- overlapping sources (`/card` and `/card/DCIM`) yield a `previewed_count` equal to
  the unique path count, and the derived deselected count stays correct
- **`safe_to_format` is true when a card containing duplicates is fully selected**
  — the 90-copied/10-skipped case; this is the regression guard for the duplicate
  accounting rule, and it fails if duplicates are dropped from `include_paths`
- **`safe_to_format` is false when any file is deselected**, and the
  `unsafe_files` entry names the deselected count and no more — duplicates and
  newly-appeared files must not inflate it
- **a duplicate unchecked before verdicts arrive still reaches the checker** —
  `include_paths` retains it via the eligibility filter, so a fully-archived card
  stays safe to format; this is the regression guard for the pre-verdict click
  window
- **files appearing after the preview produce their own `unsafe_files` entry**, so
  none of the paths this spec introduces leaves the red pill bare
- **a vanished in-scope file forces `safe_to_format` false** even though the ledger
  equality still balances — the 100-previewed/99-discovered case, with its own
  `unsafe_files` entry
- **deselect one file, then delete that same file before the job runs** — the
  ledger equality still balances (99 processed of 99 discovered) and
  `vanished_paths` is empty, so both verdicts must be forced false by the explicit
  `deselected == 0` condition rather than by arithmetic; assert across both blocks
  and both copy paths
- **the same case also forces `unverified_duplicates_only` false** with
  `trust_likely_duplicates=True` — the amber "keep the card until likely duplicates
  are verified" pill must not appear, since its remedy launders the missing file
  into a green verdict. Assert on **both** verdict blocks in **both** copy paths
  (`3312-3320`/`3321-3329` local, `1924-1933`/`1934-1943` remote); this is the
  data-loss regression guard
- partial field combinations `400`: `previewed_count` without `include_paths`,
  `include_paths` without the counts
- a relative path in `include_paths` returns `400`, not a `500` from
  `commonpath`'s `ValueError`
- the step summary's selected figure comes from `checked_count`, not
  `len(include_paths)`, so a card with duplicates doesn't overstate it
- a client repeating a path in `include_paths` does not inflate the deselected
  count (dedupe at validation)
- `discovered` reports full card contents, not the selected subset
- progress emits against the filtered workload, so a half-deselected import
  reaches 100%
- both drift signals populate correctly; an ordinary deselection reports *zero*
  files-appeared, and a mixed appear/vanish case never goes negative
- drift signals are skipped entirely when no preview ran (`previewed_count is None`)
- filtering happens before `DuplicateChecker.prepare()`
- **the remote path (`_run_remote_import_job`) honors `include_paths`** — the full
  set of assertions above, including `safe_to_format`, the duplicate accounting
  case, and the `unsafe_files` entry; a regression here silently rsyncs deselected
  files
- `include_paths` posted to `/api/jobs/import-in-place` is ignored, never silently
  half-applied (that route is unchanged by this spec)

**Frontend** (`tests/e2e/test_import_page.py`): the existing Playwright suite has a
`live_server`/`page` fixture pair and an established pattern for stubbing
`/api/import/folder-preview` via `window.fetch` override.

- checkboxes render and default to checked
- duplicates flip to unchecked and disabled when the duplicate stream completes,
  not before
- a file unchecked while the duplicate stream is draining stays unchecked
- with `Skip duplicates` off, no duplicate stream runs and every card is checked
  and enabled
- folder-header checkbox toggles its subfolder and shows indeterminate state;
  disabled duplicate cards are skipped by folder and select-all toggles
- select-all / select-none
- shift-click selects a contiguous range in visible order
- selection survives a `rerenderImportPreviewGrid()` (the "Hide duplicates" filter)
  — **conditional on merge order**; `rerenderImportPreviewGrid` /
  `lastImportPreviewRender` do not exist on this branch yet (see §Coordination)
- `#btnStart` label reflects the *checked* count, not the `include_paths` count
- **Start is disabled while the duplicate stream is draining**
- **Start is disabled while a preview is in flight**, and the prior selection is
  still intact when that preview completes
- **Start is disabled when every importable file is unchecked**, even though
  `include_paths` is non-empty because duplicates remain in it
- **Start stays *enabled* on a card where every file is a duplicate** — zero
  checked, zero eligible, and the user must still be able to run it for the
  safe-to-format verdict
- **a zero-file preview disables Start** rather than reverting to "no preview run"
- **changing a source after selecting disables Start with "Preview again before
  importing"** — it must not fall back to importing everything
- **in-place mode hides all selection controls and shows the explanatory note**
- switching from copy to in-place after selecting disables Start the same way

## Out of scope

- **Grid virtualization.** `/api/import/folder-preview` (`app.py:17443`) returns
  every discovered file with no cap, and the renderer builds a card per file —
  roughly 30k DOM nodes at 5k files. This is a pre-existing limit that checkboxes
  do not worsen, though they do make users more likely to scroll the full list.
  Revisit if 10k+ file cards become routine.
- **Filter-then-bulk-select** (select none, filter by extension or date, bulk
  check). Speculative; extension filtering is already partly served by the
  file-type checkboxes on the source card.
- **Per-file duplicate override** (checking a duplicate to force its import).
  Would put two controls in charge of one decision.
- **Identifying which files appeared** after a preview (§6), and reporting an exact
  rather than net count. Both require shipping the full previewed path set for no
  change in the user's available action.
- **Unsupported extensions in the preview grid.** `discover_source_files`
  (`vireo/ingest.py:333-394`) filters to `SUPPORTED_EXTENSIONS` and drops dotfiles,
  so `.MOV`/`.MP4`/`.XMP` on a card never enter `discovered` and the green
  safe-to-format pill can appear over a card still holding un-copied video. This
  predates the feature, but a grid of 1,234 checkboxes with an "N of M selected"
  readout materially strengthens the implication that the display enumerates the
  card — worth a follow-up under the `CORE_PHILOSOPHY.md` transparency rule, and
  worth revisiting here if any of that copy is touched.
- **In-place selection** (both folder and snapshot modes). Deferred per *Scope*;
  needs `vireo/scanner.py` taught to honor `restrict_files` without
  `restrict_dirs`. Controls are hidden with an explanation rather than shown and
  ignored, so this is a bounded follow-up: the frontend state, wire protocol, and
  validation from this spec all carry over.

## Coordination

The `hide-duplicates-checkbox` branch (worktree `banjul`) modifies
`renderImportPreviewGrid()` and `clearImportPreviewGrid()` in
`vireo/templates/import.html` and adds `lastImportPreviewRender` /
`rerenderImportPreviewGrid()`. The derived-checkbox-state decision in §1 exists
specifically to compose with it. Whichever branch merges second must verify:

- toggling "Hide duplicates" preserves `importDeselected`
- `previewed_count` still reflects the preview endpoint's response size, not the
  post-filter visible count (§3)
- **the early return at `import.html:2303` still holds.** §1 and §2 depend on the
  duplicate stream not running when `Skip duplicates` is off. If that branch — or
  any other — makes the stream unconditional so the filter has verdicts to work
  with, this spec's duplicate handling must be revisited before merge, because
  duplicate verdicts would then exist in a mode where they must not affect
  eligibility.
