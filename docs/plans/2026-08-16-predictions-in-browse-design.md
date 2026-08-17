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

That invariant is younger than the catalog, though. `group_reviewable`
arrived in #1165 (2026-07-10); before it, every multi-frame burst stored a
`group_id` with a multi-species `individual`, and those rows are still in the
database. For them the consensus genuinely differs from the row's own label,
which matters for the **confidence** the panel prints: `predictions.confidence`
is the score of the frame's own label, so folding a 95%-scored minority frame
into the consensus bucket would report a number belonging to a different
species than the one named, and float that bucket up the strength-ordered
list on evidence that was never for it. The aggregator therefore only counts
a row's confidence toward the bucket it names when the row's own label *is*
that species. The row still belongs to the bucket — accepting it does apply
the consensus — it simply carries no evidence for it. A bucket left with no
contributing row reports `min_confidence`/`max_confidence` as `null`, which
`formatPredictionConfidence` renders as "confidence unknown": the honest
answer, rather than a percentage the user would read as the classifier's
confidence in a species it never scored.

The threshold filter deliberately still uses the row's own confidence. That
floor decides whether a prediction row is worth surfacing at all, and the
row's score is a true statement about the row; what was wrong was attributing
it to a species, and attribution is now a separate step. Filtering the row out
instead would hide a real pending prediction with no accurate wording for why
— `below_threshold_count` says "below your confidence threshold", which such a
row is not.

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

**Expansion discovers undecided rows only.** The scope limits above protect
the batch endpoint, which passes one; the *unscoped* callers — Review's
`/api/predictions/<id>/accept`, highlight confirm, accept-subject — get no
such list, and a grouped accept there reaches every member of the burst
regardless of what the user already decided about them. So `accept_prediction`
now excludes every member whose status is in
`Database.DECIDED_PREDICTION_STATUSES` from the expansion itself: accepting one
frame must not resurrect a sibling the user rejected in Review (tagging that
photo with the species it was denied), re-flip a long-accepted one, or
overwrite one marked `reviewed` — each lands a history item whose recorded
previous status never happened, so undo would knock it back to pending. Rows
the caller *named*
are exempt, because then the caller chose them rather than the expansion: the
entry row, and anything in `prediction_ids`. `batch-accept` never names a
decided row (`_decided_prediction_ids` removes them first), so its behaviour
is unchanged. Locked by
`test_accept_prediction_grouped_skips_already_decided_members` in
`vireo/tests/test_db.py`.

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
| Already decided | Muted, marked `accepted`, `rejected` or `reviewed` — the three statuses in `Database.DECIDED_PREDICTION_STATUSES`, so the panel and the endpoints agree on which rows are still actionable. The tag carries a tooltip saying what the status means, since `reviewed` is written from ID Conflicts and would otherwise be a bare word where two buttons used to be. |

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

**No row data reaches the markup.** Every button this panel renders carries
integers only — an index into `detailPredictionGroups` and a photo id — and a
single delegated `click` listener on the panel container
(`handleDetailPredictionClick`) reads the ids and the species back out of that
array. The panel originally built inline handlers by interpolation:

```js
onclick='acceptDetailPredictions([12],44,"Say\'s Phoebe")'
```

The apostrophe closes the single-quoted attribute, so the browser kept a
truncated handler and parsed `s Phoebe")'` as two stray attributes: Accept and
Reject were broken outright for Say's Phoebe, Cooper's Hawk, Steller's Jay,
Bewick's Wren, Swainson's Hawk, Wilson's Warbler — possessive common names are
ordinary in North American birds, which is most of what this app is pointed
at. Escaping that interpolation would have fixed that one line; keeping data
out of markup is what stops the next button added here from reintroducing it,
and it matches what the multi-select panel has always done
(`selectionPredictionAcceptableById` / `selectionPredictionSpeciesByIdx`).
Species names still reach the DOM as *text*, through `escapeHtml`, and status
tooltips through `escapeAttr`.

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
| `refreshBrowseSidebarPanels()`, off the `document` event `vireo:edit-history-changed` | undo and redo, which the navbar performs |
| `_afterPredictionMutation` / `rejectDetailPredictions` | prediction status writes |

