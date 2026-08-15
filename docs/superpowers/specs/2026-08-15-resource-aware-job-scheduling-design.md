# Resource-aware job scheduling

**Date:** 2026-08-15

**Status:** Draft for review

**Supersedes:** The scheduling boundary in
`docs/plans/2026-05-26-pipeline-concurrency-design.md`; its GPU, regroup,
per-photo mask, and archive-destination safety rules remain in force.

## Summary

Vireo should replace its pipeline-only slot cap with one process-wide,
resource-aware scheduler for every expensive background job.

The immediate delivery commitment is narrower: fix shared model/embedding
correctness and enforce real CPU budgets, then replay the incident workload.
The full durable scheduler remains the reviewed target architecture and passes
through an explicit measurement gate before implementation.

The scheduler does not make all work serial. It admits work when the resources
for its current phase are available:

- bounded CPU permits, paired with enforceable worker/thread counts;
- one accelerator inference lease when an ONNX session actually uses CUDA or
  Core ML;
- one model-construction lease to prevent concurrent multi-gigabyte loads;
- a heavy-I/O lane per physical or network volume;
- the existing workspace mutation and per-photo safety locks; and
- single-flight keys for identical model, embedding, and per-photo
  computations.

Jobs that cannot make useful progress wait in a visible queue. Jobs that need
different resources may overlap. If two jobs request the same computation, one
produces it and the other waits for and reuses the result instead of repeating
the work.

This is a scheduling and backpressure design, not just a larger queue. A job
that receives four CPU permits must configure its process pool or ONNX session
to use at most four CPU threads. Merely counting jobs while each library creates
an unrestricted thread pool would preserve the current oversubscription.

## Motivation and observed failure

The current `JobRunner` has two execution paths:

1. `enqueue_pipeline()` limits pipelines to `SLOT_CAP = 2` and persists excess
   pipelines as queued.
2. `start()` and `start_singleton()` immediately create a worker thread for
   standalone jobs. They do not participate in the pipeline slot count.

That split protects pipeline-versus-pipeline concurrency while allowing an
import, standalone classifier, embedding precompute, scan, preview job, or
other expensive operation to run alongside both pipeline slots.

On 2026-08-15, a live 16-core, 64-GB macOS installation demonstrated the
result. One Vireo process had the following work in the same workspace:

| Work | Scheduler state | Active phase |
|---|---|---|
| In-place import of 5,618 photos | Running outside the pipeline cap | 16-worker hashing, then 1,864 working-copy extractions from an SMB volume |
| All Photos pipeline over 5,164 photos | Running in pipeline slot 1 | Subject detection |
| 560-label BioCLIP embedding precompute | Running outside the pipeline cap | Text inference |
| 1,422-photo pipeline | Running in pipeline slot 2 | BioCLIP model and label-embedding load |
| 801-label BioCLIP embedding precompute | Running outside the pipeline cap | Text inference |
| 3,780-photo pipeline | Queued | Waiting for a pipeline slot |

The pipeline queue itself was correct: `/api/pipeline/slots` reported two
active and one queued. The process as a whole was not bounded:

- Vireo sustained approximately 1,270–1,420% CPU.
- System CPU sampling showed 94–96% user CPU and effectively no idle CPU.
- One-minute load exceeded 50 on a 16-core machine.
- Vireo resident memory grew from about 6.7 GB to 11.1 GB while concurrent
  BioCLIP sessions loaded.
- Common API requests took 2–9 seconds; some thumbnail requests took about
  49 seconds.
- All jobs made at least occasional progress, so this was resource contention,
  not a deadlock.

The workload also exposed two kinds of duplicate work:

- The All Photos pipeline overlapped the two folder-specific collections.
  Different classifiers may be intentional, but their shared detector,
  rendition, mask, or feature computations should not race independently.
- The 560-label precompute and the 1,422-photo pipeline resolved the same
  BioCLIP label set. The precompute route constructs `Classifier` directly,
  outside the pipeline `ModelCache`. Both callers can observe a missing `.npy`
  cache, compute the same embeddings, and publish to the same path. The current
  `np.save()` and manifest update are not a per-key atomic publication
  protocol.

The current embedding identity is also insufficient even without a race. Its
16-character digest binds the model string, model-directory path, and supplied
label strings, but not the actual text-encoder weights, tokenizer, prompt
template set, or embedding schema. The self-heal path invalidates a cache only
when healing fires during that constructor. A valid in-place weights or
tokenizer replacement under the same directory can therefore reuse stale label
embeddings. Phase 1 is both a publication-race fix and a cache-correctness fix.

The database was in WAL mode and ordinary history reads remained fast. The
primary problem was unbounded CPU/model work and shared-volume I/O, not a
second Vireo server or a globally blocked SQLite writer.

## Goals

1. Keep Vireo responsive while long-running jobs are active.
2. Bound actual CPU concurrency, including library-created threads and process
   pools, rather than only bounding top-level job threads.
3. Preserve useful overlap between CPU, accelerator, network, and storage work.
4. Prevent identical model loads, embedding generation, and per-photo
   computation from running more than once at the same time.
5. Sequence or share work across overlapping photo scopes without blocking
   independent workspaces or unrelated model identities.
6. Give every user-visible expensive job honest queued, waiting, running,
   paused, and terminal states.
7. Preserve cancellation, pause safety, sleep inhibition, job history, and the
   existing mutation locks.
8. Make scheduling decisions observable and testable without depending on
   timing-sensitive production threads.
