# Portable computation cache

**Date:** 2026-08-08
**Status:** Experimental phase-one implementation — format remains draft
pending the cross-provider experiment

## Implementation status

The experimental phase-one bundle workflow is implemented behind an explicit
**Experimental** label in Settings. It includes:

- schema migration and full runtime/input/label identities for new detector
  and classifier runs;
- review-aware runtime replacement, including a pin from any workspace and an
  explicit Reclassify override;
- an immutable local object store and atomic `.vireo-cache` ZIP export;
- full-bundle validation before import publication, including size, digest,
  traversal, symlink, numeric-bound, and compression-ratio checks;
- immediate exact-hash fan-out to matching catalog photos, plus local-store
  reuse when a photo is cataloged after the bundle was imported;
- Settings export/import controls and status summaries; and
- automatic publication of newly computed portable detector and classifier
  output when Vireo can prove the source, model, label, and input identities.

Existing database results remain tagged `legacy`: they are valid local cache
hits but are not exported. An explicit Reclassify recomputes them with the new
identity contract. RAW+JPEG companion-backed runs also remain local-only until
the canonical multi-source rendition contract is frozen.

The cross-provider experiment, semantic-divergence diagnostics/tolerances,
folder-scoped export sheet, retention controls, and shared NAS pack/index are
still follow-up work. The artifact schema is therefore intentionally marked
experimental rather than a compatibility promise.

## Summary

Let one Vireo installation reuse expensive detector and classifier work done by
another installation, without sharing a live SQLite catalog and without
turning Vireo into a cloud service.

The first release is a portable cache bundle: computer A exports raw processing
results for selected photos, computer B imports the bundle, and Vireo matches
the results to its own catalog by the photo's SHA-256 content hash. A later
release can point both computers at an append-only cache folder on a NAS and
use the same artifact format automatically.

This complements XMP rather than replacing it:

- XMP remains the portable source of truth for accepted species, keywords,
  ratings, flags, and other user decisions.
- The computation cache carries reproducible machine output: detections,
  classifier candidates, confidences, model identities, and completed-run
  markers.
- Workspace review state, edits, collections, and pending XMP changes remain
  local unless another feature explicitly synchronizes them.

This first release skips detector and classifier inference only. A full
pipeline on computer B still computes working copies, masks, DINO embeddings,
quality metrics, and any other enabled stages that are not yet portable.

## Problem

Wildlife photographers commonly keep originals on a NAS, process on a powerful
desktop, and browse or review on a laptop. Vireo's current cache avoids repeated
work across workspaces in one catalog, but it is local to `vireo.db`. A second
computer that catalogs the same photo bytes cannot reuse the first computer's
MegaDetector or classifier results.

The current workarounds each lose something important:

- Syncing accepted species to XMP shares the final decision, but not pending
  predictions, alternatives, confidences, or detection boxes.
- Copying `vireo.db` is a whole-catalog replacement, depends on compatible
  absolute paths, and cannot safely merge two computers' work.
- Putting the active SQLite database on a network share creates locking,
  availability, and corruption risks and still does not define merge semantics.

The useful first feature is therefore **shared computation**, not shared
catalog state and not distributed job scheduling.

## Goals

1. Reuse detector and species-classifier results for byte-identical photos on
   another Vireo installation.
2. Keep the feature local-first: bundles and user-chosen filesystem locations,
   with no Vireo-hosted service required.
3. Never overwrite or implicitly transfer human review decisions.
4. Reject stale results when the model, weights, canonical input rendition,
   preprocessing, label set, or artifact schema is incompatible.
5. Make reuse visible in job progress and summaries.
6. Make export/import idempotent and safe to repeat.
7. Use one artifact format for one-off bundles now and a shared NAS cache later.

## Non-goals

- Running one Vireo instance as a remote worker or job server.
- Sharing a live `vireo.db` between machines.
- Synchronizing workspaces, collections, history, culling choices, accepted or
  rejected prediction status, pending XMP changes, or application settings.
- Resolving conflicting human edits. XMP and the existing audit flow own that
  problem.
- Sharing masks, DINO embeddings, quality metrics, or rendered working copies
  in the first release. These can use the same envelope later, but have larger
  payloads, more complex cache keys, and additional privacy implications.
