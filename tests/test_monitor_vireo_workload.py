import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

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


def test_slow_poll_skips_missed_slots_instead_of_firing_back_to_back():
    """A single poll that takes longer than `--interval` (a transient
    slow request, for example) previously left `next_sample` in the past
    and caused back-to-back catch-up polls that contaminate CPU/latency
    distributions.  Missed slots must be skipped instead so the sampling
    cadence stays honest.
    """
    clock = _FakeClock()

    class _SlowApi:
        def __init__(self):
            self.calls = 0

        def authenticate(self):
            pass

        def sample(self):
            self.calls += 1
            # Loop iteration #1 (call #2 overall) takes 5s — more than
            # the 2s interval.
            if self.calls == 2:
                clock.sleep(5.0)
            return _api_sample(
                latency=0.001, wait_count=0, wait_seconds=0.0,
                producer_starts=0, waiter_joins=0,
            )

    api = _SlowApi()
    n = 8
    process = _SequenceSampler(
        [{"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": True}
         for _ in range(n)],
        {"pid": 123, "executable_exists": True},
    )
    system = _SequenceSampler(
        [{"cpu_idle_percent": 50.0} for _ in range(n)],
        {"logical_cpu_count": 16},
    )

    report = collect_workload(
        duration=10.0,
        interval=2.0,
        api_client=api,
        process_sampler=process,
        system_sampler=system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    elapsed = [sample["elapsed_seconds"] for sample in report["samples"]]
    # No two adjacent samples may collapse into a back-to-back burst.
    for prev, cur in zip(elapsed, elapsed[1:], strict=False):
        assert cur - prev >= 2.0, f"back-to-back polls at {prev}s → {cur}s"
    # We expected samples at approximately t=2 (fast), then t=8 and t=10
    # after skipping the missed slots at 4 and 6 — never at t=7 (immediate
    # after the slow poll ended).
    assert elapsed == [2.0, 8.0, 10.0]


def test_executable_target_is_none_when_presence_is_unknown():
    """When psutil can't read `Process.exe()` (e.g. `AccessDenied`),
    every sample reports `executable_exists=None`.  The old predicate
    treated None as success and made the trustworthiness gate falsely
    pass; the gate should now surface as None ("unknown") in that case.
    """
    clock = _FakeClock()
    api = _SequenceApi([
        _api_sample(
            latency=0.001, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
        _api_sample(
            latency=0.001, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
    ])
    process = _SequenceSampler(
        [{"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": None}],
        {"pid": 123, "executable_exists": None},
    )
    system = _SequenceSampler(
        [{"cpu_idle_percent": 50.0}],
        {"logical_cpu_count": 16},
    )

    report = collect_workload(
        duration=1.0,
        interval=1.0,
        api_client=api,
        process_sampler=process,
        system_sampler=system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["summary"]["targets"]["vireo_executable_present_throughout"] is None


def test_executable_target_is_false_when_presence_verified_missing():
    clock = _FakeClock()
    api = _SequenceApi([
        _api_sample(
            latency=0.001, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
        _api_sample(
            latency=0.001, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
    ])
    process = _SequenceSampler(
        [{"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": False}],
        {"pid": 123, "executable_exists": False},
    )
    system = _SequenceSampler(
        [{"cpu_idle_percent": 50.0}],
        {"logical_cpu_count": 16},
    )

    report = collect_workload(
        duration=1.0,
        interval=1.0,
        api_client=api,
        process_sampler=process,
        system_sampler=system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["summary"]["targets"]["vireo_executable_present_throughout"] is False


def test_job_summary_tracks_history_only_jobs_and_ignores_baseline_history():
    """A short job (e.g. a cached embedding request) may start and finish
    between two polling intervals, appearing only in `history`.  Such jobs
    must be tracked so the scenario reflects the full workload, but jobs
    already terminal at baseline predate the monitoring window and must
    not appear in the per-job outcomes.
    """
    clock = _FakeClock()
    pre_existing = {
        "id": "pre-existing",
        "type": "pipeline",
        "status": "completed",
        "source": "history",
        "workspace_id": 1,
        "started_at": "2026-08-17T11:00:00+00:00",
        "finished_at": "2026-08-17T11:00:10+00:00",
        "duration": 10.0,
    }
    short_lived = {
        "id": "short-lived",
        "type": "embedding_prep",
        "status": "completed",
        "source": "history",
        "workspace_id": 1,
        "started_at": "2026-08-17T12:00:00.500+00:00",
        "finished_at": "2026-08-17T12:00:00.800+00:00",
        "duration": 0.3,
    }
    empty_metrics = {"embedding_cache": {}}
    baseline_payload = {
        "status": 200,
        "latency_seconds": 0.001,
        "resource_budget": {"waiters": 0},
        "workload_metrics": empty_metrics,
        "jobs": [pre_existing],
    }
    poll_payload = {
        "status": 200,
        "latency_seconds": 0.002,
        "resource_budget": {"waiters": 0},
        "workload_metrics": empty_metrics,
        "jobs": [pre_existing, short_lived],
    }
    api = _SequenceApi([baseline_payload, poll_payload])
    process = _SequenceSampler(
        [{"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": True}],
        {"pid": 123, "executable_exists": True},
    )
    system = _SequenceSampler(
        [{"cpu_idle_percent": 50.0}],
        {"logical_cpu_count": 16},
    )

    report = collect_workload(
        duration=1.0,
        interval=1.0,
        api_client=api,
        process_sampler=process,
        system_sampler=system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    job_ids = [job["id"] for job in report["summary"]["jobs"]]
    assert job_ids == ["short-lived"]
    scenario = report["summary"]["scenario"]
    assert scenario["observed_job_count"] == 1
    assert scenario["terminal_job_count"] == 1
    assert scenario["all_observed_jobs_terminal"] is True
    # Makespan is derived from the short-lived job's own timestamps.
    assert scenario["workload_makespan_seconds"] == 0.3


def test_latency_target_fails_when_api_samples_fail():
    """A run with one fast success and one long timeout used to report
    ``jobs_api_p95_below_500ms: true`` because failed requests were excluded
    from the percentile — the responsiveness gate must not pass when any
    API call failed.
    """
    clock = _FakeClock()
    api = _SequenceApi([
        _api_sample(  # baseline
            latency=0.001, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
        _api_sample(
            latency=0.010, wait_count=0, wait_seconds=0.0,
            producer_starts=0, waiter_joins=0,
        ),
        {"status": None, "latency_seconds": 5.0, "error": "timeout"},
    ])
    process = _SequenceSampler(
        [
            {"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": True},
            {"cpu_percent": 100.0, "rss_bytes": 100, "executable_exists": True},
        ],
        {"pid": 123, "executable_exists": True},
    )
    system = _SequenceSampler(
        [{"cpu_idle_percent": 50.0}, {"cpu_idle_percent": 50.0}],
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

    summary = report["summary"]
    assert summary["api_failure_count"] == 1
    # Only the successful 10ms response is in the percentile — p95 is well
    # under 500ms — but the target still has to fail because of the timeout.
    assert summary["jobs_api_latency_seconds"]["p95"] == 0.01
    assert summary["targets"]["jobs_api_p95_below_500ms"] is False


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
    # `phase` is deliberately dropped even when its content looks safe.
    assert compact["jobs"][0]["progress"] == {
        "current": 2,
        "total": 10,
    }


def test_compact_jobs_payload_omits_user_derived_step_labels():
    """For local-folder/sync/discard jobs, `step.label` is built from
    `root_names[root_id]` (e.g. "Copy <folder> locally").  Copying it
    verbatim would leak user folder names into every sanitized sample.
    """
    compact = compact_jobs_payload({
        "active": [{
            "id": "local-folder-1",
            "type": "local_folder_copy",
            "status": "running",
            "steps": [{
                "id": "copy",
                "label": "Copy My Vacation 2024 locally",
                "status": "running",
                "progress": {"current": 3, "total": 10},
            }],
        }],
        "history": [],
        "resource_budget": {"waiters": 0},
    })

    step = compact["jobs"][0]["running_steps"][0]
    assert step == {
        "id": "copy",
        "status": "running",
        "progress": {"current": 3, "total": 10},
    }
    assert "My Vacation 2024" not in json.dumps(compact)


def test_compact_progress_drops_free_form_phase_entirely():
    """Phase text is a mix of generic labels ("Importing photos"), paths
    ("Scanning root 1 of 2: /private/photos"), filenames ("Downloading 1/3:
    capture.nef..."), and user folder names ("Copying My Vacation locally").
    No regex can reliably split the safe from the unsafe, so `_compact_progress`
    omits the phase field entirely and reports only numeric progress plus
    `stage_id`.
    """
    unsafe_phases = [
        "Scanning root 1 of 2: /private/photos",
        "Scanning root 1 of 2: /家族/写真",
        "Scanning C:\\Program Files\\Vireo\\lib",
        "Downloading 1/3: capture.nef...",
        "Retrying secret.raw in 3s ...",
        "Copying My Vacation 2024 locally",
        "Syncing Птицы to remote",
        "Importing photos",  # generic — still dropped
    ]
    compact = compact_jobs_payload({
        "active": [
            {
                "id": f"job-{i}",
                "type": "scan",
                "status": "running",
                "progress": {
                    "current": i,
                    "total": 10,
                    "phase": phase,
                    "stage_id": "walk",
                },
            }
            for i, phase in enumerate(unsafe_phases)
        ],
        "history": [],
        "resource_budget": {"waiters": 0},
    })

    encoded = json.dumps(compact, ensure_ascii=False)
    for fragment in (
        "/private", "家族", "写真", "Program Files", "capture.nef",
        "secret.raw", "My Vacation", "Птицы", "Importing photos",
    ):
        assert fragment not in encoded, f"phase text {fragment!r} leaked into report"
    for job in compact["jobs"]:
        assert "phase" not in job["progress"]
        assert job["progress"]["stage_id"] == "walk"
        assert "current" in job["progress"] and "total" in job["progress"]


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


class _FakeConn:
    def __init__(self, port, *, ip="127.0.0.1", status="LISTEN"):
        self.status = status
        self.laddr = type("Addr", (), {"port": port, "ip": ip})()


class _FakeProc:
    def __init__(
        self,
        pid,
        *,
        name="vireo-server",
        cmdline=None,
        port=None,
        listener_ip="127.0.0.1",
        children=None,
    ):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline if cmdline is not None else [name]
        self._port = port
        self._listener_ip = listener_ip
        self._children = list(children or [])

    def as_dict(self, attrs=None):
        return {"name": self._name, "cmdline": list(self._cmdline)}

    def net_connections(self, kind="tcp"):
        if self._port is None:
            return []
        return [_FakeConn(self._port, ip=self._listener_ip)]

    def children(self, recursive=False):
        return list(self._children)


class _FakePsutil:
    Error = RuntimeError
    CONN_LISTEN = "LISTEN"

    def __init__(self, processes):
        self._processes = {p.pid: p for p in processes}

    def Process(self, pid):
        if pid not in self._processes:
            raise self.Error(f"no process with PID {pid}")
        return self._processes[pid]


def _fake_resolver(host_to_ips):
    def resolver(host, _port, type=None):
        ips = host_to_ips.get(host, [])
        if not ips:
            raise OSError(f"unknown host {host}")
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]
    return resolver


_LOOPBACK_RESOLVER = _fake_resolver({"127.0.0.1": ["127.0.0.1"]})


def test_explicit_server_accepted_when_pid_owns_url_port():
    proc = _FakeProc(4242, port=50222)
    fake = _FakePsutil([proc])

    server = discover_server(
        requested_pid=4242,
        requested_url="http://127.0.0.1:50222",
        psutil_module=fake,
        resolver=_LOOPBACK_RESOLVER,
    )

    assert server == {"pid": 4242, "url": "http://127.0.0.1:50222"}


def test_explicit_server_accepted_when_child_process_owns_port():
    child = _FakeProc(4243, port=50222)
    parent = _FakeProc(4242, port=None, children=[child])
    fake = _FakePsutil([parent, child])

    server = discover_server(
        requested_pid=4242,
        requested_url="http://127.0.0.1:50222",
        psutil_module=fake,
        resolver=_LOOPBACK_RESOLVER,
    )

    assert server == {"pid": 4242, "url": "http://127.0.0.1:50222"}


def test_explicit_server_accepted_when_listener_binds_all_interfaces():
    proc = _FakeProc(4242, port=50222, listener_ip="0.0.0.0")
    fake = _FakePsutil([proc])

    server = discover_server(
        requested_pid=4242,
        requested_url="http://127.0.0.1:50222",
        psutil_module=fake,
        resolver=_LOOPBACK_RESOLVER,
    )

    assert server == {"pid": 4242, "url": "http://127.0.0.1:50222"}


def test_explicit_server_rejected_when_pid_does_not_own_url_port():
    """A stale/typo'd PID that happens to name a live process must not be
    accepted: process CPU/memory would come from one server while API samples
    would come from another, silently corrupting every workload comparison.
    """
    proc = _FakeProc(4242, port=60000)
    fake = _FakePsutil([proc])

    with pytest.raises(RuntimeError, match="does not own"):
        discover_server(
            requested_pid=4242,
            requested_url="http://127.0.0.1:50222",
            psutil_module=fake,
            resolver=_LOOPBACK_RESOLVER,
        )


def test_explicit_server_rejected_when_url_host_does_not_match_listener_ip():
    """The URL host may resolve to a different machine even when the port
    matches, so the local PID's CPU/RSS would be paired with API samples
    from an unrelated server on the LAN.  The listener bound to loopback
    must not be accepted for a URL that resolves off-loopback.
    """
    proc = _FakeProc(4242, port=50222, listener_ip="127.0.0.1")
    fake = _FakePsutil([proc])
    resolver = _fake_resolver({"otherhost.local": ["192.168.1.5"]})

    with pytest.raises(RuntimeError, match="does not own"):
        discover_server(
            requested_pid=4242,
            requested_url="http://otherhost.local:50222",
            psutil_module=fake,
            resolver=resolver,
        )


def test_explicit_server_rejected_when_owning_process_is_not_vireo():
    proc = _FakeProc(4242, name="postgres", cmdline=["postgres", "-D", "/db"], port=50222)
    fake = _FakePsutil([proc])

    with pytest.raises(RuntimeError, match="not a vireo-server"):
        discover_server(
            requested_pid=4242,
            requested_url="http://127.0.0.1:50222",
            psutil_module=fake,
            resolver=_LOOPBACK_RESOLVER,
        )