9. Recover reconstructable queued work after restart rather than leaving it
   stranded or silently dropping it.

## Non-goals

- Distributed scheduling across multiple computers or Vireo instances.
- Running the active SQLite database on a shared filesystem.
- Automatically cancelling work because another job overlaps it.
- Guaranteeing an exact completion time or perfect resource-cost prediction.
- Replacing the existing computation-cache identities with path or filename
  heuristics.
- Treating every short database read, thumbnail request, or health check as a
  scheduled background job.
- Making the resource policy a large user-facing tuning panel in the first
  release. Safe defaults and diagnostics come first.
- Removing internal pipeline overlap that already improves throughput.

## Product decisions

These are the proposed target-architecture decisions for review. The rollout
later in this document places a measurement gate before the full scheduler
investment.

1. **One scheduler owns all expensive work.** Pipelines are no longer a special
   scheduling island. Heavy standalone jobs must submit a reconstructable job
   specification or explicitly declare why they are safe to start immediately.
2. **Scheduling is phase-aware.** A pipeline does not reserve CPU, accelerator,
   and a NAS lane for its entire lifetime. It acquires only what its current
   phase needs and releases the lease before doing unrelated work.
3. **CPU leases are enforceable budgets.** Scanner process pools, ONNX Runtime
   sessions, and Vireo-owned thread pools receive their granted concurrency.
4. **Identical work joins, not races.** Model construction, label embeddings,
   detector output, masks, and other cacheable computations use a canonical
   single-flight key. A waiter reads the producer's committed result.
5. **User work can backfill around a blocked queue head, with aging.** Strict
   global FIFO would leave resources idle when the oldest job is waiting for a
   busy NAS but a later CPU-light job is runnable. Bypass is bounded so the
   oldest job cannot starve.
6. **Queue reasons are part of the product contract.** “Waiting for CPU,”
   “Waiting for Photography volume,” and “Joining label embeddings from Job X”
   are visible states, not log-only details.
7. **Existing safety locks remain authoritative.** The scheduler reduces
   contention; it does not replace locks that preserve data integrity.
8. **The first rollout is conservative.** Defaults favor responsiveness and
   predictable progress over maximizing benchmark throughput on an idle
   machine.

## Terminology

**Job specification**

A serializable description of requested work: job type, versioned config,
workspace, priority, provenance, and initial resource profile. It contains no
closure or live database connection.

**Admission**

The transition from queued to a started worker. Admission reserves only the
job's small baseline cost; expensive phases acquire their own leases.

**Resource lease**

A cancellable, scoped allocation such as four CPU permits, one model-loader
permit, or the heavy-I/O lane for the Photography SMB share.

**Single-flight**

Coordination keyed by the exact computation identity. The first caller is the
producer. Later callers wait without holding scarce resources, then read the
producer's durable result.

**Scope overlap**

Two jobs include one or more of the same photo identities. Overlap alone is not
an error because jobs may intentionally use different classifier models or
settings.

**Waiting**

A started job or phase is blocked on a resource, dependency, or single-flight
producer. Waiting is distinct from paused: the scheduler may wake a waiting
job automatically, while a paused job requires Resume.

## Architecture

```text
POST /api/jobs/...
        |
        | validate + freeze versioned JobSpec
        v
 persistent queue / job history
        |
        | admission policy
        v
   JobScheduler --------------------------+
        |                                 |
        | starts reconstructable worker   | resource ledger + conditions
        v                                 |
     JobRunner                            |
        |                                 |
        | context.claim(phase resources) -+
        v
   stage implementation
        |
        +--> SingleFlightRegistry --> durable cache / database publication
        |
        +--> existing GPU, regroup, mask, and archive safety locks
```

`JobRunner` remains responsible for lifecycle, progress events, cancellation,
pause checkpoints, completion, sleep inhibition, and history. A new
`JobScheduler` owns admission, resource accounting, queue ordering, and waiting
reasons. Stage implementations obtain resource leases through a `JobContext`
rather than importing scheduler globals.

## Job specifications and definitions

Introduce a registry of reconstructable job definitions:

```python
@job_definition("precompute-embeddings", spec_version=1)
class PrecomputeEmbeddingsJob:
    def validate_and_freeze(request, db) -> JobSpec: ...
    def initial_claim(spec, hardware) -> ResourceRequest: ...
    def run(context, spec) -> dict: ...
```

A `JobSpec` contains at least:

```text
job_type
spec_version
config
workspace_id
priority
queued_at
provenance                  # manual, chained, maintenance, retry
scope_descriptor            # when known at enqueue time
initial_resource_profile
```

The definition registry solves the current restart limitation: a closure stored
in `_queued_pipelines` cannot be reconstructed after the process exits. A
versioned job definition can rebuild work from persisted config.

Not every existing job must migrate at once. During rollout:

- migrated heavy jobs use `scheduler.submit(spec)`;
- known light jobs may continue through `runner.start_immediate()` with an
  explicit `scheduling_class="interactive-light"` declaration; and
- an undeclared new job type fails a development assertion and logs a release
  warning rather than silently joining the immediate path.

The heavy-job migration order is defined later in this document.

## Resource model

### CPU permits

At startup, Vireo computes a CPU budget from physical cores and keeps an
interactive reserve. The initial policy is:

