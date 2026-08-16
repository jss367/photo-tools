"""Process-wide resource accounting for expensive background work.

Phase 2 uses this ledger directly from existing job paths.  The later unified
scheduler can reuse the same request and lease primitives instead of adding a
second semaphore layer.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass

_RESOURCE_OWNER = contextvars.ContextVar("vireo_resource_owner", default=None)
_RESOURCE_CANCEL_CHECK = contextvars.ContextVar(
    "vireo_resource_cancel_check", default=None,
)
_RESOURCE_ACTIVE_WAIT = contextvars.ContextVar(
    "vireo_resource_active_wait", default=None,
)
DEFAULT_CPU_INFERENCE_THREADS = 8


class ResourceWaitCancelled(RuntimeError):
    """Raised when a caller cancels while waiting for a resource claim."""


class _WaitTiming:
    """Mutable elapsed-time state for one logical resource wait."""

    def __init__(self, ledger, owner_id, started_at):
        self.ledger = ledger
        self.owner_id = owner_id
        self.started_at = started_at
        self.accumulated = 0.0
        self.suspend_depth = 0

    def elapsed(self, now):
        elapsed = self.accumulated
        if self.suspend_depth == 0:
            elapsed += max(0.0, now - self.started_at)
        return elapsed

    def suspend(self, now):
        if self.suspend_depth == 0:
            self.accumulated += max(0.0, now - self.started_at)
        self.suspend_depth += 1

    def resume(self, now):
        if self.suspend_depth < 1:
            return
        self.suspend_depth -= 1
        if self.suspend_depth == 0:
            self.started_at = now


@dataclass(frozen=True)
class CpuRequest:
    """A bounded CPU request.

    ``minimum`` is required to start, ``preferred`` is the normal grant, and
    ``maximum`` is retained for the later scheduler's adaptive grants.
    """

    minimum: int
    preferred: int
    maximum: int

    def __post_init__(self):
        for name, value in (
            ("minimum", self.minimum),
            ("preferred", self.preferred),
            ("maximum", self.maximum),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"CPU {name} must be an integer")
        if self.minimum < 1:
            raise ValueError("CPU minimum must be at least 1")
        if not self.minimum <= self.preferred <= self.maximum:
            raise ValueError("CPU request must satisfy minimum <= preferred <= maximum")


@dataclass(frozen=True)
class ResourceRequest:
    """Resources that must be acquired together for one phase."""

    cpu: CpuRequest | None = None
    lanes: tuple[str, ...] = ()
    label: str = "background work"
    cpu_reserve: int = 0

    def __post_init__(self):
        if isinstance(self.cpu_reserve, bool) or not isinstance(
            self.cpu_reserve, int,
        ):
            raise TypeError("CPU reserve must be an integer")
        if self.cpu_reserve < 0:
            raise ValueError("CPU reserve must be nonnegative")
        if self.cpu is None and self.cpu_reserve:
            raise ValueError("CPU reserve requires a CPU request")
        normalized = tuple(sorted(set(self.lanes)))
        if any(not lane for lane in normalized):
            raise ValueError("Resource lane names must be non-empty")
        object.__setattr__(self, "lanes", normalized)
        if self.cpu is None and not normalized:
            raise ValueError("A resource request must include CPU or a lane")


def _linux_physical_core_count():
    """Best-effort physical-core count from Linux's cpuinfo topology."""
    try:
        packages_and_cores = set()
        physical_id = None
        core_id = None
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for raw_line in cpuinfo:
                line = raw_line.strip()
                if not line:
                    if physical_id is not None and core_id is not None:
                        packages_and_cores.add((physical_id, core_id))
                    physical_id = None
                    core_id = None
                    continue
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                if key.strip() == "physical id":
                    physical_id = value.strip()
                elif key.strip() == "core id":
                    core_id = value.strip()
        if physical_id is not None and core_id is not None:
            packages_and_cores.add((physical_id, core_id))
        return len(packages_and_cores) or None
    except OSError:
        return None