A future keyword mutation path therefore gets panel freshness for free.
`opts.skipDetail` is for callers about to run a full `loadDetail` (which
re-fetches the photo *and* its predictions), so one accept does not issue two
identical prediction requests.

Undo/redo is the one row in that table that needs a *wider* refresh than the
prediction panels. Undoing a `prediction_accept` reverts both halves of the
accept — the row returns to pending *and* the species keyword it wrote is
stripped — and each half has its own panel in the sidebar, so repainting only
the prediction panels leaves the Keywords section listing a keyword the
database no longer holds. It is the same rule pointing the other way. Hence
`refreshBrowseSidebarPanels()`: it repaints the sidebar wholesale rather than
naming the panel that happens to be wrong today, so a panel added there later
is refreshed by default. It branches once on the selection — `loadDetail` for
a single photo (one request covering the photo *and* its predictions, so the
prediction half is told to `skipDetail`), `loadSelectionPredictions` plus
`loadSelectionKeywordSuggestions` for a multi-selection, with both cache keys
dropped first because the selection has not changed, only the state behind it.
The grid-card refresh in the same handler passes `opts.skipPanels` so one undo
does not fetch the prediction panels twice.

**The Review deep link.** "Open in Review" navigates to
`/review?photo_id=N&filters=<empty expression>`. The explicit empty handoff is
load-bearing: `VireoFilter.init()` restores the last persisted Review
expression whenever the URL carries no `filters` param, so a filter the user
left active there could exclude the very row Browse withheld — an empty queue
under a pill reading "Showing one photo from Browse". The payload has to be
well-formed (`{"root":{"mode":"all","rules":[]},"visual":null}`), not a bare
`?filters=`, which `init()` treats as a corrupt handoff and throws on.

**Repeat and contradictory decisions.** Every prediction endpoint refuses a row
whose decision is already made, against one status list that lives in
`Database.DECIDED_PREDICTION_STATUSES`: `accepted`, `rejected`, `reviewed`. No
endpoint passes its own statuses, `accept_prediction`'s group expansion reads
the same tuple, and Browse's panel mirrors it in `PREDICTION_DECIDED_STATUSES`
— so no surface can drift on what "still actionable" means, and no button is
offered for an action the server will refuse. The batch endpoints use the set
form (`_decided_prediction_ids`); the single-row endpoints use
`_prediction_status` and answer 409 naming the status they found.

`reviewed` is on that list because it is a decision, not a waypoint. ID
Conflicts writes it through `/api/predictions/<id>/reviewed` to mean "I looked
at this and chose not to act"; that endpoint already refused to move a
non-pending row, Review disables every action button on the row afterwards, and
`prediction_reviewed` is in `_NON_UNDOABLE`, so nothing walks it back to
`pending`. Treating it as still-actionable let Browse's panel render
Accept/Reject on it and let an accept record `pending` as the previous status —
a state that never existed, so Undo would restore the wrong thing. The panel now
renders it as a decided row with a `reviewed` tag and a tooltip saying where it
came from, rather than hiding it: a decided row that vanishes is the failure the
five empty states exist to prevent.

Refusing rather than overwriting matches what Review's own UI already does — it
disables Accept, Reject, Replace and Reviewed on any row whose status is not
`pending` — so no legitimate flow is blocked: a request to re-decide a settled
row only ever comes from a stale tab, a double click, or a decision that landed
in between. Review surfaces the 409 as a toast and reloads, instead of
swallowing the click.

`alternative` is deliberately *not* on the list — a runner-up is awaiting a
decision, not carrying one — but "undecided" is where the two batch endpoints
stop agreeing, and deliberately so (the full rule is below under *Ambiguity*,
and stated once here so the two places cannot drift):

