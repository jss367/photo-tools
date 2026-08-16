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
- `acceptable_prediction_ids` / `acceptable_photo_count` — the unambiguous
  rows an Accept button may act on, split by `_ambiguous_prediction_ids`,
  the same helper `batch-accept` re-runs before writing
- `acceptable_keyworded_count` — how many of those photos already carry the
  keyword, so accepting them writes no tag
- `ambiguous_prediction_ids` — the subset with alternatives or a
  `disagreement`/`refinement` category
- confidence range (min/max), not a single number

### "Already keyworded" is not "resolved"

A pending prediction on a photo that already carries the species (the user
tagged it by hand after classification) needs no keyword — but it is still
`pending`, so Review keeps queueing it. Dropping it from the panel reported
it as done and left it unresolvable from Browse forever.

`accept_prediction` handles exactly this as a status-only accept
(`changed_tag=false`): the review status flips, the existing tag is neither
removed nor re-added, and the edit stays undoable. Those rows are therefore
included in `acceptable_prediction_ids`.

That makes one button cover two outcomes, so the row names the split:
`acceptable_keyworded_count` drives a "3 already carry the keyword —
accepting only clears them from Review" note beside "Accept on 38". A count
that silently means both "gets a keyword" and "only leaves Review" is the
overloaded counter the transparency rule forbids.

Ambiguity still outranks it: a keyworded photo whose prediction has an
alternative or a disagreement goes to `ambiguous_*` and routes to Review.

### Burst consensus and the displayed species

`accept_prediction` substitutes a burst's consensus species (from
`prediction_review.individual`) whenever a row carries a `group_id`, so any
panel labelling rows from `pr.species` must show that consensus instead —
`/api/predictions` exposes it as `consensus_species` and both the detail
panel and the selection aggregator group by it.

Belt and braces: the data cannot currently diverge either.
`_store_grouped_predictions` sets `group_reviewable` — and therefore
`group_id`, `individual` and the vote counts — **only** when every frame in
the burst folds to one species key (`classify_job.py`, `group_species`), so a
burst whose frames disagree is stored with `group_id = NULL` on every member
and each row accepts as its own species.
`test_mixed_burst_never_groups_so_displayed_species_is_accepted_species` and
`test_grouped_burst_consensus_equals_each_row_species` lock that invariant,
so the display fix stays a defence rather than the only thing standing
between the user and a mislabelled Accept.

### One size limit, two endpoints

One bound, one unit, one constant: `_MAX_SELECTION_PHOTOS = 1000` **photos**,
enforced by the selection endpoints on the way in and by
`_parse_prediction_ids` (on distinct photos, not on ids) on the way back.

The invariant is *any payload the suggestions endpoint can legally emit, the
batch endpoints accept*. Bounding the write by id count cannot hold it: the
producer caps photos and then emits every matching row for them, and
rows-per-photo has no ceiling (one per detection × one per classifier
model). Three rounds of review picked a rows-per-photo guess — 1,000, then
25,000, then 200,000 — and each was either below what the producer could
emit (so the panel's own Accept button 400ed on a selection it had just
advertised) or a number with nothing behind it. Counting photos makes the
invariant hold by construction, with no margin left to re-tune.
`test_batch_accept_takes_any_payload_the_suggestions_endpoint_emits` drives
producer into consumer; `test_batch_accept_has_no_prediction_id_count_cap`
locks that length alone never rejects;
`test_batch_prediction_payload_shares_the_selection_photo_cap` locks the two
ends to the same number.

Both the id lookup and the already-accepted probe chunk through
`_SQL_PARAM_CHUNK`: a several-thousand-wide `IN` clause exceeds the
999-variable limit older SQLite builds enforce. The workspace check runs
once per distinct photo rather than once per id.

### Scoping a grouped accept: rows, not photos