- Matching visually similar or metadata-similar photos. Version 1 requires an
  exact byte hash.

## Proposed product decisions

These are recommendations for review, not yet approved decisions.

1. **Ship export/import before automatic NAS sharing.** It proves identity,
   compatibility, merge, and UX semantics without introducing concurrent
   writers or an always-online dependency.
2. **Version 1 shares raw detector and classifier output only.** This directly
   solves the species-identification use case while keeping bundles small.
3. **Match only by full-file SHA-256.** Vireo already computes and stores this
   as `photos.file_hash`. Paths, filenames, timestamps, perceptual hashes, and
   file sizes are not sufficient identity.
4. **Review state never travels in a computation artifact.** Imported
   predictions enter the destination workspace exactly as locally computed
   results would. Existing accepted/rejected state on the destination wins.
5. **Cache writes are immutable and atomic.** A shared folder contains
   content-addressed files, never a SQLite database that several machines open.
6. **Separate semantic equivalence from exact payload identity.** Execution
   providers are provenance, not compatibility identity. Artifacts preserve
   useful confidence values and have an exact integrity digest, while a
   provider-tolerance policy decides whether two payloads represent the same
   boxes and ordered species. Equivalent payloads reuse the already-
   materialized result; meaningful divergence is retained and reported rather
   than forcing inference forever.
7. **No filenames or folder paths in portable artifacts.** The photo hash is
   sufficient for matching and avoids leaking a user's library organization.
8. **Standardize inference input before shipping sharing.** The current
   classifier can consume either a working-copy JPEG or a direct original/RAW
   decode. Portable publication requires a named canonical input-rendition
   contract; current fallback-dependent runs are legacy and not exportable.

## User experience

### Export on computer A

Settings gains a **Computation Cache** section with **Export Results…**.

The phase-one controls offer:

- scope: all processed photos (selected-folder scope is follow-up work);
- artifact types: Detection and Species classification (both selected by
  default); selecting Species classification automatically includes its
  Detection dependency;
- destination: a file ending in `.vireo-cache`.

Before writing, Vireo reports how many photos have reusable results for the
selected detector/model/label-set scope, how many are missing a full content
hash, and how many legacy results lack a safe runtime identity. Global catalog
counts would be misleading here. Export can hash missing non-empty photos after
explicit confirmation; it does not silently read terabytes of originals and it
does not repeatedly chase deliberately NULL hashes for empty files.

The completion summary labels each unit explicitly: unique artifacts, affected
catalog photo rows, detector runs, classifier runs, bundle bytes, artifacts
stored for later, skipped legacy rows, and failures.

### Import on computer B

The same section provides **Import Results…**. Import validates the entire
bundle, stores valid artifacts in the local computation cache, and applies them
to matching catalog photos through the normal cache-hit paths.

The summary distinguishes:

- matched and applied now;
- stored for photos not yet present in the catalog;
- already present;
- unknown to the destination Vireo version;
- produced with a regional label set that is not installed locally (including
  its display name, species count, and full fingerprint);
- semantically divergent or invalid.

Artifacts for uncataloged photos remain useful. If those bytes are cataloged
later, the next Process/Classify run discovers and applies the cached result.

During processing, progress and the final job card show a line such as:

> Reused 1,248 detector-run artifacts across 1,310 catalog photo rows and
> 1,103 classifier-run artifacts; stored 87 artifacts for later.

The review UI does not need a permanent badge on every card. Prediction detail
may show optional provenance: origin device label, Vireo version, execution
provider, input rendition, and compute date. On first export, Vireo proposes
the hostname as an editable device label and explains that hostnames can
contain a person's name before including it.

## Architecture

```text
Computer A database
        |
        | export raw, compatible runs by photo SHA-256
        v
  .vireo-cache bundle  ----copy / NAS / AirDrop---->
        |
        v
Computer B local artifact store
        |
        | exact photo + runtime cache lookup
        v
normal detector/classifier cache-hit paths
        |
        +--> global raw results in B's catalog
        +--> B's workspace-local review reconciliation
```

### 1. Photo identity

The outer identity is:

```text
photo_sha256 = SHA-256(original file bytes)
```