**The alternative-row contract.** Wherever an `alternative` row exists on a
`(detection, classifier_model)`, `batch-accept` accepts nothing on that key —
neither the alternative itself nor the pending row it competes with, because
`_ambiguous_prediction_ids` keys ambiguity on exactly that pair and both rows
share it. Both come back under `skipped_ambiguous`, never `already_decided`,
since the reason is "we would be picking a winner you were not shown", not "a
decision already exists". `batch-reject` acts on both: submitted directly, an
alternative is rejected on its own (its winner untouched — dismissing a
runner-up says nothing about the row it lost to); submitted as the top-1, its
alternatives are swept down with it, the same rule the single-photo reject
already applies. Promoting a runner-up therefore has exactly one path,
Review's `/api/predictions/<id>/accept`, which is where the user can see what
they are choosing between. Locked by
`test_batch_accept_skips_ambiguous_rows_with_alternatives` (both rows in one
call) and `test_batch_reject_resolves_alternative_rows`.

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

**Workspace re-checked under the lock.** `_parse_prediction_ids` verifies
workspace ownership before `BEGIN IMMEDIATE`, but a folder detach is itself a
write — in WAL another connection can acquire the writer lock, delete the row
from `workspace_folders`, and commit in the window between parse and lock
succeeding. So `_out_of_workspace_prediction_ids(db, pred_ids)` runs under
the lock on both endpoints, skipping rows whose photo left the workspace mid-
flight and counting them back as `skipped_out_of_workspace`. Its own name for
the same reason `skipped_superseded` is: the user's next step is a panel
refresh, not Review, and folding it into any other count would misname the
problem. Reject shares this filter with accept — a workspace-scoped
`prediction_review` row for a now-foreign photo is the same class of leak the
accept side closes.

**Species pinned to what the button named.** Both accept panels group by
species and label the button "Accept on 38 Bald Eagle". If the row's grouping
changes between render and click, `accept_prediction` will apply a different
species than the button named: `/api/predictions/group/apply` in another tab
ungroups a burst member (clearing `individual` votes so
`_prediction_consensus_species` falls back to the raw per-frame label), or
per-vote edits shift the winner. Browse now sends `expected_species` with
every accept and `_species_drifted_prediction_ids` skips rows whose current
consensus does not match, reporting them as `skipped_species_drifted`. The
existing "all resolve to one species" 400 in the loop below is not enough on
its own: it catches a mixed-outcome drift but not a batch where *every* row
drifted to the same new species. Accept-only — reject applies to whatever
detection carried the row regardless of the species label, so drift is a
functional no-op there. Optional on the wire so older callers keep working;
Browse always passes it.

**Atomicity, and who it covers.** A precondition that is not atomic with the
write it guards only narrows the race. Every prediction-decision route
therefore opens one `BEGIN IMMEDIATE` transaction
(`_begin_prediction_decision`) *before* its first precondition read and commits
once, after the writes and the history entry. SQLite's WAL mode allows a single
writer, so a second overlapping decision — a double-clicked Accept, or Browse's
batch accept and Review's single reject fired before either page reloads —
blocks at `BEGIN IMMEDIATE` and then reads the state the first one committed,
instead of acting on state it read before the first one wrote. Python's
`sqlite3` would otherwise open its implicit transaction at the first *write*,
leaving every check outside it.

"Every route" is the guarantee, and it took two passes to become true. The lock
started on the batch endpoints alone, which made them atomic against each other
and against nothing else: Review's `/api/predictions/<id>/reject` could read a
row's last-committed state (`pending`) while a Browse batch accept held the
writer lock, block at its own first write, and then land `rejected` over the
accept the instant that batch committed — leaving the species keyword the
accept added on a photo whose row says it was dismissed. Extending it to the
five single-row routes then left five more behind. A lock only some writers
take is not a lock, so the set is now enumerated:

| Route | What it decides |
|---|---|
| `/api/predictions/batch-accept` | Browse panel accept |
| `/api/predictions/batch-reject` | Browse panel reject |
| `/api/predictions/<id>/accept` | Review per-row accept |
| `/api/predictions/<id>/accept-subject` | Review additional-subject accept |
| `/api/predictions/<id>/reject` | Review per-row reject |
| `/api/predictions/<id>/reviewed` | ID Conflicts "mark reviewed" |
| `/api/predictions/<id>/replace-keywords` | Review replace-keyword accept |
| `/api/predictions/group/apply` | Burst group pick/reject (status tail only — the flag and keyword writes above it are not review state; its precondition is the render-time baseline described below, not "already decided") |
| `/api/highlights/confirm` | Highlight confirm, via `accept_prediction` |
| `/api/highlights/relabel` | Rejects each photo's top prediction |
| `/api/undo`, `/api/redo` | Replay `prediction_review` statuses out of edit history |