```text
physical_cores = platform physical-core count, falling back to os.cpu_count()
# Both detections can report nothing (os.cpu_count() returns None on some
# platforms), so normalize to a positive value before the arithmetic below —
# otherwise ceil(None * 0.20) raises at startup.
physical_cores = max(1, physical_cores or 1)
interactive_reserve = max(2, ceil(physical_cores * 0.20))
cpu_capacity = max(1, physical_cores - interactive_reserve)
```

The unavailable-core-count path is a required test case: with both detections
returning nothing, capacity must resolve to the single-permit floor rather than
raising.

On the observed 16-core system, the background capacity would be 12 permits.
This is a starting policy to benchmark, not a promise that every permit has
identical performance on heterogeneous Apple Silicon cores.

A phase requests a range, not an unbounded “CPU heavy” label:

```text
minimum permits required to make progress
preferred permits for good throughput
maximum permits the phase can use
```

The scheduler grants between minimum and maximum. The implementation then uses
the grant:

- `ProcessPoolExecutor(max_workers=grant)` for scan hashing;
- ONNX Runtime `intra_op_num_threads` and `inter_op_num_threads` derived from
  the grant for CPU-executed sessions;
- bounded Vireo thread pools for previews, masks, and other parallel stages;
- native-library environment/session options where a dependency otherwise
  creates its own unrestricted pool.

The scheduler may resize only at explicit phase or batch boundaries. It does
not mutate a live executor's size from another thread.

CPU-only ML inference also takes one `cpu_ml` lane initially. This prevents two
independent ONNX sessions from each saturating the same permit budget through
unaccounted native threads. The lane can later be widened if benchmarks prove
that two explicitly thread-limited sessions improve total makespan without
hurting responsiveness.

### Accelerator inference

The existing process-wide GPU semaphore remains size one and is held only
around inference calls. The resource decision uses the providers reported by
the constructed ONNX session:

- CUDA or Core ML session: acquire the accelerator lease per batch;
- CPU-only session: acquire CPU permits and the `cpu_ml` lane instead;
- unknown provider: use the conservative accelerator path until identified.

Model construction is separate from inference. A phase must not hold the
accelerator lease while downloading weights, reading configuration, decoding
images, tokenizing labels, writing results, or waiting for another resource.

### Model construction and memory

Add a process-wide model-construction capacity of one. Construction includes
opening external ONNX data, creating sessions, and provider compilation. It is
released when the session is ready; inference has its own lease.

The process-wide `ModelCache` remains refcounted and keyed by complete model
identity. The scheduler does not evict a referenced entry. Idle eviction may
be accelerated under memory pressure, but Vireo does not kill a running job to
recover memory.

Before construction, the scheduler records an estimated resident-memory cost
from model metadata or measured prior loads. In the first release this is
diagnostic and prevents more than one construction at a time; it is not a hard
cross-platform memory oracle. A later release may add a memory budget after
measurements are trustworthy.

### Heavy I/O lanes

Vireo derives a stable volume key for each source and destination:

- local paths: mounted filesystem identity, not the spelling of the path;
- SMB/NFS paths: mounted share identity;
- remote SSH destinations: configured remote-target identity;
- unknown paths: a conservative shared `unknown-volume` key.

Each volume has one heavy-I/O lane by default. Metadata probes and individual
thumbnail reads do not take the lane. Operations that stream many files or
large file bodies do:

- full-file hashing;
- RAW working-copy extraction;
- bulk preview or thumbnail generation from originals;
- import copy, move, sync, and verification passes;
- model download/publication on the model-store volume.

A multi-volume operation acquires all required volume keys in sorted order to
avoid deadlock. A copy from NAS A to local disk may block another heavy reader
of NAS A while an unrelated job on NAS B proceeds.

The lane bounds concurrency, not bandwidth. Existing rsync bandwidth limits
remain useful and orthogonal.

### Network transfer lanes

HTTP model/taxonomy downloads and remote SSH transfers use a bounded network
lane keyed by destination service or configured remote target. A single global
capacity would unnecessarily serialize independent local-network and internet
traffic, while no capacity would let repeated downloads saturate a connection.

SMB and NFS photo access remains modeled as a storage volume because filesystem
latency and mount health are the user-visible constraints. A remote SSH archive
may request both its local source-volume lane and its remote-target network
lane. Endpoint labels exposed to logs or APIs are redacted in the same way as
volume labels.

### Workspace and artifact safety

The following correctness locks remain even when the scheduler believes work
does not contend:

- per-workspace regroup/miss lock;
- per-photo mask-write lock;
- archive-destination reservation;
- the existing module-level download/session locks for MegaDetector, DINOv2,
  SAM2, and eye-keypoint models;
- the existing job-level singleton for the Darktable installer;
- local-workspace transition singletons; and
- SQLite transactions and retry behavior.

Most model-download endpoints still create distinct jobs with `runner.start()`;
lower-level module locks may serialize their file work, but the duplicate jobs
remain visible and occupy workers. Phase 5 replaces that partial protection
with consistent job-level join/singleton behavior.

Resource leases are performance policy. Safety locks are correctness policy.
Code must remain correct if resource capacities are changed.

There is no global SQLite-writer lease in the initial design. WAL transactions,
short commit scopes, and existing retry behavior already protect the catalog,
and the observed incident did not show database serialization as the primary
bottleneck. Scheduler diagnostics should record SQLite busy time; a narrower
write-pressure policy can be added later if measurements justify it.

### Default phase profiles

The first implementation should encode profiles in job definitions, not a
large conditional in `JobScheduler`.

