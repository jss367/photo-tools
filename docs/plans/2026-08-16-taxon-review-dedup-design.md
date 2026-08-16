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
smallest member's stable node key alone (see "Card ID encoding" below).
The taxon key is *not* baked into `card_id` — it is recomputed from the
anchor node's rows on the mutation POST, so cards survive taxonomy-cache
transitions between the GET and the click (see "Anchor lookup and
cache-transition safety" below).

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

Without *some* split, two independently-minted bursts that collide on
`(classifier_model, labels_fingerprint, group_id)` share one graph
node; the same-taxon overlap edge test only connects *distinct* nodes,
so they could not be separated by any downstream rule and would merge
into one card even with no shared photos. The question is what signal
a read-time split can legitimately use.

A photo-membership partition does not work: each `predictions` row
has exactly one `photo_id` (`db.py:865-883`), and an ordinary
multi-photo burst has one row per distinct photo, so *no two rows in
one burst share a photo*. A "rows share a photo iff their `photo_id`s
coincide" rule would shatter every normal burst into single-photo
subsets and render one card per frame instead of one card for the
burst.

**Neither write time nor capture time can partition a legacy bucket.**
Two earlier revisions of this design tried to split a colliding bucket
by a time signal. Both are wrong, and the read path uses neither.

*Write time (`predictions.created_at`) — rejected.* The theory was
that one `_store_grouped_predictions` call writes its rows in a single
transaction, so a job's rows are seconds apart while two colliding
jobs are minutes-to-days apart (`db.py:881`, `TEXT DEFAULT
(datetime('now'))`). That theory is wrong:

- On a non-`reclassify` run, the gated branch re-injects *cached*
  predictions into `raw_results` (`classify_job.py:1657-1712`,
  `_existing: True`) so grouping still sees those photos. A single
  burst can therefore be composed of some photos classified today and
  some whose rows were written weeks ago.
- `add_prediction` uses `INSERT OR IGNORE` (`db.py:15862`) against
  `UNIQUE(detection_id, classifier_model, labels_fingerprint,
  species)`, so re-storing a cached row is a no-op and the row keeps
  its **original** `created_at` even though
  `_store_grouped_predictions` just assigned the whole burst a fresh
  `group_id`.
- Nothing records when the grouping assignment happened:
  `prediction_review` (`db.py:925-935`) has `group_id` but no
  creation timestamp, and its `reviewed_at` is `NULL` while pending.

So `created_at` is the *first-ever* insert time of each row, not a run
boundary, and a perfectly ordinary mixed cached/new burst would be
shredded into several nodes and several cards by a `created_at` gap
rule.

*Capture time (`photos.timestamp`) — also rejected.* The theory here
was better: the grouper itself walks capture timestamps
(`group_by_timestamp`, `grouping.py:12-55`), so a stored burst should
be capture-time-connected by construction, and a read-side gap rule
whose window `W_read` is at least as wide as any window the grouper
could legally have used (the schema max, `3600`,
`vireo/config_schema.py:89-93`) could never shatter a real burst. It
is still wrong, for two independent reasons:

1. **Stored bursts are not capture-time-connected.**
   `_store_grouped_predictions` does not store `group_by_timestamp`'s
   output. It stores the output of `refine_groups_by_similarity`
   (`classify_job.py:2170-2172`), which re-partitions each timestamp
   group by embedding similarity: a photo joins the first subgroup
   holding *any* member it is similar to, so intervening photos can
   land in a different subgroup (`grouping.py:83-119`). A stored burst
   is one of those subgroups, and its adjacent members can be
   arbitrarily far apart in capture time — bounded by
   `(n-1) × grouping_window`, not by `grouping_window`. Concretely at
   the schema-max window: photos at t=0, t=3500 and t=7000 form one
   timestamp group; if the first and last are visually similar and the
   middle one is not, the stored burst is {0, 7000}, and a gap rule
   with `W_read = 3600` splits one legitimate burst into two cards.
   Widening `W_read` does not fix it — the bound grows with burst
   length, not with the window — and it is the *common* path
   (similarity refinement runs on every classify job), not a legacy
   corner.
2. **Capture time is mutable, and the failure is not safe.**
   `_refresh_photo_metadata` (`vireo/capture_time.py:265-283`) and
   scanner refreshes re-read EXIF and update `photos.timestamp`, so a
   partition derived from it is not a function of immutable stored
   state. A correction landing between a Review GET and the mutation
   POST can *merge* two subsets, and the merge direction does not fail
   closed: if subsets A and B merge and A held the lower minimum
   `predictions.id`, the merged subset keeps A's anchor, so a POST
   carrying A's stale handle still resolves — and now mutates B's
   previously-separate, previously-hidden rows. Only a request anchored
   on B becomes unrecognized and returns 400. An earlier revision
   asserted the failure mode was "a 400, never a silent mismerge"; that
   held only for the *split* direction, and a safe-failure argument
   that covers one direction of a boundary change is not a
   safe-failure argument.

Together these rule out any read-time partition keyed on a photo
timestamp, so the design stops trying to reconstruct run boundaries
from time at all.

**Node identity is a pure function of immutable row columns.** The
graph keys each burst-group node as `(classifier_model,
labels_fingerprint, group_id, species_key)`, where `species_key` is
the ASCII-folded match key of `predictions.species` — the same folding
§1 step 4 uses (`_folded_species_key` / `_species_match_key` in
`classify_job.py`). Every element is a column already on the row.
Nothing is derived from a timestamp, from write order, or from which
rows the query returned.

Splitting a bucket by species key is free for real bursts, and it is a
structural guarantee rather than an assumption:
`_store_grouped_predictions` stamps a `group_id` only when
`group_reviewable` — `len({_species_match_key(p) for p in group}) == 1`
(`classify_job.py:2269-2272`); a burst whose frames disagree on species
is stored with `group_id=None` and its rows become singleton nodes.
**Every stored burst is therefore unanimous in species match key by
construction**, and always yields exactly one node no matter how many
species-string variants (casing, apostrophes) its individual frames
spell. That is the property the capture-time rule claimed and did not
actually have.

On the legacy surface, this separates two pre-Phase-0 bursts that
collided on `f"g{job_id[-6:]}-{group_count:04d}"` whenever they are
*different species* — the case where merging them is visibly wrong,
because one card would assert a taxon over photos the classifier
called something else. Two colliding bursts of the **same** species
stay one node and one card. That residual is:

- **not recoverable from stored rows** by any rule — the original job
  identity is gone (no `job_id` column) and no time signal
  reconstructs it (above);
- **not a regression** — `review.html` already dedups on `group_id`
  alone today, so those two bursts render as one card today, and the
  card shows their union of photos either way; and
- **closed prospectively by Phase 0**, after which `group_id` is
  unique on its own and every bucket holds exactly one burst.

Accepting that residual is the deliberate trade. The alternative — a
time-based partition — bought a fix for a rare, pre-existing,
visible-to-the-user legacy artifact at the price of shattering
ordinary similarity-refined bursts on the common path. Under-splitting
a legacy collision is invisible and unchanged from today;
over-splitting a real burst is a regression every user would see.

Over-splitting by species key is itself self-correcting rather than
harmful: if a bucket ever did hold two species-string variants of the
*same* taxon, the graph's same-taxon + overlapping-photos edge
re-merges the two nodes into one card. Node keying can only propose a
finer partition; the edge test decides the final card.

**Filter-invariant by construction, at zero query cost.** An earlier
revision computed its partition over the *unfiltered* bucket so that a
handle minted under one filter state would still resolve under
another, at the cost of an extra bucket-scoped fetch of
`(prediction_id, photo_id, timestamp)` per bucket. Per-row intrinsic
identity gets the same guarantee for free: there is no partition to
scope, all four fields are already on every row the endpoint selects,
and a `node_id` minted under any filter state decodes to the same node
under any other. Filters still hide *rows* exactly as §2 "Filter
semantics" specifies; they cannot move a node's identity. The server
stamps the encoded `node_id` on each returned row, so the client never
recomputes node identity itself.

**Phase 0 (new, prerequisite of §2)** widens
`_store_grouped_predictions` to mint group IDs with enough entropy to
be unique on their own:

```python
gid = f"g{secrets.token_hex(16)}-{group_count:04d}"
```

128 bits per group; collision probability across the entire history of
classify runs is effectively zero. The ID stays an opaque
backwards-compatible string, so read-side consumers are unchanged. Two
weaker options are explicitly rejected:

- `f"g{job_id}-{group_count:04d}"` (the full job ID). `job_id` is
  **not** unique across jobs by construction: `JobRunner` builds it as
  `f"{job_type}-{int(time.time() * 1000)}-{seq}"` from an
  `_enqueue_counter` that is initialized to `0` on every process start
  (`jobs.py:112, 687-689`). The counter only separates jobs *within* a
  process; across restarts the sole separator is the wall clock, so a
  restart combined with a backward clock adjustment (NTP step, manual
  clock change, VM snapshot restore, a DB carried to another machine)
  can mint the same `job_id` for the same job type at the same
  sequence number — recreating exactly the collision Phase 0 exists to
  eliminate. A key that has to hold for the life of the catalog should
  not rest on wall-clock monotonicity across process restarts.
- `secrets.token_hex(4)` — 32 bits. Because
  `_store_grouped_predictions` resets `group_count` per job, the
  counter suffix is shared across every job, so IDs minted at a common
  counter value reach ~50% birthday-collision probability after
  roughly 77k draws. Not safe at catalog scale.

The node key stays `(classifier_model, labels_fingerprint, group_id,
species_key)`. Phase 0 makes the fourth element redundant for new rows
(a self-unique `group_id` already means one bucket is one burst) and
leaves it doing useful work only on pre-Phase-0 rows. No schema change
to `predictions` or `prediction_review`, no new column, and no
backfill — node identity is read entirely off columns that already
exist. Phase 0 lands before the merge-graph work in Phase 3, so every
row the merge graph reads with the new semantics has a self-unique
`group_id`.

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

**Payload changes.** Each prediction row gains `taxon_key`, `card_id`,
`node_id` (the encoded node identity from "Node identity" above — the
handle the client echoes back when a filter is active), and
`display_name` (§4). Rows are *not* collapsed server-side — the client keeps
all rows (it already receives every group member) and dedups by `card_id`
instead of `group_id`, so per-model detail remains available for rendering.

**Client changes (`review.html`).**

- `getVisibleItems` dedups by `card_id` (fallback to `group_id` then
  prediction id for old payload shapes during rollout).
- **Card status and actions are aggregated across every member row**, not
  read off whichever row won the dedup sort. Rule: the badge shows
  *pending* — with accept/reject actions rendered — whenever *any* member
  is pending; *accepted* (no action, undo hook only) only when every
  member is accepted; *rejected* only when every member is rejected. The
  aggregate is computed from the full pre-dedup row bucket for the
  `card_id` (or `node_id` under a filter — §2 "Mutation ID from the
  fallback view"), not from the surviving representative. Deriving
  badge/actions from a representative row is the exact motivating bug:
  when BioCLIP-2.5 has already auto-accepted "Blue Tit" on the burst and
  iNat21's "Eurasian Blue Tit" arrives pending on the same photos, the
  merged card's sort-winning row can be the accepted BioCLIP row and the
  card would render as Accepted with no visible action — silently
  collapsing the pending duplicate that the user needs to see and
  resolve. The aggregate rule forces the card to surface the pending
  action whenever any duplicate survives, and the accept path (§3) then
  flips every pending member to accepted in one click. Symmetric for
  reject. The rule also makes card status robust to sort-order changes
  (confidence order vs. capture-time order vs. id order): the aggregate
  is a set predicate over member statuses, so no ordering choice can
  flip a mixed card between "actionable" and "already resolved". *Test
  fixture (Phase 3):* a **mixed-status card fixture** — one card
  containing an accepted BioCLIP row and a pending iNat21 row on the
  same photo set renders as pending with accept/reject visible under
  every representative-row sort order tried (species-string asc,
  confidence desc, prediction-id asc); accepting resolves both members
  (§3 sibling pass); the card then renders as accepted with no action.
- The card shows: union photo count; one chip per model with that model's
  consensus confidence and vote counts (e.g.
  `BioCLIP-2.5 92% · iNat21 88%`); the display name (§4).
- The group review modal opens with the union membership. New endpoint
  `GET /api/predictions/card?id=<card_id>` returns the union of member
  groups with per-photo, per-model rows — the existing
  `/api/predictions/group/<group_id>` machinery stays for compatibility and
  is what the card endpoint composes.

**Card ID encoding.** `card_id` is treated as opaque bytes on the wire.
Node keys carry model and fingerprint strings *and* the folded
`species_key` ("Node identity" above), so for `name:`-keyed cards the
folded label is literally inside the encoded id, and those fields come from arbitrary
user-supplied inputs that may contain `/`, `?`, `#`, `%`, or other
URL-significant characters — and may contain the delimiter characters
(`|`, `:`) that appear inside taxon keys and node keys themselves.
Two-part rule:

1. The card endpoint takes the id as a **query parameter**
   (`/api/predictions/card?id=<card_id>`), not as a path segment, so any
   byte survives the round trip once percent-encoded. A Flask
   `<card_id>` path converter does not match a decoded slash even when
   the client uses `encodeURIComponent`, so a path-segment id for a
   label like `hawk/owl` would 404; the query parameter avoids that.
2. The server-emitted `card_id` string is base64url-encoded (RFC 4648
   §5, unpadded — alphabet `[A-Za-z0-9_-]`) over a **structured**
   payload, not a delimiter-joined string. Concretely, the payload is
   the UTF-8 encoding of `json.dumps([smallest_member_key],
   separators=(",", ":"), ensure_ascii=False)` — a single-element JSON
   array wrapping the anchor node key. JSON string escaping makes any
   byte inside the anchor unambiguous — including `|`, `:`, `"`, `\`,
   `/`, and control chars — so the server can decode with `json.loads`
   and recover exactly `smallest_member_key` regardless of what a
   classifier model name looks like. Base64url over the JSON keeps
   the id safe to embed anywhere (DOM attributes, path segments if
   some future route wants them, log lines) without further escaping,
   and keeps it opaque to the client. Where the client persists an id
   (e.g. in URL hash for deep links), it stores the already-encoded
   form verbatim. *Alternative implementation, same guarantee:* an
   opaque digest (e.g. SHA-256 of the canonical JSON) with a
   server-side lookup table from digest → `member_key`; equivalent
   correctness, one extra table lookup per card open. Rejected as
   unnecessary — the structured base64url form is round-trip decodable
   without state.

*Why the anchor alone, and not `(taxon_key, anchor)`.* An earlier draft
prefixed the id with the card's `taxon_key` "so distinct taxa cannot
collide". That prefix is redundant, because a node belongs to at most
one component per Review payload: the merge graph builds components on
`(same taxon_key, overlapping photos)` and a node has exactly one
`taxon_key` at a given time, so no two distinct-taxon components can
share the same anchor node. Worse, embedding `taxon_key` made the id
brittle across taxonomy-cache transitions: §1's background resolver
opportunistically enqueues any `name:`-fallback label the GET emits,
and the resolver can persist a hit *between* the GET that stamped a
`name:`-keyed `card_id` and the POST that submits it. Rebuilding the
graph from stored rows then produces a `taxon:`-keyed `card_id` for
those same rows and the client's submitted id decodes to a `taxon_key`
that no current component carries — so a card still on screen would
return 400 or, worse, resolve to nothing at all. Encoding only the
anchor eliminates the dependency: the anchor node's stored rows are
untouched by the cache transition, `smallest_member_key` decodes to
the same tuple, and "Anchor lookup and cache-transition safety" below
covers how the server rebuilds the card under the *current* taxon
key.

**Anchor lookup and cache-transition safety.** On a `card_id` mutation
POST the server:

1. Decodes `card_id` to recover `smallest_member_key`.
2. Locates that node's stored rows (join `prediction_review` with
   `predictions` under the active workspace, filtered by the node's
   `(classifier_model, labels_fingerprint, group_id, species_key)`
   tuple for grouped rows, or `prediction_id` for singletons). A node
   whose rows have all been deleted or a bucket a re-run rewrote
   returns 400 — the same stale-handle response the design already
   specifies elsewhere.
3. Computes the taxon key for that anchor node's rows *now* using §1's
   `taxon_key_for` (which reads the current cache — a hit that landed
   between the GET and the POST resolves to `taxon:...`, a miss stays
   `name:...`).
4. Runs the same card-building graph the GET runs and returns the
   connected component that contains the anchor node. That component
   is the card the mutation resolves against, filtered by the scope
   tuple as §3 already specifies.

The intended cache-transition sequence is: GET emits `name:blue tit`
card with anchor `A`, `card_id` = base64url(JSON([`A`])). Background
resolver populates the cache for "blue tit" → *Cyanistes caeruleus*
(iNat 13094). User clicks Accept. POST sends `card_id`. Server decodes
to `A`, computes taxon key from `A`'s rows now — `taxon:13094` — builds
the graph, finds `A`'s component (which may have grown to include
groups from another model that already resolved to `taxon:13094`
directly), and mutates it. No 400, no lost click; the merge just
*works*, and if the component grew during the transition the user's
accept correctly resolves the enlarged card. The symmetric case where
the anchor's own `taxon:`-keyed component *shrank* because a hit made
it merge with a previously-separate group under a different anchor
also resolves correctly: the shrunk component still contains `A`, so
`A`'s component is still findable, and the mutation still names the
right card.

The one transition this cannot silently paper over is when the anchor
node's rows are gone entirely (bucket rewrite, deletion) — that
returns 400 as before. Cache resolution does not delete rows; it just
changes what taxon they resolve to, so the `name:`→`taxon:` case
never triggers this failure.

**Filter semantics.** When **any** filter that removes rows is active —
server-applied *or* client-applied, the full five-predicate list below,
not just model and fingerprint — cards are built from the matching rows
only, per node identity. Merging becomes an intra-filter no-op and the
page shows exactly what the visible rows said. This keeps every filter
honest (a merged card has no single model, no single fingerprint, no
single confidence, and no single status) and costs nothing: the server
already receives the server-side filter context, and the client groups
the visible rows by the node identity the server stamped on each row.

The rule is deliberately "any row-hiding predicate", not "the filters
that happen to be server-side". A `card_id` computed from the *full*
row set is only a truthful card identity for a view that shows the
full row set. The moment a predicate — a confidence slider, a status
tab, a visual clause — removes a row that was a *bridge* between two
components, the server's `card_id` describes a card the user is not
looking at. See "Fallback dedup key" below for what the client uses
instead.

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
species_key)` for grouped rows, and `(classifier_model,
labels_fingerprint, "p" + prediction_id)` for singletons. The client
does not assemble that tuple itself — the server stamps the encoded
`node_id` on every returned row (§2, "Filter-invariant by
construction"), and the client echoes it back verbatim. Because all
four fields are intrinsic row columns, the `node_id` stamped on a row
is the same value no matter which filters were active on the GET —
which is exactly why the fallback can use it as a mutation handle at
all. Neither a positional subset index nor any computed partition
could be used here: both are functions of which rows the query
returned, so a filter that hid an entire earlier subset would make the
server renumber or re-derive the survivor on the mutation rebuild, and
a valid click would 400. Using node identity — not
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
(`(classifier_model, labels_fingerprint, group_id, species_key)` for
grouped rows, `(classifier_model, labels_fingerprint, "p" +
prediction_id)` for singletons — encoded as the `node_id` the server
stamped on that row) plus the full five-predicate scope tuple from
§3, and does *not* send the server's `card_id`. The mutation POST
therefore distinguishes two request shapes: (i) unfiltered — carries
`card_id`, scope tuple all-`null`, server resolves the full component;
(ii) filtered — carries `node_id` (the encoded node identity tuple,
same base64url-of-JSON encoding as `card_id` for uniformity, decoding
to `[classifier_model, labels_fingerprint, group_id, species_key]` for
grouped rows or `[classifier_model, labels_fingerprint, "p" +
prediction_id]` for singletons) plus the
scope tuple, and the server treats the card as exactly that single
node, resolving photos only from that node's members and mutating
strictly the node's own rows (§3 "`node_id` request" — no cross-model
taxon-matched sibling scan, so the mutation cannot reach any other
visible node's rows on a shared photo).
The server resolves a `node_id` by matching those columns on stored
rows and only then intersects the node's members with the scope tuple.
Because node identity is intrinsic to the rows (§2, "Node identity is
a pure function of immutable row columns"), that match is independent
of both the filter state and any concurrent write to `photos`, so a
handle minted on the GET always names the same node on the POST that
follows it. The one way a `node_id` stops resolving is that its rows
are gone — a re-run rewrote the bucket's `group_id`s, or the rows were
deleted — which is a true stale handle, not a boundary artifact.
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
   request, the server decodes to the anchor node key, computes the
   anchor's *current* taxon key from its stored rows (§2, "Anchor
   lookup and cache-transition safety"), re-runs the same
   card-building graph over the same scoped row set the GET used
   under that current taxon key, and returns the component containing
   the anchor; it then intersects that resolved component with the
   returned rows so mutation membership can never exceed the displayed
   membership. Recomputing the taxon key at mutation time is what
   makes a click safe when the background taxonomy resolver populated
   a hit between the GET and the POST — the anchor is still findable
   and the (possibly-grown, possibly-shrunk) component still resolves
   correctly under the new key. For a `node_id` request, the
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
   The pass differs by request shape, in order to honour what §2 already
   promises for `node_id` resolution ("the server treats the card as
   exactly that single node … without any component expansion"):
   - **`card_id` request (unfiltered view).** For each photo in the
     resolved component's union, find pending predictions on that photo
     whose taxon key matches the card's `taxon_key`, from **any**
     classifier model that was in scope for the GET (i.e., predictions
     the user's filter would have surfaced), restricted per model to its
     latest `labels_fingerprint` (reuse the latest-fingerprint subquery
     from `accept_subject_species`), and accept each via the existing
     `_accept_for_photo` primitive. This is what closes the
     BioCLIP-vs-iNat21 duplicate on the motivating case and carries
     acceptance across A→B→C in a transitive component.
   - **`node_id` request (filtered view, per-node fallback).** The
     mutation touches **only the named node's own rows** — no cross-model
     sibling scan, no expansion onto other visible nodes, even for a
     photo the node shares with a visible sibling node that has the same
     taxon. Concretely: `_accept_for_photo` is called exactly on the
     node's own `(photo_id, prediction_id)` set, and the cross-model
     taxon-matched sibling scan of the `card_id` branch is skipped. The
     visual contract of the per-node fallback is "each card is its own
     click" — the client rendered visible nodes A and B as separate
     cards precisely because a filter made component-wide expansion
     unsafe (§2 "Why the fallback matters for privacy"), and a `node_id`
     accept that reached across the two visible nodes on their shared
     photo would (a) mutate a different card the user did not click and
     (b) leave that sibling card partially resolved — its non-shared
     rows still pending — recreating the exact duplicate-card bug the
     design exists to eliminate, just re-cast between two visible nodes
     instead of between two rendered cards. The sibling node B remains a
     separately clickable card whose own accept touches only its own
     rows; two clicks resolve two cards, symmetric with what the user
     sees. Once every filter is cleared, subsequent Review loads issue
     `card_id` requests again and component-wide expansion resumes.

   The bifurcation only concerns the *sibling scan*: step 1's
   `_apply_visual_to_rules` handling, step 2's per-photo enumeration
   from the resolved membership, and step 4's undo recording all apply
   identically to both request shapes. Under a `node_id` request, step
   2's "resolved (filtered) component" degenerates to that one node's
   own photos, so step 3's `_accept_for_photo` loop already visits only
   the correct photo set — skipping the cross-model scan is the single
   behavioural difference.

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
landed before §4's canonicalized keyword write, accepting a Blue-Tit
+ Eurasian-Blue-Tit merged card would write *both* synonym keywords
to the photo — exactly the fragmentation this design is intended to
prevent. §4 therefore has to be live before §3's cross-variant accept
fires, so every sibling accept in the loop resolves to the same
canonical keyword regardless of which row's `species` string it
carries. The Implementation-phases section below sequences the two
accordingly (keyword canonicalization lands as Phase 4; the
cross-model accept/reject and Compare broadening land as Phase 5,
after canonicalization is in place).

This ordering is only sufficient because §4's canonicalization is
specified **inside the per-row accept primitive, keyed on the accepted
row's own taxon** — see §4, "Keyword written on accept". Compare's
`accept_subject_species` has no card to key on, so a card-keyed rule
would have left the Compare half of Phase 5 fragmenting synonyms even
with Phase 4 shipped first. Row-keyed canonicalization covers both
loops, and covers the first-ever accept of a taxon (where no keyword
exists yet to reuse) because precedence 2 also resolves through the
taxon rather than through the row's raw label. Neither loop therefore
needs to be restructured to "tag once and only update sibling
statuses" — the write is idempotent per taxon on its own.

### 4. Display name and keyword canonicalization

**Card display name:** the resolved taxon's preferred common name from the
taxonomy ("Blue Tit"), with the raw per-model labels visible on the model
chips ("iNat21: Blue Tit · BioCLIP-2.5: Eurasian Blue Tit"). Unresolved
(`name:`-keyed) cards display the raw label as today.

**Keyword written on accept** — precedence. The precedence below is
applied **inside `_accept_for_photo` / `accept_prediction`, keyed on
the taxon of the row being accepted** (`taxon_key_for(row.species,
row.scientific_name, tax)` from §1) — *not* keyed on the calling
card. That placement is load-bearing rather than incidental:

- It is the only level at which both call sites are covered. Review's
  sibling pass has a card; **Compare's `accept_subject_species` does
  not** — it walks agreeing detection rows on one photo with no card
  in scope. A card-keyed rule would leave the Compare loop writing
  one keyword per row's own `species`, so accepting a "Blue Tit" +
  "Eurasian Blue Tit" agreement in Compare would still tag both
  synonyms. Row-keyed canonicalization fixes both loops with one
  change, and leaves Compare's detection-scoped semantics untouched.
- It makes the property *per row*, so it holds no matter how many
  times the loop runs or in what order: every agreeing row resolves
  through the same taxon to the same keyword, so the loop is
  idempotent on the keyword set by construction. Neither loop needs a
  "tag once, then only flip sibling statuses" special case — which
  would otherwise be a second, separately-testable code path in each
  caller.
- Both rows in an agreeing pair resolve to the same taxon *by
  definition* (that is what made them siblings), so row-keyed and
  card-keyed agree whenever a card exists. Row-keyed is strictly
  more general.

Precedence, then:

1. If a keyword already exists whose `keywords.taxon_id` matches the
   accepted row's taxon, reuse it — *its* name is what gets written.
   This keeps new accepts consistent with photos already tagged (no
   "Blue Tit" keyword appearing alongside an established "Eurasian
   Blue Tit" keyword), because keywords are global across workspaces
   and feed XMP sidecars on disk.

   **ID-space translation is required before this comparison.** The
   row's `taxon_key` carries the *iNaturalist* ID: `Taxonomy.lookup`
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
   `SELECT id FROM taxa WHERE inat_id = ?` with the row's iNat id,
   and only then `SELECT id FROM keywords WHERE taxon_id = ?` with
   that local `taxa.id`. If the taxa row is absent (an unknown iNat
   id, or a payload that predates the taxa refresh), precedence-1
   yields no match and step 2 runs. For `name:`-keyed rows (no
   resolved taxon), precedence-1 is skipped entirely.
2. Otherwise, create/use **the taxon's preferred common name** — again
   the taxon's, not the row's raw `species`. This is what makes the
   *first* accept of an agreeing pair safe: before any keyword exists
   for *Cyanistes caeruleus*, precedence 1 misses for both rows, and
   only a taxon-derived name makes the "Blue Tit" row and the
   "Eurasian Blue Tit" row converge on one keyword. Falling back to
   the row's own `species` here would reintroduce the exact
   fragmentation precedence 1 prevents, one accept earlier.
   `name:`-keyed rows (unresolved) keep writing their raw label, as
   today.

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
   `f"g{secrets.token_hex(16)}-{group_count:04d}"`. Neither the
   truncated nor the *full* `job_id` is acceptable: `JobRunner` resets
   `_enqueue_counter` to `0` on every process start (`jobs.py:112,
   687-689`), so `job_id` is unique only within a process and a
   restart plus a backward clock adjustment can re-mint one. Tests:
   the same job's group IDs remain distinct; two independently minted
   jobs' group IDs are always distinct across every combination of
   `classifier_model` × `labels_fingerprint`; a **restart-collision
   fixture** — two jobs of the same `job_type` given the *same*
   `job_id` (simulating a `_enqueue_counter` reset with a repeated
   wall-clock millisecond) still mint disjoint `group_id`s, which the
   full-`job_id` template would have failed; existing consumers of
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
   mint distinct `group_id`s under the Phase 0 write path — 128 bits
   of entropy per group makes them different by construction — so
   their `(classifier_model, labels_fingerprint, group_id,
   species_key)` nodes are distinct and their disjoint bursts stay as
   two separate cards; **legacy-collision
   split fixture** — the same fixture built with legacy pre-Phase-0
   rows (same short suffix + counter, colliding `group_id`) where the
   two bursts are of *different* species resolves as two cards,
   because the species key splits the bucket into two nodes with no
   edge between them; a **same-species legacy fixture** where the two
   colliding bursts share a species collapses into one node and one
   card — the documented residual, identical to today's
   `group_id`-only client dedup, and closed prospectively by Phase 0;
   a **similarity-refined burst fixture** — the regression that killed
   the capture-time rule: one stored burst produced by
   `refine_groups_by_similarity` (`classify_job.py:2170-2172`) whose
   member capture times are 0s and 7000s apart because the
   non-similar intervening frame went to another subgroup
   (`grouping.py:83-119`). Assert it resolves as **one** node and one
   card. A `W_read`-gap partition with any fixed window splits it; a
   species-key partition cannot, because the burst is unanimous in
   species by construction (`group_reviewable`,
   `classify_job.py:2269-2272`); a **cached-plus-new burst
   fixture** — the regression that killed the
   `predictions.created_at` rule: one burst whose photos are half
   cached rows (written weeks earlier, re-injected into
   `raw_results` by the non-`reclassify` gated path,
   `classify_job.py:1657-1712`, and left untouched by
   `add_prediction`'s `INSERT OR IGNORE`) and half freshly inferred
   rows, all assigned one `group_id` by
   `_store_grouped_predictions`. Assert it resolves as **one** node
   and one card; a
   **normal-burst-not-split fixture** — an ordinary
   single-job burst of eight rows on eight distinct photos resolves as
   *one* node, not eight (the regression the "photo-connectivity"
   rule would have introduced by putting every single-photo row in its
   own subset); a **case-and-apostrophe-variant burst fixture** — a
   stored burst whose frames spell `Say's Phoebe`, `Say’s Phoebe` and
   `Say's phoebe` resolves as **one** node, because `species_key` is
   the ASCII-folded match key and not the raw string; the residual
   pre-Phase-0 collision surface (two disjoint same-species bursts
   sharing a colliding `group_id`) is documented as unfixable from
   stored rows and closed prospectively by Phase 0; a
   **filter-invariant node identity
   fixture** — the `node_id` the server stamps on a given row is
   byte-identical across GETs made with no filter, with
   `minConfidence` raised above every other row of its bucket, and
   with a status tab active (the case a positional `subset_index` or
   any query-scoped partition would have failed: hiding rows
   renumbers or re-derives the survivor, and the client's `node_id`
   stops resolving);
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
   `smallest_member_key`; a **cache-transition card-id fixture** —
   a `name:blue tit` card is emitted with
   `card_id = base64url(JSON([A]))` where `A` is the anchor node
   key, the background resolver then persists `blue tit` →
   *Cyanistes caeruleus* (iNat 13094), and a subsequent mutation
   POST that carries the *original* `card_id` resolves to the
   anchor's rows, computes their current taxon key as `taxon:13094`,
   and finds the anchor's component under that key without returning
   400 — including the sub-case where a previously-separate
   BioCLIP-2.5 group `B` that already carried `taxon:13094` is now
   in the same component (the resolved component grew) and the
   sub-case where the same-taxon merge shifted the component's
   smallest-member anchor to a different node than `A` (the component
   still contains `A`, so `A`'s component is still findable and
   `A`'s `card_id` still resolves); a
   **cache-transition-anchor-deleted fixture** — the same setup
   but with the anchor's rows deleted between the GET and the POST
   (e.g. a re-run rewrote the bucket) returns 400, the documented
   stale-handle failure mode (§2, "Anchor lookup and cache-transition
   safety"); a **historical-window fixture** — a legacy burst
   captured under `grouping_window_seconds = 600` with a 400s gap
   between consecutive frames resolves as one node regardless of the
   workspace's current effective `grouping_window_seconds`, because
   node identity reads no timestamp and no config value at all; a
   **timestamp-mutation invariance fixture** — two pre-Phase-0
   colliding legacy bursts of different species render as two nodes
   and two cards; `_refresh_photo_metadata` then rewrites
   `photos.timestamp` on rows of both bursts between the GET and the
   POST, and each `node_id` still resolves to exactly its own burst.
   This is the guard for the failure the capture-time partition could
   not close: a timestamp correction that *merged* two subsets left
   the lower-anchored handle resolving successfully onto the other
   subset's hidden rows (§2, "Capture time — also rejected");
   **filtered-view mutation-ID
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
   reused when a newly accepted *row* resolves to the iNat id for
   *Cyanistes caeruleus* (precedence 1 hits after `taxa.inat_id`
   translation); a fabricated collision case where `taxa.id` for
   taxon A equals `taxa.inat_id` for taxon B does *not* reuse
   taxon B's keyword when accepting a row for taxon A; an unknown
   iNat id (not in the local `taxa` table) falls through to
   precedence 2 rather than raising or reusing an arbitrary keyword;
   a `name:`-keyed row skips precedence 1 entirely and writes its raw
   label; a
   **variant-agreement fixture** — accepting a photo through the
   existing per-row `accept_prediction` primitive when a
   taxon-matched keyword ("Eurasian Blue Tit") already exists writes
   *only* the canonical keyword even if the accepted row's `species`
   string is "Blue Tit", so the Phase 5 sibling loop cannot introduce
   synonym fragmentation; a **first-accept convergence fixture** —
   with **no** keyword yet existing for *Cyanistes caeruleus*,
   accepting a "Blue Tit" row and a "Eurasian Blue Tit" row on the
   same photo yields exactly **one** keyword (the taxon's preferred
   common name), proving precedence 2 converges and that Phase 4 does
   not merely defer fragmentation to the first accept; a
   **no-card-caller fixture** — the same convergence holds when the
   accept is driven by Compare's `accept_subject_species`, which has
   no card, confirming the canonicalization is keyed on the row's
   taxon rather than on a card (§4, "Keyword written on accept").
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
   resolves only F; a **visible-sibling-node fixture** — under an
   active filter that forces per-node fallback (e.g.
   `currentModel = 'BioCLIP-2.5'` chip active but the taxon-key merger
   would otherwise unite it with an iNat21 node, or a similarity re-run
   that split one component into two visible nodes A and B), A and B
   have the same `taxon_key` and share photo `p*`, both are visible, no
   row is hidden; the accept POST for A's `node_id` flips only A's own
   rows (including its row on `p*`), B's row on `p*` and all of B's
   other rows stay pending, and a subsequent accept POST for B's
   `node_id` flips only B's rows; the pair reaches full resolution in
   exactly two clicks (matching the two cards the user sees) and never
   in one; running the same scenario without a filter routes the POST
   through `card_id` and one click accepts both nodes (regression guard
   that the bifurcation is not a permanent loss of the transitive-merge
   behaviour, only a scope-honest suppression while a filter is active);
   a **filtered-mutation shape fixture** — a POST that carries both `card_id` and `node_id` is
   rejected 400; a POST that carries a `node_id` unknown to the server
   (e.g. after a re-run rewrote group IDs) is rejected 400; a stale
   POST that omits the scope tuple resolves the full-workspace card
   (documented legacy behavior); a POST that carries a scope narrower
   than the server's full component cannot exceed the displayed
   membership; a **hidden-sibling-node fixture** — a legacy
   colliding bucket holding two species whose *first* node's rows are
   entirely hidden by `min_confidence` (and, in a second variant, by
   the status tab): the `node_id` the client minted for the surviving
   node still resolves on the POST and mutates exactly that node; a
   **mixed-status card fixture** — an accepted BioCLIP-2.5 row and a
   pending iNat21 row share one card (same taxon, overlapping photos);
   under every representative-row sort order tried (species-string asc,
   confidence desc, prediction-id asc) the aggregate rule renders the
   card as *pending* with accept/reject visible; accepting resolves
   both members via the `card_id` sibling pass; the card then renders
   as *accepted* with only the undo hook (no accept/reject action),
   guarding §2 "Client changes" against the representative-row
   regression that would silently collapse the pending duplicate.
   This is the 400-on-a-valid-click that a positional `subset_index`
   plus a filter-scoped partition rebuild would have produced, and the
   fixture is the regression guard for keying node identity on
   intrinsic row columns rather than on anything the query scope can
   move (§2, "Node identity is a pure function of immutable row
   columns").

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
