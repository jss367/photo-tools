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

A module-level `importSelected` map of `path -> bool`, mirroring
`_previewSelected` in `pipeline.html:1687`, plus an `importSelectionTouched` Set
of paths the user has explicitly clicked.

**Seeding is separate from rendering, and happens in two stages.** This matters
because `renderImportPreviewGrid()` has four call sites and runs up to three times
per preview — `import.html:2290` (files only), then either `2310` (in-place, with
destination data) or `2357` (copy, with duplicate verdicts) and `2364` (both).
Duplicate verdicts do not exist at first render.

- **Stage 1**, when the file list arrives: seed every path not already in
  `importSelected` to `true`, except files with `available === false`, which seed
  `false`.
- **Stage 2**, when the duplicate stream completes **and only if `chkSkipDuplicates`
  is checked**: flip every duplicate path to `false` unless it is in
  `importSelectionTouched`. Checking the touched set keeps this from stomping a
  deliberate choice made while the stream was draining.

  The `chkSkipDuplicates` gate is essential. The duplicate stream at
  `import.html:2318` runs on *every* copy-mode preview — it is not conditioned on
  that checkbox, because the preview needs duplicate verdicts to render badges
  either way. Deselecting duplicates unconditionally would mean that with Skip
  duplicates **off**, `include_paths` omits the very files the user turned the
  flag off in order to import, and they are dropped with no indication. With the
  flag off, duplicates stay checked and enabled; the badge is informational only.

  A path that is both touched-`true` and a duplicate, **with Skip duplicates on**,
  is reachable only by unchecking then rechecking during the drain. There the
  duplicate verdict wins: the card ends up unchecked and `disabled` like every
  other duplicate, because the flag will exclude it regardless and a checked box
  would promise an import that cannot happen (§2). `importSelectionTouched`
  protects a user's choice among *importable* files; it does not override
  eligibility.

The renderer itself never seeds. It is a pure view over existing state. This is
required because the in-flight `hide-duplicates-checkbox` branch adds a
client-side filter that re-renders from cached inputs via
`rerenderImportPreviewGrid()`; seeding inside the renderer would silently reset a
hand-picked selection every time that filter is toggled.

**Invalidation.** A selection is only meaningful for the file set that produced
it. The `importPreviewSignature()` in effect at seed time is stored alongside.
`startImport()` recomputes the signature; if it differs, the selection is
discarded entirely and the import proceeds as though no preview had run. Changing
sources, file types, or `recursive` after selecting therefore cannot silently
apply a stale selection.

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
affected paths to `importSelectionTouched`.

**Duplicates.** While `Skip duplicates` is on, duplicate cards render unchecked and
`disabled`, badge reading `Duplicate — skipped`. They are already excluded by that
global toggle, so drawing a checked box next to them would promise an import that
will not happen. Turning `chkSkipDuplicates` off re-enables and checks them; since
that flag is part of the preview signature, flipping it invalidates the preview
and forces a re-preview, so the two mechanisms can never disagree.

A hand-picked selection is lost when `Skip duplicates` is toggled. This is
accepted: the toggle changes which files are eligible at all, so the prior
selection is not meaningfully transferable.

**Readouts.** A `#previewSelectedCount` element shows `"1,147 of 1,234 selected"`.
`#btnStart` relabels to `Start import (1,147 files)` whenever a selection is
active.

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

- `include_paths` — the selected paths.
- `previewed_count` — an integer: **the number of files in the preview endpoint's
  response**, not the number of cards visible in the grid. These differ once the
  sibling `hide-duplicates-checkbox` filter is active, and defining it as
  "displayed" would make every hidden duplicate inflate the files-appeared count in
  §6. Needed for honest drift reporting; see why an integer suffices there.

Three states for `include_paths`, and the distinction is load-bearing:

| State | Meaning |
|---|---|
| key absent | no preview run, or signature went stale → import everything (current behavior, unchanged) |
| non-empty list | import exactly these paths |
| empty list | client-side error `"No files selected."`; never sent |