Vireo already stores this value in `photos.file_hash`, and scanner hashes are
full-file SHA-256. The same photo may have a different filename, folder, mount
point, or photo id on the destination computer and still match.

An edited derivative is a different photo because its bytes differ. XMP-only
changes do not change the original's hash and do not invalidate raw inference;
workspace categorization is recomputed when the artifact is applied.

### 2. Canonical input rendition

The original-file hash identifies the library asset, but it does not fully
identify the pixels Vireo feeds a model today. Classification prefers a
4096-pixel quality-92 working-copy JPEG and falls back to a direct original
decode. RAW decoding mode/version and RAW+JPEG companion substitution can also
change pixels while `photos.file_hash` remains unchanged.

Portable publication therefore has a prerequisite: define versioned canonical
rendition recipes for detector and classifier inputs. A recipe covers:

- source-selection rules, including companion substitution;
- the SHA-256 of every source file whose bytes contributed pixels;
- RAW decode mode and decoder implementation/version;
- orientation, color space, resize/crop algorithm, maximum edge, and JPEG
  settings where an intermediate JPEG is used;
- model tensor size and normalization; and
- a digest of the actual canonical pixel/tensor input produced for the run.

These are concrete algorithm constants, not a vague application-version tag.
For today's detector they include the 640-pixel tensor, padding/box decoding,
NMS IoU `0.45`, category mapping, and raw confidence floor. For today's
classifier the current sequence is: decode the working copy/original without a
load-time size cap, crop with 20% subject-box padding, thumbnail that crop to
1024×1024 with Lanczos, then apply the model-specific tensor preprocessing.
The latter's input size, mean, and standard deviation come from the model
directory's preprocessing config; portable identity hashes that config file
rather than duplicating its values in hand-maintained version constants.

The recipe should reuse the existing working-copy path where possible, but a
missing working copy must not silently switch an exportable run to a different
input. Vireo either creates the canonical rendition or treats the fallback run
as local-only legacy output. This refactor is required before portable
artifacts are enabled.

### 3. Runtime identity

Current local gates use `detector_model` and
`(classifier_model, labels_fingerprint)`. Portable reuse needs a stronger
identity because a model name can survive a weights or preprocessing change.

Introduce a central `runtime_fingerprint` helper that hashes canonical JSON
containing:

- artifact type and artifact schema version;
- public model id;
- exact model-weights SHA-256 or immutable upstream revision;
- preprocessing/postprocessing pipeline version;
- inference implementation version;
- canonical input-rendition recipe version;
- raw detector output floor for detection artifacts;
- classifier label fingerprint for classification artifacts;
- detector runtime fingerprint for a box-based classifier artifact.

The portable label fingerprint is the full SHA-256 of the canonical label set,
not the current 12-character display/storage prefix. `compute_fingerprint()`
and the existing PK/UNIQUE key columns do **not** change: changing their return
value would miss every existing classifier run and reclassify the catalog.
Instead, add a `compute_full_fingerprint()` helper and nullable
`labels_fingerprint_full` compatibility columns alongside the existing short
keys. New runs write both. Existing rows are backfilled in place only when
their canonical label source is available; otherwise they remain usable local
legacy hits but are not portable. If an imported full hash collides on the same
12-character prefix, retain it externally and refuse automatic materialization
rather than aliasing two label sets.

The ONNX execution provider and hardware are recorded as provenance but are
not part of compatibility identity. CPU, CUDA, and CoreML are expected to
produce small numeric differences for the same logical result; including the
provider would defeat desktop-to-laptop reuse.

The detector threshold visible in workspace settings is deliberately absent:
MegaDetector stores raw boxes above its fixed floor and applies the workspace
threshold at read time. Changing that preference must remain a cache hit.

Run tables need to record the runtime fingerprint used at compute time. This is
more than a cosmetic provenance column: every local cache gate must evaluate it
under the staleness rules below, or a weights/preprocessing change would still
silently reuse stale unreviewed rows while the portable store correctly missed.

The recommended migration avoids rebuilding the largest primary keys in the
first release:

- add the runtime fingerprint to `detector_runs` and `classifier_runs` as a
  required validity field for new runs;
- add the detector runtime fingerprint to each `detections` row so stale-row
  retirement can prove which run owns a box;
