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
| zero files returned (`import.html:2286-2289`) | no preview run; Start enabled (nothing to select) |
| fetch throws (`2366-2372`) | previous state retained; Start disabled, stale |
| superseded by `importPreviewSeq` or a signature change (`2281`, `2350`, `2363`) | previous state retained; the newer run owns the transition |

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
- **when the checked count is zero**, labelled *"No files selected"*

That last one is the real guard against "the user deselected everything," not the
empty-`include_paths` rejection in §3. Because duplicates remain in
`include_paths`, deselecting all 90 importable files on a 100-file card still
yields a *non-empty* list of 10 duplicate paths, which passes shape validation and
runs an import that copies nothing. The checked count is the number that reflects
what the user actually sees and intends; §3's empty-list `400` is a backstop for
malformed clients, not the primary check.

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

`startImport()` adds two fields to the `/api/jobs/import-photos` POST body only.
`/api/jobs/import-in-place` is unchanged, and the page never sends either field to
it (per *Scope*).

- `include_paths` — `previewedPaths - importDeselected` per §1. **Not** the checked
  boxes: duplicates are present here even though they render unchecked.
- `previewed_count` — an integer: **the number of files in the preview endpoint's
  response**, not the number of cards visible in the grid. These differ once the
  sibling `hide-duplicates-checkbox` filter is active, and defining it as
  "displayed" would make every hidden duplicate inflate the files-appeared count in
  §6. Needed for honest drift reporting; see why an integer suffices there.

Because `include_paths = previewedPaths - importDeselected`, the server can recover
the user-deselected count as `previewed_count - len(include_paths)` without a third
field. §5 uses this for the `unsafe_files` message.

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
- `previewed_count` is a non-negative integer, and is present whenever
  `include_paths` is
- `include_paths` is **deduplicated** before any length comparison, and
  `len(set(include_paths)) <= previewed_count` — the selection cannot exceed what
  was previewed. Deduping matters beyond tidiness: §5 derives the deselected count
  from `previewed_count - len(include_paths)`, so a client repeating paths would
  otherwise shrink or invert that figure.

**Containment validation.** Each path must live under one of the request's
already-validated `sources` directories, checked with `os.path.commonpath`. Any
path that does not → `400`. Import must not become a path-admission escape hatch
because a client asked for a file outside the folders the user actually chose.

**Resolve both sides with `os.path.realpath` for the containment check, and keep
the raw string for discovery matching.** Lexical `normpath` cannot detect a symlink
escape — a link inside the source pointing outside it normalizes to a path that
still appears contained, which would defeat the check entirely. Resolving *both*
the candidate and the source directory keeps symlinked mount points working (the
failure mode is only present when one side is resolved and the other isn't), while
actually catching escapes. The resolved form is used for comparison only; the
original string is what goes into `ImportParams.include_paths`, because §5 matches
it against unresolved `discover_source_files` output.

**A symlinked file that escapes the source is dropped, not fatal.**
`discover_source_files` walks with `followlinks` off but still *returns* symlinked
files inside a source, and the preview endpoint applies no containment filter — so
such a file is displayed, checked by default, and would arrive in `include_paths`
through no fault of the user. Rejecting the whole request with a `400` would break
an import that works today. Drop the offending path from `include_paths`, log a
warning, and proceed. Escapes that indicate a crafted client — paths that were
never in any source at all — still `400`; the distinction is whether the path was
reachable from a source the user chose.

### 5. Job execution

`ImportParams` (`vireo/import_job.py:202`) gains two fields:
`include_paths: set | None = None` and `previewed_count: int | None = None`. The
latter is the transport for §6 — drift is computed against `discovered_paths`,
which only exists inside the job body, so the count has to travel with the params.

**Do not persist `include_paths` into `job_config`** (`app.py:23883`). The other
params are recorded there by convention, but there is no re-run-from-config path
today and a 5,000-entry path list would bloat the job row for nothing.

**Both copy paths must be wired.** `run_import_job` delegates to
`_run_remote_import_job` (`import_job.py:523`) at line 1987 whenever
`params.remote_target` is set, and that function has its own duplicated discovery
loop and its own `discovered = len(files)` at `import_job.py:646`, with its own
`safe_to_format` at 1932/1942. The same filter goes in both, at the same relative
position. Wiring only `run_import_job` would leave remote imports rsyncing every
deselected file.

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
  formatting data loss. Placed after, the equality fails automatically on any
  deselection — and still holds when the user selected everything, which is
  genuinely safe. No new `partial_scope` flag is needed; the existing guard does
  the right thing on its own.
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
            "reason": f"{deselected} files you deselected were not copied — "
                      "the card still holds the only copies of them",
        })
```

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

`discovered` continues to back `safe_to_format` unchanged, and the step summary
should name both so the difference is legible rather than mysterious:

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
  the user selected that are no longer on disk. Note this covers *selected* files
  only — a deselected file that vanished is invisible to both signals, which is
  acceptable since the user had already declined to import it.

Both are carried in the job result and surfaced on the result card:

> At least 20 files were added to the source folder after your preview and were
> not imported. Re-preview to include them.

> 3 files you selected were no longer on disk when the import ran.

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
- path-escape rejection: `../` traversal, **symlink pointing outside the source**,
  sibling directory — the symlink case must fail containment, which is what forces
  `realpath` over `normpath`
- a source directory that is itself reached through a symlink still imports
  successfully (the regression `realpath`-ing only one side would cause)
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
  the red pill is never bare
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