`accept_prediction` expands a grouped (burst) accept to the whole group. The
batch endpoint must confine that to what the user submitted, and it passes
`prediction_ids` — the submitted row ids — rather than the union of their
photo ids. A photo is not a unique key for a prediction: one burst frame
carries a row per classifier model and a row per detection. Under *any*
photo-set limit — the batch-wide union, or a per-`(group, model)` bucket —
photo A submitting model X and photo B submitting model Y let A's grouped
accept walk the burst under X and accept B's X row too, a row the panel had
deliberately omitted (below threshold, already accepted, ambiguous). Row
identity has no such projection to widen, whatever column next distinguishes
two rows on one photo. The submitted set is also built *after* the
already-accepted filter, so a resubmitted row cannot be re-accepted through
a sibling's group expansion. `photo_ids` stays for callers whose intent really is
"these photos" (highlight confirm, accept subject).

`accept_prediction` returns `accepted_prediction_ids` so the batch loop skips
siblings a grouped accept already resolved; re-entering them was O(N²) in
group size and appended a second, status-only history item per photo whose
recorded previous status was a fiction. For that skip to be safe, rejecting
the losing alternatives moved from "once, for the entry row" to "once per
accepted row" — a grouped accept decides every member's detection, so every
member's alternatives have to be resolved with it. Locked by
`test_batch_accept_scopes_grouped_accept_to_the_submitted_rows`,
`test_batch_accept_does_not_re_enter_rows_a_grouped_accept_covered` and
`test_batch_accept_dedup_still_rejects_skipped_detection_alternatives`.

### The category is a snapshot; the Accept button is not

`predictions.category` records how the prediction compared to the photo's
keywords *when the classifier ran*. Nothing rewrites it afterwards — the only
writers are the classify path and duplicate merge — so a photo keyworded
Robin after a pending Sparrow prediction was stored as `new` still reads
`new`, and Browse would offer a bare Accept that tags a species the photo
already contradicts. Compare never trusted the column; it recomputes per
request. `_effective_category_resolver` shares that computation and both
Browse surfaces — `/api/predictions` (as `effective_category`) and the
selection aggregator — recompare before deciding "actionable here" vs "route
to Review".

Three details are load-bearing:

- the comparison runs on the **consensus** species, since that is what
  Accept would add;
- the keyword side comes from `get_species_keywords_for_photos` and the
  prediction side from `resolve_species_display_name`, the same
  canonicalization Compare applies — comparing raw `keywords.name` text
  would make a photo tagged with the hierarchy leaf `Desert Verdin` read as
  *conflicting* with a `Verdin` prediction whenever the taxonomy file is
  unavailable, inventing an ambiguity and sending a settled photo to Review;
- the fresh comparison **wins outright**; the stored snapshot is the
  fallback for when no fresh comparison could be made, not an extra OR
  condition. ORing them makes ambiguity a one-way ratchet — a photo whose
  conflicting keyword was since removed would be routed to Review forever,
  naming a conflict that no longer exists. That is the same staleness bug
  pointing the other way.

`test_browse_predictions_stay_acceptable_when_keywords_agree` and
`test_browse_clears_stale_ambiguity_when_keywords_no_longer_conflict` hold
the "do not over-route" direction; the routine's
`test_selection_prediction_suggestions_recomputes_ambiguity_after_keyword_edit`
and `test_predictions_api_effective_category_reflects_current_keywords` hold
the "do route the real conflict" direction.

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
| `detector_ran = true`, `classifier_ran = false`, `detection_count = 0` | Nothing detected — the detector ran and found no animals. |
| `detector_ran = true`, `classifier_ran = false`, `detection_count > 0` | Not yet classified — detections found, but no classifier has run yet. |
| `detector_ran = true`, `classifier_ran = true`, rows exist but all below `threshold` | No species above threshold — every prediction is below your confidence floor. |
| `detector_ran = true`, `classifier_ran = true`, no rows at all | Classification ran and produced no species for this photo. |

The five conditions are mutually exclusive and exhaustive, so each row names
exactly one cause on its own — no reader (or reimplementation) has to infer a
fallthrough order to get the right message. `detector_ran = true` is stated
explicitly rather than implied by `classifier_ran`, because the two are
recorded independently: a photo can carry classifier rows from an earlier
label set while the current detector run is still pending, and "nothing
detected" would then be a claim no run supports.

