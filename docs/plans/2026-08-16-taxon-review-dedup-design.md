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
`(classifier_model, labels_fingerprint, group_id)`. That tuple is
collision-resistant *only if* the raw `group_id` is itself unique across
jobs sharing the same model and fingerprint — which today's
`_store_grouped_predictions` scheme
(`f"g{job_id[-6:]}-{group_count:04d}"`, `classify_job.py:2212`) cannot
guarantee: two same-model, same-fingerprint jobs whose truncated
`job_id[-6:]` suffix and per-job group counter both align mint identical
`group_id`s for disjoint photo sets. Two such bursts would occupy one
graph node and become one card without any same-taxon overlap edge,
contradicting the goal.

The read side cannot recover the *original* job identity after the
fact: `predictions` has no `job_id` column, `add_prediction` does not
receive a job identifier, and `prediction_review` carries only the
plain `group_id` string (`db.py:865-883, 925-935, 15802-15818`). Only
the truncated 6-char suffix embedded inside `group_id` survives to
disk, so no merge-time key over stored rows can recover the full job
identity without a schema change and a companion backfill — which
would violate the "works retroactively on existing prediction rows
with no destructive migration" goal.

But the read side *can* — and must — split legacy short-form group
IDs whose rows are demonstrably from different classify runs, before
graph construction. Without that split, two independently-minted
bursts that collide on `(classifier_model, labels_fingerprint,
group_id)` share one graph node; the same-taxon overlap edge test
only connects *distinct* nodes, so they cannot be separated by any
downstream rule and would merge into one card even with no shared
photos.

A photo-membership partition does not work: each `predictions` row
has exactly one `photo_id` (`db.py:865-883`), and an ordinary
multi-photo burst has one row per distinct photo, so *no two rows in
one burst share a photo*. A "rows share a photo iff their `photo_id`s
coincide" rule would shatter every normal burst into single-photo
subsets and render one card per frame instead of one card for the
burst. The correct disambiguator has to be a signal that is
tight-within-one-job and loose-across-jobs; `predictions.created_at`
(`db.py:881`, `TEXT DEFAULT (datetime('now'))`) is exactly that.
`_store_grouped_predictions` writes a job's rows in a single
transaction, so their `created_at` values fall within milliseconds
of each other (SQLite `datetime('now')` has second precision;
same-transaction rows land in the same second or in two adjacent
seconds). Two disjoint classify jobs whose short group IDs happen to
collide are, by construction, from separate `_store_grouped_predictions`
calls — typically minutes, hours, or days apart, never microseconds.

