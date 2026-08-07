# Free up card space (card cleanup) — design

**Date:** 2026-08-07
**Status:** Spec-review approved (2026-08-07); reviewer's advisory
recommendations incorporated; awaiting maintainer sign-off before
implementation planning
**Scope:** New feature: delete files from a local import source (memory card)
only after verifying, per file and at deletion time, that the identical bytes
are already in the archive. Two new job types plus an import-page UI section.
No changes to the import pipeline itself.

## Problem

A long import (e.g. 40 hours of travel photos over a slow link) can be
stopped partway — the import is batch-committed and crash-safe, and a re-run
skips everything already cataloged — but the user gets no help clearing the
card afterward:

- The import does not persist per-file source paths; the card-path →
  archive-file mapping exists only in memory during the run.
- Files are imported in destination-folder (chronological) order, not card
  order, so no card region corresponds to "the imported part."
- The only safety signal is the all-or-nothing `safe_to_format` pill, which
  correctly says "Do NOT format the card yet" after a partial run.

So a traveler who imported half a card and needs the space back has no safe
option: manually guessing which files landed risks deleting the only copy.

## Goals

1. After any partial (or complete, or historical) import, the user can point
   Vireo at the card and delete exactly the files whose content is verifiably
   in the archive — nothing else.
2. The preview shown before deletion is computed from ground truth at scan
   time (card bytes + current catalog), never from a stale record of what a
   past run claimed. This is the CORE_PHILOSOPHY transparency rule: the
   numbers must mean what the user reads them as.
3. The card stays usable throughout: the user can keep shooting between scan
   and delete; new or changed files are never touched.

## Non-goals (v1 scope cuts)

- **Local sources only.** Memory cards are local mounts; deleting files on a
  remote/SSH source is out.
- **No delete-as-you-go during import.** This tool's verify-then-delete core
  is the building block if that is ever wanted; it is not part of v1.
- **No persistence of per-file import provenance.** The scan recomputes
  everything from the card and catalog, so no schema changes are needed.
- **No empty-directory cleanup** on the card — files only. Cameras recreate
  their DCIM structure as needed.
- **No card formatting.** Vireo deletes individual verified files; formatting
  remains a user action on their own machine.

## Approaches considered

**A. Standalone scan-verify-delete tool (chosen).** Re-scan the card, match
each file against the catalog by content hash, preview, then delete only the
verified set. Works regardless of when/how the files were imported (this
run, last week, another machine), needs no schema changes, and reuses the
matching machinery `ingest()` already has. Cost: a re-hash pass over the
card — local reads, minutes not hours.

**B. Delete-as-you-go during import.** Frees space *during* the import, but
partially erases the card mid-run, is unavailable for the run the user is
already in, and the remote path is not always hash-verified. Rejected for
v1; A is its prerequisite anyway.

**C. Run-scoped cleanup** (persist per-file source paths during import,
offer "delete what this run imported"). Needs schema changes, goes stale if
the card is touched between runs, and covers only one run. Strictly worse
than A, which computes the same answer from ground truth at deletion time.

## Design

### UX flow

A "Free up card space" section on the import page, plus an entry point next
to the existing card-safety pill after an import finishes or is cancelled
(the "Do NOT format the card yet" state is exactly when the user needs this
tool). The user picks the card folder with the existing source picker and
starts a **scan**. When the scan job finishes, the page shows a preview:

> **1,842 files / 61 GB** verified in the archive — safe to delete
> **2,105 files / 70 GB** not in the archive — will be kept
> **214 files** ignored (not photo files Vireo imports)

Each bucket expands to its file list (path, size, and for the verified
bucket the matched archive path; for kept files, the reason). A **Delete
verified files** button opens a confirmation dialog that states plainly:

- Deletion is permanent — memory cards have no trash.
- After deletion, the archive holds the only copy of these photos.

Confirming starts the **delete** job with live per-file progress. The final
summary reports exact counts: deleted, kept, skipped-because-changed, and
failed, each with reasons.

### Scan job

A new `JobRunner` job type (working name `card_cleanup_scan`), started via
`POST /api/card-cleanup/scan` with `{source, recursive}`. SSE progress via
the existing `/api/jobs/<id>/stream`.

Phases:

1. **Discover** — `vireo.ingest.discover_source_files` (`ingest.py:286`)
   enumerates candidates exactly as an import would, with
   `file_types="both"` (all photo types, independent of any import-config
   filter), so the deletable set can never exceed what import considers a
   photo. Everything else found under the root is bucketed **ignored** and
   never touched.
