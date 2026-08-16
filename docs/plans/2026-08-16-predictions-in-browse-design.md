# Predictions in Browse — Design

**Date:** 2026-08-16
**Branch:** `predictions-panel-in-browse`

## Problem

Browse never shows what the classifier thinks — only what the user has
already committed. Predictions live in the database but surface in browse
only as filter fields (`prediction_confidence`, `prediction_status`). To see
a prediction you must leave for Review or Pipeline.

The multi-select panel added in #1476 (`browse.html:1721`) looks like it
covers this, but `/api/selection/keyword-suggestions` (`app.py:9758`) is a
plain join on `photo_keywords`. It aggregates keywords **already applied**.
Predictions are absent from both the single-photo detail panel and the
multi-select panel.

## Scope

Two changes:

- **Single photo** — a Predictions section in the detail panel with
  accept/reject for unambiguous predictions.
- **Multi-select** — a combined prediction view that accepts one species
  across the selection in a single action.

Browse gets a *reduced* prediction surface, not a second Review. Review's
full decision UI — alternatives, `disagreement`/`refinement` comparison,
accept-subject, reject-with-siblings — stays in Review. Browse triages the
simple cases and routes the rest there, visibly.

### Rejected alternatives

**Full parity with Review.** Reproducing alternatives, accept-subject and
disagreement comparison inside a narrow resizable sidebar means two
implementations of the same decision logic and every prediction bug fixed
twice. Premature.

**Accept-only fast path** (show only high-confidence, alternative-free
predictions). Cheapest, but a photo with a messy prediction would look
identical to a photo with no prediction at all. That violates the
no-black-boxes rule in `CORE_PHILOSOPHY.md`.

## Backend

### Single photo — extend `/api/predictions`

No new route. `/api/predictions` (`app.py:15277`) already handles workspace
scoping, dedup to the latest `labels_fingerprint`, nested alternatives, and
`existing_species` enrichment for disagreements. It only lacks a per-photo
entry point.

Add a `photo_ids` query arg forwarding to `db.get_predictions(photo_ids=...)`,
which already accepts it (`db.py:16145`). Browse then renders the same
enriched rows Review does, so there is one notion of what a prediction is.

### Multi-select — `POST /api/selection/prediction-suggestions`

Mirrors `/api/selection/keyword-suggestions` deliberately: same request
shape, same 1000-photo cap, same `_photo_in_workspace` check per id.

Aggregation key is the **species name**, not the prediction id — each photo
carries its own prediction row. Each entry returns:

- `predicted_count` — selected photos with this species predicted
- `keyworded_count` — of those, how many already carry the keyword
- `missing_photo_ids` — predicted but not yet keyworded
- `prediction_ids` — the prediction rows for those photos
- `ambiguous_prediction_ids` — the subset with alternatives or a
  `disagreement`/`refinement` category
- confidence range (min/max), not a single number

### Accept is not a tag

The multi-select Add button must **not** call `/api/batch/keyword`. That
tags only. Predictions go through `accept_prediction`, which tags *and*
flips `prediction_review.status`. Tagging alone would keyword 38 photos in
browse and leave all 38 pending in Review — the same work done twice.

A batch-accept endpoint takes a list of prediction ids and records **one**
`prediction_accept` edit covering all of them, following the existing
`changed_tag` / `no_tag` encoding (`app.py:15699`). One undo reverses
"accepted eagle on 38 photos", not 38 separate steps.

## UI

### Single photo

A `detail-section single-only` titled **Predictions**, directly above the
existing Keywords section — accepting a prediction writes a keyword into the
box immediately below, so cause and effect are adjacent.

Row: `Bald Eagle · 87% · SpeciesNet`, in one of three states:

| State | Rendering |
|---|---|
| Pending, unambiguous | `Accept` / `Reject`, wired to the existing per-prediction endpoints |
| Pending, ambiguous | No bare Accept. States why — "3 alternatives", "conflicts with keyworded Bald Eagle" — and offers **Open in Review** |
| Already decided | Muted, marked Accepted or Rejected |

An empty list has five different meanings and a blank panel implies only the
first, so `/api/predictions?photo_ids=…` returns a `photo_states` entry per
photo (`detector_ran`, `detection_count`, `classifier_ran`, `threshold`) and
the panel names the real cause:

| Condition | Rendering |
|---|---|
| `detector_ran = false` | Not yet classified — no detector has run on this photo. |
| `classifier_ran = false`, `detection_count = 0` | Nothing detected — the detector ran and found no animals. |
| `classifier_ran = false`, `detection_count > 0` | Not yet classified — detections found, but no classifier has run yet. |
| `classifier_ran = true`, rows exist but all below `threshold` | No species above threshold — every prediction is below your confidence floor. |
| `classifier_ran = true`, no rows at all | Classification ran and produced no species for this photo. |

The last two must stay apart: blaming the threshold when the classifier
emitted nothing names a cause that never fired. `classifier_ran` is therefore
tested *before* `detection_count` — once the classifier has run, the reason
is a classifier fact.

`detection_count` counts only real detections — `detector_model =
'full-image'` synthetic anchors and rows below the workspace
`detector_confidence` floor are excluded, matching
`count_real_detections_in_scope`. A photo whose only row is the whole-frame
fallback the detector writes *because* it found nothing must read as
"nothing detected", not "detections found".

Predictions below the workspace `classifier_confidence` are not silently
dropped. A collapsed line — `2 below threshold (0.35)` — keeps them
visible without cluttering.

### Multi-select

A **Predictions** section above the existing Keywords section in
`selectionPanel`, reading like the keyword rows already there:

```text
Bald Eagle — predicted on 38 of 40, keyworded on 0     [Accept on 38]
```

Where some predictions are ambiguous, the button covers only the clean
subset and the row says so:

```text
Bald Eagle — predicted on 38 of 40     [Accept on 35 · 3 need review]
```

A button labelled "Accept on 38" that quietly acts on 35 is precisely what
the UI-transparency rule forbids.

## Data flow

**Loading.** Single-photo predictions fetch inside the existing
`loadDetail(photoId)` path. Multi-select reuses the *pattern* of the
`selectionKeywordKey` / seq guard (`browse.html:6053`) — same cache key
shape, same stale-response drop — but with its own `selectionPredictionKey`
and `selectionPredictionSeq`. Sharing the keyword panel's key would let one
panel's response invalidate the other's, or make a valid response look
stale. That guard exists because a fast selection change previously rendered
results for the prior selection (see the review note at `browse.html:2034`);
an independently-invented guard would reintroduce it. One seq counter per
panel, responses dropped on mismatch. A failed load clears its key so the
same selection can retry instead of sticking on the error text.

**After accept.** An accept mutates keywords, so it runs the same fan-out
`applySelectionKeyword` already performs:

- `_refreshBrowseKeywordState(ids)`
- `loadKeywords()`
- `scheduleCollectionCountsRefresh()`
- `refreshActiveCollectionAfterMembershipChange()`
- `refreshPendingSyncBanner()` — accepting writes pending XMP changes
- `showUndoToast()` — the batch is one `prediction_accept` edit, so one
  Cmd-Z reverses the whole thing
- invalidate the prediction cache key so row state re-renders

**After reject.** A rejection touches no keywords, so it refreshes only the
prediction rows plus the membership-dependent surfaces:

- repaint the detail/selection prediction panels (cache key invalidated)
- `scheduleCollectionCountsRefresh()` and
  `refreshActiveCollectionAfterMembershipChange()` — a reject changes
  membership in prediction-status collections such as "pending predictions"
- **no** `showUndoToast()`. `prediction_reject` is in the database's
  `_NON_UNDOABLE` set (`_apply_undo` has no handler), so a toast would
  advertise some *earlier* edit as reversible and Cmd-Z would silently undo
  that unrelated action instead of the rejection.

**Repeat accepts.** `/api/predictions/batch-accept` skips prediction ids
already marked accepted and reports them as `already_accepted`. A
double-clicked button would otherwise record a status-only edit whose
"previous" status is a fiction, and undoing it would knock a
long-accepted prediction back to pending.

## Testing

`vireo/tests/test_app.py`:

- Workspace scoping — a photo outside the active workspace returns 403,
  matching the keyword endpoint.
- The 1000-photo cap.
- Aggregation counts when a species is predicted on some selected photos
  and already keyworded on others.
- Batch accept flips `prediction_review.status`, not just the keyword.
- Batch accept records a single undo entry.

Plus a Playwright pass driving the real panel — selecting photos, accepting,
confirming the Review queue drains.