The read path therefore partitions the rows sharing each
`(classifier_model, labels_fingerprint, group_id)` bucket into
**time-connected subsets** — sort the bucket's rows ascending by
`created_at` and break the sequence whenever the gap between two
consecutive rows exceeds a threshold `T_split` (design default: 300
seconds / 5 minutes; each run is one contiguous block, so the exact
value is not sensitive as long as it exceeds any single
`_store_grouped_predictions` transaction's duration and is far
shorter than the interval between two classify jobs a user would
run back-to-back on the same model/fingerprint). Each contiguous
run is one subset; the subset index is assigned by chronological
order (earliest = 0). The node key becomes `(classifier_model,
labels_fingerprint, group_id, subset_index)`. A single
non-colliding group's rows are all in one time-connected subset
(the burst's own transaction), so `subset_index` is always 0 and
the key is unchanged in the common case. Two colliding bursts from
different runs fall into two subsets with indices 0 and 1 and become
two distinct nodes; the same-taxon overlap edge test then correctly
leaves them separate when their photos do not intersect (and correctly
merges them when they do). The partition is a pure read-time
transform over the row set `/api/predictions` already fetches — no
schema change, no new column, no backfill — and is stable across
requests because chronological ordering by `created_at` is total (ties
break by `id`, itself monotone).

The partition is deliberately conservative: it can only *split* rows
apart, never combine them. In the pathological case where two
colliding legacy bursts were classified within `T_split` of each other
(e.g., a user launched two back-to-back re-runs of the same model on
the same fingerprint within five minutes and got a group-ID
collision), they stay one node — but this collapses at most into a
same-model / same-fingerprint one-card view of two near-simultaneous
runs, which is behaviorally close to what the intended merge would
have shown on shared frames. The one case the read path cannot
disentangle is the (extremely narrow) legacy collision where two
disjoint runs happen to have been minted within the same `T_split`
window *and* to share a photo carrying the same taxon: both would
collapse into one subset, then one card — indistinguishable from a
legitimate cross-model merge. This surface predates the feature and
shrinks to zero for new rows once Phase 0 ships; it is documented
rather than mitigated.

The write-path fix stays. **Phase 0 (new,
prerequisite of §2)** widens `_store_grouped_predictions` to mint
group IDs with enough entropy to be unique on their own. Two
independently correct options — either lands the design's guarantees:

- `gid = f"g{job_id}-{group_count:04d}"` — keeps the full `job_id`
  (e.g. `classify-1732000000000-3`), which is unique across jobs by
  construction (`jobs.py:689,763`). Backwards-compatible string;
  read-side treats it as opaque.
- `gid = f"g{secrets.token_hex(16)}-{group_count:04d}"` — appends 128
  bits of entropy per group; collision probability across the entire
  history of classify runs is effectively zero. (An earlier draft used
  `secrets.token_hex(4)` — 32 bits — which, because
  `_store_grouped_predictions` resets `group_count` per job, would
  share the counter suffix across every job and thus give ~50%
  birthday-collision probability among IDs minted at a common counter
  value after roughly 77k draws. That is not a safe assumption at
  catalog scale and is rejected.)

The design assumes option (a) unless profiling of downstream string
handling shows the longer key hurts. Either way, the node key stays
`(classifier_model, labels_fingerprint, group_id, subset_index)` —
Phase 0 makes `subset_index` always 0 for new rows (the raw
`group_id` is unique on its own, so the read-time partition trivially
yields one subset), and the read-side subset split defined above
handles the pre-Phase-0 legacy collision surface. No schema change to
`predictions` or `prediction_review`. Rows written *before* Phase 0
keep their short IDs; when two same-model, same-fingerprint jobs
whose short suffix and counter both align *and* whose bursts do not
share a photo, the read-time partition puts them in separate subsets
and they resolve as two nodes; the residual collision surface — two
disjoint-burst rows on the same photo with the same taxon — predates
this feature and is left as-is. Phase 0 closes the window
prospectively before the merge-graph work in Phase 3 ships, so all
rows the merge graph reads with the new semantics land in
subset 0.

Singleton nodes key on `(classifier_model, labels_fingerprint, "p" +
prediction_id)`; `prediction_id` is a unique primary key
(`db.py:866`), so this tuple is collision-resistant on its own without
depending on Phase 0.

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
For `name:`-keyed cards the folded label is embedded in the id, and
folded labels come from arbitrary user-supplied label files that may
contain `/`, `?`, `#`, `%`, or other URL-significant characters — and
may also contain the delimiter characters (`|`, `:`) that appear inside
taxon keys and node keys themselves. Two-part rule:

1. The card endpoint takes the id as a **query parameter**
   (`/api/predictions/card?id=<card_id>`), not as a path segment, so any
   byte survives the round trip once percent-encoded. A Flask
   `<card_id>` path converter does not match a decoded slash even when
   the client uses `encodeURIComponent`, so a path-segment id for a
   label like `hawk/owl` would 404; the query parameter avoids that.
2. The server-emitted `card_id` string is base64url-encoded (RFC 4648
   §5, unpadded — alphabet `[A-Za-z0-9_-]`) over a **structured**
   payload, not a delimiter-joined string. Concretely, the payload is
   the UTF-8 encoding of `json.dumps([taxon_key, smallest_member_key],
   separators=(",", ":"), ensure_ascii=False)`. JSON string escaping
   makes any byte inside either field unambiguous — including `|`, `:`,
   `"`, `\`, `/`, and control chars — so the server can decode with
   `json.loads` and recover exactly `(taxon_key, member_key)` regardless
   of what the user's label file contains or what a classifier model
   name looks like. Base64url over the JSON keeps the id safe to embed
   anywhere (DOM attributes, path segments if some future route wants
   them, log lines) without further escaping, and keeps it opaque to
   the client. Where the client persists an id (e.g. in URL hash for
   deep links), it stores the already-encoded form verbatim.
   *Alternative implementation, same guarantee:* an opaque digest (e.g.
   SHA-256 of the canonical JSON) with a server-side lookup table from
   digest → `(taxon_key, member_key)`; equivalent correctness, one extra
   table lookup per card open. Rejected as unnecessary — the structured
   base64url form is round-trip decodable without state.

**Filter semantics.** When the model filter or the labels-fingerprint
filter is active, cards are built from the matching rows only — merging
becomes an intra-filter no-op and the page shows exactly what that
model/fingerprint said. This keeps "filter by model" and "filter by
label set" honest (a merged card has no single model or fingerprint) and
costs nothing: the server already receives the filter context, or the
client can group the filtered subset by node identity.

*Active-filter detection.* The fallback runs whenever *any* of the
five predicates `getVisibleItems` and the filter bar apply would hide
rows the server returned — not just the model and fingerprint filters.
Concretely (matching `review.html:1091-1096, 1281-1300` verbatim):

- `minConfidence > 0` — hides rows whose `confidence` is below the
  slider value.
- `currentModel && currentModel !== 'all'` — hides rows from other
  classifier models.
- `currentLabelsFingerprint` truthy (non-null, non-empty string) —
  hides rows written under other label-set fingerprints.
- `currentTab && currentTab !== 'all'` — hides rows whose `status`
  differs from the selected tab (`pending` / `accepted` / `rejected`).
- `VireoFilter.getVisual()` non-null — a visual-search clause that the
  server resolves via `_apply_visual_to_rules` (`db.py:8272`) into a
  matched photo-id set; the client sends this payload as a **separate**
  `visual=<json>` parameter from `rules` on the GET
  (`review.html:1091-1096`), and rows on photos outside that matched
  set are hidden from the Review grid.

Any single one being true triggers the per-node fallback for card
construction. This is stricter than "model or fingerprint only" for
good reason: a below-`minConfidence` bridge row, an already-accepted
`status != currentTab` sibling row, or a same-taxon row on a photo
outside the visual clause's match set is exactly the kind of hidden
bridge that lets the server-computed full-component `card_id` stitch
two visible groups into one displayed card — and lets
`/api/predictions/card?id=<card_id>` re-expose the hidden bridge rows
on open. Forwarding the same predicates into the mutation POST (§3)
is necessary but not sufficient: without also rebuilding the displayed
card on the client, the *display* would still show a merged card whose
members the mutation then refuses to touch, and the user would see a
click that "does nothing" to visible siblings while silently mutating
hidden ones. So the fallback trigger and the mutation scope tuple stay
symmetric: same five predicates on both sides, always.

No sentinel change is required — the existing `currentModel` default
(`'all'`), `currentLabelsFingerprint` default (`null`), `minConfidence`
default (`0`), `currentTab` default (`'all'`), and
`VireoFilter.getVisual()` default (`null`) all resolve to
"no filter active" under the checks above. (If the client is later
refactored to use `'all'` as the labels-fingerprint no-filter sentinel
too, the fingerprint check collapses to `!== 'all'`; the design does
not require that change.)

*Fallback dedup key.* Server computes `card_id` per row from the *full*
row set; the client, whenever a filter is active, ignores `card_id`
entirely — both for deduping the displayed row set and for the
subsequent mutation — and instead uses the row's **node identity** — the
same tuple the server uses when building the merge graph (§2, "Node
identity"): `(classifier_model, labels_fingerprint, group_id,
subset_index)` for grouped rows, and `(classifier_model,
labels_fingerprint, "p" + prediction_id)` for singletons. The client
receives `subset_index` (usually `0`) as a per-row field alongside
`card_id` so it never needs to recompute the read-side partition
itself. Using node identity — not
`(taxon_key, group_id)` — is essential: (a) singleton rows all carry
`group_id = NULL`, so `(taxon_key, group_id)` would collapse every
ungrouped prediction of the same taxon on unrelated photos into one
displayed row; (b) node identity is exactly the granularity the server's
component graph is nodes-on, and the granularity the merged-card
endpoint does *not* expose across filter boundaries, so the fallback is a strict
subset of what the unfiltered view would show. The merged-card endpoint
is never invoked from a filtered view.

*Mutation ID from the fallback view.* Every row the server returns
still carries the full-component `card_id`. In a filtered view that
`card_id` is not a usable mutation handle: when the server-computed
component contains N > 1 nodes and the filter causes the client to
render some subset of them as separate fallback cards, every rendered
row *shares the same* `card_id` (the anchor of the full component), so
a POST that names only `card_id` cannot tell the server which of the
visible fallback nodes was clicked; further, when the filter hides the
node the server anchored on, the `card_id` decodes to a
`smallest_member_key` that isn't in the visible set at all. Both
failures produce silent mismatches between what the user clicked and
what the mutation resolves. So from a filtered view the client sends
the clicked row's node identity tuple verbatim
(`(classifier_model, labels_fingerprint, group_id, subset_index)` for
grouped rows, `(classifier_model, labels_fingerprint, "p" +
prediction_id)` for singletons — `subset_index` is what the server
returned on that row) plus the full five-predicate scope tuple from
§3, and does *not* send the server's `card_id`. The mutation POST
therefore distinguishes two request shapes: (i) unfiltered — carries
`card_id`, scope tuple all-`null`, server resolves the full component;
(ii) filtered — carries `node_id` (the encoded node identity tuple,
same base64url-of-JSON encoding as `card_id` for uniformity, decoding
to `[classifier_model, labels_fingerprint, group_id_or_pid,
subset_index]` for grouped rows or `[classifier_model,
labels_fingerprint, "p" + prediction_id]` for singletons) plus the
scope tuple, and the server treats the card as exactly that single
node, resolving photos only from that node's members and running the
same taxon-matched sibling pass §3 describes within the scope tuple.
An unrecognized `node_id` (stale after a re-run) is a 400; a POST
that carries both `card_id` and `node_id` is a client bug and
rejected as a 400. From an unfiltered view the mutation shape is
unchanged from what §3 already specifies.

*Why the fallback matters for privacy.* If server-computed components
stitched groups A and C together only through a bridge row B that the
current filter hides — a group at another fingerprint, a
below-`minConfidence` row, a `status != currentTab` sibling, or a
row from another classifier model — opening the "A+C card" from the
filtered view would otherwise re-expose B's hidden rows through the
`/api/predictions/card?id=<card_id>` union. Falling back to per-node
cards *and* per-node `node_id` mutation handles in filtered views
eliminates that exposure entirely: the union endpoint is never called,
and every mutation names exactly one visible node under the same scope
tuple. When the user clears every filter, the full server components
(and the merged card endpoint) apply again.

### 3. Cross-model accept and reject

Generalize the pattern `accept_subject_species` already implements, from
string matching to taxon matching, and from Compare-only to Review.

**Accept.** `db.accept_prediction` currently accepts the clicked prediction
and, if grouped, its group siblings — restricted to `pr.classifier_model =
?`. The taxon-keyed accept operates on the **entire card component's photo
union**, not just the clicked group's photos:

1. **Resolve the card, with the same filter scope the client rendered.**
   The mutation POST carries **either** `card_id` (unfiltered view) **or**
   `node_id` (filtered view — see §2 "Mutation ID from the fallback
   view"), never both, **plus every filter `getVisibleItems` applies**
   on the way from `/api/predictions` rows to what the card actually
   shows. Concretely the scope tuple is: `rules`, `collection_id`,
   `model` (from `currentModel`), `labels_fingerprint` (from
   `currentLabelsFingerprint`), `min_confidence` (from `minConfidence`,
   `review.html:1281-1283`), `status` (from `currentTab`,
   `review.html:1298-1300`), and `visual` (from
   `VireoFilter.getVisual()`, `review.html:1091-1096`) — server-applied
   filters that affected the GET plus client-applied filters that
   affected the displayed card, one uniform tuple. The client already
   threads `rules`/`collection_id`/`visual` into `/api/predictions`
   (`review.html:1091-1111`) — `visual` as its own JSON-encoded query
   parameter, distinct from `rules`; the mutation is extended to carry
   the full tuple (`review.html:1576-1581` and the reject path)
   verbatim, `visual` included, so the server can call the same
   `_apply_visual_to_rules` (`db.py:8272`) resolver on the mutation path
   and reproduce the exact matched-photo-id set the GET used. Note the
   three hidden failure modes this closes: (a) a below-`minConfidence`
   sibling row that bridges two groups would otherwise stitch the
   server's full component through a row the user couldn't see; (b) a
   `status != currentTab` row (e.g. an already-accepted sibling on the
   "pending" tab) could similarly bridge or be re-mutated; (c) a
   same-taxon sibling row on a photo *outside* the active visual
   clause's match set would similarly bridge two visible groups
   through a photo the user couldn't see, and a mutation that only
   carried `rules` would re-expose it because `rules` does not
   subsume the visual clause. All three are excluded from the resolved
   component by forwarding the same predicates. For a `card_id`
   request, the server re-runs the same card-building graph over the
   same scoped row set the GET used, then intersects the resolved
   component with the returned rows so mutation membership can never
   exceed the displayed membership. For a `node_id` request, the
   server resolves exactly the named single node under the scope
   tuple, without any component expansion — matching the per-node
   fallback the filtered view rendered. Where the client sends only a
   `prediction_id`/`group_id` (older payloads, deep-link buttons), the
   server treats the scope as "unfiltered" and resolves the
   full-workspace card; a stale-but-scoped POST is therefore safe
   (narrower than what the user sees), and a stale-and-unscoped POST
   is a legacy behavior that simply matches today's semantics.
2. **Enumerate photos from the resolved (filtered) component.** The
   candidate photo set is the union of every member group's/singleton's
   photos *within the resolved card* — not the clicked group's photos, and
   not the unfiltered full-workspace component. This fixes the transitive
   case: if the displayed card is {A: photos 1-2, B: photos 2-3, C:
   photos 3-4}, accept iterates photos {1, 2, 3, 4}. It also enforces the
   filter's promise: a hidden group D that a collection filter excluded
   from the GET is excluded from the mutation too, even if its taxon and
   photos would otherwise have joined the component.
3. **Sibling pass, taxon-matched, per photo, within the resolved scope.**
   For each photo in the union, find pending predictions on that photo
   whose taxon key matches the card's `taxon_key`, from **any** classifier
   model that was in scope for the GET (i.e., predictions the user's
   filter would have surfaced), restricted per model to its latest
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
same component-wide photo union so transitive cards resolve completely,
and it carries the same full scope tuple as accept
(`rules`/`collection_id`/`model`/`labels_fingerprint`/`min_confidence`/`status`/`visual`)
so that a rejection issued from a filtered view never touches rows the
user could not see, including rows on photos outside the active
visual clause's match set.

**Compare.** `accept_subject_species` swaps its
`lower(trim(species))` equality for the same taxon-key helper. Its
detection-scoped, single-photo semantics stay unchanged.

**Ordering constraint (§3 depends on §4's canonicalized keyword write).**
Both the Review sibling pass ("Sibling pass, taxon-matched, per photo,
within the resolved scope" above) and Compare's broadened
`accept_subject_species` iterate over agreeing rows and route each
through the existing `_accept_for_photo` / `accept_prediction`
primitives. Those primitives write a keyword whose name comes from
*each row's own* `species` value. If §3's taxon-match broadening
landed before §4's precedence-1 keyword lookup, accepting a Blue-Tit
+ Eurasian-Blue-Tit merged card would write *both* synonym keywords
to the photo — exactly the fragmentation this design is intended to
prevent. §4's precedence-1 lookup therefore has to be live before
§3's cross-variant accept fires, so every sibling accept in the loop
resolves to the same canonical keyword regardless of which row's
`species` string it carries. The Implementation-phases section below
sequences the two accordingly (keyword canonicalization lands as
Phase 4; the cross-model accept/reject and Compare broadening land as
Phase 5, after canonicalization is in place).

### 4. Display name and keyword canonicalization

**Card display name:** the resolved taxon's preferred common name from the
taxonomy ("Blue Tit"), with the raw per-model labels visible on the model
chips ("iNat21: Blue Tit · BioCLIP-2.5: Eurasian Blue Tit"). Unresolved
(`name:`-keyed) cards display the raw label as today.

**Keyword written on accept** — precedence:

1. If a keyword already exists whose `keywords.taxon_id` matches the
   card's taxon, reuse it — *its* name is what gets written. This keeps
   new accepts consistent with photos already tagged (no "Blue Tit"
   keyword appearing alongside an established "Eurasian Blue Tit"
   keyword), because keywords are global across workspaces and feed
   XMP sidecars on disk.

   **ID-space translation is required before this comparison.** The
   card's `taxon_key` carries the *iNaturalist* ID: `Taxonomy.lookup`
   populates its entries from the taxonomy payload's `taxon_id` field,
   which is the iNat identifier (`taxonomy.py:1217`). But
   `keywords.taxon_id` is a foreign key to the local
   autoincrement `taxa.id` (`db.py:724`), and the iNat value is stored
   separately as `taxa.inat_id` (`db.py:706-708`). Comparing the card's
   iNat id directly against `keywords.taxon_id` will therefore usually
   miss an established synonym keyword — and, worse, an accidental
   numeric collision (`taxa.id == some_other_taxon.inat_id`) would
   silently reuse an *unrelated* keyword. Precedence-1 lookup therefore
   resolves through `taxa.inat_id` first:
   `SELECT id FROM taxa WHERE inat_id = ?` with the card's iNat id,
   and only then `SELECT id FROM keywords WHERE taxon_id = ?` with
   that local `taxa.id`. If the taxa row is absent (an unknown iNat
   id, or a payload that predates the taxa refresh), precedence-1
   yields no match and step 2 runs. For `name:`-keyed cards (no
   resolved taxon), precedence-1 is skipped entirely.
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

0. **Collision-resistant `group_id` in `_store_grouped_predictions`**
   (prerequisite of Phase 3; see §2 "Node identity"). Change the ID
   template from `f"g{job_id[-6:]}-{group_count:04d}"` to
   `f"g{job_id}-{group_count:04d}"` (or the `secrets.token_hex(16)`
   variant — 128 bits, not the 32-bit `token_hex(4)` — if the longer
   key impacts downstream string handling). Tests:
   the same job's group IDs remain distinct; two independently minted
   jobs' group IDs are always distinct across every combination of
   `classifier_model` × `labels_fingerprint`; existing consumers of
   `group_id` (`add_prediction`, `prediction_review.group_id`,
   `_folded_species_key`, the review-endpoint dedup path) accept the
   longer string unchanged (opaque). No schema change; no read-side
   change; safe to land ahead of the merge feature.
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
   one `card_id` covering the full union; **group-id-uniqueness
   fixture** (two classify jobs with the *same* `classifier_model` and
   `labels_fingerprint`, run back-to-back so their timestamps land in
   the same second and their per-job group counters both start at 1)
   mint distinct `group_id`s under the Phase 0 write path — full
   `job_id` in the ID template makes them different by construction —
   so their `(classifier_model, labels_fingerprint, group_id,
   subset_index)` nodes are distinct (each at `subset_index = 0`) and
   their disjoint bursts stay as two separate cards; **legacy-collision
   split fixture** — the same fixture built with legacy pre-Phase-0
   rows (same short suffix + counter, colliding `group_id`, same
   taxon) whose `created_at` values are separated by more than
   `T_split` (default 5 minutes; the fixture writes the second burst's
   rows with a synthetic `created_at` offset of one hour) resolves as
   two cards because the read-side time-connectivity partition (§2,
   "Node identity") assigns them `subset_index = 0` and
   `subset_index = 1` respectively, giving them distinct nodes with
   no edge between them; a **within-window legacy fixture** where the
   two colliding bursts' `created_at` values fall within `T_split` of
   each other (e.g., two back-to-back re-runs on the same
   model/fingerprint) collapses into one subset — documented, matches
   what a same-model near-simultaneous re-run merge would have shown
   anyway; a **normal-burst-not-split fixture** — an ordinary
   single-job burst of eight rows on eight distinct photos (all
   `created_at` values within one transaction, i.e. within seconds)
   resolves as *one* node at `subset_index = 0`, not eight (this is
   the regression the earlier "photo-connectivity" rule would have
   introduced by putting every single-photo row in its own subset —
   the time-connectivity rule keeps the burst intact); the residual
   pre-Phase-0 collision surface (two disjoint-burst rows on the
   *same* photo with the same taxon *and* `created_at` values within
   `T_split`) is documented as unfixable from stored rows and closed
   prospectively by Phase 0;
   **cross-fingerprint hidden-row fixture** (groups A and C at
   fingerprint X, group B at fingerprint Y bridging them by shared
   photos, plus a singleton S with the same taxon on an unrelated
   photo) — with the fingerprint filter set to X, the client dedups
   the filtered rows by node identity and shows A, C, and S as three
   separate cards (the singleton is not collapsed into A/C), and the
   merged-card endpoint is not called; with the filter cleared, A+B+C
   become one card and S stays separate (no photo overlap) as
   expected; **min-confidence hidden-bridge fixture** — groups A and F
   at confidence above the slider, group E at confidence below, E
   shares a photo with both A and F; with `minConfidence` raised above
   E, the fallback triggers (per §2 "Active-filter detection"), A and
   F render as two separate cards, and the merged-card endpoint is
   not called; with `minConfidence = 0`, A+E+F become one card;
   **status-tab hidden-bridge fixture** — a `currentTab = 'pending'`
   view with an already-accepted sibling G on a photo shared between
   pending groups A and F: the fallback triggers, A and F render as
   two separate cards, and G is untouched by any subsequent mutation;
   **visual-clause hidden-bridge fixture** — an active
   `VireoFilter.getVisual()` visual-search clause whose matched
   photo-id set excludes photo `p*`, on which a same-taxon sibling
   row V sits bridging pending groups A and F (both otherwise inside
   the matched set): the fallback triggers on the non-null visual
   value, A and F render as two separate cards, V is not rendered,
   and the merged-card endpoint is not called; with the visual
   clause cleared, A+V+F become one card;
   **singleton-collapse-bug fixture** (three singleton predictions of
   the same taxon on three unrelated photos, any filter active) — the
   fallback preserves all three as distinct rows and does not collapse
   them into one; **URL-hostile card-id fixture** — a `name:`-keyed
   card derived from a custom label whose folded form contains `/`,
   `?`, `#`, `|`, and `:` (all of which appear inside the raw
   taxon/member key syntax) round-trips through the JSON-structured
   base64url-encoded id and the card endpoint's query parameter
   without a 404, and the server decodes back to the exact
   `(taxon_key, member_key)` pair; **filtered-view mutation-ID
   fixture** — with any of the five active filters (including a
   non-null visual clause), the client's mutation POST carries
   `node_id` (not `card_id`), and a fixture POST that names a
   non-existent `node_id` or that carries both `card_id` and
   `node_id` returns 400.