The last two must stay apart: blaming the threshold when the classifier
emitted nothing names a cause that never fired. `classifier_ran` is therefore
tested *before* `detection_count` — once the classifier has run, the reason
is a classifier fact.

`predictionEmptyMessage` (`browse.html`) evaluates them in the order above —
`detector_ran`, then `classifier_ran`, then `detection_count` — which matches
the table but is not what makes it correct.

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
Bald Eagle — predicted on 38 of 40     [Accept on 38]
```

Here both keyworded counts are zero — no selected photo already carries the
species, so every one of the 38 gets a tag. The row therefore states no
keyworded subset at all: a "keyworded on 0" clause would be noise on a row
where nothing is keyworded.

Where some predictions are ambiguous, the button covers only the clean
subset and the row says so. The keyworded subset is disclosed on the same
row, because the two splits are independent: ambiguity decides *which* photos
the button touches, `acceptable_keyworded_count` decides *what accepting does*
to the ones it touches.

```text
Bald Eagle — predicted on 38 of 40, already keyworded on 2     [Accept on 35]
3 photos need review · 2 already carry the keyword — accepting only
clears them from Review
```

So "Accept on 35" here means 33 photos get the keyword and 2 are cleared out
of Review with no tag written. A button labelled "Accept on 38" that quietly
acts on 35 is precisely what the UI-transparency rule forbids — and so is an
"Accept on 35" that folds tag writes and status-only accepts into one
undifferentiated number.

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

**Panel freshness is not a per-call-site concern.** Everything the panels
display is derived state: a row's `effective_category` (ambiguous or not) and
a selection row's `keyworded_count` / `missing_photo_ids` are recomputed by
the server from the photos' *current* species keywords and review status. So
the panels go stale on far more than accept/reject — any keyword add, remove
or retype, and any undo/redo, invalidates them. An "Accept on 38" left over
from before the user keyworded 10 of those photos states a falsehood, which
`CORE_PHILOSOPHY.md`'s "no black boxes" forbids.

Adding a reload to each new mutation site is how this drifted; instead one
`refreshPredictionPanels(opts)` hangs off the chokepoints every mutation
already passes through:

| Chokepoint | Covers |
| --- | --- |
| `_refreshBrowseKeywordState(ids, opts)` | every keyword write in Browse — `addKeyword`, `removeKeyword`, `applySelectionKeyword`, `removeSelectionKeyword`, the batch keyword modal, `setKeywordType` — because each must refetch card badges anyway |
| `document` event `vireo:edit-history-changed` | undo and redo, which the navbar performs |
| `_afterPredictionMutation` / `rejectDetailPredictions` | prediction status writes |

A future keyword mutation path therefore gets panel freshness for free.
`opts.skipDetail` is for callers about to run a full `loadDetail` (which
re-fetches the photo *and* its predictions), so one accept does not issue two
identical prediction requests.

**The Review deep link.** "Open in Review" navigates to
`/review?photo_id=N&filters=<empty expression>`. The explicit empty handoff is
load-bearing: `VireoFilter.init()` restores the last persisted Review
expression whenever the URL carries no `filters` param, so a filter the user
left active there could exclude the very row Browse withheld — an empty queue
under a pill reading "Showing one photo from Browse". The payload has to be
well-formed (`{"root":{"mode":"all","rules":[]},"visual":null}`), not a bare
`?filters=`, which `init()` treats as a corrupt handoff and throws on.

**Repeat and contradictory decisions.** Both batch endpoints skip rows whose
decision is already made, through one helper that owns the status list:
`_decided_prediction_ids(db, pred_ids)` returns the `accepted` and `rejected`
rows. Neither endpoint passes its own statuses, so the two cannot drift apart
on what "still actionable" means. `alternative` rows stay actionable on both
paths — they are the runners-up an accept promotes and a reject sweeps up.

Both report the skipped count as `already_decided` (the accept side's earlier
`already_accepted` was renamed once it also covered rejected rows — a field
name narrower than its contents is exactly the quiet mis-description
`CORE_PHILOSOPHY.md` rules out).

The two directions fail in mirrored ways without the filter:

- A stale **reject** of an `accepted` row leaves the species keyword the
  accept added attached to a prediction now marked `rejected`.
- A stale **accept** of a `rejected` row tags the photo with the loser the
  user just rejected by accepting its sibling in Review or a second tab, and
  the batch's undo entry then resets the whole sibling scope to
  pending/alternative instead of restoring the winner's accepted state.

In both directions a re-submitted row also records a status-only edit whose
"previous" status is a fiction, so undoing it would knock a long-decided
prediction back to pending.

**Ambiguity is revalidated, not trusted.** The status filter above only
catches rows whose *status* moved. A row can stay `pending` and still stop
being safe to bare-accept, because ambiguity is a function of the photo's
current species keywords: add a Golden Eagle keyword from Review, a second
Browse tab, or an XMP sync after Browse rendered "Accept on 35", and the Bald
Eagle row the panel listed as acceptable is now a conflict. The panel's own
refresh (`vireo:edit-history-changed` + `_refreshBrowseKeywordState`) covers
mutations inside one document; only the server sees the rest.

So `batch-accept` re-derives the verdict before writing, through
`_ambiguous_prediction_ids(db, rows)` — **the same helper that produces the
panel's `acceptable_prediction_ids`**, not a second implementation. It owns
both halves of the rule (an `alternative` sibling on the row's
`(detection, model)`, and `_prediction_is_ambiguous` against current
keywords), so the button's promise and the write's precondition cannot
describe different sets. This is the lesson the status precondition and the
accept scope each taught earlier: a rule with two implementations is a rule
that drifts.

**Superseded label sets.** The third precondition, and the same shape as the
first two: a payload that was truthful when it rendered and is not any more.
Re-classifying a detection against a new label set leaves the old prediction
row `pending` — nothing rewrites it — while `get_predictions`, and so every
panel, Review grid and summary, has already switched to the newest
`labels_fingerprint` for that `(detection, classifier_model)`. Accepting the
old row would tag the photo from a label set nothing displays and mark
accepted a row the user can no longer see; rejecting it would report a
dismissal while the current prediction stays pending in the panel in front of
them.

`_superseded_prediction_ids(db, pred_ids)` is a peer of
`_decided_prediction_ids`, used by both endpoints — one rule, one
implementation — but deliberately *not* a branch inside
`_ambiguous_prediction_ids`. The two verdicts are different facts leading to
different next steps: an ambiguous row needs a decision in Review, a
superseded row needs nothing at all, since the panel refresh puts the current
row in its place. Counting it under `skipped_ambiguous` would send the user
hunting for a keyword conflict that does not exist. It is checked *before*
ambiguity so the two counts stay disjoint.

**Atomicity.** A precondition that is not atomic with the write it guards only
narrows the race. Both batch endpoints therefore open one `BEGIN IMMEDIATE`
transaction (`_begin_prediction_decision`) *before* the first precondition read
and commit once, after the writes and the history entry. SQLite's WAL mode
allows a single writer, so a second overlapping decision — a double-clicked
Accept, or an Accept and a Reject fired before the panel reloads — blocks at
`BEGIN IMMEDIATE` and then reads the state the first one committed, instead of
acting on state it read before the first one wrote. Python's `sqlite3` would
otherwise open its implicit transaction at the first *write*, leaving every
check outside it.

A conditional `UPDATE ... WHERE status = 'pending'` was the alternative. It was
rejected because it guards only the status column: ambiguity is a function of
the photo's keywords, and the keyword, sibling, group and history writes hang
off the same decision — the invariant would hold for one column and need a
second code path for the rest. When the lock cannot be taken inside the
connection's 30 s `busy_timeout`, the endpoint returns 503 with nothing
written, rather than falling back to a non-atomic path.

`_parse_prediction_ids` stays outside the transaction on purpose: it validates
payload shape and workspace ownership, which is not the state these
preconditions race against, and it can walk a 1,000-photo selection.

**The endpoint's contract.** Every row `batch-accept` accepts is one that is
still undecided, still unambiguous, AND still from the current label set *at
the moment of the write* — literally that moment, since the checks and the
write share one transaction. All three are judged against the database rather
than against whatever the caller's panel believed. Rows failing any of them are
skipped — never accepted — and counted back as `already_decided`,
`skipped_superseded` and `skipped_ambiguous`. A stale
payload can accept *less* than the caller asked for, never something the
caller was not shown as acceptable. Skipping beats a 400: the rest of the
batch is still exactly what the user asked for, and failing the whole call
would strand 34 honest accepts on one row that moved.

Two consequences at the caller:

- Browse names the gap. `_reportSkippedAccepts` turns a non-zero
  `already_decided` / `skipped_ambiguous` / `skipped_superseded` into a toast,
  because the distance
  between "Accept on 35" and 33 accepts has to be spoken, not left for the
  user to spot. The counts stay separate because the next step differs —
  a decided row needs nothing, an ambiguous one needs Review, a superseded one
  is simply replaced by the refresh.
- The undo toast is gated on `accepted`. A payload whose rows have all been
  decided or turned ambiguous returns `accepted: 0` and records no undoable
  edit, so an unconditional toast would advertise — and Ctrl+Z would
  reverse — some older, unrelated edit.

Because the check applies to alternatives too, a row with an `alternative`
sibling can no longer be accepted through this endpoint at all. That matches
what the panel offers; Review's single-photo `/api/predictions/<id>/accept`
is the path that deliberately still accepts with alternatives on screen, and
is where the group-expansion behaviour is now covered.

## Testing

`vireo/tests/test_app.py`:

- Workspace scoping — a photo outside the active workspace returns 403,
  matching the keyword endpoint.
- The 1000-photo cap.
- Aggregation counts when a species is predicted on some selected photos
  and already keyworded on others.
- Batch accept flips `prediction_review.status`, not just the keyword.
- Batch accept records a single undo entry.
- Batch reject skips already-accepted and already-rejected rows, leaving the
  accept's keyword and status intact and writing no history entry.
- `test_batch_accept_revalidates_ambiguity_after_keywords_move` — a payload
  rendered when both photos were clean, submitted after one gained a
  conflicting keyword: the clean row still accepts, the moved row is skipped
  and stays pending and untagged, and a reload of the suggestions endpoint
  agrees with what the write just did.
- `test_batch_accept_skips_ambiguous_rows_with_alternatives` — the
  alternative-sibling half of the same rule, and the no-op leaves nothing on
  the undo stack.
- `test_batch_accept_skips_superseded_label_set_rows` /
  `test_batch_reject_skips_superseded_label_set_rows` — a payload rendered
  before a re-classification: the old row is skipped and counted as
  `skipped_superseded` (not as ambiguous), stays pending and untagged, and the
  current row is still offered by the panel afterwards.
- `test_batch_accept_checks_and_writes_are_one_transaction` — the overlap
  itself, in-process and deterministically. A competing connection tries to
  record a decision on the row being accepted at the exact instant between the
  endpoint's checks and its first write, hooked through
  `Database.accept_prediction`, with a 0.5 s `busy_timeout`. It either commits
  (the bug) or is refused because the endpoint holds the writer lock (the
  guarantee); the hook blocks the request until the competing writer has its
  answer, so nothing depends on thread-scheduling luck. Verified to fail
  against a build with the `BEGIN IMMEDIATE` removed.
- The panels' invalidation lives in `_refreshBrowseKeywordState` (before its
  early returns) and in a `vireo:edit-history-changed` listener, so the
  coverage is structural rather than per-call-site.
- "Open in Review" sends an explicit empty `filters` handoff.

Plus a Playwright pass driving the real panel — selecting photos, accepting,
confirming the Review queue drains.
