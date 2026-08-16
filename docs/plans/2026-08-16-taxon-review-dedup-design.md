# Taxon-keyed Review deduplication — design

**Date:** 2026-08-16
**Status:** Proposed
**Motivating bug:** Two classifiers (BioCLIP-2.5 and iNat21) classified the same
8-photo burst as "Eurasian Blue Tit" and "Blue Tit". Both labels mean
*Cyanistes caeruleus*, but Review shows two cards for the same eight photos,
and accepting one leaves the other pending as a hidden duplicate.

## Problem

Three independent mechanisms conspire to produce duplicate cards:

1. **Burst group IDs are minted per classify job** —
   `gid = f"g{job_id[-6:]}-{group_count:04d}"` in
   `classify_job.py::_store_grouped_predictions`. Two models classifying the
   same burst always produce two distinct `group_id`s.
2. **Review deduplicates client-side by `group_id` only** —
   `review.html::getVisibleItems` keeps the first prediction per `group_id`.
   Nothing compares cards across groups.
3. **Cross-model agreement is matched by string equality** — the one place
   that resolves agreeing models together, `db.py::accept_subject_species`
   (used by Compare), matches siblings with
   `lower(trim(pr.species)) = lower(trim(?))`. "Blue Tit" ≠
   "Eurasian Blue Tit", so even that path would not have merged this pair.

A hard-coded alias ("Eurasian Blue Tit" → "Blue Tit") would fix one species
and recur for every regional common name. The durable fix is to compare
predictions by **taxon**, not by label string.

## Goals

- One Review card per (species claim, burst/subject), regardless of how many
  classifier models produced it or which common-name variant each used.
- Accepting or rejecting that card resolves **all** agreeing model rows — no
  hidden pending duplicates.
- Both models' outputs stay visible on the merged card (model, confidence,
  votes) — per CORE_PHILOSOPHY, the merge must not hide what each model said.
- Works retroactively on existing prediction rows with no destructive
  migration.
- Genuine disagreements (different taxa) keep separate cards. This feature
  only merges *agreement*.

## Non-goals

- Changing burst grouping itself (timestamp windows, similarity refinement).
- Retroactively renaming existing keywords ("Eurasian Blue Tit" keywords on
  disk stay as they are; see §4 for how new accepts avoid fragmentation).
- Merging across taxonomic ranks (a genus-level "Cyanistes" claim does not
  merge with a species-level claim). Strict taxon equality only.
- Cross-taxonomy synonym resolution beyond what the local taxa tables and the
  cached iNat alternate-name lookup provide.

## Design

Four parts: (1) a canonical taxon key per prediction, (2) server-side card
building in `/api/predictions`, (3) taxon-keyed cross-model accept/reject,
(4) display-name and keyword canonicalization.

### 1. Canonical taxon key

New helper (in `taxonomy.py`, usable from both `app.py` and `db.py`):

```python
def taxon_key_for(species, scientific_name, tax):
    """Return a canonical merge key for a prediction.

    ('taxon', taxon_id)      when the label resolves in the taxonomy
    ('name', folded_string)  fallback — merges only identical labels
    """
```

Resolution ladder:

1. `predictions.scientific_name` → `Taxonomy.lookup()` (the `_by_scientific`
   index). This column is populated at classify time from the label set's own
   taxonomy metadata, so it is the most reliable signal when present.
2. `predictions.species` → `Taxonomy.lookup()` (common name, scientific name,
   punctuation-normalized common name).
3. `predictions.species` → `Taxonomy.api_lookup()`. This already exists and
   already solves the motivating case: iNat's autocomplete matches
   *alternate/regional* common names ("Eurasian Blue Tit" is precisely an
   alternate name for *Cyanistes caeruleus*), and both hits and misses are
   cached persistently, so the network cost is one-time per unique label.
   Offline or API failure degrades to step 4 — cards simply don't merge until
   the name resolves; no error surfaces.
4. Fallback: `('name', ascii_folded(species))` using the same folding rules
   as `_folded_species_key` in `classify_job.py`. Unresolvable labels
   (custom label files, informal groups like "gull sp.") merge only when the
   strings are identical — i.e., current behavior is preserved for them.

Rules:

- A `('name', …)` key never merges with a `('taxon', …)` key, even if one is
  a substring of the other. No guessing.
- `NULL`/empty species never merges with anything.
- Rank is respected implicitly: genus and species resolve to different
  `taxon_id`s, so they never merge.

**No schema change.** The key is computed at read time. The taxonomy JSON is
already held in memory as dicts, so per-row resolution is O(1); `api_lookup`
runs only on cache misses. If profiling later shows cost on large Review
payloads, an additive nullable `predictions.taxon_id` column with lazy
write-back is the escape hatch — explicitly deferred (solo-user DB, easy to
add later; see also the `user_version` drift caveat in memory — any future
column should be guarded by a `db_meta` marker or a PRAGMA column check, not
a version-gated migration).