The list is declared in `_PREDICTION_DECISION_ROUTES` and checked against the
set derived from `create_app`'s own call graph by
`test_route_contract.py::test_every_prediction_decision_route_locks`, which
walks every function inside `create_app`, finds the routes that can reach a
`prediction_review` writer, and fails if that set differs from the declared one
or if any declared route does not reach `_begin_prediction_decision`. Derived
rather than hand-maintained, because both gaps so far were omissions, and a
list maintained by memory would produce a third.

The single-row endpoints hold the lock inline, rolling back explicitly at each
early exit; the multi-exit routes go through `_under_prediction_decision_lock`,
which rolls back in a `finally`. Both matter for the same reason: a 404 or 409
returns without committing, and leaving `BEGIN IMMEDIATE` open would hold the
database's single writer lock for the rest of that connection's life. Undo and
redo run their edit-recipe XMP queueing *after* the locked section — it commits
on its own and touches no prediction state.

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

**Group apply compares against a baseline, not against "decided".** Every other
decision route treats an already-decided row as untouchable, and can, because
its buttons only exist on pending rows: if the row is decided, somebody else
decided it. The burst modal breaks that inference. It opens from any card
carrying a `group_id` — including one this same user applied a minute ago — and
`loadGroupData` re-derives picks and rejects from quality scores rather than
from the stored statuses. Refusing every decided row there would block a real
flow (re-open the burst, change the split, apply) *and* would report the user's
own prior decision as somebody else's, which is the mis-description
`CORE_PHILOSOPHY.md` rules out.

So `grmApply` sends `observed`: the status it displayed for each member when it
loaded. `_stale_group_apply_photos` skips a photo only when its member no
longer holds the status the modal showed — the same rule as everywhere else in
this PR ("decided after the payload was rendered"), stated against a baseline
instead of inferred from a button's existence. A deliberate re-decision passes
(observed `accepted` still matches current `accepted`); an accept or reject
that landed from Browse or a second tab does not.

The check runs **once**, with the writer lock already held, and every write
the route makes happens after it and inside the same transaction. Getting
there took four passes, each of which moved one write across the boundary and
left the next one behind:

1. reads were hoisted above the writes they described;
2. the writes were serialized with `BEGIN IMMEDIATE`;
3. the route sweep found the decision routes that took no lock at all;
4. group apply's status tail moved under the lock — leaving the flag, keyword,
   pending-change and history writes committing outside it.

That fourth shape is why one check is not enough and two are worse than they
look. With a pre-lock check *and* an in-lock recheck, a decision committing
between them made the recheck skip only the status write, while the writes
already committed stayed: the photo ends up flagged and tagged with a species
whose prediction reads `rejected` — exactly the contradiction the precondition
exists to prevent, reached from the remaining side. Splitting a decision into
"the part under the lock" and "the part beside it" recreates the race no
matter which part moves. "These writes are not review state" was the reasoning
that kept them outside; it was never a reason they could be *decided*
separately.

So the whole body of `_apply_group_decisions` is the decision. Enumerated,
because "which side of the boundary is this on" is the question that kept
being answered one write at a time:

| Side effect | Inside the lock | After the staleness decision |
| --- | --- | --- |
| `add_keyword` (species row) | yes | yes |
| `update_photo_flag` — picks and rejects | yes | yes |
| `tag_photo` (species keyword) | yes | yes |
| `queue_change` `keyword_add` (XMP sync) | yes | yes |
| `record_edit` `keyword_add` (history) | yes | yes |
| `queue_flag_change_if_enabled` (XMP sync) | yes | yes |
| `record_edit` `flag` (history) | yes | yes |
| `update_predictions_status_by_photo` | yes | yes |
| `ungroup_prediction` (`removed`) | yes | deliberately not gated |
| `_prune_edit_history` | after the commit | n/a |

