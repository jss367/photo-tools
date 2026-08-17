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
import re
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


# URLs — cover before path patterns so "http://…" isn't split by the path REs.
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s]+")
# Absolute unix paths ("/foo/bar") anchored to a start-of-string or whitespace so
# fractions such as "1/3" in job phase strings are not misread as paths.  The
# body accepts any non-delimiter character so unicode segments (``/家族/写真``)
# and library names with spaces (``/My Photos``) redact instead of leaking.
_UNIX_PATH_RE = re.compile(r"(?:(?<=\s)|^)/[^:\r\n\"']+")
# Windows drive-letter paths ("C:\Users\..." or "C:/Users/..."), also accepting
# any non-delimiter character in the body so unicode and spaced segments match.
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^:\r\n\"']+")
# Filenames with a common-shaped extension (`name.ext`, extension starts with a
# letter and is 2-8 chars long) so numeric fragments like "1.2" are ignored.
# `\w` is unicode-aware in Python 3, so `写真.raw` is redacted the same way as
# `capture.nef`.
_FILENAME_RE = re.compile(r"\b[\w\-]+\.[A-Za-z][A-Za-z0-9]{1,7}\b")


def _sanitize_phase(phase):
    """Strip filesystem paths and filenames from a job phase string.

    `vireo/app.py` builds phase text such as ``"Scanning root 1 of 2:
    /private/photos"`` and ``"Downloading 1/3: capture.nef..."``.  Reports
    promise to exclude filenames and full paths, so before including a phase
    in the sanitized report we redact any URL, path, or filename tokens it
    contains — including unicode segments and paths that carry spaces.
    Returns ``None`` when the sanitized value would be empty.
    """
    if not isinstance(phase, str):
        return None
    sanitized = _URL_RE.sub("[url]", phase)
    sanitized = _WIN_PATH_RE.sub("[path]", sanitized)
    sanitized = _UNIX_PATH_RE.sub("[path]", sanitized)
    sanitized = _FILENAME_RE.sub("[file]", sanitized)
    sanitized = sanitized.strip()
    return sanitized or None


def _compact_progress(progress):
    if not isinstance(progress, dict):
        return {}
    compact = {
        key: progress[key]
        for key in ("current", "total", "stage_id", "rate")
        if progress.get(key) is not None
    }
    phase = _sanitize_phase(progress.get("phase"))
    if phase is not None:
        compact["phase"] = phase
    return compact


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
        running_steps.append({
            "id": step.get("id"),
            "label": step.get("label"),
            "status": step.get("status"),
            "progress": _compact_progress(step.get("progress")),
        })
    if running_steps:
        compact["running_steps"] = running_steps
    return compact


