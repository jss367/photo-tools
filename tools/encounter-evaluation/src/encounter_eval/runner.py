"""Bounded experiments and resumable trial results; never change app settings."""

from __future__ import annotations

import itertools
import json
import random
import time
from collections import Counter
from pathlib import Path

from .algorithms import SEARCH_SPACE, run_algorithm
from .common import digest, write_json
from .library import read_bundle
from .scoring import score, summarize


def evaluate(output, manifest, algorithm, params, partition):
    if partition not in {"train", "development", "test", "all"}:
        raise ValueError("Invalid partition")
    identity = {"data": manifest["data_digest"], "code": manifest["code"],
                "algorithm": algorithm, "params": params, "partition": partition}
    key = digest(identity)
    path = Path(output) / "trials" / f"{key}.json"
    if path.exists():
        result = json.loads(path.read_text())
        if result["identity"] != identity:
            raise ValueError("Trial identity mismatch")
        return result
    started = time.monotonic()
    counts = Counter()
    sessions = []
    for entry in manifest["sessions"]:
        if partition != "all" and entry["partition"] != partition:
            continue
        bundle = read_bundle(output, entry)
        groups = run_algorithm(algorithm, bundle["photos"], params, manifest["grouping_config"])
        scored = score(bundle["photos"], bundle["answers"], groups)
        counts.update(scored)
        sessions.append({"id": entry["id"], "metrics": summarize(scored)})
    metrics = summarize(counts)
    losses = [s["metrics"]["objective"] for s in sessions if s["metrics"]["objective"] is not None]
    metrics["mean_session_objective"] = sum(losses) / len(losses) if losses else None
    result = {"id": key, "identity": identity, "algorithm": algorithm, "params": params,
              "partition": partition, "metrics": metrics, "sessions": sessions,
              "runtime_seconds": time.monotonic() - started}
    path.parent.mkdir(exist_ok=True)
    write_json(path, result)
    return result


def parameter_trials(space, method, budget, seed):
    if not isinstance(space, dict) or not space or any(not isinstance(v, list) or not v for v in space.values()):
        raise ValueError("Search space must be a nonempty object of nonempty value lists")
    keys = sorted(space)
    size = 1
    for key in keys:
        size *= len(space[key])
    if size > 1_000_000:
        raise ValueError("Search space exceeds one million combinations; narrow it")
    if method == "grid":
        values = itertools.islice(itertools.product(*(space[k] for k in keys)), budget)
        return [dict(zip(keys, value, strict=True)) for value in values]
    if method != "random":
        raise ValueError("Search method must be grid or random")
    indices = random.Random(seed).sample(range(size), min(size, budget))
    trials = []
    for index in indices:
        trial = {}
        for key in reversed(keys):
            index, offset = divmod(index, len(space[key]))
            trial[key] = space[key][offset]
        trials.append(trial)
    return trials


def tune(output, manifest, *, algorithm="sequence", space=None, method="random", trials=12,
         seconds=300, min_coverage=0.5, max_encounters_per_1000=1000):
    if algorithm == "production" and space is None:
        space = {"species_hard_cut_confidence": [0.5, 0.65, 0.8], "species_hard_cut_margin": [0.2, 0.4, 0.6],
                 "hard_cut_score": [0.35, 0.42, 0.5], "merge_score": [0.55, 0.62, 0.7]}
    candidates = parameter_trials(space or SEARCH_SPACE, method, trials, manifest["seed"])
    available = {s["partition"] for s in manifest["sessions"]}
    if not {"train", "development"} <= available:
        raise ValueError("Tuning needs labeled sessions in both train and development; include more sessions")
    started = time.monotonic()
    baselines = [evaluate(output, manifest, "production", {}, partition) for partition in ("train", "development")]
    completed = []
    for i, params in enumerate(candidates):
        # Cooperative deadline between trials; an in-flight trial always finishes.
        if time.monotonic() - started >= seconds:
            break
        result = evaluate(output, manifest, algorithm, params, "train")
        completed.append(result)
        print(f"Trial {i + 1}/{len(candidates)}: objective={result['metrics']['objective']:.4f}", flush=True)
    eligible = [r for r in completed if r["metrics"]["objective"] is not None
                and r["metrics"]["review_coverage"] >= min_coverage
                and r["metrics"]["encounters_per_1000_photos"] <= max_encounters_per_1000]
    eligible.sort(key=lambda r: (r["metrics"]["objective"], r["id"]))
    finalists = []
    for result in eligible[:3]:
        if time.monotonic() - started >= seconds:
            break
        finalists.append(evaluate(output, manifest, algorithm, result["params"], "development"))
    valid = [r for r in finalists if r["metrics"]["review_coverage"] >= min_coverage
             and r["metrics"]["encounters_per_1000_photos"] <= max_encounters_per_1000]
    winner = min(valid, key=lambda r: (r["metrics"]["objective"], r["id"])) if valid else None
    result = {"mode": "tune", "algorithm": algorithm, "search_space": space or SEARCH_SPACE,
              "method": method, "trial_budget": trials, "seconds_budget": seconds,
              "elapsed_seconds": time.monotonic() - started, "completed_trials": len(completed),
              "min_coverage": min_coverage, "max_encounters_per_1000": max_encounters_per_1000,
              "baseline_ids": [r["id"] for r in baselines], "trial_ids": [r["id"] for r in completed],
              "finalist_ids": [r["id"] for r in finalists], "winner": winner,
              "test_partition_evaluated": False}
    if winner:
        baseline = baselines[1]
        result["improves_development_baseline"] = winner["metrics"]["objective"] < baseline["metrics"]["objective"]
        result["recommendation"] = "evaluate-candidate-on-test" if result["improves_development_baseline"] else "keep-production"
        write_json(Path(output) / "selected-candidate.json", {"algorithm": winner["algorithm"], "params": winner["params"],
                   "selected_on_data": manifest["data_digest"], "selected_on_code": manifest["code"],
                   "development_metrics": winner["metrics"]})
    else:
        result["recommendation"] = "keep-production"
        (Path(output) / "selected-candidate.json").unlink(missing_ok=True)
    write_json(Path(output) / "search.json", result)
    return result, [*baselines, *completed, *finalists]