| Phase | Initial resource request | Notes |
|---|---|---|
| Import discovery and metadata | 1–2 CPU; source volume only for sustained traversal | Keep UI reads responsive |
| Scanner hashing (all callers) | 2–8 CPU; source heavy-I/O lane | Scanner owns the process pool; executor size equals its grant |
| Working-copy extraction | 2–4 CPU; source and cache-volume lanes | RAW decode may use native threads and must honor the grant where supported |
| Thumbnail/preview generation | 1–4 CPU; source heavy-I/O lane for uncached originals | Cached checks remain light |
| Model construction | model-construction lease; 1–2 CPU; model-store lane while reading | Shared through `ModelCache` |
| CPU ONNX inference | 2–8 CPU; `cpu_ml` lane | Session thread counts equal the grant |
| CUDA/Core ML inference | accelerator lease per batch; 1–2 CPU for preparation | Existing per-batch release behavior remains |
| Label embedding generation | model/embedding single-flight plus appropriate inference lease | No separate uncoordinated `Classifier` construction |
| Group encounters / missed-shot analysis | 1–4 CPU; workspace mutation lock | No accelerator lease while doing database/math work |
| Move/sync/archive | source and destination heavy-I/O lanes; 1–2 CPU | Preserve commit-point cancellation rules |
| Download/install | network class; destination volume; per-artifact job join | Existing lower-level locks remain as safety backstops |
| Ephemeral health/backfill | low priority; at most 1–2 CPU | Must yield to explicit user work |

Profiles are observable in diagnostics and covered by tests. They may be tuned
without changing API contracts.

## Admission and queue policy

### Priorities

Three priorities are sufficient initially:

1. `user` — explicitly started work and retries;
2. `chained` — work requested as part of an import/process chain; and
3. `maintenance` — automatic walks, backfills, cache verification, and other
   deferrable work.

Chained work is not permanently lower than later clicks. Every queued job gains
age credit. After a bounded wait, it has the same effective priority as user
work. Maintenance gains credit more slowly but cannot starve forever.

Destructive commit sections are not a fourth scheduling priority. Once a job
crosses its existing uncancellable commit point, it finishes that short section
without preemption.

### Runnable backfill

Within effective priority, the scheduler examines jobs oldest first. If the
oldest job cannot acquire its minimum resources, a later job may backfill when:

- it uses a resource the older job is not waiting for;
- starting it cannot consume a resource required to satisfy the older job's
  minimum claim; and
- the older job has not reached its maximum bypass count or age threshold.

Once the threshold is reached, the scheduler reserves newly released resources
for the older job until it starts. This avoids both strict-FIFO idle time and
unbounded starvation.

Queue order is deterministic under a fake clock. The policy must not depend on
thread wake-up order.

### Baseline admission versus phase waiting

Queued jobs have no worker thread. Admission starts the worker after reserving
its small baseline cost. The worker releases that baseline or converts it into
a phase lease at the first checkpoint.

A running job may later wait between phases. Its job status stays `running`,
while the current step reports `status="waiting"` and structured scheduling
metadata identifies the blocker. It holds no scarce lease while waiting.

This avoids creating thousands of waiting threads for a long queue while still
allowing a multi-stage pipeline to retain its lifecycle and progress card.

### Pause and cancellation

- Cancelling a queued job atomically marks it cancelled; no worker exists.
- Cancelling a resource waiter removes it from the condition queue immediately.
- A cooperative pause request becomes `pausing` until the next safe checkpoint.
- At the checkpoint, the job releases phase leases, moves to `paused`, and
  stops competing for admission. Cache handles may follow their normal idle
  eviction policy.
- Resume re-enters resource acquisition; it does not jump ahead of older user
  work without age credit.
- Existing uninterruptible commit points remain unchanged.

## Single-flight computation

### General contract

Introduce a `SingleFlightRegistry` with exact keys and producer/waiter roles:

```python
with singleflight.join_or_produce(key, cancel_check=context.cancelled) as flight:
    if flight.is_producer:
        with context.claim(resources):
            value = compute()
        publish_atomically(value)
        flight.succeed()
    else:
        flight.wait()
    return read_committed_value()
```

Rules:

1. A waiter holds no CPU, accelerator, model-loader, or heavy-I/O lease.
2. The producer publishes durable state before notifying waiters.
3. A waiter re-reads the durable cache/database; it never trusts a producer's
   mutable in-memory object as the source of truth.
4. Producer cancellation or failure wakes every waiter with a structured
   dependency failure. A waiter may then retry and become producer according
   to job policy.
5. Registry entries are removed after all participants leave. Durable cache
   identity prevents later duplication.
6. Single-flight keys use the same runtime and input identities as cache
   validity checks. Display model names are not keys.

### Model sessions

`ModelCache.acquire(key, factory)` already implements per-key load joining for
pipeline callers. Every production `Classifier` construction path must use the
same cache and embedding service: `pipeline_job.py`, `classify_job.py`, the
embedding-precompute route in `app.py`, `analyze.py`, and `label_photos.py`.
The latter four currently bypass the pipeline cache. A cache handle owns the
session bundle; it does not imply an inference resource lease.

### Label embedding cache

Move label embedding lookup and publication out of `Classifier.__init__` into
an `EmbeddingCache` service keyed by:

```text
model runtime identity
text-encoder weights identity
canonical full label fingerprint
tokenizer/preprocessing identity
prompt-template-set identity
embedding schema version
```