4. **Keyword canonicalization** (taxon-matched keyword reuse) + the
   "tags as …" transparency note. This phase lands **before** the
   cross-model accept broadening (Phase 5) so that the accept path's
   sibling loop cannot fragment keywords across name variants — see §3
   "Ordering constraint" for the full rationale. DB tests:
   **inat-id-translation fixture** — an existing "Eurasian Blue Tit"
   keyword linked to the local *Cyanistes caeruleus* taxa row is
   reused when a new accept's card carries the iNat id for
   *Cyanistes caeruleus* (precedence 1 hits after `taxa.inat_id`
   translation); a fabricated collision case where `taxa.id` for
   taxon A equals `taxa.inat_id` for taxon B does *not* reuse
   taxon B's keyword when accepting a card for taxon A; an unknown
   iNat id (not in the local `taxa` table) falls through to
   precedence 2 rather than raising or reusing an arbitrary keyword;
   a `name:`-keyed card skips precedence 1 entirely; a
   **variant-agreement fixture** — accepting a photo through the
   existing per-row `accept_prediction` primitive when a
   taxon-matched keyword ("Eurasian Blue Tit") already exists writes
   *only* the canonical keyword even if the accepted row's `species`
   string is "Blue Tit", so the Phase 5 sibling loop cannot introduce
   synonym fragmentation.