def compact_jobs_payload(payload):
    """Remove paths, workspace names, configs, results, and filenames."""
    active = [
        _compact_job(job, "active")
        for job in payload.get("active", [])
        if job.get("status") not in TERMINAL_STATUSES
    ]
    history = [
        _compact_job(job, "history")
        for job in payload.get("history", [])
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
            result.update(compact_jobs_payload(payload))
        return result


class ProcessTreeSampler:
    def __init__(self, pid, *, psutil_module=psutil):
        if psutil_module is None:
            raise RuntimeError(
                "psutil is required; install the Vireo development dependencies"
            )
        self.psutil = psutil_module
        self.root = psutil_module.Process(pid)
        self.processes = {}
        try:
            self.executable = self.root.exe()
        except (OSError, self.psutil.Error):
            self.executable = None
        try:
            command = self.root.cmdline()
        except (OSError, self.psutil.Error):
            command = []
        try:
            started_at = datetime.fromtimestamp(
                self.root.create_time(), UTC
            ).isoformat(timespec="seconds")
        except (OSError, self.psutil.Error):
            started_at = None
        self._metadata = {
            "pid": self.root.pid,
            "process_started_at": started_at,
            "executable_name": (
                os.path.basename(self.executable) if self.executable else None
            ),
            "command_name": os.path.basename(command[0]) if command else None,
        }

    def metadata(self):
        return {
            **self._metadata,
            "executable_exists": (
                os.path.exists(self.executable) if self.executable else None
            ),
        }

    def _current_processes(self):
        discovered = [self.root]
        with contextlib.suppress(self.psutil.Error):
            discovered.extend(self.root.children(recursive=True))
        live = {}
        for process in discovered:
            live[process.pid] = self.processes.get(process.pid, process)
        self.processes = live
        return list(live.values())

    def prime(self):
        for process in self._current_processes():
            try:
                process.cpu_percent(interval=None)
            except self.psutil.Error:
                continue

    def sample(self):
        cpu_percent = 0.0
        rss_bytes = 0
        thread_count = 0
        process_count = 0
        for process in self._current_processes():
            try:
                cpu_percent += process.cpu_percent(interval=None)
                rss_bytes += process.memory_info().rss
                thread_count += process.num_threads()
                process_count += 1
            except self.psutil.Error:
                continue
        metadata = self.metadata()
        return {
            "cpu_percent": round(cpu_percent, 2),
            "rss_bytes": rss_bytes,
            "process_count": process_count,
            "thread_count": thread_count,
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
    tracked_ids = {
        job.get("id")
        for _elapsed, api_sample in observations
        for job in api_sample.get("jobs", [])
        if job.get("source") == "active" and job.get("id")
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
                "first_resource_wait_seconds": job.get("resource_wait_seconds", 0.0),
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


def build_summary(baseline, samples):
    successful = [sample for sample in samples if sample["api"].get("status") == 200]
    api_failure_count = len(samples) - len(successful)
    api_latencies = [sample["api"]["latency_seconds"] for sample in successful]
    process_cpu = [sample["process"]["cpu_percent"] for sample in samples]
    process_rss = [sample["process"]["rss_bytes"] for sample in samples]
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
    for sample in successful:
        capacity = (
            (sample["api"].get("resource_budget") or {})
            .get("cpu", {})
            .get("capacity")
        )
        if capacity and sample["process"]["cpu_percent"] > capacity * 110:
            cpu_cap_violations += 1
    latency_summary = _number_summary(api_latencies)
    idle_summary = _number_summary(system_idle)
    embedding_delta = _counter_delta(baseline, final_api)
    jobs = _job_summary(observations)
    has_embedding_diagnostics = bool(
        baseline.get("workload_metrics", {}).get("embedding_cache")
        and final_api.get("workload_metrics", {}).get("embedding_cache")
    )
    executable_present = all(
        sample["process"].get("executable_exists") is not False
        for sample in samples
    )
    return {
        "sample_count": len(samples),
        "api_failure_count": api_failure_count,
        "vireo_process_tree_cpu_percent": _number_summary(process_cpu),
        "vireo_process_tree_rss_bytes": {
            **(_number_summary(process_rss) or {}),
            "growth": process_rss[-1] - process_rss[0] if process_rss else None,
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
            ),
            "system_idle_cpu_p05_at_least_10_percent": (
                idle_summary is not None and idle_summary["p05"] >= 10.0
            ),
            "no_embedding_single_flight_violations": (
                embedding_delta.get("single_flight_violations", 0) == 0
                if has_embedding_diagnostics
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
):
    api_client.authenticate()
    baseline = api_client.sample()
    if baseline.get("status") != 200:
        raise RuntimeError(
            f"GET /api/jobs failed before sampling: "
            f"{baseline.get('error') or baseline.get('status')}"
        )
    process_sampler.prime()
    system_sampler.prime()
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
            samples.append({
                "captured_at": _utc_now(),
                "elapsed_seconds": round(elapsed, 3),
                "process": process_sampler.sample(),
                "system": system_sampler.sample(),
                "api": api_client.sample(),
            })
            if monotonic() >= deadline:
                break
            next_sample = min(next_sample + interval, deadline)
    except KeyboardInterrupt:
        interrupted = True
    if not samples:
        samples.append({
            "captured_at": _utc_now(),
            "elapsed_seconds": round(monotonic() - started, 3),
            "process": process_sampler.sample(),
            "system": system_sampler.sample(),
            "api": api_client.sample(),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "interrupted": interrupted,
        "requested_duration_seconds": duration,
        "interval_seconds": interval,
        "process": process_sampler.metadata(),
        "host": system_sampler.metadata(),
        "baseline": baseline,
        "summary": build_summary(baseline, samples),
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


def _listener_reachable_via(host, listener_ip, *, resolver=socket.getaddrinfo):
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
            if all_bind or (listener_addr is not None and listener_addr.is_loopback):
                return True
            continue
        if not all_bind and addr_str == listener_ip:
            return True
    return False


def _process_owns_url(process, port, host, *, psutil_module, resolver=socket.getaddrinfo):
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
            if _listener_reachable_via(host, connection.laddr.ip, resolver=resolver):
                return candidate
    return None


def discover_server(
    *,
    requested_pid=None,
    requested_url=None,
    psutil_module=psutil,
    resolver=socket.getaddrinfo,
):
    if psutil_module is None:
        raise RuntimeError(
            "psutil is required; install the Vireo development dependencies"
        )
    if requested_pid is not None and requested_url:
        parsed = urllib.parse.urlparse(requested_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise RuntimeError("--url must include http(s), a host, and a port")
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
            parsed.port,
            parsed.hostname,
            psutil_module=psutil_module,
            resolver=resolver,
        )
        if owner is None:
            raise RuntimeError(
                f"PID {requested_pid} does not own {requested_url}: no process "
                f"in its tree is listening on port {parsed.port} at an address "
                f"reachable via {parsed.hostname!r}"
            )
        if not _is_vireo_process(owner, psutil_module=psutil_module):
            raise RuntimeError(
                f"PID {requested_pid} owns port {parsed.port} but the listening "
                f"process is not a vireo-server"
            )
        return {"pid": requested_pid, "url": requested_url.rstrip("/")}
    requested_port = None
    if requested_url:
        parsed = urllib.parse.urlparse(requested_url)
        requested_port = parsed.port
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
                host = connection.laddr.ip
                if host in {"0.0.0.0", "::", "::1"}:
                    host = "127.0.0.1"
                candidates.append({
                    "pid": process.pid,
                    "url": f"http://{host}:{port}",
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
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        server = discover_server(requested_pid=args.pid, requested_url=args.url)
        api_client = VireoApiClient(server["url"], timeout=args.timeout)
        process_sampler = ProcessTreeSampler(server["pid"])
        system_sampler = SystemSampler()
        print(
            f"Monitoring Vireo PID {server['pid']} at {server['url']} for "
            f"{args.duration:g}s; press Ctrl-C to save early.",
            file=sys.stderr,
        )
        report = collect_workload(
            duration=args.duration,
            interval=args.interval,
            api_client=api_client,
            process_sampler=process_sampler,
            system_sampler=system_sampler,
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
        f"API failures={summary['api_failure_count']}"
    )


if __name__ == "__main__":
    main()