The canonical label fingerprint is computed from the labels actually fed to
the encoder after stripping leading/trailing whitespace and rejecting empty
entries. It preserves the authoritative post-merge order and case; it does not
hash a caller's unnormalized input and then encode a different stripped list.
Any label deduplication remains the responsibility of the existing label-set
loader before it reaches this service.

Legacy cache entries use the weaker path-based key and are not promoted into
the new identity implicitly. They may remain on disk for compatibility during
one release, but the new service recomputes them lazily under the complete key.
The existing self-heal invalidation remains until all callers use the new key,
then becomes redundant rather than being silently dropped early.

`EmbeddingCache.get_or_compute()` uses single-flight per key and publishes via:

1. create a unique temporary file in the cache directory;
2. write the complete NumPy payload;
3. flush and close it;
4. validate shape, dtype, label count, and digest;
5. atomically replace the final path; and
6. update the manifest through its own locked, atomic replacement.

The manifest is metadata, not cache validity. A crash after publishing the
`.npy` but before updating the manifest must still leave a valid discoverable
entry.

If a pipeline and a precompute request need the same key, the pipeline joins
the precompute or takes over after failure. If active labels are a merge of two
sets, caches for the two individual sets do not masquerade as the merged cache.
The UI should not imply that separately precomputing A and B prepares A+B.

Standalone precompute becomes low-cost orchestration around this shared
service. It must not instantiate an independent image encoder merely to
generate text embeddings when the service can load only the required text
side.

### Per-photo computation

Cacheable stages use keys derived from the portable computation identities
where available:

- detector run: photo input rendition + detector runtime;
- classifier run: detection identity + classifier runtime + full label
  fingerprint;
- mask/features: photo input rendition + mask/DINO runtime and variant;
- working copy or preview: source identity + rendition recipe;
- full-file hash: stable file snapshot identity.

The first implementation should cover detector runs and masks because
overlapping pipelines reach them frequently. After acquiring an existing
per-photo safety lock, the producer rechecks durable cache state before
computing. This turns the lock from “serialize duplicate writes” into “serialize
and let the loser reuse.”

Single-flight is process-local. Durable cache checks still make retries and
post-restart runs idempotent.

## Overlapping photo scopes

Scope overlap is advisory at submission and exact at computation time.

For a catalog-backed job, submission freezes the resolved photo IDs or a
durable snapshot reference. The scope descriptor records:

- workspace and collection/source provenance;
- photo count;
- stable scope digest; and
- an exact membership source available to the scheduler/UI when comparison is
  requested.

The scheduler may report:

> 1,422 photos overlap “Process All Photos.” Shared detector work will be
> reused; model-specific work will still run.

It does not reject the later job. Two pipelines may intentionally run different
classifiers or review strategies over the same photos.

The scope is not used as a global mutex. Exact per-artifact single-flight keys
decide which work can be shared. This avoids unnecessarily serializing:

- two jobs over the same photos with different classifier models;
- a metadata-only job and an inference job; or
- independent stages of partially overlapping collections.

Source-import pipelines cannot freeze photo IDs before ingest. Their scope
descriptor starts with normalized source and destination volume/path claims,
then records catalog photo identities as ingest commits them.

## Persistence and restart

The existing `job_history` table remains the user-facing history source. Add
the scheduling fields needed to reconstruct queued jobs and explain waits:

```text
spec_version
priority
queue_sequence
queued_at
admitted_at
provenance
scheduling_state_json
scope_digest
```

The existing `config` JSON stores the frozen versioned job config. Large exact
scope membership may live in a separate `job_scope_items(job_id, photo_id)`
table with an index on `photo_id`, rather than inflating every history poll.
Terminal retention removes matching scope rows.

On startup:

- prior `running`, `pausing`, or `paused` jobs become failed with the existing
  interrupted-by-restart explanation unless that job type later implements a
  durable resume protocol;
- queued jobs whose `(job_type, spec_version)` has a registered definition are
  rehydrated and reconsidered for admission in queue order;
- queued jobs with an unknown definition version fail visibly with an upgrade
  incompatibility message; and
- ephemeral maintenance work is not recovered unless its definition explicitly
  opts in.

Rehydration does not trust enqueue-time validation forever. Before admission,
the registered definition revalidates immutable identity and prerequisites such
as the workspace, folders, source snapshot, selected label material, model, and
destination. It does not silently re-resolve mutable settings into a different
job. A missing or incompatible prerequisite fails the queued job visibly with
recovery guidance.

This intentionally changes current behavior, where queued pipeline closures
cannot survive restart and the startup sweep marks them failed.

Queue promotion remains an atomic conditional database transition. The
scheduler never holds its in-memory lock while waiting on SQLite, filesystem,
network, model, or safety locks.

## Locking and deadlock prevention

The scheduler resource ledger is not a lock that stage code keeps while
acquiring arbitrary locks. A resource lease is recorded under the scheduler
mutex; the mutex is released before returning the lease.

Required rules:

1. Never call user/stage code while holding `JobScheduler._lock` or
   `JobRunner._lock`.
2. Never perform SQLite, filesystem, network, or ONNX work while holding either
   scheduler lifecycle lock.
3. A single-flight waiter holds no resource lease.
4. A single-flight producer claims producer identity first, releases the
   registry mutex, then waits for resources.
5. A phase requests its minimum scheduler resources atomically; it does not
   incrementally hold one scheduler lease while waiting for another. Multiple
   volume keys are normalized and sorted as part of that single request.
6. The accelerator lease remains the innermost inference lease and is released
   after each batch.
