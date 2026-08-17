# Measuring Vireo Under Concurrent Workloads

Use the live workload monitor to evaluate responsiveness and resource use while
imports, pipelines, embedding preparation, and background scans overlap. The
monitor is read-only: it does not submit, pause, resume, or cancel jobs.

Reports are diagnostic evidence, not timing-sensitive test assertions. They
record process-tree CPU and resident memory, system CPU and memory, Jobs API
latency, CPU grants, resource waits, sanitized job progress, job wall time,
workload makespan, and label-embedding single-flight activity.

## Prepare a trustworthy run

Run Vireo from an installed or otherwise stable application path. Do not launch
the packaged application inside a disposable build workspace and then delete
or move that workspace: a one-file packaged server may need its original
executable for lazy imports after startup, which invalidates both the workload
and responsiveness measurements.

For comparisons, keep the machine, library, storage connection, Vireo build,
and job sequence the same. Note whether source files are on local storage or a
network volume. Close unrelated CPU-heavy applications when the goal is to
measure Vireo's idle-CPU reserve.

Install the development dependencies if needed:

```bash
python -m pip install -e '.[dev]'
```

## Record a workload

With one local Vireo server running, start a 30-minute monitor:

```bash
python scripts/monitor_vireo_workload.py --duration 1800
```

The monitor discovers the listening `vireo-server`, authenticates through the
normal local browser-session flow, and writes a timestamped report under
`.context/benchmarks/`. Start the jobs in Vireo after the monitor begins. Press
Ctrl-C to save a partial report early.

If more than one Vireo server is running, identify the intended instance:

```bash
python scripts/monitor_vireo_workload.py \
  --pid 48335 \
  --url http://127.0.0.1:50222 \
  --duration 1800
```

Use `--output PATH` to select another report location and `--interval SECONDS`
to change the default two-second sampling interval. The report excludes job
configuration, results, workspace names, filenames, cache keys, label names,
and full executable paths so it can be reviewed without exposing library
contents.

## Recommended concurrent workload

For the scheduling design's measurement gate, use a representative library and
run this sequence:

1. Begin a multi-folder in-place import.
2. Start a broad pipeline.
3. Start an overlapping narrower pipeline.
4. Request embeddings for the same label set used by one pipeline.
5. Request embeddings for a different label set.

Let the run continue until the jobs finish or until the chosen measurement
window ends. Repeat the same sequence when comparing builds or resource policy.

## Interpret the report

The `summary.targets` object reports the initial responsiveness gates:

- Jobs API 95th-percentile (`p95`) latency below 500 milliseconds.
- System idle CPU 5th-percentile (`p05`) of at least 10 percent.
- No equal-key embedding single-flight violations.
- The Vireo executable remained present for the complete run.

The summary also includes CPU and resident-memory distributions, resource-wait
deltas, maximum queued jobs and waiters, cache hits, producer starts and joins,
per-job outcomes, and workload makespan when every observed job reaches a
terminal state.

Interpret system idle CPU alongside other applications: the monitor observes
the whole machine and cannot attribute unrelated load to Vireo. Resident-memory
growth is evidence to investigate rather than an automatic failure because the
model mix determines the expected steady-state footprint.

The rollout decision remains evidence-driven:

- If CPU and responsiveness pass and jobs progress normally, retain the current
  resource budget without adding a larger scheduler.
- If network-volume work delays interactive reads or unrelated jobs, prioritize
  volume-aware I/O coordination.
- If standalone work remains misleadingly `running`, queued work starves, or
  restart loses admission state, proceed with unified durable scheduling.