### 2. Server-side card building in `/api/predictions`

Today the client dedups by `group_id`, which cannot see cross-model
duplicates. Move card identity to the server, where the taxonomy is
available, and where the accept path needs the same key anyway.

`api_predictions` (app.py:15196) computes for each returned prediction:

- `taxon_key` — from §1, serialized as e.g. `"taxon:13094"` or
  `"name:blue tit"`.
- `card_id` — the merge unit, computed as follows.

**Merge rule.** Build a graph whose nodes are burst groups (by `group_id`)
plus singleton predictions (no group). Connect two nodes when they have the
**same `taxon_key`** and their **photo memberships intersect**. Each
connected component is one card; `card_id` is the lexicographically smallest
member `group_id` (or `p<prediction_id>` for pure singletons), prefixed with
the taxon key so distinct taxa can never collide.

Why overlap, not identical membership: the two models' burst groups for the
same event frequently differ by a frame or two — grouping runs per job, and
each model independently diverts frames to auto-accept based on the XMP
keywords present *when that model ran*. Requiring identical membership would
silently fail to merge most real duplicates (the 8-vs-7 case). Union
membership is safe because every member row asserts "these photos show taxon
X" — exactly the claim accepting the card applies, photo by photo.

Why connected components can't over-merge: an edge requires same taxon *and*
shared photos. Two different bursts of the same species on different photos
stay separate cards; two models' views of the same burst merge.

**Payload changes.** Each prediction row gains `taxon_key`, `card_id`, and
`display_name` (§4). Rows are *not* collapsed server-side — the client keeps
all rows (it already receives every group member) and dedups by `card_id`
instead of `group_id`, so per-model detail remains available for rendering.

**Client changes (`review.html`).**

- `getVisibleItems` dedups by `card_id` (fallback to `group_id` then
  prediction id for old payload shapes during rollout).
- The card shows: union photo count; one chip per model with that model's
  consensus confidence and vote counts (e.g.
  `BioCLIP-2.5 92% · iNat21 88%`); the display name (§4).
- The group review modal opens with the union membership. New endpoint
  `GET /api/predictions/card/<card_id>` returns the union of member groups
  with per-photo, per-model rows — the existing
  `/api/predictions/group/<group_id>` machinery stays for compatibility and
  is what the card endpoint composes.

**Filter semantics.** When the model filter (`currentModel !== 'all'`) or
the labels-fingerprint filter is active, cards are built from the matching
rows only — merging becomes an intra-model no-op and the page shows exactly
what that model said. This keeps "filter by model" honest (a merged card has
no single model) and costs nothing: the server already receives the filter
context, or the client can group the filtered subset by `(taxon_key,
group_id)`. Simplest implementation: server computes `card_id` per row from
the *full* row set; the client, when a model filter is active, falls back to
`group_id` dedup for the filtered rows.

### 3. Cross-model accept and reject

Generalize the pattern `accept_subject_species` already implements, from
string matching to taxon matching, and from Compare-only to Review.

**Accept.** `db.accept_prediction` currently accepts the clicked prediction
and, if grouped, its group siblings — restricted to `pr.classifier_model =
?`. After that pass, add a sibling-resolution pass: for each affected photo,
find pending predictions on that photo whose taxon key matches the accepted
taxon, from **any** classifier model, restricted per model to its latest
`labels_fingerprint` (reuse the latest-fingerprint subquery from
`accept_subject_species`), and accept each via the existing
`_accept_for_photo` primitive.

- Matching is by `(photo_id, taxon_key)`, not `detection_id`. This covers
  models that classified detections from different detectors (different
  detection rows for the same photo), and it is safe for multi-species
  photos: two birds of *different* species have different taxon keys and are
  untouched; two detections of the *same* species on one photo collapse into
  one photo-level keyword anyway.
- Taxon keys are computed in Python via §1's helper (the candidate set — 
  pending predictions on the card's photos — is small), so no SQL-side
  taxonomy join is needed.
- **Undo:** `_accept_for_photo` already records every status flip —
  including status-only no-ops — in `affected`, which feeds the
  edit-history/undo machinery. Sibling accepts go through the same
  primitive, so their flips are recorded and undoable for free. The
  `prediction_accept` history entry therefore restores *both* models' rows
  to pending on undo.

**Reject.** Mirror logic: rejecting a card rejects all member predictions
(all models) for the card's photos and taxon. Today reject is per
prediction/group; it gains the same sibling pass.