2. **Hash & match** — each candidate is content-hashed on the card and
   matched against the global photo catalog by hash, using the strict
   `verify_by_hash` identity from `vireo/import_dedup.py`
   (`DuplicateChecker(CatalogIndex.from_db(db), verify_by_hash=True)`) —
   not the metadata (filename/size/EXIF-time) shortcut. A metadata match is
   not sufficient grounds to delete someone's only other copy. The scan
   calls `match()` only — never `record()`/`check_and_record()` — so
   card-only twin files cannot make each other look "known" the way
   ingest's seen-state accumulation would.
3. **Archive check** — `match()` returns an opaque hash token, not a photo
   row, so a matched hash is followed by a `photos WHERE file_hash = ?`
   lookup. The file counts as **deletable** only if at least one matching
   row has `photos.hash_status = 'ok'` and a `stat` of that row's archive
   file confirms it exists with the expected size; the preview shows the
   first row that passes. The archive is SMB over Tailscale, so the check
   is one stat round-trip per file — never a re-read of archive bytes.

Every other candidate is **kept**, with a per-file reason: not in catalog,
not integrity-verified (`hash_status` not `'ok'`), archive file missing or
wrong size, unreadable on card. Photos cataloged by an archive *scan*
rather than a verified import have `file_hash` but NULL `hash_status`, so
whole scan-cataloged archives land in this bucket — safely conservative,
but the keep-reason copy must point at the remedy ("not verified by a
checksummed import — run the integrity audit") so the tool doesn't read as
broken.

The job result is a manifest persisted with the job (as job results already
are): per file — card path, size, `mtime_ns`, content hash, bucket, matched
archive path or keep-reason — plus bucket totals (count and bytes). The scan
is cancellable at file boundaries; a cancelled scan produces no manifest and
the UI says so.

Duplicate files on the card (two identical copies matching one archive
photo) are both deletable — the rule is content-based, not one-to-one.

### Delete job

`POST /api/card-cleanup/delete` with `{scan_job_id}` starts
`card_cleanup_delete`. It loads the scan job's manifest and refuses to start
if the scan job is missing, unfinished, cancelled, or its manifest is empty.
Only one delete job per scan manifest may run at a time.

For each **deletable** manifest entry:

1. **Drift gate** — re-`stat` the card file. If size or `mtime_ns` differs
   from the manifest (camera rewrote it, file replaced), the file is
   **skipped** and reported; it is not deleted. This makes the
   scan-to-delete gap safe without freezing the card: new files simply are
   not in the manifest, changed files fail the gate.
2. **Delete** — `os.remove`. Per-file errors (read-only card, vanished
   file) are recorded as **failed** with the OS error; the job continues.

Progress is per-file over SSE. Cancellation stops at a file boundary;
already-deleted files stay deleted and the summary honestly reports
deleted vs. remaining. Directories are left in place.

The delete job never re-reads the manifest's *kept* or *ignored* buckets —
they exist only for the preview.

### Endpoints

- `POST /api/card-cleanup/scan` — body `{source: str, recursive: bool}`.
  Validates the source path exists and is a directory. Returns the job id.
- `POST /api/card-cleanup/delete` — body `{scan_job_id}`. Returns the job
  id. 409 if a delete for that manifest is already running; 400/404 for
  missing or unusable scan jobs.
- Progress and results ride the existing job endpoints (stream, status,
  history).

Both jobs are global (photos and their hashes are global, not
workspace-scoped), matching how the catalog works.

### Error handling

- Unreadable card file at scan time → kept, reason "could not read".
- Archive stat failure (mount down, permission) → kept, reason "archive
  file not reachable"; the scan completes and says how many files were
  unverifiable so the user knows the mount was the problem.
- Card unmounted mid-delete → per-file failures accumulate; the job
  finishes with an honest failure count rather than aborting silently.
- Scan manifest older than the card's current state is handled entirely by
  the per-file drift gate; there is no root-level signature check, because
  the whole point is that the user keeps shooting on the card.

## Testing

Unit tests with temp directories (no real card or SMB mount):

- Bucket assignment: verified / not-in-catalog / ignored non-photo files.
- Hash match but `hash_status` ≠ `'ok'` → kept.
- Hash match but archive file missing or wrong size → kept.
- Drift gate: file modified between scan and delete → skipped, not deleted.
- Cancellation mid-delete → already-deleted files gone, summary counts
  correct.
- Two identical card files matching one archive photo → both deletable,
  both deleted.
- Delete refuses to start on a cancelled or missing scan job.
- Per-file delete failure (permission) → recorded as failed, job continues.
- Endpoint validation: nonexistent source dir, concurrent delete → 409.
