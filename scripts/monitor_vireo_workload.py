#!/usr/bin/env python3
"""Record a read-only resource and responsiveness report for a live Vireo.

The monitor discovers a single local ``vireo-server`` by default, establishes
the same browser-session cookie as the desktop UI, and samples the process tree
and ``GET /api/jobs``.  It never starts, pauses, cancels, or otherwise mutates a
job.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import math
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - exercised by the CLI environment
    psutil = None


SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"cancelled", "completed", "failed"}
DIAGNOSTIC_COUNTERS = {
    "cache_hits",
    "cache_misses",
    "producer_starts",
    "producer_publications",
    "producer_failures",
    "waiter_joins",
    "single_flight_violations",
}


def _utc_now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _number_summary(values):
    clean = [float(value) for value in values if isinstance(value, (int, float))]
    if not clean:
        return None
    return {
        "min": round(min(clean), 4),
        "p05": round(_percentile(clean, 0.05), 4),
        "p50": round(_percentile(clean, 0.50), 4),
        "p95": round(_percentile(clean, 0.95), 4),
        "max": round(max(clean), 4),
        "mean": round(sum(clean) / len(clean), 4),
    }


def _compact_progress(progress):
    if not isinstance(progress, dict):
        return {}
    # `phase` is intentionally omitted.  Vireo builds phase text from a mix
    # of generic labels ("Importing photos"), user-derived folder names
    # ("Copying <folder> locally"), absolute paths ("Scanning root 1 of
    # 2: /photos"), and filenames ("Downloading 1/3: capture.nef..."), and
    # no path/URL/filename regex can reliably tell the safe ones apart from
    # phrases that leak library contents.  The numeric progress
    # (current/total/rate) and the generated `stage_id` are enough to
    # correlate steps across polls without exposing free-form text.
    return {
        key: progress[key]
        for key in ("current", "total", "stage_id", "rate")
        if progress.get(key) is not None
    }


def _compact_job(job, source):
    compact = {
        key: job.get(key)
        for key in (
            "id",
            "type",
            "status",
            "workspace_id",
            "started_at",
            "finished_at",
            "duration",
            "error_count",
            "resource_wait_seconds",
            "resource_wait_count",
        )
        if job.get(key) is not None
    }
    compact["source"] = source
    progress = _compact_progress(job.get("progress"))
    if progress:
        compact["progress"] = progress
    running_steps = []
    for step in job.get("steps") or job.get("tree") or []:
        if step.get("status") != "running":
            continue
        # Deliberately omit `label`: for local-folder, sync, and discard
        # jobs the label is built from `root_names[root_id]` (e.g.
        # "Copy <folder> locally"), so copying it verbatim would leak
        # user folder names into every sample.  The `id` is enough for
        # correlating steps across polls.
        running_steps.append({
            "id": step.get("id"),
            "status": step.get("status"),
            "progress": _compact_progress(step.get("progress")),
        })
    if running_steps:
        compact["running_steps"] = running_steps
    return compact


def compact_jobs_payload(payload):
    """Remove paths, workspace names, configs, results, and filenames."""
    history_jobs = payload.get("history", [])
    history_ids = {job.get("id") for job in history_jobs if job.get("id")}
    active = [
        _compact_job(job, "active")
        for job in payload.get("active", [])
        # Ephemeral jobs never enter persisted history, so their terminal
        # record in ``active`` is the only evidence that they completed.
        # Persisted jobs can briefly appear in both lists; prefer the history
        # record in that case to keep one observation per job.
        if (
            job.get("status") not in TERMINAL_STATUSES
            or job.get("id") not in history_ids
        )
    ]
    history = [
        _compact_job(job, "history")
        for job in history_jobs
    ]
    return {
        "jobs": active + history,
        "resource_budget": payload.get("resource_budget"),
        "workload_metrics": payload.get("workload_metrics", {}),
        "keeping_awake": payload.get("keeping_awake"),
    }


class VireoApiClient:
    def __init__(self, base_url, timeout=5.0, *, opener=None, clock=time.perf_counter):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def _request(self, path):
        url = urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))
        started = self.clock()
        error = None
        try:
            with self.opener.open(url, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            error = f"HTTP {exc.code}"
        except (OSError, urllib.error.URLError) as exc:
            return {
                "latency_seconds": round(self.clock() - started, 6),
                "status": None,
                "error": str(exc),
            }
        elapsed = self.clock() - started
        if status != 200:
            return {
                "latency_seconds": round(elapsed, 6),
                "status": status,
                "error": error or f"HTTP {status}",
            }
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            return {
                "latency_seconds": round(elapsed, 6),
                "status": status,
                "error": f"invalid JSON: {exc}",
            }
        return {
            "latency_seconds": round(elapsed, 6),
            "status": status,
            "payload": payload,
        }

    def authenticate(self):
        result = self._request("/")
        if result.get("status") != 200:
            raise RuntimeError(
                f"could not establish a Vireo browser session: "
                f"{result.get('error') or result.get('status')}"
            )

    def sample(self):
        result = self._request("/api/jobs")
        payload = result.pop("payload", None)
        if payload is not None:
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("active"), list)
                or not isinstance(payload.get("history"), list)
            ):
                result["error"] = "invalid Jobs API payload"
            else:
                result.update(compact_jobs_payload(payload))
        return result


def _is_successful_jobs_sample(sample):
    """Return whether a Jobs API sample is usable for workload analysis."""
    return (
        sample.get("status") == 200
        and not sample.get("error")
        and isinstance(sample.get("jobs"), list)
    )


class ProcessTreeSampler:
    def __init__(
        self,
        pid,
        *,
        psutil_module=psutil,
        monotonic=time.monotonic,
        platform_system=platform.system,
    ):
        if psutil_module is None:
            raise RuntimeError(
                "psutil is required; install the Vireo development dependencies"
            )
        self.psutil = psutil_module
        self.monotonic = monotonic
        self._cpu_accounting_complete = platform_system() not in {
            "Darwin", "Windows",
        }
        self.root = psutil_module.Process(pid)
        self.processes = {}
        self._cpu_times = {}
        self._last_cpu_sample_at = None
        self._reaped_children_cpu_times = {}
        self._known_child_cpu_lifetimes = {}
        self._unreconciled_departed_child_cpu = 0.0
        self._needs_rebaseline_after_incomplete = False
        self._initial_rss_bytes = None
        self._initial_executable_exists = None
        try:
            self.executable = self.root.exe()
        except (OSError, self.psutil.Error):
            self.executable = None
        try:
            command = self.root.cmdline()
        except (OSError, self.psutil.Error):
            command = []
        try:
            self._root_create_time = self.root.create_time()
            started_at = datetime.fromtimestamp(
                self._root_create_time, UTC
            ).isoformat(timespec="seconds")
        except (OSError, self.psutil.Error):
            self._root_create_time = None
            started_at = None
        self._metadata = {
            "pid": self.root.pid,
            "process_started_at": started_at,
            "executable_name": (
                os.path.basename(self.executable) if self.executable else None
            ),
            "command_name": os.path.basename(command[0]) if command else None,
        }

    def _metadata_snapshot(self, *, identity_verified):
        return {
            **self._metadata,
            "identity_verified": identity_verified,
            "initial_rss_bytes": self._initial_rss_bytes,
            "initial_executable_exists": self._initial_executable_exists,
            "cpu_accounting_complete": self._cpu_accounting_complete,
            "executable_exists": (
                os.path.exists(self.executable) if self.executable else None
            ),
        }

    def metadata(self):
        self._assert_root_alive()
        return self._metadata_snapshot(identity_verified=True)

    def metadata_unverified(self):
        """Return diagnostics after a late identity check has failed."""
        return self._metadata_snapshot(identity_verified=False)

    def _assert_root_alive(self):
        try:
            alive = self.root.is_running()
            current_create_time = self.root.create_time()
            status = self.root.status()
        except (OSError, self.psutil.Error) as exc:
            raise RuntimeError(
                f"monitored Vireo PID {self.root.pid} exited"
            ) from exc
        if (
            not alive
            or status == getattr(self.psutil, "STATUS_ZOMBIE", "zombie")
            or (
                self._root_create_time is not None
                and current_create_time != self._root_create_time
            )
        ):
            raise RuntimeError(
                f"monitored Vireo PID {self.root.pid} exited or was replaced"
            )

    def verify_identity(self):
        """Abort if process metrics and API samples can no longer be paired."""
        self._assert_root_alive()

    def _reaped_cpu_seconds(self, cpu_times):
        if not self._cpu_accounting_complete:
            return 0.0
        if not (
            hasattr(cpu_times, "children_user")
            and hasattr(cpu_times, "children_system")
        ):
            self._cpu_accounting_complete = False
            return 0.0
        return cpu_times.children_user + cpu_times.children_system

    def _current_processes(self):
        discovered = [self.root]
        enumeration_complete = True
        try:
            discovered.extend(self.root.children(recursive=True))
        except (OSError, self.psutil.Error):
            enumeration_complete = False
            discovered.extend(
                process
                for process in self.processes.values()
                if process.pid != self.root.pid
            )
        live = {}
        for process in discovered:
            try:
                identity = (process.pid, process.create_time())
            except (OSError, self.psutil.Error):
                enumeration_complete = False
                continue
            # A descendant PID can be reused between polls. Reuse a cached
            # psutil object only when its creation time still identifies the
            # same process; otherwise retain the newly discovered object.
            live[identity] = self.processes.get(identity, process)
        self.processes = live
        return list(live.values()), enumeration_complete

    def prime(self):
        self._assert_root_alive()
        self._initial_executable_exists = (
            os.path.exists(self.executable) if self.executable else None
        )
        self._cpu_times = {}
        self._reaped_children_cpu_times = {}
        self._known_child_cpu_lifetimes = {}
        initial_rss_bytes = 0
        process_count = 0
        prime_complete = True
        processes, enumeration_complete = self._current_processes()
        if not enumeration_complete:
            raise RuntimeError(
                "could not enumerate the Vireo process tree before sampling"
            )
        for process in processes:
            try:
                identity = (process.pid, process.create_time())
                cpu = process.cpu_times()
                total_cpu_seconds = cpu.user + cpu.system
                reaped_cpu_seconds = self._reaped_cpu_seconds(cpu)
                self._cpu_times[identity] = total_cpu_seconds
                self._reaped_children_cpu_times[identity] = reaped_cpu_seconds
                if process.pid != self.root.pid:
                    self._known_child_cpu_lifetimes[identity] = (
                        total_cpu_seconds + reaped_cpu_seconds
                    )
                initial_rss_bytes += process.memory_info().rss
                process_count += 1
            except (OSError, self.psutil.Error):
                prime_complete = False
        if not prime_complete:
            raise RuntimeError(
                "could not capture a complete Vireo process-tree baseline"
            )
        self._unreconciled_departed_child_cpu = 0.0
        self._needs_rebaseline_after_incomplete = False
        self._initial_rss_bytes = initial_rss_bytes if process_count else None
        self._last_cpu_sample_at = self.monotonic()

    def sample(self):
        self._assert_root_alive()
        recovering_from_incomplete = self._needs_rebaseline_after_incomplete
        sampled_at = self.monotonic()
        elapsed = (
            sampled_at - self._last_cpu_sample_at
            if self._last_cpu_sample_at is not None
            else 0.0
        )
        cpu_seconds = 0.0
        current_cpu_times = {}
        current_reaped_children_cpu_times = {}
        current_child_cpu_lifetimes = {}
        rss_bytes = 0
        thread_count = 0
        process_count = 0
        processes, process_tree_complete = self._current_processes()
        for process in processes:
            try:
                identity = (process.pid, process.create_time())
                cpu = process.cpu_times()
                total_cpu_seconds = cpu.user + cpu.system
                reaped_cpu_seconds = self._reaped_cpu_seconds(cpu)
                current_cpu_times[identity] = total_cpu_seconds
                current_reaped_children_cpu_times[identity] = reaped_cpu_seconds
                if process.pid != self.root.pid:
                    current_child_cpu_lifetimes[identity] = (
                        total_cpu_seconds + reaped_cpu_seconds
                    )
                # A child first discovered after prime() was spawned during
                # this sampling window. Its cumulative process CPU time is
                # therefore the correct delta from zero; psutil.cpu_percent
                # would instead return a synthetic zero on this first call.
                previous_cpu_seconds = self._cpu_times.get(identity)
                if previous_cpu_seconds is None:
                    previous_cpu_seconds = (
                        total_cpu_seconds if recovering_from_incomplete else 0.0
                    )
                cpu_seconds += max(
                    total_cpu_seconds - previous_cpu_seconds,
                    0.0,
                )
                rss_bytes += process.memory_info().rss
                thread_count += process.num_threads()
                process_count += 1
            except (OSError, self.psutil.Error):
                process_tree_complete = False
                continue
        measurement_complete = process_tree_complete
        if not measurement_complete:
            # An enumeration/read failure is not evidence that a cached child
            # departed. Keep its cumulative baselines so a later successful
            # poll does not charge its entire lifetime again from zero.
            for identity, value in self._cpu_times.items():
                current_cpu_times.setdefault(identity, value)
            for identity, value in self._reaped_children_cpu_times.items():
                # Do not consume a parent's reaped-child delta until a
                # complete recovery poll can confirm which cached descendants
                # actually departed and reconcile their observed lifetimes.
                current_reaped_children_cpu_times[identity] = value
            for identity, value in self._known_child_cpu_lifetimes.items():
                current_child_cpu_lifetimes.setdefault(identity, value)
        departed = (
            self._known_child_cpu_lifetimes.keys()
            - current_child_cpu_lifetimes.keys()
        )
        self._unreconciled_departed_child_cpu += sum(
            self._known_child_cpu_lifetimes[identity]
            for identity in departed
        )
        # On platforms that expose cumulative CPU for reaped children, this
        # closes two gaps in live process enumeration: CPU accrued after a
        # descendant's last observation, and helpers created and reaped
        # entirely between polls. Read it from every live process so helpers
        # reaped by long-lived workers are included too. Subtract full
        # lifetimes of departed descendants already observed live so their
        # work is not double-counted when an ancestor later reaps them.
        reaped_delta = sum(
            max(
                total - self._reaped_children_cpu_times.get(identity, 0.0),
                0.0,
            )
            for identity, total in current_reaped_children_cpu_times.items()
        )
        reconciled = min(
            reaped_delta,
            self._unreconciled_departed_child_cpu,
        )
        cpu_seconds += reaped_delta - reconciled
        self._unreconciled_departed_child_cpu -= reconciled
        self._cpu_times = current_cpu_times
        self._reaped_children_cpu_times = current_reaped_children_cpu_times
        self._known_child_cpu_lifetimes = current_child_cpu_lifetimes
        self._last_cpu_sample_at = sampled_at
        self._needs_rebaseline_after_incomplete = not measurement_complete
        process_tree_complete = (
            measurement_complete and not recovering_from_incomplete
        )
        cpu_percent = cpu_seconds / elapsed * 100.0 if elapsed > 0 else 0.0
        metadata = self.metadata()
        return {
            "cpu_percent": round(cpu_percent, 2),
            "rss_bytes": rss_bytes,
            "process_count": process_count,
            "thread_count": thread_count,
            "process_tree_complete": process_tree_complete,
            "cpu_accounting_complete": (
                self._cpu_accounting_complete and process_tree_complete
            ),
            "executable_exists": metadata["executable_exists"],
        }


class SystemSampler:
    def __init__(self, *, psutil_module=psutil):
        if psutil_module is None:
            raise RuntimeError(
                "psutil is required; install the Vireo development dependencies"
            )
        self.psutil = psutil_module

    def metadata(self):
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpu_count": self.psutil.cpu_count(logical=True),
            "physical_cpu_count": self.psutil.cpu_count(logical=False),
            "memory_bytes": self.psutil.virtual_memory().total,
        }

    def prime(self):
        self.psutil.cpu_times_percent(interval=None)

    def sample(self):
        cpu = self.psutil.cpu_times_percent(interval=None)
        memory = self.psutil.virtual_memory()
        swap = self.psutil.swap_memory()
        try:
            load_average = list(os.getloadavg())
        except (AttributeError, OSError):
            load_average = None
        return {
            "cpu_idle_percent": round(cpu.idle, 2),
            "cpu_user_percent": round(cpu.user, 2),
            "cpu_system_percent": round(cpu.system, 2),
            "memory_available_bytes": memory.available,
            "memory_percent": memory.percent,
            "swap_used_bytes": swap.used,
            "load_average": load_average,
        }


def _counter_delta(first, last):
    first_cache = first.get("workload_metrics", {}).get("embedding_cache", {})
    last_cache = last.get("workload_metrics", {}).get("embedding_cache", {})
    return {
        key: last_cache[key] - first_cache.get(key, 0)
        for key in sorted(DIAGNOSTIC_COUNTERS)
        if isinstance(last_cache.get(key), (int, float))
    }


def _resource_delta(first, last):
    first_budget = first.get("resource_budget") or {}
    last_budget = last.get("resource_budget") or {}
    return {
        "wait_count": (last_budget.get("wait_count") or 0) - (first_budget.get("wait_count") or 0),
        "wait_seconds": round(
            (last_budget.get("wait_seconds") or 0.0)
            - (first_budget.get("wait_seconds") or 0.0),
            4,
        ),
    }


def _job_summary(observations):
    # The first observation is the baseline. Any terminal job already there
    # completed before monitoring began and is not part of the workload;
    # this includes ephemeral jobs whose only terminal record remains in the
    # API's `active` list. Everything else — including jobs that start and
    # finish between polling intervals — must be tracked.
    baseline_terminal_ids = set()
    baseline_wait_by_id = {}
    if observations:
        _elapsed0, baseline_sample = observations[0]
        for job in baseline_sample.get("jobs", []):
            job_id = job.get("id")
            if not job_id:
                continue
            if job.get("status") in TERMINAL_STATUSES:
                baseline_terminal_ids.add(job_id)
            else:
                baseline_wait_by_id[job_id] = job.get(
                    "resource_wait_seconds", 0.0,
                )
    tracked_ids = {
        job.get("id")
        for _elapsed, api_sample in observations
        for job in api_sample.get("jobs", [])
        if job.get("id") and job["id"] not in baseline_terminal_ids
    }
    jobs = {}
    for elapsed, api_sample in observations:
        for job in api_sample.get("jobs", []):
            job_id = job.get("id")
            if not job_id or job_id not in tracked_ids:
                continue
            record = jobs.setdefault(job_id, {
                "id": job_id,
                "type": job.get("type"),
                "workspace_id": job.get("workspace_id"),
                "first_observed_seconds": round(elapsed, 3),
                "first_status": job.get("status"),
                "started_at": job.get("started_at"),
                # Jobs present and active at baseline may already have accrued
                # wait outside this benchmark window. Jobs absent from the
                # baseline started during the window, so all wait visible at
                # their first sample belongs to this run.
                "first_resource_wait_seconds": baseline_wait_by_id.get(
                    job_id, 0.0,
                ),
            })
            record.update({
                "last_observed_seconds": round(elapsed, 3),
                "last_status": job.get("status"),
                "finished_at": job.get("finished_at"),
                "duration": job.get("duration"),
                "last_resource_wait_seconds": job.get("resource_wait_seconds", 0.0),
                "resource_wait_count": job.get("resource_wait_count", 0),
            })
            progress = job.get("progress") or {}
            if progress:
                record["last_progress"] = progress
    for record in jobs.values():
        record["observed_resource_wait_seconds"] = round(
            record.pop("last_resource_wait_seconds")
            - record.pop("first_resource_wait_seconds"),
            4,
        )
    return sorted(jobs.values(), key=lambda job: (job["first_observed_seconds"], job["id"]))


def _scenario_summary(jobs):
    terminal = [job for job in jobs if job.get("last_status") in TERMINAL_STATUSES]
    starts = []
    finishes = []
    for job in jobs:
        try:
            starts.append(datetime.fromisoformat(job["started_at"]))
            finishes.append(datetime.fromisoformat(job["finished_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    makespan = None
    if jobs and len(terminal) == len(jobs) and len(starts) == len(jobs) and len(finishes) == len(jobs):
        makespan = round((max(finishes) - min(starts)).total_seconds(), 3)
    return {
        "observed_job_count": len(jobs),
        "terminal_job_count": len(terminal),
        "all_observed_jobs_terminal": bool(jobs) and len(terminal) == len(jobs),
        "workload_makespan_seconds": makespan,
    }


def build_summary(
    baseline,
    samples,
    *,
    process_metadata=None,
    interrupted=False,
):
    successful = [
        sample for sample in samples
        if _is_successful_jobs_sample(sample["api"])
    ]
    api_failure_count = len(samples) - len(successful)
    api_latencies = [sample["api"]["latency_seconds"] for sample in successful]
    complete_process_samples = [
        sample for sample in samples
        if sample["process"].get("process_tree_complete") is not False
    ]
    process_cpu = [
        sample["process"]["cpu_percent"] for sample in complete_process_samples
    ]
    cpu_summary = _number_summary(process_cpu)
    if cpu_summary is not None:
        cpu_summary["accounting_complete"] = bool(
            (process_metadata or {}).get("cpu_accounting_complete")
            and all(
                sample["process"].get("cpu_accounting_complete") is not False
                for sample in samples
            )
        )
    process_rss = [
        sample["process"]["rss_bytes"] for sample in complete_process_samples
    ]
    initial_rss = (process_metadata or {}).get("initial_rss_bytes")
    system_idle = [sample["system"]["cpu_idle_percent"] for sample in samples]
    observations = [(0.0, baseline)] + [
        (sample["elapsed_seconds"], sample["api"])
        for sample in successful
    ]
    final_api = successful[-1]["api"] if successful else baseline
    queued_counts = [
        sum(job.get("status") == "queued" for job in sample["api"].get("jobs", []))
        for sample in successful
    ]
    waiter_counts = [
        (sample["api"].get("resource_budget") or {}).get("waiters", 0)
        for sample in successful
    ]
    cpu_cap_violations = 0
    last_cpu_capacity = (
        (baseline.get("resource_budget") or {}).get("cpu", {}).get("capacity")
    )
    for sample in samples:
        if sample["process"].get("process_tree_complete") is False:
            continue
        capacity = (
            (sample["api"].get("resource_budget") or {})
            .get("cpu", {})
            .get("capacity")
        )
        if capacity:
            last_cpu_capacity = capacity
        if (
            last_cpu_capacity
            and sample["process"]["cpu_percent"] > last_cpu_capacity * 110
        ):
            cpu_cap_violations += 1
    latency_summary = _number_summary(api_latencies)
    idle_summary = _number_summary(system_idle)
    embedding_delta = _counter_delta(baseline, final_api)
    jobs = _job_summary(observations)
    has_embedding_diagnostics = bool(
        baseline.get("workload_metrics", {}).get("embedding_cache")
        and final_api.get("workload_metrics", {}).get("embedding_cache")
    )
    # `executable_exists` is tri-state: True (verified present), False
    # (verified missing at some point), or None (unknown — e.g. psutil
    # `AccessDenied` on `Process.exe()`).  Treating unknown as True would
    # let the trustworthiness gate falsely pass in restricted environments,
    # so require an explicit True to pass, an explicit False to fail, and
    # otherwise surface the gate as None.
    executable_states = []
    if process_metadata and "initial_executable_exists" in process_metadata:
        executable_states.append(process_metadata["initial_executable_exists"])
    executable_states.extend(
        sample["process"].get("executable_exists") for sample in samples
    )
    if process_metadata and "executable_exists" in process_metadata:
        executable_states.append(process_metadata["executable_exists"])
    if process_metadata and process_metadata.get("identity_verified") is False:
        # A late exit/replacement means process presence was not verified for
        # the complete run even if the executable path still exists on disk.
        executable_states.append(False)
    if not executable_states or any(state is False for state in executable_states):
        executable_present = False
    elif all(state is True for state in executable_states):
        executable_present = True
    else:
        executable_present = None
    return {
        "sample_count": len(samples),
        "incomplete_process_sample_count": (
            len(samples) - len(complete_process_samples)
        ),
        "api_failure_count": api_failure_count,
        "vireo_process_tree_cpu_percent": cpu_summary,
        "vireo_process_tree_rss_bytes": {
            **(_number_summary(process_rss) or {}),
            "growth": (
                process_rss[-1]
                - (initial_rss if isinstance(initial_rss, (int, float)) else process_rss[0])
                if process_rss else None
            ),
        },
        "system_cpu_idle_percent": idle_summary,
        "jobs_api_latency_seconds": latency_summary,
        "max_queued_jobs": max(queued_counts, default=0),
        "max_resource_waiters": max(waiter_counts, default=0),
        "resource_wait_delta": _resource_delta(baseline, final_api),
        "embedding_cache_delta": embedding_delta,
        "cpu_capacity_burst_samples": cpu_cap_violations,
        "targets": {
            # Failed requests (timeouts, non-200) are excluded from the p95
            # calculation, so a run with one fast success and many long
            # timeouts would otherwise report the gate as met.  Require zero
            # API failures for the responsiveness gate to pass.
            "jobs_api_p95_below_500ms": (
                latency_summary is not None
                and latency_summary["p95"] < 0.5
                and api_failure_count == 0
            ) if not interrupted else None,
            "system_idle_cpu_p05_at_least_10_percent": (
                idle_summary is not None and idle_summary["p05"] >= 10.0
            ),
            # `embedding_delta` is computed from the last *successful* API
            # sample.  If polling failed at some point, any single-flight
            # violation that occurred after that last success would be
            # invisible, so we surface the invariant as unknown (None)
            # rather than falsely reporting it verified.
            "no_embedding_single_flight_violations": (
                embedding_delta.get("single_flight_violations", 0) == 0
                if (
                    has_embedding_diagnostics
                    and api_failure_count == 0
                    and not interrupted
                )
                else None
            ),
            "vireo_executable_present_throughout": executable_present,
        },
        "scenario": _scenario_summary(jobs),
        "jobs": jobs,
    }


def collect_workload(
    *,
    duration,
    interval,
    api_client,
    process_sampler,
    system_sampler,
    monotonic=time.monotonic,
    sleep=time.sleep,
    on_ready=None,
):
    api_client.authenticate()
    baseline = api_client.sample()
    if not _is_successful_jobs_sample(baseline):
        raise RuntimeError(
            f"GET /api/jobs failed before sampling: "
            f"{baseline.get('error') or baseline.get('status')}"
        )
    process_sampler.prime()
    system_sampler.prime()
    if on_ready is not None:
        on_ready()
    started_at = _utc_now()
    started = monotonic()
    deadline = started + duration
    next_sample = min(started + interval, deadline)
    samples = []
    interrupted = False
    try:
        while True:
            sleep_for = next_sample - monotonic()
            if sleep_for > 0:
                sleep(sleep_for)
            elapsed = monotonic() - started
            process_sample = process_sampler.sample()
            system_sample = system_sampler.sample()
            try:
                api_sample = api_client.sample()
            except KeyboardInterrupt:
                interrupted = True
                api_sample = {
                    "status": None,
                    "latency_seconds": None,
                    "error": "interrupted during GET /api/jobs",
                }
                samples.append({
                    "captured_at": _utc_now(),
                    "elapsed_seconds": round(elapsed, 3),
                    "process": process_sample,
                    "system": system_sample,
                    "api": api_sample,
                })
                break
            # The API request can outlive the selected process. Recheck after
            # it completes so a replacement server on the same URL cannot
            # contribute the terminal Jobs payload to the original PID's
            # process samples.
            process_sampler.verify_identity()
            samples.append({
                "captured_at": _utc_now(),
                "elapsed_seconds": round(elapsed, 3),
                "process": process_sample,
                "system": system_sample,
                "api": api_sample,
            })
            if monotonic() >= deadline:
                break
            next_sample += interval
            # If a poll (or another sampler) ran longer than `interval`,
            # `next_sample` is now in the past.  Firing back-to-back
            # catch-up polls would both add load to an already unresponsive
            # server and record CPU samples over abnormally short windows,
            # contaminating the resource and latency distributions this
            # tool is meant to compare — skip the missed slots so the
            # sampling schedule stays aligned to the original cadence.
            now = monotonic()
            if next_sample <= now:
                # Advance strictly beyond ``now``.  ``ceil`` alone leaves
                # the deadline equal to ``now`` when a poll finishes exactly
                # on a later cadence boundary, triggering an immediate
                # zero-sleep sample.
                slots_missed = math.floor((now - next_sample) / interval) + 1
                next_sample += slots_missed * interval
            next_sample = min(next_sample, deadline)
    except KeyboardInterrupt:
        interrupted = True
    process_exit_error = None
    try:
        process_metadata = process_sampler.metadata()
    except RuntimeError as exc:
        # Samples whose post-request identity checks succeeded remain valid.
        # Preserve a long-running report if the process exits only between the
        # last accepted poll and final metadata collection.
        process_exit_error = str(exc)
        metadata_unverified = getattr(
            process_sampler,
            "metadata_unverified",
            lambda: {"identity_verified": False},
        )
        process_metadata = metadata_unverified()
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "interrupted": interrupted,
        "process_exit_error": process_exit_error,
        "requested_duration_seconds": duration,
        "interval_seconds": interval,
        "process": process_metadata,
        "host": system_sampler.metadata(),
        "baseline": baseline,
        "summary": build_summary(
            baseline,
            samples,
            process_metadata=process_metadata,
            interrupted=interrupted,
        ),
        "samples": samples,
    }


def _is_vireo_process(process, *, psutil_module):
    try:
        info = process.as_dict(attrs=["name", "cmdline"])
    except (OSError, psutil_module.Error):
        return False
    name = info.get("name") or ""
    cmdline = " ".join(info.get("cmdline") or [])
    return "vireo-server" in f"{name} {cmdline}".lower()


def _resolve_hostname(hostname, *, resolver=socket.getaddrinfo):
    try:
        infos = resolver(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return frozenset()
    return frozenset(info[4][0] for info in infos if info and info[4])


def _listener_reachable_via(
    host,
    listener_ip,
    *,
    resolver=socket.getaddrinfo,
    local_addresses=None,
):
    """Return True if a connection to ``host`` would reach ``listener_ip``.

    A listener bound to ``0.0.0.0``/``::`` accepts any local address, so
    loopback URLs are considered reachable; a listener bound to a specific
    address requires the URL to resolve to that same address (loopback
    hostnames also match loopback listeners).
    """
    resolved = _resolve_hostname(host, resolver=resolver)
    if not resolved:
        return False
    all_bind = listener_ip in {"0.0.0.0", "::"}
    if all_bind and local_addresses is None:
        local_addresses = _resolve_hostname(
            socket.gethostname(), resolver=resolver,
        )
    try:
        listener_addr = ipaddress.ip_address(listener_ip)
    except ValueError:
        listener_addr = None
    for addr_str in resolved:
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        if addr.is_loopback:
            if (
                listener_addr is not None
                and addr.version == listener_addr.version
                and (all_bind or addr == listener_addr)
            ):
                return True
            continue
        if (
            all_bind
            and listener_addr is not None
            and addr.version == listener_addr.version
            and addr_str in (local_addresses or ())
        ):
            return True
        if not all_bind and addr_str == listener_ip:
            return True
    return False


def _process_owns_url(
    process,
    port,
    host,
    *,
    psutil_module,
    resolver=socket.getaddrinfo,
    local_addresses=None,
):
    candidates = [process]
    with contextlib.suppress(OSError, psutil_module.Error):
        candidates.extend(process.children(recursive=True))
    for candidate in candidates:
        try:
            connections = candidate.net_connections(kind="tcp")
        except (OSError, psutil_module.Error):
            continue
        for connection in connections:
            if connection.status != psutil_module.CONN_LISTEN:
                continue
            if connection.laddr.port != port:
                continue
            if _listener_reachable_via(
                host,
                connection.laddr.ip,
                resolver=resolver,
                local_addresses=local_addresses,
            ):
                return candidate
    return None


def _parse_server_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(
            "--url must include http(s), a host, and a valid port"
        ) from exc
    if parsed.scheme not in {"http", "https"} or not hostname or not port:
        raise RuntimeError(
            "--url must include http(s), a host, and a valid port"
        )
    return parsed, hostname, port


def discover_server(
    *,
    requested_pid=None,
    requested_url=None,
    psutil_module=psutil,
    resolver=socket.getaddrinfo,
    local_addresses=None,
):
    if psutil_module is None:
        raise RuntimeError(
            "psutil is required; install the Vireo development dependencies"
        )
    if requested_pid is not None and requested_url:
        _parsed, requested_hostname, requested_port = _parse_server_url(
            requested_url
        )
        try:
            process = psutil_module.Process(requested_pid)
        except psutil_module.Error as exc:
            raise RuntimeError(f"Vireo PID {requested_pid} is not running") from exc
        # Verify the PID (or a descendant) actually owns the URL's listening
        # port *and* is reachable via the URL's host; otherwise samples could
        # be mixed from two processes (or two hosts on the same port) and
        # silently corrupt the workload comparison.
        owner = _process_owns_url(
            process,
            requested_port,
            requested_hostname,
            psutil_module=psutil_module,
            resolver=resolver,
            local_addresses=local_addresses,
        )
        if owner is None:
            raise RuntimeError(
                f"PID {requested_pid} does not own {requested_url}: no process "
                f"in its tree is listening on port {requested_port} at an address "
                f"reachable via {requested_hostname!r}"
            )
        if not _is_vireo_process(owner, psutil_module=psutil_module):
            raise RuntimeError(
                f"PID {requested_pid} owns port {requested_port} but the listening "
                f"process is not a vireo-server"
            )
        # Attribute CPU/RSS and executable-presence samples to the process
        # that owns the listener.  ``requested_pid`` may be a shell or
        # supervisor whose child is the actual vireo-server.
        return {"pid": owner.pid, "url": requested_url.rstrip("/")}
    requested_port = None
    requested_hostname = None
    if requested_url:
        _parsed, requested_hostname, requested_port = _parse_server_url(
            requested_url
        )
    candidates = []
    for process in psutil_module.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            name = process.info.get("name") or ""
            if "vireo-server" not in f"{name} {command}".lower():
                continue
            if requested_pid is not None and process.pid != requested_pid:
                ancestors = {parent.pid for parent in process.parents()}
                if requested_pid not in ancestors:
                    continue
            for connection in process.net_connections(kind="tcp"):
                if connection.status != psutil_module.CONN_LISTEN:
                    continue
                port = connection.laddr.port
                if requested_port is not None and requested_port != port:
                    continue
                # When --url was passed, the URL's hostname must actually
                # resolve to the listener's bound address; otherwise a
                # local vireo sharing the port with a *remote* vireo would
                # be selected while the URL fetches from the remote one,
                # pairing local CPU/RSS with remote Jobs data.
                if requested_hostname is not None and not _listener_reachable_via(
                    requested_hostname,
                    connection.laddr.ip,
                    resolver=resolver,
                    local_addresses=local_addresses,
                ):
                    continue
                host = connection.laddr.ip
                if host == "0.0.0.0":
                    host = "127.0.0.1"
                elif host == "::":
                    host = "::1"
                url_host = f"[{host}]" if ":" in host else host
                candidates.append({
                    "pid": process.pid,
                    "url": f"http://{url_host}:{port}",
                    "started": process.info.get("create_time") or 0,
                })
        except (OSError, psutil_module.Error):
            continue
    if not candidates:
        hint = " Pass --pid and --url explicitly." if requested_pid or requested_url else ""
        raise RuntimeError(f"no listening local vireo-server found.{hint}")
    candidates.sort(key=lambda item: item["started"], reverse=True)
    unique = {(item["pid"], item["url"]): item for item in candidates}
    candidates = list(unique.values())
    if len(candidates) > 1:
        choices = ", ".join(
            f"PID {item['pid']} at {item['url']}" for item in candidates
        )
        raise RuntimeError(
            f"multiple Vireo servers are listening ({choices}); pass --pid or --url"
        )
    discovered = candidates[0]
    return {
        "pid": discovered["pid"],
        "url": requested_url.rstrip("/") if requested_url else discovered["url"],
    }


def _default_output_path():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(".context") / "benchmarks" / f"vireo-workload-{timestamp}.json"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=300.0, help="sampling duration in seconds (default: 300)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between samples (default: 2)")
    parser.add_argument("--pid", type=int, help="vireo-server PID; discovered automatically when omitted")
    parser.add_argument("--url", help="Vireo base URL; discovered automatically when omitted")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds (default: 5)")
    parser.add_argument("--output", type=Path, help="JSON report path (default: .context/benchmarks/...)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be greater than zero")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        server = discover_server(requested_pid=args.pid, requested_url=args.url)
        api_client = VireoApiClient(server["url"], timeout=args.timeout)
        process_sampler = ProcessTreeSampler(server["pid"])
        system_sampler = SystemSampler()
        report = collect_workload(
            duration=args.duration,
            interval=args.interval,
            api_client=api_client,
            process_sampler=process_sampler,
            system_sampler=system_sampler,
            on_ready=lambda: print(
                f"Monitoring Vireo PID {server['pid']} at {server['url']} for "
                f"{args.duration:g}s; press Ctrl-C to save early.",
                file=sys.stderr,
            ),
        )
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    report["vireo_url"] = server["url"]
    output = args.output or _default_output_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    api = summary["jobs_api_latency_seconds"] or {}
    idle = summary["system_cpu_idle_percent"] or {}
    cpu = summary["vireo_process_tree_cpu_percent"] or {}
    print(f"Report: {output}")
    print(
        "Summary: "
        f"Vireo CPU p95={cpu.get('p95', 'n/a')}%, "
        f"system idle p05={idle.get('p05', 'n/a')}%, "
        f"Jobs API p95={api.get('p95', 'n/a')}s, "
        f"API failures={summary['api_failure_count']}, "
        f"CPU accounting="
        f"{'complete' if cpu.get('accounting_complete') else 'incomplete'}"
    )


if __name__ == "__main__":
    main()