Two entries are deliberately not symmetric. Ungrouping is inside the
transaction but not gated on the baseline: it changes which rows the modal
shows together, not what any of them means, and a rolled-back apply must not
leave a surviving ungroup behind. Pruning history runs after the commit
because `record_edit(_commit=False)` defers it — the same shape the batch
endpoints use.

Every write passes `_commit=False`; a helper committing halfway would release
the writer lock in the middle of the decision. `update_photo_flag` (and
`PhotoReviewRepository.set_flag` beneath it) gained that parameter for this.
Its workspace verification still runs, and it is the first write for both
picks and rejects, so a photo that detached from the workspace after the
route's pre-lock check raises before it can be tagged or have
workspace-scoped review state written — and because that now happens inside
the transaction, the resulting 403 leaves *nothing* written instead of a
half-applied burst.
`test_group_apply_writes_every_side_effect_in_one_transaction` drives exactly
that path and asserts the earlier photo's flag, keyword, pending change,
history entry and even the created keyword row are all absent.

Skipped photos come back as `already_decided`, counted in photos (the unit the
modal works in), and the modal names them in a toast and reloads rather than
patching its local statuses. An absent `observed` means no baseline and
applies unconditionally — the server can only refuse what the client claims to
have seen — so `test_group_apply_client_sends_the_observed_baseline` asserts
the rendered page still sends it, the same pairing as the panel's
decided-status test.

**Known remaining gap: undo/redo replay.** `api_undo` and `api_redo` take the
decision lock (pass 3 above put them on the list), but
`Database.undo_last_edit` → `_apply_undo` calls helpers that commit per item —
`update_photo_rating`, `update_photo_flag`, `set_color_label`,
`set_photo_edit_recipe`, `untag_photo`, and an explicit commit in the
`prediction_accept` branch. The first of those releases the writer lock
mid-replay, so an undo spanning several items is ordered but not atomic
against a concurrent decision. It is strictly better than the pre-PR state
(no lock at all) and it is the same class as the four passes above. Closing it
means threading `_commit` through eight more `Database` helpers and
re-auditing `_apply_redo`, which is a change to undo/redo rather than to
Browse's prediction panel — recorded here rather than folded into this PR.

**The endpoint's contract.** Every row `batch-accept` accepts is one that is
still undecided, still unambiguous, still from the current label set, still
in the caller's workspace, AND — when the caller passed one — still resolves
to the species the button named, all judged *at the moment of the write*
(literally that moment, since the checks and the write share one
transaction). Every check runs against the database rather than against
whatever the caller's panel believed. Rows failing any of them are skipped —
never accepted — and counted back as `already_decided`, `skipped_superseded`,
`skipped_ambiguous`, `skipped_out_of_workspace` and
`skipped_species_drifted`. A stale payload can accept *less* than the caller
asked for, never something the caller was not shown as acceptable. Skipping
beats a 400: the rest of the batch is still exactly what the user asked for,
and failing the whole call would strand 34 honest accepts on one row that
moved.

Two consequences at the caller:

- Browse names the gap. `_reportSkippedAccepts` turns a non-zero
  `already_decided` / `skipped_ambiguous` / `skipped_superseded` /
  `skipped_out_of_workspace` / `skipped_species_drifted` into a toast,
  because the distance
  between "Accept on 35" and 33 accepts has to be spoken, not left for the
  user to spot. The counts stay separate because the next step differs —
  a decided row needs nothing, an ambiguous one needs Review, a superseded or
  out-of-workspace row is simply replaced by the refresh, and a
  species-drifted row is a caller-side re-render (the row was still there,
  its label just moved). `_reportSkippedRejects` does the same for
  `batch-reject`'s counters — `already_decided`, `skipped_superseded` and
  `skipped_out_of_workspace` — using the same wording for the same fact. A
  reject whose photo detached mid-flight comes back `rejected: 0,
  skipped_out_of_workspace: 1`, and reporting nothing for it would be a click
  that visibly did nothing: precisely the silence this section exists to
  remove.
