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
3. `predictions.species` → **cached** `Taxonomy.api_lookup` result only —
   i.e., the request path reads the persistent iNat-lookup cache but must
   never issue a live HTTP request. iNat's autocomplete matches
   *alternate/regional* common names ("Eurasian Blue Tit" is precisely an
   alternate name for *Cyanistes caeruleus*), so cached hits let the merge
   work; cache misses degrade to step 4 until a background resolver (below)
   populates the cache.
4. Fallback: `('name', ascii_folded(species))` using the same folding rules
   as `_folded_species_key` in `classify_job.py`. Unresolvable labels
   (custom label files, informal groups like "gull sp.") merge only when the
   strings are identical — i.e., current behavior is preserved for them.

**No live network I/O in `/api/predictions`.** `Taxonomy.api_lookup`'s
existing 10-second HTTP timeout and its silent-on-failure behavior (a
connection error returns without adding the label to `_api_misses`, so a
firewalled host would retry per-refresh) are the reason: calling it inline
would let one offline install stall Review for 10s per unresolved label on
every load. Resolution therefore splits in two:

- **Request path (§1 above):** cache-only reads. `taxon_key_for` calls a new
  `Taxonomy.cached_api_lookup(label)` that returns a hit from the persistent
  cache, a sentinel for a cached negative, or `None` for "unknown, ask the
  resolver". Never issues an HTTP request; never blocks.
- **Background resolver:** unresolved labels are enqueued (a) at classify
  time when `_store_grouped_predictions` first stores a row whose
  `species`/`scientific_name` don't hit steps 1–2, and (b) opportunistically
  when `/api/predictions` observes a `name:` fallback for a label it has
  not seen. A single-flight background job in `jobs.py`
  (`resolve_taxonomy_labels`) drains the queue, calls `api_lookup` with the
  existing 10s timeout, and **persists both hits and misses** — extending
  `Taxonomy._api_misses` to a bounded on-disk negative cache (label →
  {resolved_at, retry_after}) with an exponential-then-daily retry so a
  transient outage doesn't pin a label as unresolvable forever, and so an
  offline host never re-hits the network on refresh. The Review card for a
  still-unresolved label simply doesn't merge until the resolver succeeds
  and the next refresh sees the cache entry.

Rules:

- A `('name', …)` key never merges with a `('taxon', …)` key, even if one is
  a substring of the other. No guessing.
- `NULL`/empty species never merges with anything.
- Rank is respected implicitly: genus and species resolve to different
  `taxon_id`s, so they never merge.

**No schema change to `predictions`.** The key is computed at read time
from the in-memory taxonomy dict (O(1) per row) plus the cached iNat lookup
table. The background resolver's negative cache is stored in the existing
`taxonomy_api_cache` mechanism (or a peer table with the same lifecycle) —
additive rows, not a schema migration on hot tables. If profiling later
shows cost on large Review payloads even with a cold cache, an additive
nullable `predictions.taxon_id` column with lazy write-back is the escape
hatch — explicitly deferred (solo-user DB, easy to add later; see also the
`user_version` drift caveat in memory — any future column should be guarded
by a `db_meta` marker or a PRAGMA column check, not a version-gated
migration).

### 2. Server-side card building in `/api/predictions`

Today the client dedups by `group_id`, which cannot see cross-model
duplicates. Move card identity to the server, where the taxonomy is
available, and where the accept path needs the same key anyway.

`api_predictions` (app.py:15196) computes for each returned prediction:

- `taxon_key` — from §1, serialized as e.g. `"taxon:13094"` or
  `"name:blue tit"`.
- `card_id` — the merge unit, computed as follows.