- add nullable full-label-fingerprint columns without changing the existing
  short keys or `compute_fingerprint()` return value;
- keep at most one materialized runtime for an existing local logical run key;
  the external artifact store may retain several runtimes;
- on a runtime mismatch with unreviewed output, treat the local row as stale and
  eligible for a portable hit or fresh inference;
- on a runtime mismatch with a manually reviewed/pinned result, satisfy the
  gate without inference, preserve the result, and surface **Reviewed with an
  older runtime**. Only an explicit Reclassify may replace it.

Detections and raw predictions are global while review rows are workspace-
scoped, so “reviewed/pinned” means a manual review in **any** workspace that
references the materialized result. Otherwise replacing a global row for
workspace B could silently cascade-delete workspace A's decision. Reclassify
must enumerate affected workspaces in its existing destructive warning.

Pinning is enforced at **photo × detector model**, not only at classifier-run
granularity. If any prediction on any detection for that photo/model is
manually reviewed in any workspace, a detector runtime mismatch is a pinned
stale hit: Vireo does not re-detect the photo automatically. Detector boxes are
the parent rows of predictions, so moving or retiring one would cascade-delete
the prediction and every workspace's review before a classifier gate could
protect it.

Stale-row retirement is runtime-scoped. The ordinary detection UPSERT may
retire only boxes whose `runtime_fingerprint` equals the incoming run. Replacing
an older detector runtime is a separate atomic operation that first proves the
photo/model is unpinned, then deletes the old runtime and writes the new run.
It never lets `_upsert_detection_rows` cross a runtime boundary implicitly.
This also fixes the existing hazard where a same-named MegaDetector weights
change nudges a quantized box id and silently cascades reviewed predictions.

An implementation plan must enumerate and update every detector/classifier
gate and write path. A migration marks existing rows `legacy` until Vireo can
prove their identity from an immutable bundled model revision. Unverifiable
legacy results continue to work locally but are not exported.

### 4. Artifact model

One photo/run artifact contains only raw machine output. Local database ids,
workspace ids, comparison categories, and review status are excluded.

Artifact integrity and semantic equivalence are separate:

- The **lookup key** is the photo hash, artifact type, runtime fingerprint, and
  canonical input identity. It finds candidate payloads without assuming their
  floating-point bytes are identical.
- The **artifact digest** hashes canonical JSON of the exact portable payload.
  Canonical JSON normalizes negative zero, rejects NaN/infinity, and uses a
  stable field/list order, but it does not coarsely round away useful detector
  confidence. Classification confidences retain the precision Vireo already
  persists.
- A versioned **semantic comparison policy** decides whether two exact
  payloads are equivalent enough to reuse as the same logical output.

For detections, semantic comparison uses the raw stored set above the fixed
raw floor—never the workspace's read-time threshold. It pairs same-category
boxes within an empirically chosen geometric tolerance, compares confidence
within a separate tolerance, and permits an unmatched box only inside an
empirically chosen epsilon band around `RAW_CONF_FLOOR`. For classifications,
it requires the same subject mapping and ordered top-k species, then compares
confidences with a provider tolerance. A top-1 change is semantic divergence.
The comparison-policy version is part of portable compatibility.

`detection_id.py`'s four-decimal box quantization remains the local database-id
rule; it is useful evidence but is not assumed to be a sufficient portable
confidence or equivalence policy.

`input_fingerprint` is SHA-256 of the canonical input block: recipe id, ordered
source roles/hashes, and pixel/tensor digest. A classification artifact's input
fingerprint covers its ordered subject input digests and the detector artifact
identity. Different input fingerprints are cache misses, not semantically
divergent candidates—a RAW decode and a companion-JPEG rendition must never be
compared as two executions of the same input.

Illustrative detector record:

```json
{
  "artifact_schema": 1,
  "type": "detection",
  "photo_sha256": "…",
  "runtime_fingerprint": "…",
  "input_fingerprint": "…",
  "input": {
    "recipe": "detector-input-v1",
    "pixel_sha256": "…",
    "sources": [{"role": "original", "sha256": "…"}]
  },
  "completed": true,
  "subjects": [
    {
      "key": "d0",
      "kind": "box",
      "box": {"x": 0.12, "y": 0.08, "w": 0.43, "h": 0.61},
      "confidence": 0.94,
      "category": "animal"
    }
  ]
}
```