**Compare.** `accept_subject_species` swaps its
`lower(trim(species))` equality for the same taxon-key helper. Its
detection-scoped, single-photo semantics stay unchanged.

### 4. Display name and keyword canonicalization

**Card display name:** the resolved taxon's preferred common name from the
taxonomy ("Blue Tit"), with the raw per-model labels visible on the model
chips ("iNat21: Blue Tit · BioCLIP-2.5: Eurasian Blue Tit"). Unresolved
(`name:`-keyed) cards display the raw label as today.

**Keyword written on accept** — precedence:

1. If a keyword already exists whose `keywords.taxon_id` matches the card's
   taxon, reuse it — *its* name is what gets written. This keeps new accepts
   consistent with photos already tagged (no "Blue Tit" keyword appearing
   alongside an established "Eurasian Blue Tit" keyword), because keywords
   are global across workspaces and feed XMP sidecars on disk.
2. Otherwise, create/use the preferred common name.

**Transparency requirement (CORE_PHILOSOPHY):** when the keyword string that
accept will write differs from the card's display name, the card says so —
e.g. a subdued `tags as "Eurasian Blue Tit"` note under the title. The card
must answer "what will accepting this do", not just "what species is this".

No retroactive rename of existing keywords. Follow-up (out of scope):
surface taxon-duplicate keywords ("2 keywords resolve to *Cyanistes
caeruleus*") on the Keywords page with a one-click merge.

## Edge cases

- **Unresolvable labels** (custom label files, "Duck sp."): fold-string key;
  merge only on identical strings — behavior unchanged from today.
- **Taxonomy not loaded / offline:** every key degrades to `name:`;
  Review behaves exactly as it does today. Deterministic, no errors.
- **Alternatives** (`status="alternative"` rows): not review cards; excluded
  from card building and untouched by sibling resolution.
- **Workspace scoping:** all card building and sibling resolution joins
  through `prediction_review` for the active workspace, as accept does now.
- **`api_lookup` latency:** only on first-ever sight of an unresolved label,
  10s timeout, result (or miss) cached persistently. Never in a tight loop:
  resolution iterates unique labels, not rows.
- **Two same-taxon groups from one model** (e.g. re-runs under different
  fingerprints): they merge into one card if their photos overlap — which is
  the correct de-duplication, and the fingerprint filter still separates
  them when the user asks.

## Alternatives considered

- **Hard-coded alias map** ("Eurasian Blue Tit" → "Blue Tit"): fixes one
  species, recurs for every regional name, and encodes taxonomy opinions in
  code. Rejected.
- **Normalize labels at classify/store time** (rewrite `species` to the
  preferred common name before insert): destroys fidelity of what the model
  actually said (transparency violation), breaks the
  `labels_fingerprint`-aware skip gate (`get_existing_prediction_photo_ids`
  matches stored rows against label-set contents), and risks colliding with
  the `UNIQUE(detection_id, classifier_model, labels_fingerprint, species)`
  constraint on re-runs. Rejected.
- **Client-side merging:** the client lacks the taxonomy, and the accept
  path needs the same key server-side regardless. Rejected.
- **Merge only on identical photo membership** (the naive rule): silently
  fails whenever the models' groups differ by one frame, which is common;
  the feature would appear broken. Rejected in favor of overlap components.

## Implementation phases

Each phase lands as its own PR and is independently useful.

1. **Taxon key helper** (`taxonomy.py`) + unit tests: scientific-name hit,
   common-name hit, normalized hit, alternate-name via mocked `api_lookup`,
   unresolvable fallback, NULL handling, rank separation.
2. **Server-side cards**: `taxon_key`/`card_id`/`display_name` in
   `/api/predictions`, card endpoint, `review.html` dedup + merged-card
   rendering + filter semantics. API tests: the Blue Tit fixture (two
   models, same taxon, 8-vs-7 overlap) yields one `card_id`; different taxa
   don't merge; model filter yields per-model cards.
3. **Cross-model accept/reject** + undo coverage. DB tests: accepting the
   merged card flips both models' rows; undo restores both; reject mirrors;
   Compare's `accept_subject_species` matches across name variants.
4. **Keyword canonicalization** (taxon-matched keyword reuse) + the
   "tags as …" transparency note.

## Test plan

```bash
python -m pytest tests/test_workspaces.py vireo/tests/test_db.py \
  vireo/tests/test_app.py vireo/tests/test_photos_api.py \
  vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py \
  vireo/tests/test_darktable_api.py vireo/tests/test_config.py -v
```

plus the new tests per phase above. Manual verification: reproduce the
original state (both classifiers over the same burst with the two label
variants), confirm one card, accept it, confirm zero pending rows remain for
either model, undo, confirm both return.