7. Workspace/per-photo correctness locks are acquired only at documented stage
   boundaries and are never acquired by scheduler callbacks.
8. Progress publication must not acquire a resource on behalf of a stage.

The implementation plan must update the existing lock-order comment in
`pipeline_locks.py` after tracing the exact call graph. The design deliberately
does not prescribe a misleading single total order across locks that are never
held together.

## API and user experience

### Job payload

Active and queued job JSON gains a `scheduling` object:

```json
{
  "state": "waiting",
  "priority": "user",
  "queue_position": 2,
  "waiting_for": ["cpu", "volume:Photography"],
  "reason": "Waiting for CPU and Photography storage",
  "resources_held": [],
  "joined_job_ids": ["pipeline-..."],
  "scope_overlap": {
    "photo_count": 1422,
    "job_ids": ["pipeline-..."]
  }
}
```

Internal volume keys and cache digests must not leak credentials, mount-user
names, or full private paths. The API exposes a safe display label and opaque
identifier.

`status` retains the existing lifecycle values. A waiting phase uses a waiting
step plus `scheduling.state`; old clients may continue treating the job as
running.

### Jobs panel

The Jobs panel becomes the canonical scheduler view:

- all expensive work appears, including precompute and imports;
- queued and waiting cards state why they are blocked;
- joined work says which result it is reusing;
- running cards may show compact labels such as `CPU 6/12`, `Accelerator`, or
  `Photography storage`;
- queue position is shown only when meaningful because runnable backfill can
  change the next admitted job;
- pause, resume, stop, and cancel affordances preserve their existing safety
  rules; and
- diagnostics may expand to show the resource ledger for troubleshooting.

The UI should describe outcomes rather than implementation names. “Waiting for
photo storage” is preferable to “blocked on io_lane:smb:7a1c.”

### Compatibility endpoints

`GET /api/pipeline/slots` remains during migration. It is derived from the
unified scheduler and continues returning `active`, `queued`, and `slot_cap`
for older pipeline-page code. New UI uses the richer scheduler fields from
`GET /api/jobs`.

The two-pipeline cap may remain as a conservative pipeline-level admission
guard during early rollout. Once every expensive pipeline phase has enforceable
resource profiles, measurement can determine whether the fixed cap still adds
value. Removing it is not required for the first release.

## Observability

The scheduler emits structured events for:

- submitted, admitted, phase-resource requested/granted/released;
- queued or phase-waiting reason changes;
- bypass and aging decisions;
- single-flight producer/joiner/success/failure;
- configured versus granted CPU concurrency;
- model construction, reuse, eviction, and measured resident-memory delta;
- volume-lane wait and hold duration; and
- cancellation or pause while waiting.

Do not log every batch at info level. Aggregate counters and transitions are
enough for production logs.

Expose an authenticated diagnostic snapshot containing capacities, allocations,
waiters, and redacted resource labels. It must be read-only and cheap enough for
the Jobs panel. Historical summaries record queue wait, resource-wait time,
active compute time, and wall time separately.

These measurements answer whether concurrency improves total makespan rather
than merely making every individual job slower.

## Failure handling

- Failure to acquire a resource is a wait, not a failed job.
- Invalid or impossible resource requests fail validation before enqueue.
- If the single-flight producer fails, waiters receive the producer's concise
  failure and may offer Retry; they do not hang.
- If cache publication validation fails, the temporary file is removed and the
  final cache path remains unchanged.
- If the scheduler thread crashes, a watchdog marks scheduling unhealthy,
  rejects new heavy submissions with a visible error, and leaves currently
  running workers under their existing cancellation behavior. It must not
  silently fall back to unbounded starts.
- A missing volume keeps the job queued/waiting with the existing storage
  guidance. It does not consume a CPU permit while probing repeatedly.
- Database persistence failure prevents promotion of a queued job; memory and
  database state must not disagree about whether a worker was admitted.

## Configuration

Initial policy is automatic. Add one advanced diagnostic setting only if field
experience requires it:

```text
background_cpu_limit = 0  # 0 means automatic
```

The existing `scan_workers` setting remains accepted for compatibility but
becomes a maximum request, not an entitlement. For example, `scan_workers=16`
on a 12-permit scheduler may receive 12 while idle and four while an inference
phase holds eight permits. `scan_workers=1` continues to force sequential
hashing. Although legacy schema/UI copy calls this a thread count, scanner
hashing uses `ProcessPoolExecutor`; revised copy should call these workers or
processes.

Environment-variable overrides used by tests and benchmarks must be explicitly
test-only or documented developer controls. Production scheduling should not
depend on shell startup state.

## Performance and correctness targets

The implementation is successful when it meets all of these on representative
local-SSD and SMB/NAS fixtures:

1. Background CPU usage never exceeds the granted scheduler capacity because
   of Vireo-owned pools or configured ONNX threads, aside from short measured
   native-library bursts.
2. A heavy workload preserves at least 10% idle CPU under the default policy on
   an otherwise idle machine, unless an explicit user override removes the
   reserve.
3. Cached API requests used by navigation and the Jobs panel have p95 latency
   below 500 ms during sustained processing on the reference 16-core system.
4. Identical label-embedding requests keep at most one active encoder producer
   per request key and converge on exactly one valid durable cache entry under
   forced races and cancellation. A retry after producer cancellation or
   failure may run the encoder again — that is the deliberate consequence of
   the failure contract above, not a violation of this criterion.