An empty `subjects` list with `completed: true` is a valid cached empty scene.
A failed or cancelled run is never exported as completed.

A classification artifact references subjects by the bundle-local `key` and
contains the ordered species candidates and confidences for each subject. A
synthetic full-image subject uses `kind: "full_image"`; each subject result
also records its canonical model-input digest. Scientific taxonomy
may travel as source metadata, but destination-dependent categories `match`,
`new`, `conflict`, and `refinement` are always reconciled locally rather than
trusted from the source catalog.

Classification metadata includes the label-set display name, count, and full
SHA-256 fingerprint. Version 1 does not include the complete regional species
list by default; that list is not needed to reuse the candidates and may reveal
location. If the exact list is absent on the destination, the import summary
says so rather than calling the artifact corrupt or silently claiming it
matches the destination's current list.

Each candidate may carry its source taxonomy id and lineage plus the taxonomy
snapshot identity. The destination prefers its local taxonomy when available
and falls back to the carried lineage for display/enrichment. Relationship
categorization that needs a taxonomy graph remains pending with a clear
"taxonomy required" reason when neither source is sufficient; it must not
silently degrade an otherwise matching result to `new`.

The artifact digest is SHA-256 of the complete canonical artifact. Provenance
is stored in bundle manifests and import history, outside the artifact, and
does not change whether the computation is reusable.

If a result for the same lookup/runtime key is already materialized locally, it
remains selected whether it is reviewed or not; a later lower-digest import
never churns detections or repeats review reconciliation. A reviewed result is
additionally pinned across runtime changes by the stale-runtime rule above. An
unreviewed result from a different/stale runtime remains replaceable. When
several candidates are discovered before anything is materialized, the
lexicographically lowest artifact digest wins. All payloads remain available
for diagnostics, and semantic divergence is reported. This converges among
installations choosing from the same candidate set while making later imports
order-stable; it does not claim that machines which have seen different
artifact sets magically converge.

### 5. Local artifact store

Import places validated objects under the Vireo profile, separate from the
catalog and thumbnail cache:

```text
~/.vireo/computation-cache/
  objects/<photo-hash-prefix>/<photo-sha256>/
    detection/<runtime-fingerprint>/<input-fingerprint>/<artifact-digest>.json
    classification/<runtime-fingerprint>/<input-fingerprint>/<artifact-digest>.json
  divergence/
```

Writers use a temporary file in the destination directory, `fsync`, and atomic
rename. Existing identical objects are a no-op. The store is rebuildable and
may be deleted without losing accepted metadata or catalog state.

New local inference publishes to this store after its database transaction
commits. Export also lazily synthesizes artifacts from compatible database rows
created before the store existed.

### 6. Bundle format

`.vireo-cache` is a ZIP container using only standard ZIP compression so the
first release adds no compression dependency:

```text
manifest.json
objects/<artifact-digest>.json
```

The manifest includes format version, creation time, optional device label,
object count, byte totals, and each object's digest, uncompressed size, and
optional provenance. It does not contain secrets, filesystem paths, XMP,
thumbnails, or original image bytes.

Import enforces limits before extraction, rejects absolute or parent-traversal
paths and symlinks, checks declared and actual sizes, hashes every object, and
validates numeric bounds and required fields. Validation happens before any
artifact is made visible.

### 7. Applying imported results

The importer must not insert review rows directly. For each matched photo it:

1. matches against the stored `photos.file_hash` (it does not re-read the
   original during ordinary import);
2. materializes the detector run and normalized detections using local ids;
3. maps bundle-local subject keys to those detection ids;
4. materializes raw prediction candidates and classifier-run markers;
5. invokes the same cache-reuse reconciliation used by a normal Process or
   Classify job.

That last step preserves current behavior: taxonomy categories are evaluated
against the destination's XMP/keywords, existing manual review is preserved,
and a cached result is surfaced into grouping and downstream pipeline stages.

The apply operation is transactional per photo. A malformed classifier record
cannot leave a completed `classifier_runs` marker without its predictions.