5. **Cross-model accept/reject** + undo coverage. Depends on Phase 4
   being live so that the per-row keyword write resolves to the
   canonical keyword — otherwise the sibling loop across "Blue Tit" /
   "Eurasian Blue Tit" rows would tag both synonyms on the same photo,
   the exact fragmentation §3's "Ordering constraint" describes. DB
   tests: accepting the merged card (unfiltered — POST carries
   `card_id`) flips both models' rows; undo restores both; reject
   mirrors; Compare's `accept_subject_species` matches across name
   variants and writes exactly one keyword per photo (asserting the
   Phase 4 dependency actually holds end-to-end);
   **transitive-component accept fixture** — accepting the A-B-C card
   (POST carries `card_id`, no filter scope) flips every pending row on
   photos 1-4 for the matching taxon and leaves other taxa untouched;
   undo restores every flipped row (including C's); **scoped-mutation
   fixture** — with a collection filter that excludes group D (same
   taxon, overlapping photos), the accept POST carries `card_id` plus
   the collection scope and D's rows stay pending; without the filter,
   D merges and is accepted; a **min-confidence-scoped fixture** —
   `minConfidence` is set above group E's confidence so E is hidden and
   would otherwise bridge two visible groups A and F through a shared
   photo, the accept POST from a filtered view carries `node_id`
   (naming A's node) plus `min_confidence` and only A is resolved,
   E and F stay pending; accepting F likewise resolves only F; a
   **status-scoped fixture** — with `currentTab` = `pending`, an
   already-accepted sibling row G on a bridging photo is excluded from
   the displayed component and a per-node `node_id` accept plus
   `status = "pending"` in the scope tuple leaves G untouched and does
   not stitch unrelated groups together; a **visual-scoped fixture** —
   an active `VireoFilter.getVisual()` clause whose match set excludes
   photo `p*`, on which a same-taxon sibling row V would otherwise
   bridge visible groups A and F, the accept POST from the filtered
   view carries `node_id` (naming A's node) plus the same `visual`
   JSON payload that the GET sent, the server re-runs
   `_apply_visual_to_rules` on the mutation path and resolves against
   the identical matched-photo-id set, and only A is resolved — V
   (on the excluded photo) and F stay pending; accepting F likewise
   resolves only F; a **filtered-mutation shape fixture** — a POST that carries both `card_id` and `node_id` is
   rejected 400; a POST that carries a `node_id` unknown to the server
   (e.g. after a re-run rewrote group IDs) is rejected 400; a stale
   POST that omits the scope tuple resolves the full-workspace card
   (documented legacy behavior); a POST that carries a scope narrower
   than the server's full component cannot exceed the displayed
   membership.

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