5. Two overlapping pipelines execute each identical detector/runtime/photo
   computation at most once while still running distinct classifier identities.
6. Peak resident memory for the observed overlapping BioCLIP scenario is
   bounded by shared model sessions and does not grow with each identical
   caller.
7. Sequential single-job throughput regresses by no more than 10% from the
   pre-scheduler baseline.
8. Mixed-resource total makespan is better than strict global serialization and
   interactive latency is materially better than the current unbounded run.
9. No queued user job starves under continuous later submissions.
10. Cancellation, pause, restart sweep, sleep inhibition, and mutation safety
    tests remain green.

Targets may be adjusted after recording a reproducible baseline, but they must
not be replaced with “looks faster.”

## Test strategy

### Deterministic scheduler tests

Use a fake clock and fake resources. Cover:

- minimum/preferred/maximum CPU grants;
- admission and release;
- runnable backfill and starvation reservation;
- user, chained, and maintenance priority aging;
- cancellation before admission and during a phase wait;
- pause releasing leases and Resume rejoining fairly;
- atomic promotion when Cancel races admission;
- multi-volume all-or-nothing acquisition; and
- scheduler shutdown with queued, waiting, and running work.

No scheduler unit test should depend on `sleep()` to establish ordering.

### Single-flight and cache tests

- Many callers for one key produce exactly once.
- Different keys compute concurrently when resources allow.
- Waiters hold no resource permits.
- Producer cancellation wakes waiters and permits retry.
- A partial temporary file never becomes the final cache entry.
- Manifest-write failure leaves the payload valid and discoverable.
- A pipeline and standalone precompute with the same labels share one result.
- Merged labels A+B do not hit separate A or B cache entries.
- Model self-heal changes the key/invalidation outcome without mixing old and
  new image/text encoders.

### Job integration tests

- Import hashing receives and obeys the scheduler worker grant.
- CPU ONNX session thread counts match the granted budget.
- CPU-only ML phases serialize on the initial `cpu_ml` lane.
- Accelerator sessions alternate at existing batch boundaries.
- Heavy operations on one SMB volume queue; independent volumes overlap.
- Overlapping detector work joins while different classifiers both run.
- Mask waiters recheck durable cache inside the per-photo lock.
- Queued reconstructable jobs survive restart in order.
- Unknown job-spec versions fail visibly after restart.
- The compatibility pipeline-slots endpoint reflects unified state.

### Performance harness

Create an opt-in benchmark scenario modeled on the observed incident:

- one multi-folder in-place import from a throttled storage fixture;
- one broad pipeline;
- one overlapping narrow pipeline;
- one identical-label embedding request; and
- one different-label request.

Record CPU, resident memory, queue/resource wait, API latency, duplicate
computation count, per-job wall time, and total makespan. Run the same fixture
in strict-serial, current-unbounded, and resource-aware modes. Performance
assertions belong in the benchmark report, not timing-sensitive unit tests.

## Rollout plan

Each phase should be independently reviewable and safe to ship.

Approval of this architecture does not commit the project to all seven phases
immediately. Phases 1 and 2 are the incident-driven commitment: fix cache
correctness, enforce CPU budgets, and replay the observed workload. Phase 3 and
beyond proceed when post-Phase-2 measurements still show harmful admission,
I/O, memory, duplicate-computation, visibility, or restart behavior—or when the
durable walk-away queue is an explicit product priority. If measurements justify
stopping after Phase 2, record that decision and retain this design as the
reviewed expansion path rather than building a large scheduler speculatively.

### Phase 1: Instrument and fix duplicate cache publication

- Add scheduler-oriented timing fields to jobs without changing admission.
- Extract `EmbeddingCache` from `Classifier`.
- Add the complete correctness key, per-key single-flight, and atomic
  payload/manifest publication.
- Route all production construction paths through the shared `ModelCache` and
  `EmbeddingCache`: `pipeline_job.py`, `classify_job.py`, the precompute route
  in `app.py`, `analyze.py`, and `label_photos.py`.
- Add a repository-level check or narrow allowlist so a future direct
  `Classifier(...)` caller cannot silently bypass the services.
- Correct stale `SLOT_CAP=1` comments in `jobs.py` and `app.py` while updating
  the surrounding lifecycle documentation.

This addresses the concrete duplicate BioCLIP race before the broader
scheduler ships.

### Phase 2: Enforce CPU budgets and measure

- Add the reusable resource-ledger core with CPU permits, but leave existing job
  submission and persistence paths in place.
- Make every scanner caller consume a grant and size
  `ProcessPoolExecutor(max_workers=grant)` accordingly.
- Configure CPU ONNX sessions with granted thread counts.
- Bound Vireo-owned preview, mask, and extraction pools.
- Introduce the initial exclusive `cpu_ml` lane.
- Surface basic resource-wait timing in existing job diagnostics.
- Replay the observed import + overlapping pipelines + embeddings benchmark and
  evaluate the delivery gate above.

This slice must be reusable by the later `JobScheduler`; it is not a temporary
second semaphore system. It directly addresses oversubscription without first
requiring durable job specifications or queue rehydration.

### Phase 3: Add unified scheduler and durable admission

- Add `JobSpec`, job-definition registry, `JobScheduler`, admission policy, and
  deterministic fake-clock tests around the Phase-2 resource ledger.
- Preserve the current pipeline cap and use a conservative one-for-one policy
  for migrated heavy standalone work initially.
- Add durable queue reconstruction, priorities, bounded backfill, and scheduling
  state to APIs and the Jobs panel.