An absent key and an empty list must not collapse into the same case. Absent means
"no opinion"; empty means "the user deselected everything," which is a mistake to
catch, not an instruction to import the whole card.

**String form.** `include_paths` carries the client's original path strings
unmodified, matching `ingest()`'s existing `skip_paths` convention. Normalization
is used only for the containment check in §4 — the stored set must still match
raw `discover_source_files` output by string equality, or the filter matches
nothing and imports zero files.

### 4. Backend validation

`include_paths` is untrusted client input in the same class as the in-place
snapshot's frozen path list, and is validated the same way: each path must resolve
under one of the request's already-validated `sources` directories, checked with
`os.path.commonpath`. Any path that does not → `400`. Import must not become a
path-admission escape hatch because a client asked for a file outside the folders
the user actually chose.

**Use `os.path.normpath` only — never `realpath`.** Neither `/api/jobs/import-photos`
nor preview discovery resolves symlinks on `sources`, so `realpath`-ing one side of
the comparison would reject legitimate imports from symlinked mount points. Both
sides must be normalized the same way.

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
{"path": "Deselected files",
 "reason": f"{deselected} files you deselected were not copied — "
           "the card still holds the only copies of them"}
```

Note every `unsafe_files` entry is also mirrored into `result["errors"]`
(`import_job.py:1965`, `3353`), so this wording appears in two places.

Without it, the warning is exactly the black box `CORE_PHILOSOPHY.md` prohibits:
technically correct, and unreadable as to why.

**Progress and summary counts stay on the full card.** `_emit(..., emitted,
discovered)` (`import_job.py:2348`, 3200) and the step summary
`"{failed} failed of {discovered} discovered"` (3245) all use `discovered`, so a
half-deselected import shows a bar that completes at roughly half. This is
consistent with `discovered` meaning "files on the card" and needs no change —
but the result card copy above is what makes it legible.

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
- path-escape rejection: `../` traversal, symlink out of tree, sibling directory
- **`safe_to_format` is false when any file is deselected, and true when all files
  are selected** — the card-safety regression guard
- `discovered` reports full card contents, not the selected subset
- both drift signals populate correctly; an ordinary deselection reports *zero*
  files-appeared, and a mixed appear/vanish case never goes negative
- drift signals are skipped entirely when no preview ran (`previewed_count is None`)
- interaction with `skip_duplicates` — a selected file that is also a duplicate is
  still skipped when the flag is on
- filtering happens before `DuplicateChecker.prepare()`
- **the remote path (`_run_remote_import_job`) honors `include_paths`** — the full
  set of assertions above, including `safe_to_format` and the `unsafe_files` entry;
  a regression here silently rsyncs deselected files
- a deselection produces an `unsafe_files` entry naming the uncopied count
- `include_paths` posted to `/api/jobs/import-in-place` is ignored, never silently
  half-applied (that route is unchanged by this spec)

**Frontend** (`tests/e2e/test_import_page.py`): the existing Playwright suite has a
`live_server`/`page` fixture pair and an established pattern for stubbing
`/api/import/folder-preview` via `window.fetch` override.

- checkboxes render and default to checked
- duplicates flip to unchecked and disabled when the duplicate stream completes,
  not before
- a file toggled while the duplicate stream is draining keeps the user's choice
- folder-header checkbox toggles its subfolder and shows indeterminate state
- select-all / select-none
- shift-click selects a contiguous range in visible order
- selection survives a `rerenderImportPreviewGrid()` (the "Hide duplicates" filter)
- `#btnStart` label reflects the selected count
- changing a source after selecting discards the selection
- **in-place mode hides all selection controls and shows the explanatory note**
- switching from copy to in-place after selecting discards the selection

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
`rerenderImportPreviewGrid()`. The seeding-outside-the-renderer decision in §1
exists specifically to compose with it. Whichever branch merges second must
verify that toggling "Hide duplicates" preserves selection state.