`photos.file_hash` is intentionally not unique because exact duplicates are a
first-class catalog feature. Apply fans one artifact out to every non-rejected
photo row carrying that hash, generating each row's local detection ids. Import
and job summaries report both unique artifacts and affected catalog photo rows
so their counts remain explainable. A later Audit/Verify Hashes run still owns
detecting files changed behind a stale stored hash.

### 8. Job integration and reclassify

The current standalone Classify job resolves/downloads and constructs its model
before the per-photo classifier cache gate. The pipeline also starts model
loading before it knows whether all classifier work can be served portably.
The promise that destination weights are unnecessary therefore requires a job
refactor, not just an import path:

1. resolve the requested public runtime and label-set identity without loading
   weights;
2. resolve database, local-store, and shared-store hits;
3. lazily download/load detector or classifier weights only when the first
   genuine miss is reached; and
4. let progress mark model loading as **Skipped — all results reused**.

For a regional list, the job compares the full canonical label hash. An
imported result remains reviewable when that list is absent, but a newly
requested run cannot claim the same cache key without the list. UI explains
the missing display name/count rather than degrading to a generic model miss.

Tree-of-Life identity must also be resolvable without installed ToL files.
Today `_load_labels` discovers ToL readiness from `tol_embeddings.npy` and
`tol_classes.json` under the downloaded model, creating a cache-key paradox on
a fresh laptop. The model registry therefore gains an immutable label-space id,
ToL taxonomy/embedding revision, and portable fingerprint independent of local
readiness. On-disk checks answer only whether inference can run after a miss.
If an older/custom model has no registry identity, Vireo says the portable key
is unresolvable and falls back to today's explicit model/label setup; it does
not download speculatively just to ask the cache.

**Reclassify means fresh inference.** It bypasses the database cache, portable
local store, and shared store for the selected scope, then publishes the fresh
normalized artifact. It retains today's destructive/review warnings. Ordinary
Process/Classify is the action that reuses imported artifacts; importing a
bundle itself may immediately apply hits via the same cancellable reuse path.

## Phase 2: shared cache folder

After bundle import/export is proven, Settings may allow one or more **Shared
Cache Folders**, typically on a mounted NAS. They use the same artifact and
identity format, but not necessarily the local store's one-file-per-object
physical layout.

A pipeline over 100,000 photos cannot issue several `stat` calls per photo over
SMB/VPN. The shared format therefore needs batch discovery: immutable pack
files plus immutable, per-writer index generations that readers copy locally
and query in one pass. Each writer owns its own namespace and atomically
publishes a small current-generation pointer, so no two machines write the same
index database. Phase 2 planning must benchmark lookup and hit-fetch round trips
on LAN SMB and high-latency VPN mounts before choosing the exact pack size.

Lookup order during processing becomes:

1. normal result already materialized in the local catalog;
2. local artifact store;
3. configured shared stores in user-defined order;
4. local inference.

A shared hit is copied into the local store before it is materialized, so an
intermittent NAS does not break later use. Shared folders are reuse-only by
default; publishing is a separate opt-in. Locally computed objects that are
published use immutable writer packs. Identical outputs converge by artifact
digest at lookup, while semantically divergent outputs use the deterministic
selection rule above.

Offline or read-only shared folders degrade to local processing and a visible
warning. They never block a pipeline indefinitely. Cache cleanup is explicit;
version 1 of shared folders does not automatically delete another machine's
objects.

## Privacy and trust

The feature is local-only by default and never uploads artifacts.

Although a detector box or species prediction is less sensitive than an
original photograph, photo hashes and inferred species can still reveal
information about a collection. Import treats bundles as untrusted input, and
the UI explains what an export contains.

Embeddings are excluded from version 1 because they may encode substantially
more information about image content. Adding them requires a separate product
and privacy review as well as a binary payload format.

Imported artifacts are data, never executable plugins. Unknown model/runtime
identities may be retained for a future compatible Vireo release but are not
loaded into active results. A known compatible model does not need to have its
weights downloaded on the destination: reuse should avoid that download as
well as inference.

## Failure and conflict semantics