- Re-trace lock order and keep both comments in `pipeline_locks.py` and
  `pipeline_job.py` synchronized with the implemented call graph.

The scheduler does not widen mixed concurrency until the Phase-2 CPU limits are
proven in packaged builds.

### Phase 4: Add volume-aware I/O

- Resolve and redact volume identities.
- Migrate import hashing, working-copy extraction, previews, move, sync, and
  archive phases to heavy-I/O leases.
- Test same-volume serialization and different-volume overlap.

### Phase 5: Migrate heavy standalone jobs

- Embedding precompute and standalone classify/extract-masks;
- import, scan, thumbnails, previews, and duplicate/hash verification;
- downloads, move/sync/archive, and local-workspace transitions, replacing
  module-only download serialization with visible job-level join semantics;
  and
- deferrable maintenance/backfill work.

Every migrated job receives a versioned reconstructable definition and restart
behavior.

### Phase 6: Make pipelines phase-aware

- Replace coarse pipeline admission assumptions with stage resource requests.
- Keep the existing two-pipeline cap as a safety ceiling initially.
- Add per-photo detector and mask single-flight reuse.
- Surface overlap and joined-work information.

### Phase 7: Tune policy from benchmarks

- Compare serial, current, and resource-aware modes.
- Adjust CPU reserve, phase grants, backfill thresholds, and `cpu_ml` capacity.
- Remove or raise the fixed pipeline cap only if measurements and safety tests
  justify it.

## Alternatives considered

### Keep the two-pipeline cap and add more ad hoc guards

This would solve only the combinations we remember to check. The observed
incident involved an import and two precompute jobs that were invisible to the
pipeline cap. As new job types arrive, the same failure returns.

### Run exactly one background job at a time

This is safe but wastes useful overlap. A NAS copy can coexist with bounded
accelerator inference, and a short workspace regroup need not wait behind an
unrelated model download. It also regresses the walk-away queue use case that
the pipeline-concurrency work intentionally enabled.

### Let macOS schedule unrestricted threads

The OS can time-slice threads but cannot infer Vireo's latency goals, cache
identity, NAS semantics, or which work is duplicate. Native ONNX pools can
create far more runnable threads than physical cores, producing the high load
and poor progress observed here.

### Rely only on semaphores around GPU calls

The live workload was dominated by CPU and model construction despite the
existing accelerator semaphore. It also does not prevent duplicate cache
publication or storage contention.

### Reject every overlapping photo scope

Overlap can be intentional: the same photos may need different classifiers or
review strategies. Exact computation identities can share the common work
without blocking the distinct work.

### Use a second worker process or external task queue

Another process does not create more CPU, memory, or NAS bandwidth and makes
the in-process model/cache sharing problem harder. A durable external queue may
be appropriate for distributed Vireo someday, but it is unnecessary for one
desktop process.

## Risks and open questions

1. **ONNX thread enforcement differs by provider and model.** The implementation
   plan must prove which session options bound CPU work on packaged macOS,
   Windows, and Linux builds.
2. **Volume identity is platform-specific.** SMB aliases, symlinks, drive
   letters, and remote targets need canonicalization tests without exposing
   credentials.
3. **Pipeline internal concurrency is complex.** Stage queues may hold CPU or
   I/O work concurrently inside one job. Resource claims must cover internal
   producers, not just the outer pipeline thread.
4. **Resource estimation can be wrong.** Conservative defaults and measured
   grants matter more than pretending estimates are exact.
5. **Large exact scope snapshots add database rows.** Retention, indexes, and
   history-query isolation need measurement before choosing
   `job_scope_items` over a compact serialized snapshot.
6. **Priority terminology needs product review.** The user should not have to
   understand `user/chained/maintenance`, but the Jobs panel must explain why
   later work started first.
7. **Precompute product behavior may be simplified.** If pipelines compute
   embeddings on demand through single-flight, automatic standalone precompute
   may be unnecessary or should remain maintenance-priority warming only.
8. **The fixed pipeline cap may still be valuable.** Phase-aware scheduling
   reduces its importance, but pipelines also create internal queues and
   memory pressure that resource profiles may not capture initially.
9. **Restart semantics change.** Rehydrating queued jobs is more useful than
   failing them, but versioned specs and frozen mutable settings must be
   complete before enabling recovery.
10. **One background I/O lane can still hurt interactive reads.** Serializing
    heavy work per SMB/NFS volume is better than several unbounded streams, but
    one sustained RAW decode or copy can still compete with an interactive
    thumbnail request. The performance harness must measure this explicitly;
    chunk-level yielding, bandwidth pacing, or an interactive-read reservation
    may be needed after Phase 4.

## Acceptance criteria for design approval

Before producing the Phase-3-and-beyond scheduler implementation plan,
reviewers should agree on:

- one unified scheduler for expensive work;
- enforceable CPU permits and the initial interactive reserve;
- phase-level resource acquisition rather than lifetime-wide reservation;
- per-volume heavy-I/O lanes;
- shared model and atomic single-flight embedding caches;
- exact per-artifact sharing for overlapping scopes;
- visible queue/wait reasons and bounded backfill with aging;
- versioned reconstructable job specifications and restart behavior;
- retention of existing safety locks; and
- the staged rollout and benchmark gates above.

Questions that do not block the architecture—exact CPU grant numbers, UI copy,
and whether the final pipeline cap remains two—can be settled from Phase 1–2
measurements in the implementation plan.
