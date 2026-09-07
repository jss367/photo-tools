# Encounter grouping evaluation

Compare and tune Vireo's encounter grouping using cached classifier evidence and
current human species labels. Each new run reads the latest library data. The
tool never writes to the library, runs image models, or changes app settings.

This developer package lives outside the application package and has its own
dependencies. Full-library experiments are not part of application builds.

## Install from a Vireo checkout

Use a separate environment; the application itself does not need to be installed:

```sh
python3 -m venv .context/encounter-evaluation-venv
.context/encounter-evaluation-venv/bin/python -m pip install -e 'tools/encounter-evaluation[test]'
```

The examples below assume that environment is activated:

```sh
source .context/encounter-evaluation-venv/bin/activate
vireo-evaluate-encounters inventory
vireo-evaluate-encounters compare --workspace 22 --max-sessions 40
vireo-evaluate-encounters tune --workspace 22 --max-sessions 80 --trials 12 --seconds 300
```

Choose your workspace ID from `inventory`; 22 is only an example. Database access
defaults to `~/.vireo/vireo.db`; override it with `--db`. The tool detects the
checkout from an editable installation, or accepts `--repo` explicitly.

Runs are written to `~/.vireo/encounter-evaluation/runs/<time-id>/`. Open the
printed `report.html` locally. It contains comparative scores, the largest
session improvements and regressions, photo context around changed boundaries,
human labels, and existing thumbnails where their absolute paths are available.
Images are not uploaded or copied into the repository.

## Algorithms and tuning

`compare` evaluates the real production encounter implementation, a conservative
per-photo species-set candidate, and a sequence candidate by default. All use
the same materialized evidence. Production uses its existing flattened top-five
representation, while experimental candidates retain detection/source identity.
This first comparison therefore measures both representation and grouping
changes; independent versus sequence isolates the effect of sequence reasoning.

The sequence candidate infers continuity through short detector misses and
splits on inferred species-set changes. A credible second species in a single
frame is retained. An unclassified second subject or qualified model
disagreement remains unresolved. It is an experimental heuristic, not a
calibrated probability model or a production change. Empty detector output never
becomes a verified empty-photo label.

`tune` searches sequence parameters by default. It scores trials on training
sessions, then evaluates at most three eligible finalists on development
sessions. It never scores test sessions. Selection requires the configured
review coverage and encounter-count limit, then minimizes the documented error
cost. A selected candidate is the best eligible experiment; the report separately
states whether it improves the baseline. Selection is not permission to deploy.

```sh
vireo-evaluate-encounters tune --workspace 22 --candidate production --trials 20
vireo-evaluate-encounters tune --workspace 22 --space parameter-space.json --method grid --trials 16
```

Example `parameter-space.json` for the sequence candidate:

```json
{
  "confidence": [0.4, 0.55, 0.7],
  "margin": [0.1, 0.2],
  "context_frames": [2, 4],
  "transition_penalty": [0.2, 0.5, 0.9]
}
```

`--seconds` is a cooperative search deadline checked between trials, including
baseline/finalist evaluation time. Preparation and report generation are outside
that budget; a running trial finishes before the deadline is checked again.
`--trials` bounds attempted search combinations. If no candidate passes the
constraints, or time expires before development evaluation, no candidate file is
written. Increase the budget rather than interpreting that outcome as failure
of the algorithm.

Use `--resume /path/to/run` to reuse retained inputs and completed trial results,
possibly with a larger trial/time budget. Resume requires matching source and
Python/NumPy versions. It intentionally does not pick up new labels; start a new
run for that. Trial results are keyed by data, code, algorithm, parameters, and
partition. The command is single-process; do not write concurrently to the same
run or split registry.

Evaluate a selected candidate on held-out sessions explicitly:

```sh
vireo-evaluate-encounters compare --resume /path/to/run --partition test \
  --candidate-file /path/to/run/selected-candidate.json
```

Treat this as a selection milestone. Repeatedly tuning after viewing test scores
would turn those sessions into development data. `--partition all` is useful for
descriptive diagnostics, but its scores must not be reported as held-out results.

## Labels, coverage, and evolving data

Labels are hidden before feature preparation, including the production loader's
weak-detection rescue. Algorithms receive no keyword assignments, review status,
ratings, or expected rosters. Taxonomy normalization remains available because
taxon identity is not a per-photo answer.

By default all species keywords are positive-only reference labels. A missing
tag does not assert species absence. Imported tags with unknown provenance are
usable and reported separately from manual-only associations. `--label-source
manual` restricts reference labels to associations recorded as manual; this can
exclude photographer-authored labels imported from sidecars.