- The undo toast is gated on `accepted`. A payload whose rows have all been
  decided or turned ambiguous returns `accepted: 0` and records no undoable
  edit, so an unconditional toast would advertise — and Ctrl+Z would
  reverse — some older, unrelated edit.

The same standard applies to a failure the caller cannot see the inside of. ID
Conflicts surfaces the new 409s, and "not applied" is a claim about the
database that only holds when the server answered: these routes commit inside
`BEGIN IMMEDIATE`, so a dropped connection or a 5xx leaves an outcome this end
genuinely cannot distinguish. `jsonFetch` therefore carries the response status
on the error (`err.status`, `err.answered`). `answered` is true for a 4xx *and*
for any response carrying an `error` body whatever its status — a body the
server wrote to explain its refusal is itself the answer, and classifying on
the status alone reported those as unconfirmed, which understates what this end
knows. Only a status-only failure (a 5xx, or a fetch that threw) stays unknown.
The single-row handler reports a
refusal as a refusal and anything else as "could not confirm the decision", and
`batchAction` counts the two classes separately in its toast. Both paths reload
afterwards, which is what actually resolves the ambiguity.

**The 409 the batch caused itself.** That accounting still had one row it could
not name honestly. `batchAction` loops the single-row route once per selected
photo, and accept, replace and accept-subject expand through the burst group —
so two selected photos of one burst are *one* decision on the server. The first
request settles both rows; the second meets the terminal-status 409 and was
counted as "not applied" for work that had just landed, under this batch's own
hand. Both directions of the same rule are wrong there: it is a false claim
about the database, and it is a false claim about a write the caller performed.

The fix is on the write, not on the refusal. `/accept` and `/replace-keywords`
now return `prediction_ids` — the rows that transaction actually decided, taken
straight from `accept_prediction`'s existing `accepted_prediction_ids`, in the
shape `/accept-subject` already returned. `batchAction` collects them and skips
a later photo whose row is in the set, counting it in the denominator and
against no failure bucket. Nothing is deduplicated in advance from client-side
group membership, which would skip on a belief about the burst rather than on
what was written.

The rejected alternative — and it was briefly the shipped one, in `4d0d723c`,
which this replaces — was to read the row's current status off the 409 and let
the client treat "already `<what you asked for>`" as success, either from a
status field added to `json_error` or by substring-matching the refusal text.
It answers the wrong question. The server cannot say *who* settled a row, so an
accept the user made in another tab is scored as this batch's success. For
replace it is worse than imprecise: a replace answering "already accepted" may
be sitting on a plain accept that never stripped the old species keywords, so
reporting the replacement as done is the same false claim pointing the other
way — the failure mode swapped for its mirror rather than removed. And the
match itself was a client-side parse of a Python f-string, which makes the
message wording load-bearing without saying so anywhere near it. The version
carried on the write has none of that: a row settled by anyone else still 409s
and is still reported as a refusal, which is what the second half of the
regression test pins.

This is where the alternative-row contract stated above lands: because the
check keys on `(detection, classifier_model)`, neither a row with an
`alternative` sibling nor an `alternative` row itself can be accepted through
this endpoint, and both are reported as `skipped_ambiguous`. That matches what
the panel offers; Review's single-photo `/api/predictions/<id>/accept` is the
path that deliberately still accepts with alternatives on screen, and is where
the group-expansion behaviour is now covered. `batch-reject` is unaffected —
it has no ambiguity check because there is no winner to pick.

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
  alternative half of the same rule, in both its shapes: the pending row that
  *has* an alternative and the alternative row itself, submitted in one call,
  both skipped as ambiguous (not as decided), and the no-op leaves nothing on
  the undo stack.
- `test_batch_reject_resolves_alternative_rows` — the reject side of that
  contract: an alternative submitted directly is rejected while its winner
  stays undecided, and an alternative sibling of a submitted top-1 goes down
  with it.
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
- `test_single_reject_serializes_with_concurrent_batch_accept` — the same proof
  for the single-row side, through the same deterministic interleave hook.