def _darwin_physical_core_count():
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.physicalcpu"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
        return int(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def detect_physical_core_count():
    """Return a best-effort physical-core count, or ``None``."""
    system = platform.system()
    if system == "Darwin":
        return _darwin_physical_core_count()
    if system == "Linux":
        return _linux_physical_core_count()
    return None


def _read_cgroup_v2_max(path):
    """Parse a cgroup v2 ``cpu.max`` file into a CPU count or ``None``.

    Returns ``None`` for missing files, malformed content, or an
    unlimited ``max <period>`` line. Rounds fractional quotas up so
    ``--cpus=1.5`` yields ``2`` for whole-CPU decisions rather than
    silently truncating to 1.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2:
        return None
    quota_str, period_str = parts
    if quota_str == "max":
        return None
    try:
        quota = int(quota_str)
        period = int(period_str)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def _read_cgroup_v1_pair(quota_path, period_path):
    """Parse a cgroup v1 quota/period pair into a CPU count or ``None``."""
    try:
        with open(quota_path, encoding="utf-8") as f:
            quota = int(f.read().strip())
        with open(period_path, encoding="utf-8") as f:
            period = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def _process_cgroup_paths():
    """Return ``(v2_relative_path, v1_cpu_relative_path)`` for this process.

    ``/proc/self/cgroup`` has one line per hierarchy. On cgroup v2 the
    line is ``0::<path>`` (single unified hierarchy). On cgroup v1 there
    is one line per controller; the ``cpu`` (or ``cpu,cpuacct``)
    controller row holds the process-specific subpath. Missing values
    surface as ``None`` so callers fall back to the root cgroup file.
    """
    v2_path = None
    v1_cpu_path = None
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                fields = line.split(":", 2)
                if len(fields) != 3:
                    continue
                hier_id, controllers, subpath = fields
                if hier_id == "0" and controllers == "":
                    v2_path = subpath or "/"
                elif "cpu" in controllers.split(","):
                    v1_cpu_path = subpath or "/"
    except OSError:
        pass
    return v2_path, v1_cpu_path


def _ancestor_dirs(path):
    """Yield ``path`` and each parent directory down to ``/``.

    ``"/system.slice/vireo.service"`` → ``["/system.slice/vireo.service",
    "/system.slice", "/"]``. Ancestor quotas below the leaf can also
    apply — cgroup enforces the tightest along the chain — so walking
    every ancestor and taking the ``min`` matches kernel semantics.
    """
    if not path:
        return
    normalized = path.rstrip("/") or "/"
    yield normalized
    while normalized != "/":
        parent = normalized.rsplit("/", 1)[0] or "/"
        yield parent
        if parent == "/":
            break
        normalized = parent


def _cgroup_cpu_quota_cpus():
    """Return the effective CPU count from the cgroup CFS quota, or ``None``.

    Docker ``--cpus=2``, Kubernetes CPU limits, and systemd
    ``CPUQuota=`` impose a CFS bandwidth quota WITHOUT narrowing the
    process's CPU affinity — ``os.sched_getaffinity`` and
    ``os.process_cpu_count`` therefore still report the host's full
    affinity set, missing this ceiling.

    Nested cgroup hierarchies (systemd services, containers running
    under a slice) store the effective quota under the process's own
    cgroup path, not the hierarchy root — reading only
    ``/sys/fs/cgroup/cpu.max`` at the root would silently fall back to
    the affinity count for a ``vireo.service`` with a ``CPUQuota=200%``
    directive. Resolve the process's cgroup path from
    ``/proc/self/cgroup`` and check every ancestor, taking the ``min``
    quota — cgroup enforces the tightest along the chain, and this
    matches that semantics for capacity sizing.

    Prefers cgroup v2's unified ``cpu.max``. Falls back to cgroup v1's
    ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us`` split (``-1`` =
    unlimited). Returns ``None`` when no readable quota is set anywhere
    on the process's cgroup chain so the caller falls back to
    affinity-based counts.
    """
    v2_path, v1_cpu_path = _process_cgroup_paths()

    candidates = []

    # cgroup v2: /sys/fs/cgroup{path}/cpu.max, walking ancestors.
    if v2_path is not None:
        for ancestor in _ancestor_dirs(v2_path):
            mount = "/sys/fs/cgroup" + ("" if ancestor == "/" else ancestor)
            value = _read_cgroup_v2_max(mount + "/cpu.max")
            if value is not None:
                candidates.append(value)
    else:
        # No /proc/self/cgroup — fall back to the root file so at least
        # a non-nested container is still detected.
        value = _read_cgroup_v2_max("/sys/fs/cgroup/cpu.max")
        if value is not None:
            candidates.append(value)

    # cgroup v1: /sys/fs/cgroup/cpu{path}/cpu.cfs_quota_us, walking
    # ancestors. Some distros (Ubuntu, Debian, Alpine, older Fedora)
    # co-mount the ``cpu`` and ``cpuacct`` controllers under the
    # combined ``cpu,cpuacct`` name — /proc/self/cgroup still reports
    # ``cpu,cpuacct`` but the mount directory is that literal string,
    # not ``cpu``. Probe both possible mount bases so containers on
    # those hosts still get their quota respected.
    if v1_cpu_path is not None:
        for ancestor in _ancestor_dirs(v1_cpu_path):
            for base in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
                mount = base + ("" if ancestor == "/" else ancestor)
                value = _read_cgroup_v1_pair(
                    mount + "/cpu.cfs_quota_us",
                    mount + "/cpu.cfs_period_us",
                )
                if value is not None:
                    candidates.append(value)
    else:
        for base in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
            value = _read_cgroup_v1_pair(
                base + "/cpu.cfs_quota_us",
                base + "/cpu.cfs_period_us",
            )
            if value is not None:
                candidates.append(value)

    return min(candidates) if candidates else None


def process_usable_cpu_count():
    """Return the CPU count actually usable by this process, or ``None``.

    ``/proc/cpuinfo`` describes the host's full topology regardless of any
    per-process restriction, and plain ``os.cpu_count()`` is likewise
    host-wide. Deployments that constrain the process — ``taskset``,
    systemd ``CPUAffinity=``, a container cpuset, or a cgroup CPU quota —
    would otherwise be handed a capacity based on cores the process
    cannot actually schedule on, which oversubscribes the assigned CPUs
    and defeats both the process-wide budget and the interactive
    reserve.

    Combine every clamp that applies to this process and take the
    minimum:

    - ``os.process_cpu_count`` (Python 3.13+) or
      ``os.sched_getaffinity`` (Linux) for scheduling-affinity /
      cpuset restrictions.
    - ``_cgroup_cpu_quota_cpus`` for a Docker/Kubernetes CFS bandwidth
      quota that leaves the affinity set alone
      (``os.process_cpu_count`` reads affinity, not the cgroup quota
      — verified against CPython source; a fresh Codex review flagged
      that the earlier revision documented quota support it did not
      actually deliver).

    Returns ``None`` when no process-scoped signal is available
    (Darwin/Windows on old Python, no cgroup, unlimited quota, etc.),
    in which case callers fall back to the host counts.
    """
    candidates = []
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        with contextlib.suppress(OSError):
            candidates.append(process_cpu_count())
    else:
        sched_getaffinity = getattr(os, "sched_getaffinity", None)
        if sched_getaffinity is not None:
            with contextlib.suppress(OSError):
                candidates.append(len(sched_getaffinity(0)))

    quota = _cgroup_cpu_quota_cpus()
    if quota:
        candidates.append(quota)

    positive = [c for c in candidates if c and c > 0]
    if not positive:
        return None
    return min(positive)


_USABLE_CORES_UNSET = object()


def automatic_cpu_capacity(
    physical_cores=None, logical_cores=None, usable_cores=_USABLE_CORES_UNSET,
):
    """Apply Vireo's interactive reserve and return a positive capacity.

    The detected physical/logical core counts describe the host
    topology, not what this process is actually allowed to schedule on.
    Clamp the effective core count by ``process_usable_cpu_count`` when
    available so a ``taskset`` / systemd / container-cpuset / cgroup-
    quota deployment doesn't derive a budget from cores it can't touch.
    """
    if physical_cores is None:
        physical_cores = detect_physical_core_count()
    if logical_cores is None:
        logical_cores = os.cpu_count()
    cores = max(1, physical_cores or logical_cores or 1)
    if usable_cores is _USABLE_CORES_UNSET:
        usable_cores = process_usable_cpu_count()
    if usable_cores:
        cores = max(1, min(cores, usable_cores))
    reserve = max(2, math.ceil(cores * 0.20))
    return max(1, cores - reserve)


def cpu_phase_request(capacity, *, minimum=1, preferred=8, maximum=8):
    """Clamp a phase profile to the process capacity."""
    capacity = max(1, int(capacity))
    maximum = max(1, min(int(maximum), capacity))
    minimum = max(1, min(int(minimum), maximum))
    preferred = max(minimum, min(int(preferred), maximum))
    return CpuRequest(minimum=minimum, preferred=preferred, maximum=maximum)


def cpu_inference_request(capacity):
    """Return the exact CPU grant matching Vireo's ONNX thread pool."""
    return cpu_phase_request(
        capacity,
        minimum=DEFAULT_CPU_INFERENCE_THREADS,
        preferred=DEFAULT_CPU_INFERENCE_THREADS,
        maximum=DEFAULT_CPU_INFERENCE_THREADS,
    )


def resolve_resource_cancel_check(cancel_check=None):
    """Return an explicit cancellation probe or the current bound probe."""
    return (
        cancel_check
        if cancel_check is not None
        else _RESOURCE_CANCEL_CHECK.get()
    )


class ResourceLease:
    """An idempotently releasable allocation returned by ``ResourceLedger``."""

    def __init__(
        self, ledger, *, cpu_permits, lanes, wait_seconds, label, cpu_reserve,
    ):
        self._ledger = ledger
        self.cpu_permits = cpu_permits
        self.lanes = lanes
        self.wait_seconds = wait_seconds
        self.label = label
        self.cpu_reserve = cpu_reserve
        self._released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def release(self):
        if not self._released:
            self._released = True
            self._ledger._release(self)


class ResourceLedger:
    """Thread-safe, process-local CPU and exclusive-lane accounting."""

    def __init__(self, cpu_capacity, lane_capacities=None, *, clock=None):
        cpu_capacity = int(cpu_capacity)
        if cpu_capacity < 1:
            raise ValueError("CPU capacity must be at least 1")
        lane_capacities = {
            "cpu_ml": 1,
            "model_construction": 1,
            **(lane_capacities or {}),
        }
        if any(int(capacity) < 1 for capacity in lane_capacities.values()):
            raise ValueError("Resource lane capacities must be at least 1")

        self.cpu_capacity = cpu_capacity
        self._lane_capacities = {
            name: int(capacity) for name, capacity in lane_capacities.items()
        }
        self._cpu_allocated = 0
        # Flexible requests with ``cpu_reserve`` form one shared allocation
        # class. Track their permits separately so ordinary CPU holders that
        # already consume the reserved slice (for example, inference that
        # acquired first) do not cause the reserve to be subtracted twice.
        self._reserved_cpu_allocated = 0
        self._active_cpu_reserves = []
        self._lane_allocated = {name: 0 for name in self._lane_capacities}
        self._condition = threading.Condition(threading.Lock())
        self._clock = clock or time.monotonic
        self._total_wait_seconds = 0.0
        self._wait_count = 0
        self._waiters = 0
        self._owner_timing = {}
        # Wait start times for owners currently blocked in acquire().
        # Snapshot readers add the elapsed active wait to the reported
        # totals so ``/api/jobs`` shows accurate diagnostics for the job
        # that is right now waiting on resources — not just after the
        # wait ends. A single owner may hold multiple entries if several
        # of its threads are waiting concurrently, so this is a list per
        # owner, not a single timestamp.
        self._active_owner_waits = {}

    def _validate_request(self, request):
        if request.cpu is not None and request.cpu.minimum > self.cpu_capacity:
            raise ValueError(
                f"CPU request minimum {request.cpu.minimum} exceeds capacity "
                f"{self.cpu_capacity}"
            )
        if (
            request.cpu is not None
            and request.cpu_reserve
            and request.cpu.minimum > self.cpu_capacity - request.cpu_reserve
        ):
            raise ValueError(
                f"CPU request minimum {request.cpu.minimum} cannot preserve "
                f"reserve {request.cpu_reserve} within capacity "
                f"{self.cpu_capacity}"
            )
        unknown = [lane for lane in request.lanes if lane not in self._lane_capacities]
        if unknown:
            raise ValueError(f"Unknown resource lane(s): {', '.join(unknown)}")

    def _available_cpu(self):
        return self.cpu_capacity - self._cpu_allocated

    def _grantable_cpu(self, request):
        available = self._available_cpu()
        if not request.cpu_reserve:
            return available
        reserve = max([request.cpu_reserve, *self._active_cpu_reserves])
        unreserved_allocated = self._cpu_allocated - self._reserved_cpu_allocated
        reserve_shortfall = max(0, reserve - unreserved_allocated)
        return max(0, available - reserve_shortfall)

    def _can_grant(self, request):
        if request.cpu is not None and self._grantable_cpu(request) < request.cpu.minimum:
            return False
        return all(
            self._lane_allocated[lane] < self._lane_capacities[lane]
            for lane in request.lanes
        )

    @staticmethod
    def _cancel_requested(cancel_check):
        if cancel_check is None:
            return False
        return bool(cancel_check())

    def _record_wait_locked(self, owner_id, wait_timing):
        wait_seconds = wait_timing.elapsed(self._clock())
        self._total_wait_seconds += wait_seconds
        self._wait_count += 1
        self._waiters -= 1
        self._drop_active_owner_wait_locked(owner_id, wait_timing)
        if owner_id is not None:
            timing = self._owner_timing.setdefault(
                owner_id, {"wait_seconds": 0.0, "wait_count": 0}
            )
            timing["wait_seconds"] += wait_seconds
            timing["wait_count"] += 1
        return wait_seconds

    def _track_active_owner_wait_locked(self, owner_id, wait_timing):
        if owner_id is None:
            return
        self._active_owner_waits.setdefault(owner_id, []).append(wait_timing)

    def _drop_active_owner_wait_locked(self, owner_id, wait_timing):
        if owner_id is None:
            return
        starts = self._active_owner_waits.get(owner_id)
        if not starts:
            return
        try:
            starts.remove(wait_timing)
        except ValueError:
            return
        if not starts:
            self._active_owner_waits.pop(owner_id, None)

    @contextlib.contextmanager
    def track_external_wait(self, *, owner_id=None):
        """Include a non-ledger resource wait in job timing diagnostics.

        Some process-wide resources, such as the accelerator semaphore, are
        deliberately coordinated outside ``acquire``. This context preserves
        the same live and completed owner timing semantics for those waits.
        """
        owner_id = owner_id if owner_id is not None else _RESOURCE_OWNER.get()
        wait_timing = _WaitTiming(self, owner_id, self._clock())
        with self._condition:
            self._waiters += 1
            self._track_active_owner_wait_locked(owner_id, wait_timing)
        token = _RESOURCE_ACTIVE_WAIT.set(wait_timing)
        try:
            yield
        finally:
            _RESOURCE_ACTIVE_WAIT.reset(token)
            with self._condition:
                self._record_wait_locked(owner_id, wait_timing)

    def acquire(
        self, request, *, cancel_check=None, owner_id=None, on_wait=None,
    ):
        """Block until ``request`` can be allocated atomically.

        Cancellation callbacks run outside the ledger mutex.  They may call
        JobRunner or stage code without creating a lock-order cycle.

        When ``cancel_check`` is not supplied, the current execution
        context's bound probe (see :func:`bind_resource_cancel_check`) is
        used instead so background jobs that establish one at their top
        level can cancel waits from any downstream inference site without
        threading a callable through every helper.
        """
        if not isinstance(request, ResourceRequest):
            raise TypeError("request must be a ResourceRequest")
        self._validate_request(request)
        owner_id = owner_id if owner_id is not None else _RESOURCE_OWNER.get()
        cancel_check = resolve_resource_cancel_check(cancel_check)
        wait_timing = None
        announce_wait = False

        while True:
            wait_token = None
            if wait_timing is not None:
                wait_token = _RESOURCE_ACTIVE_WAIT.set(wait_timing)
            try:
                cancelled = self._cancel_requested(cancel_check)
            except BaseException:
                if wait_timing is not None:
                    with self._condition:
                        self._record_wait_locked(owner_id, wait_timing)
                raise
            finally:
                if wait_token is not None:
                    _RESOURCE_ACTIVE_WAIT.reset(wait_token)
            if cancelled:
                if wait_timing is not None:
                    with self._condition:
                        self._record_wait_locked(owner_id, wait_timing)
                raise ResourceWaitCancelled(
                    f"Cancelled while waiting for {request.label} resources"
                )
            if announce_wait:
                announce_wait = False
                if on_wait is not None:
                    try:
                        on_wait(request)
                    except BaseException:
                        with self._condition:
                            self._record_wait_locked(owner_id, wait_timing)
                        raise
            with self._condition:
                if self._can_grant(request):
                    available = self._grantable_cpu(request)
                    cpu_permits = 0
                    if request.cpu is not None:
                        cpu_permits = min(request.cpu.preferred, available)
                        self._cpu_allocated += cpu_permits
                        if request.cpu_reserve:
                            self._reserved_cpu_allocated += cpu_permits
                            self._active_cpu_reserves.append(request.cpu_reserve)
                    for lane in request.lanes:
                        self._lane_allocated[lane] += 1

                    wait_seconds = 0.0
                    if wait_timing is not None:
                        wait_seconds = self._record_wait_locked(
                            owner_id, wait_timing,
                        )
                    return ResourceLease(
                        self,
                        cpu_permits=cpu_permits,
                        lanes=request.lanes,
                        wait_seconds=wait_seconds,
                        label=request.label,
                        cpu_reserve=request.cpu_reserve,
                    )

                if wait_timing is None:
                    wait_timing = _WaitTiming(
                        self, owner_id, self._clock(),
                    )
                    self._waiters += 1
                    self._track_active_owner_wait_locked(
                        owner_id, wait_timing,
                    )
                    announce_wait = True
                    # Run the callback outside the ledger mutex before
                    # sleeping. The next loop rechecks availability first.
                    continue
                self._condition.wait(timeout=0.2)

    def _release(self, lease):
        with self._condition:
            self._cpu_allocated -= lease.cpu_permits
            if lease.cpu_reserve:
                self._reserved_cpu_allocated -= lease.cpu_permits
                self._active_cpu_reserves.remove(lease.cpu_reserve)
            for lane in lease.lanes:
                self._lane_allocated[lane] -= 1
            self._condition.notify_all()

    def owner_timing(self, owner_id, *, remove=False):
        """Return recorded wait totals for ``owner_id`` including active waits.

        A wait is recorded only when it ends (grant, cancellation, or a
        raising probe). Snapshots taken while an owner is still blocked
        in :meth:`acquire` would therefore report zero — precisely for
        the job that most needs the diagnostic. Fold in any currently
        active wait so ``/api/jobs`` shows accurate ``resource_wait_*``
        while a job is stuck in contention. When called with
        ``remove=True`` (once per job at finalization), the currently
        active waits are left untouched — an active wait entry belongs
        to the still-running :meth:`acquire` call and its final total
        will be recorded when that call ends.
        """
        with self._condition:
            timing = self._owner_timing.get(owner_id)
            result = dict(timing or {"wait_seconds": 0.0, "wait_count": 0})
            active_waits = self._active_owner_waits.get(owner_id)
            if active_waits:
                now = self._clock()
                for wait_timing in active_waits:
                    result["wait_seconds"] += wait_timing.elapsed(now)
                result["wait_count"] += len(active_waits)
            if remove:
                self._owner_timing.pop(owner_id, None)
        result["wait_seconds"] = round(result["wait_seconds"], 3)
        return result

    def snapshot(self):
        with self._condition:
            lanes = {
                name: {
                    "capacity": capacity,
                    "allocated": self._lane_allocated[name],
                    "available": capacity - self._lane_allocated[name],
                }
                for name, capacity in sorted(self._lane_capacities.items())
            }
            return {
                "cpu": {
                    "capacity": self.cpu_capacity,
                    "allocated": self._cpu_allocated,
                    "available": self._available_cpu(),
                },
                "lanes": lanes,
                "wait_count": self._wait_count,
                "wait_seconds": round(self._total_wait_seconds, 3),
                "waiters": self._waiters,
            }


_DEFAULT_LEDGER = None
_DEFAULT_LEDGER_LOCK = threading.Lock()


def get_resource_ledger():
    global _DEFAULT_LEDGER
    with _DEFAULT_LEDGER_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = ResourceLedger(automatic_cpu_capacity())
        return _DEFAULT_LEDGER


@contextlib.contextmanager
def suspend_resource_wait_timing():
    """Exclude deliberate parking from the current resource wait interval."""
    wait_timing = _RESOURCE_ACTIVE_WAIT.get()
    if wait_timing is None:
        yield
        return
    ledger = wait_timing.ledger
    with ledger._condition:
        wait_timing.suspend(ledger._clock())
    try:
        yield
    finally:
        with ledger._condition:
            wait_timing.resume(ledger._clock())


@contextlib.contextmanager
def bind_resource_owner(owner_id):
    """Attribute claims in the current execution context to a job."""
    token = _RESOURCE_OWNER.set(owner_id)
    try:
        yield
    finally:
        _RESOURCE_OWNER.reset(token)


@contextlib.contextmanager
def bind_resource_cancel_check(cancel_check):
    """Route ledger waits in the current context through ``cancel_check``.

    Downstream inference sites (classifier, detector, mask, embedding, text
    encoder, keypoints, timm) claim ``cpu_ml`` without seeing the owning
    job's cancellation probe directly. Binding the probe here lets those
    waits wake promptly on Cancel — the same guarantee scanner hashing
    already gets by threading ``cancel_check`` through function arguments —
    so a cancelled worker cannot outlive ``JobRunner.shutdown()``.

    Callers that pass ``cancel_check`` explicitly to :meth:`ResourceLedger.acquire`
    always take precedence; the bound probe is only consulted when the
    argument is omitted or None.
    """
    token = _RESOURCE_CANCEL_CHECK.set(cancel_check)
    try:
        yield
    finally:
        _RESOURCE_CANCEL_CHECK.reset(token)


def _set_resource_ledger_for_tests(ledger):
    """Replace the process-wide ledger; tests must restore it afterwards."""
    global _DEFAULT_LEDGER
    with _DEFAULT_LEDGER_LOCK:
        previous = _DEFAULT_LEDGER
        _DEFAULT_LEDGER = ledger
    return previous