**Merge rule.** Build a graph whose nodes are burst groups plus singleton
predictions (no group). Connect two nodes when they have the **same
`taxon_key`** and their **photo memberships intersect**. Each connected
component is one card; `card_id` is derived from the lexicographically
smallest member's stable node key (see below) prefixed with the taxon key
so distinct taxa can never collide, then URL-safe encoded (see "Card ID
encoding" below).

**Node identity.** The graph keys each burst-group node as
`(classifier_model, labels_fingerprint, group_id)`, not by `group_id`
alone. `_store_grouped_predictions` mints group IDs as
`f"g{job_id[-6:]}-{group_count:04d}"`, which can collide across process
restarts or between concurrent classify jobs whose truncated `job_id`
suffix happens to match. Keying by the full `(model, fingerprint,
group_id)` tuple guarantees that two unrelated bursts never occupy the same
node even if their raw `group_id`s collide, so the connected-component
computation stays sound without depending on `group_id` uniqueness.
Singleton nodes key on `(classifier_model, labels_fingerprint,
"p" + prediction_id)`. A follow-up cleanup should also widen
`_store_grouped_predictions` to use the full `job_id` (or a UUID) so
downstream storage and history are collision-free too — but the merge
graph does not depend on that landing first.

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
  `GET /api/predictions/card?id=<card_id>` returns the union of member
  groups with per-photo, per-model rows — the existing
  `/api/predictions/group/<group_id>` machinery stays for compatibility and
  is what the card endpoint composes.

**Card ID encoding.** `card_id` is treated as opaque bytes on the wire.
For `name:`-keyed cards the folded label is embedded in the id, and folded
labels come from arbitrary user-supplied label files that may contain `/`,
`?`, `#`, `%`, or other URL-significant characters. A Flask
`<card_id>` path converter does not match a decoded slash even when the
client uses `encodeURIComponent`, so the merged-card modal for a label
like `hawk/owl` would 404. Two-part rule to avoid that:

1. The card endpoint takes the id as a **query parameter**
   (`/api/predictions/card?id=<card_id>`), not as a path segment, so any
   byte survives the round trip once percent-encoded.
2. The server-emitted `card_id` string is base64url-encoded (RFC 4648
   §5, unpadded — alphabet `[A-Za-z0-9_-]`) over the raw
   `<taxon_key>|<smallest_member_key>` bytes. This makes ids safe to
   embed anywhere (DOM attributes, path segments if some future route
   wants them, log lines) without further escaping, keeps them opaque to
   the client, and gives a stable string that the server can decode back
   to `(taxon_key, member_key)` when looking up the component. Where the
   client persists an id (e.g. in URL hash for deep links), it stores the
   already-encoded form verbatim.

**Filter semantics.** When the model filter (`currentModel !== 'all'`) or
the labels-fingerprint filter (`currentLabelsFingerprint !== 'all'`) is
active, cards are built from the matching rows only — merging becomes an
intra-filter no-op and the page shows exactly what that model/fingerprint
said. This keeps "filter by model" and "filter by label set" honest (a
merged card has no single model or fingerprint) and costs nothing: the
server already receives the filter context, or the client can group the
filtered subset by `(taxon_key, group_id)`. Simplest implementation:
server computes `card_id` per row from the *full* row set; the client,
whenever **either** the model filter or the labels-fingerprint filter is
active, falls back to `(taxon_key, group_id)` dedup over the filtered
rows and never invokes the merged-card endpoint from that view. This
matters for both correctness and privacy of the filter: if server-computed
components stitched groups A and C together only through a group B whose
fingerprint the user has just filtered out, opening the "A+C card" from
the filtered view would otherwise re-expose B's hidden rows through the
`/api/predictions/card/<card_id>` union. Falling back to per-group cards
in filtered views eliminates that exposure entirely. When the user clears
the filter, the full server components (and the merged card endpoint)
apply again.

### 3. Cross-model accept and reject

Generalize the pattern `accept_subject_species` already implements, from
string matching to taxon matching, and from Compare-only to Review.

**Accept.** `db.accept_prediction` currently accepts the clicked prediction
and, if grouped, its group siblings — restricted to `pr.classifier_model =
?`. The taxon-keyed accept operates on the **entire card component's photo
union**, not just the clicked group's photos:

1. **Resolve the card.** Given the clicked prediction, look up its
   `card_id` (accept API accepts a `card_id`; where the client still sends a
   `prediction_id`/`group_id`, the server maps it to the containing card
   using the same graph as `/api/predictions`, so a stale or narrower client
   can't downgrade the accept scope). The card exposes both `taxon_key` and
   the **union photo set** across every group/singleton in its component.
2. **Enumerate photos from the component.** The candidate photo set is the
   union of every member group's/singleton's photos — not the clicked
   group's photos. This is what fixes the transitive case: if the card is
   {A: photos 1-2, B: photos 2-3, C: photos 3-4}, accept iterates photos
   {1, 2, 3, 4}, not {1, 2}.
3. **Sibling pass, taxon-matched, per photo.** For each photo in the union,
   find pending predictions on that photo whose taxon key matches the card's
   `taxon_key`, from **any** classifier model, restricted per model to its
   latest `labels_fingerprint` (reuse the latest-fingerprint subquery from
   `accept_subject_species`), and accept each via the existing
   `_accept_for_photo` primitive.

- Matching is by `(photo_id, taxon_key)`, not `detection_id`. This covers
  models that classified detections from different detectors (different
  detection rows for the same photo), and it is safe for multi-species
  photos: two birds of *different* species have different taxon keys and are
  untouched; two detections of the *same* species on one photo collapse into
  one photo-level keyword anyway.
- Taxon keys are computed in Python via §1's helper (the candidate set —
  pending predictions on the card's union photos — is bounded by the card
  size), so no SQL-side taxonomy join is needed.
- **No need to iterate to closure.** The card component is fully materialized
  before the accept fires (§2 already builds it via connected components
  over `(same taxon_key, overlapping photos)`). Iterating photo-by-photo
  over the pre-computed union is closure — sibling matches on photo 4 are
  reached even though the clicked group only covered photos 1-2. A later
  push that introduces a *new* group after accept fires does not retroactively
  join this card; that new group appears as a fresh pending card on the next
  Review load, which is the intended behavior.
- **Undo:** `_accept_for_photo` already records every status flip —
  including status-only no-ops — in `affected`, which feeds the
  edit-history/undo machinery. Sibling accepts go through the same
  primitive, so their flips are recorded and undoable for free. The
  `prediction_accept` history entry therefore restores *every* member row
  across the component to pending on undo, not just the clicked group's.

**Reject.** Mirror logic: rejecting a card rejects all member predictions
(all models) across the same union photo set and card taxon. Today reject
is per prediction/group; it gains the same sibling pass, scoped to the
same component-wide photo union so transitive cards resolve completely.

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
- **`api_lookup` latency:** never on the request path. The Review GET
  reads cached results only; unresolved labels degrade to `name:` fallback
  until the background resolver populates the cache. Offline/firewalled
  installs therefore never wait on the network to render Review, and a
  transient outage does not degrade page latency at all.
- **Transitive card components:** the accept path always operates on the
  card's pre-computed union of photos, so a chain (A: 1-2, B: 2-3, C: 3-4)
  resolves in one click. See §3, "Enumerate photos from the component".
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
   common-name hit, normalized hit, alternate-name via **pre-seeded lookup
   cache** (no live HTTP in the test path), unresolvable fallback,
   NULL handling, rank separation. Also: `cached_api_lookup` returns
   `None` on miss and never opens a socket.
2. **Background taxonomy resolver** (`jobs.py::resolve_taxonomy_labels`):
   drains the enqueued-labels queue, persists hits and misses (with
   exponential-then-daily retry on misses), single-flight. Tests use a
   stubbed `api_lookup` and assert (a) hits populate the cache, (b) misses
   are recorded with a retry timestamp, (c) `/api/predictions` never calls
   `api_lookup` directly.
3. **Server-side cards**: `taxon_key`/`card_id`/`display_name` in
   `/api/predictions`, card endpoint, `review.html` dedup + merged-card
   rendering + filter semantics. API tests: the Blue Tit fixture (two
   models, same taxon, 8-vs-7 overlap) yields one `card_id`; different taxa
   don't merge; model filter yields per-model cards; **transitive overlap
   fixture** (three groups A/B/C forming an A-B-C chain, same taxon) yields
   one `card_id` covering the full union; **group-id-collision fixture**
   (two classify jobs whose truncated `job_id` suffix collides, minting the
   same raw `group_id` for disjoint photo sets) stays as two separate
   nodes and does not merge into one card; **cross-fingerprint hidden-row
   fixture** (groups A and C at fingerprint X, group B at fingerprint Y
   bridging them by shared photos) — with the fingerprint filter set to
   X, the client dedups A and C into per-group cards and the merged-card
   endpoint is not called; with the filter cleared, A+B+C become one card
   as expected; **URL-hostile card-id fixture** (a `name:`-keyed card
   derived from a custom label containing `/`, `?`, and `#`) round-trips
   through the base64url-encoded id and the card endpoint's query
   parameter without a 404.
4. **Cross-model accept/reject** + undo coverage. DB tests: accepting the
   merged card flips both models' rows; undo restores both; reject mirrors;
   Compare's `accept_subject_species` matches across name variants;
   **transitive-component accept fixture** — accepting the A-B-C card flips
   every pending row on photos 1-4 for the matching taxon and leaves other
   taxa untouched; undo restores every flipped row (including C's).
5. **Keyword canonicalization** (taxon-matched keyword reuse) + the
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