- `test_every_prediction_decision_route_locks` — the completeness check
  described above: the declared route list versus the one derived from
  `create_app`'s call graph, plus an assertion that each declared route reaches
  `_begin_prediction_decision`. It is the only test that fails when a *new*
  decision route forgets the lock.
- `test_browse_panel_treats_reviewed_status_as_decided` — the panel's
  `PREDICTION_DECIDED_STATUSES` is asserted equal to
  `Database.DECIDED_PREDICTION_STATUSES`, so drift in either direction fails
  rather than only the direction that was fixed.
- `test_mark_reviewed_transition_and_reject_refuses_reviewed`,
  `test_single_accept_refuses_reviewed_prediction`,
  `test_batch_endpoints_skip_reviewed_predictions` and
  `test_grouped_accept_expansion_skips_reviewed_group_member` — `reviewed` is
  terminal on the single-row routes (409, state and history untouched),
  counted as `already_decided` by the batch pair, and invisible to grouped
  expansion.
- `test_id_conflicts_batch_accept_reports_group_expanded_rows_as_applied`
  (`test_app.py`) — the real `batchAction`, lifted from the rendered page and
  run under Node against a stub server that expands a decision through the
  burst exactly as `accept_prediction` does. Two photos of one burst produce
  one request and no failure toast; the control case, where the second row was
  settled by somebody else, still sends both and still reports "1 of 2 not
  applied". Verified to fail against the pre-fix template.
- `test_grouped_accept_names_every_row_it_decided` and
  `test_grouped_replace_names_every_row_it_decided`
  (`test_predictions_api.py`) — the server half: both routes return the full
  set of burst rows the write settled, and the second row's own request is
  still a 409, which is why the client skips it rather than classifying it
  afterwards.
- `test_decision_routes_leave_no_transaction_open`,
  `test_undo_of_an_accept_still_works_under_the_decision_lock` and
  `test_group_apply_records_decisions_under_the_lock` — the newly locked
  routes still do what they did, and no refusal or empty path strands the
  writer lock: each ends with a decision that must still succeed.
- `test_group_apply_skips_photos_decided_since_the_modal_rendered`,
  `test_group_apply_allows_a_deliberate_re_decision`,
  `test_group_apply_rejects_a_malformed_baseline` and
  `test_group_apply_client_sends_the_observed_baseline` — the baseline
  precondition in all four directions: the stale photo keeps its decision and
  takes no flag or keyword while the rest of the burst applies, the user's own
  re-decision still goes through, an unreadable baseline is a 400 rather than
  a silent reversion to overwriting, and the modal still sends one.
- The panels' invalidation lives in `_refreshBrowseKeywordState` (before its
  early returns) and in a `vireo:edit-history-changed` listener, so the
  coverage is structural rather than per-call-site.
- `test_browse_sidebar_panels_refresh_on_undo_and_redo` — the undo/redo
  handler goes through `refreshBrowseSidebarPanels`, and that function
  reaches the prediction *and* keyword halves (including clearing the
  selection keyword cache key before reloading, or the refresh is a no-op).
- "Open in Review" sends an explicit empty `filters` handoff.
- `test_browse_detail_prediction_buttons_survive_apostrophe_species` — the
  panel's real renderer is executed under `node` (its own functions, lifted
  from the served page, plus the real `escapeHtml`/`escapeAttr`) for a
  prediction on `Say's Phoebe`. The emitted markup is parsed with the stdlib
  HTML parser and the Accept and Reject buttons must carry *exactly* their
  four expected attributes — the old bug showed up as extra attributes split
  out of the species — with no fragment of the name in any attribute
  anywhere. Then the rendered button's attributes are fed back through the
  delegated listener, which must call `acceptDetailPredictions` with the
  species intact. Asserting the markup alone would pass on a panel whose
  buttons no longer do anything.
- `test_browse_reject_toast_names_workspace_detach_skips` — the same node
  harness runs `_reportSkippedRejects`: a `skipped_out_of_workspace` reject
  produces exactly one toast naming it, it appears alongside the other
  reasons rather than replacing them, and a clean reject stays silent.

Plus a Playwright pass driving the real panel — selecting photos, accepting,
confirming the Review queue drains.
