import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.monitor_vireo_workload import (
    VireoApiClient,
    collect_workload,
    compact_jobs_payload,
    discover_server,
)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _SequenceApi:
    def __init__(self, samples):
        self.samples = iter(samples)
        self.authenticated = False

    def authenticate(self):
        self.authenticated = True

    def sample(self):
        return next(self.samples)


class _SequenceSampler:
    def __init__(self, samples, metadata):
        self.samples = iter(samples)
        self._metadata = metadata
        self.primed = False

    def prime(self):
        self.primed = True

    def sample(self):
        return next(self.samples)

    def metadata(self):
        return self._metadata


def _api_sample(
    *, latency, wait_count, wait_seconds, producer_starts, waiter_joins,
    status="running", duration=None, progress=0,
):
    source = "history" if status == "completed" else "active"
    return {
        "status": 200,
        "latency_seconds": latency,
        "resource_budget": {
            "cpu": {"capacity": 12, "allocated": 8, "available": 4},
            "wait_count": wait_count,
            "wait_seconds": wait_seconds,
            "waiters": 1 if status == "running" else 0,
        },
        "workload_metrics": {
            "embedding_cache": {
                "cache_hits": 4,
                "cache_misses": 2,
                "producer_starts": producer_starts,
                "producer_publications": producer_starts,
                "producer_failures": 0,
                "waiter_joins": waiter_joins,
                "single_flight_violations": 0,
            }
        },
        "jobs": [{
            "id": "pipeline-1",
            "type": "pipeline",
            "status": status,
            "source": source,
            "workspace_id": 7,
            "started_at": "2026-08-17T12:00:00+00:00",
            "finished_at": (
                "2026-08-17T12:00:42+00:00" if status == "completed" else None
            ),
            "duration": duration,
            "resource_wait_seconds": wait_seconds,
            "resource_wait_count": wait_count,
            "progress": {"current": progress, "total": 10},
        }],
    }


def test_collect_workload_builds_deltas_targets_and_job_summary():
    clock = _FakeClock()
    api = _SequenceApi([
        _api_sample(
            latency=0.001, wait_count=10, wait_seconds=20.0,
            producer_starts=5, waiter_joins=2, progress=0,
        ),
        _api_sample(
            latency=0.010, wait_count=11, wait_seconds=21.0,
            producer_starts=6, waiter_joins=3, progress=5,
        ),
        _api_sample(
            latency=0.020, wait_count=12, wait_seconds=22.5,
            producer_starts=6, waiter_joins=3, status="completed",
            duration=42.0, progress=10,
        ),
    ])
    process = _SequenceSampler(
        [
            {"cpu_percent": 800.0, "rss_bytes": 100, "executable_exists": True},
            {"cpu_percent": 900.0, "rss_bytes": 120, "executable_exists": True},
        ],
        {"pid": 123, "executable_exists": True},
    )
    system = _SequenceSampler(
        [
            {"cpu_idle_percent": 30.0},
            {"cpu_idle_percent": 20.0},
        ],
        {"logical_cpu_count": 16},
    )

    report = collect_workload(
        duration=2.0,
        interval=1.0,
        api_client=api,
        process_sampler=process,
        system_sampler=system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert api.authenticated
    assert process.primed and system.primed
    assert [sample["elapsed_seconds"] for sample in report["samples"]] == [1.0, 2.0]
    summary = report["summary"]
    assert summary["jobs_api_latency_seconds"]["p95"] == 0.02
    assert summary["system_cpu_idle_percent"]["p05"] == 20.0
    assert summary["vireo_process_tree_rss_bytes"]["growth"] == 20
    assert summary["resource_wait_delta"] == {
        "wait_count": 2,
        "wait_seconds": 2.5,
    }
    assert summary["embedding_cache_delta"]["producer_starts"] == 1
    assert summary["embedding_cache_delta"]["waiter_joins"] == 1
    assert summary["targets"] == {
        "jobs_api_p95_below_500ms": True,
        "system_idle_cpu_p05_at_least_10_percent": True,
        "no_embedding_single_flight_violations": True,
        "vireo_executable_present_throughout": True,
    }
    assert summary["scenario"] == {
        "observed_job_count": 1,
        "terminal_job_count": 1,
        "all_observed_jobs_terminal": True,
        "workload_makespan_seconds": 42.0,
    }
    assert summary["jobs"][0]["last_status"] == "completed"
    assert summary["jobs"][0]["duration"] == 42.0
    assert summary["jobs"][0]["observed_resource_wait_seconds"] == 2.5


def test_compact_jobs_payload_removes_paths_configs_results_and_filenames():
    compact = compact_jobs_payload({
        "active": [{
            "id": "pipeline-1",
            "type": "pipeline",
            "status": "running",
            "config": {"source": "/private/photos"},
            "result": {"path": "/private/result"},
            "progress": {
                "current": 2,
                "total": 10,
                "phase": "Detecting",
                "current_file": "private-name.nef",
            },
            "steps": [{
                "id": "detect",
                "label": "Detect",
                "status": "running",
                "current_file": "private-name.nef",
                "progress": {"current": 2, "total": 10},
            }],
        }],
        "history": [],
        "workspace_names": {"7": "Private workspace"},
        "resource_budget": {"waiters": 0},
    })

    encoded = json.dumps(compact)
    assert "/private" not in encoded
    assert "private-name" not in encoded
    assert "Private workspace" not in encoded
    assert compact["jobs"][0]["progress"] == {
        "current": 2,
        "total": 10,
        "phase": "Detecting",
    }


def test_api_client_establishes_browser_cookie_before_reading_jobs():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Set-Cookie", "vireo_session=test; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(b"<html>Vireo</html>")
                return
            if self.path == "/api/jobs" and "vireo_session=test" in self.headers.get("Cookie", ""):
                body = json.dumps({
                    "active": [],
                    "history": [],
                    "resource_budget": {"waiters": 0},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(401)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = VireoApiClient(f"http://127.0.0.1:{server.server_port}")
        client.authenticate()
        sample = client.sample()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert sample["status"] == 200
    assert sample["resource_budget"] == {"waiters": 0}


def test_explicit_server_identity_does_not_require_socket_discovery():
    server = discover_server(
        requested_pid=os.getpid(),
        requested_url="http://127.0.0.1:54321",
    )

    assert server == {
        "pid": os.getpid(),
        "url": "http://127.0.0.1:54321",
    }