For folders you know have complete species tagging, repeat `--complete-folder`:

```sh
vireo-evaluate-encounters compare --workspace 22 --complete-folder 17 --complete-folder 29
```

This declares completeness only for tagged photos in those folders. Untagged
photos remain unlabeled, never verified empty. The current database does not
provide a trusted explicit empty-photo review signal to this tool.

The report measures incorrect additions only on complete rosters. For partial
labels it reports recovered/missing positives and unverified additional species.
The search objective is:

```text
(2 × incorrect additions + missing species on resolved photos
 + unresolved labeled photos + 0.02 × encounter count) / labeled photos
```

Lower is better. Coverage and fragmentation are reported alongside error. A
uniform group with the wrong species fails, abstaining on every photo has a cost,
and unnecessary fragmentation has a small cost. With positive-only labels,
false additions are not measurable: treat optimization as exploratory until
enough complete rosters are available. Scores are not estimates of human time
saved. Same-species subdivisions can be legitimate photographic events.

Every selected session retains all neighboring photos, including untagged and
rejected images and frames without predictions. Sessions use folder/day ordering
and a 30-minute hard gap; candidate grouping may subdivide them further.
`--max-sessions` samples whole eligible sessions using a seeded hash. Missing
prediction coverage is visible rather than silently excluded.

Entire capture days across folders receive stable 60% training, 20% development,
and 20% test assignments. Exact file hashes link duplicate capture days. Split
membership is persisted beside the run directories in `split-membership-<namespace>.json`.
The namespace uses the resolved database path and workspace ID, so unrelated
libraries and workspaces do not share assignments or quarantined dates. The
manifest records the exact registry path. Use the same registry and seed across
experiments; when moving a library or retaining an older `split-membership.json`,
pass its registry explicitly with `--split-registry`. Newly discovered duplicate
links that cross existing partitions quarantine those days rather than leaking
them across splits. Label changes do not reshuffle membership. Different-date
near-duplicates without matching hashes still need an audit. Missing timestamps
fall back to folder/filename order and cannot establish temporal continuity.

New comparisons read a consistent database view, save the necessary session
records, then close the database before optimization. Current and candidate
algorithms run against those same records. Retained gzip JSON inputs, manifests,
and trial records allow replay. The manifest includes source-content signatures
(including uncommitted code), data signatures, configuration, source fingerprints,
coverage, and partition membership. The records may contain private filenames
and labels; keep run directories outside Git. Rerun the baseline whenever labels
change; scores on different data revisions are not a controlled comparison.

The baseline uses recorded application defaults unless `--config` supplies a
JSON object with `detector_confidence`, `classification_threshold`, and/or
`pipeline` settings. It does not read user credentials or silently depend on live
workspace overrides. Cached embedding variants are accepted by default and the
production grouping code handles dimension differences; set
`pipeline.dinov2_variant` explicitly to filter them. This may differ from the
active app's feature selection and is stated in the report.

Source taxon IDs take precedence over names, including when two species share
the same display name. Older libraries without source-ID columns are read through
connection-local compatibility views; the tool never migrates the live database.

Stored sources use the same most-recent fingerprint selection as production.
Existing classifiers are treated as exclusive. Custom multi-label sources,
full label-list coverage validation, and reliable automatic absence inference
need dedicated adapters. Stored inference may already reflect label lists
chosen using human knowledge; this is cached-evidence evaluation, not a claim
about an untouched historical new import.

## Development and packaging checks

```sh
python -m pytest -c tools/encounter-evaluation/pyproject.toml tools/encounter-evaluation/tests -q
ruff check tools/encounter-evaluation
```

The dedicated workflow runs on tool/shared-code changes with synthetic fixtures.
It does not access the private library. Tests verify that the app wheel excludes
the tool, application imports do not depend on it, and the executable archive
guard detects accidental inclusion. The app build explicitly excludes
`encounter_eval` and inspects archive indexes before copying/signing the binary.
No optimizer dependencies were added to Vireo's runtime or `dev` dependencies.

Add new candidates to `algorithms.py` behind `run_algorithm(name, photos, params,
grouping_config)`. They return ordered contiguous `Group` objects and cannot
drop, duplicate, or reorder photos. Keep scoring in `scoring.py` and search in
`runner.py`. Once a candidate is ready to ship, move the inference code into
`vireo/` and have the tool call that shared implementation; retain search and
reporting here.