| Situation | Behavior |
| --- | --- |
| Same photo at a different path | Reuse by SHA-256 |
| Original bytes changed | Cache miss; leave artifact stored |
| Same detector, different workspace threshold | Reuse raw detections |
| Same classifier, different label fingerprint | Reuse detection; classifier miss |
| Runtime unknown to this Vireo version | Store but do not apply |
| Known runtime whose weights are not downloaded | Apply; weights are unnecessary |
| Any reviewed prediction under a photo's older detector runtime | Pin photo × detector model; no inference/box retirement; show stale-runtime state |
| Identical artifact imported twice | No-op |
| Same key, differences within semantic tolerances | Reuse materialized result, or lowest digest if none is materialized |
| Same key, semantic divergence | Report/retain all; keep materialized result, otherwise lowest digest wins |
| Lower-digest artifact imported after materialization | Keep current result; do not churn reconciliation |
| Bundle partially corrupt | Reject bundle before publishing objects |
| App exits during import | Atomic objects survive; incomplete temp files ignored |
| NAS unavailable in phase 2 | Warn and continue with local cache/inference |

## Observability

Job events and history should separately count:

- database cache hits;
- local portable-cache hits;
- shared-cache hits;
- detector and classifier inference runs;
- incompatible, semantically divergent, and invalid artifacts.

An import-history record stores the bundle digest, source label, counts, and
time. It does not own the imported objects and deleting the history row does
not invalidate cache entries.

## Testing

### Identity and compatibility

- Same bytes under different names and paths reuse successfully.
- One-byte modification is a miss.
- A detector threshold change remains a hit.
- Measured CPU/CUDA/CoreML drift within the frozen tolerances is semantically
  equivalent even when exact artifact digests differ.
- Detector comparison uses raw stored boxes, and floor-band noise does not
  depend on a workspace's visible threshold.
- Different box counts or top species are reported as semantic divergence and
  fresh installations with the same candidate set choose the same winner.
- A model-weight, preprocessing-version, or label-fingerprint change misses
  only the affected artifact type.
- Working-copy versus direct-original fallback runs are not accidentally
  treated as the same portable rendition.
- A RAW+JPEG companion artifact matches only when every contributing source
  hash matches.
- Different input fingerprints occupy different store keys and are cache
  misses, never semantic-divergence candidates.
- Completed zero-detection runs round-trip and skip detector inference.
- Failed/cancelled runs are not exported as completed.
- All-cache-hit jobs skip model download/session construction; the first miss
  triggers lazy loading.
- A ToL cache key resolves from registry metadata on a machine with no ToL
  files or model weights installed.
- Adding full label fingerprints neither changes existing short keys nor
  triggers catalog-wide classification.
- Reclassify bypasses every cache layer and publishes its fresh result.

### Merge and review safety

- Import is idempotent.
- Imported predictions surface through the same path as locally cached
  predictions.
- Existing accepted, rejected, and manually corrected prediction state is not
  overwritten.
- A reviewed older-runtime row satisfies later cache gates without repeated
  inference and exposes its stale-runtime state.
- One reviewed prediction in any workspace pins the complete photo × detector-
  model run; an automatic runtime change neither invokes detection nor retires
  any of that photo's boxes.
- Same-runtime stale-box retirement cannot delete a different runtime's rows;
  explicit runtime replacement first proves the photo/model is unpinned.
- A late lower-digest artifact does not replace an already-materialized
  unreviewed result or rerun reconciliation.
- One artifact fans out idempotently to several non-rejected duplicate photo
  rows, and summaries separate artifact count from affected-row count.
- Destination XMP and local taxonomy are used to recategorize imported raw
  output; carried lineage is the fallback when local taxonomy is unavailable.
- Applying one bad photo rolls back that photo without corrupting successful
  photos.
- A classifier-run marker is never committed without its required prediction
  rows.

### Bundle security

- Reject path traversal, absolute paths, symlinks, undeclared objects, digest
  mismatches, oversized fields, excessive object counts, and ZIP bombs.
- Unknown artifact versions are retained only when safe and never applied.
- No config secrets, paths, filenames, or original bytes appear in an export.

### Shared folder (phase 2)

- Concurrent identical writers converge without partial objects.
- Concurrent semantically divergent writers retain both payloads; readers with
  the same candidate set and no materialized result choose the same winner.
- Read-only, disconnected, and reconnecting shares degrade safely.
- A shared hit is usable after the share disconnects because it was copied
  locally first.
- Scope lookup uses copied indexes/batched queries rather than per-photo SMB
  metadata round trips.

## Pre-format cross-provider experiment

Semantic tolerances are a measured compatibility contract, not values to pick
from intuition. Before the artifact format is frozen, run a representative set
of roughly 200 JPEG and RAW photos through each supported execution provider
using the exact same canonical renditions and model bytes. Include empty
scenes, multiple subjects, detections close to the raw floor, and visually
similar species.

Record and retain as test fixtures:

- maximum and percentile detector/classifier confidence deltas;
- normalized box deltas and IoU after local four-decimal id quantization;
- detection-count changes, especially inside bands around `RAW_CONF_FLOOR`;
- ordered top-k and top-1 species flip rates; and
- resulting semantic-equivalence and divergence rates under proposed policies;
- exact objects per lookup key/provider and projected store size as the number
  of contributing machines grows.

Run detector and classifier experiments separately, with separate policies.
MegaDetector's current external-data ONNX excludes CoreML, so its realistic Mac
laptop comparison is CPU↔CUDA desktop; do not manufacture a CoreML detector
axis the product cannot run. Exercise CPU↔CUDA and CPU/CUDA↔CoreML for each
classifier model only where that model is actually supported; do not infer
CoreML behavior merely from nominal provider availability.

The experiments set per-artifact box, confidence, and raw-floor tolerances and
make artifact multiplicity/store growth a first-class acceptance result. If no
useful tolerance separates benign provider drift from meaningful confidence
changes, keep confidences out of semantic identity and expose their variance as
provenance instead of destroying their precision. The format remains draft
until this experiment has a checked-in result and fixtures.

## Rollout

1. Standardize and version detector/classifier input renditions, run the
   cross-provider experiment, and freeze semantic comparison tolerances.
2. Add runtime fingerprints to run tables and update every local cache gate,
   replacement path, full-label-fingerprint sidecar column, and
   migration/legacy rule.
3. Refactor jobs to resolve cache hits before lazily loading model weights.
4. Add the local artifact store and publish new inference results to it behind
   a feature flag.
5. Add bundle export/import and cache-hit observability.
6. Enable by default after cross-provider, round-trip, large-catalog upgrade,
   and macOS/Windows testing.
7. Design and benchmark shared pack indexes, then add reuse-only shared-folder
   lookup and opt-in publishing as a separate phase.

Steps 1–3 are invasive core-path changes, so they do not land as one hidden
prerequisite stack. Each ships and is validated independently on an existing
Vireo benefit:

- canonical renditions make repeated local inference reproducible;
- runtime-aware gates prevent silent stale reuse after same-named weights or
  preprocessing changes; and
- lazy loading makes today's all-database-cache-hit jobs start faster and work
  offline without touching already-cached model assets.

Each step has its own rollback/exit point. Portable bundle work proceeds only
if those changes are stable and the provider experiment shows acceptable
semantic agreement and store multiplicity.

## Open questions

1. Should import immediately apply matching artifacts, or only make them
   available for the next explicit Process/Classify run? This draft recommends
   immediate application with a reviewable summary.
2. Should exported bundles optionally include accepted/rejected status? This
   draft recommends no; accepted metadata already has the safer XMP path.
3. Should a device label be included by default, requested on first export, or
   omitted unless configured? This draft recommends a first-export prompt with
   the hostname prefilled and editable.
4. Should missing hashes be computed inside Export, or should Export direct
   the user to an Audit/Verify Hashes job first? This draft offers an explicit
   compute-now choice for non-empty files.
5. What retention controls should the local store expose: maximum size, age,
   per-model cleanup, or manual-only cleanup for the first release? The initial
   recommendation is manual cleanup plus a user-visible maximum-size cap.
6. Should bundles optionally include the complete canonical regional label
   list, or only its display name, count, and hash? This draft recommends
   metadata only by default because the full list can disclose location.
7. Should confidence participate in semantic equivalence once provider deltas
   are measured, or should it remain payload/provenance only? The experiment
   above decides this before the format is frozen.
