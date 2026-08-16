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
DEFAULT_CPU_INFERENCE_THREADS = 8


class ResourceWaitCancelled(RuntimeError):
    """Raised when a caller cancels while waiting for a resource claim."""


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


def automatic_cpu_capacity(physical_cores=None, logical_cores=None):
    """Apply Vireo's interactive reserve and return a positive capacity."""
    if physical_cores is None:
        physical_cores = detect_physical_core_count()
    if logical_cores is None:
        logical_cores = os.cpu_count()
    cores = max(1, physical_cores or logical_cores or 1)
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

    def _record_wait_locked(self, owner_id, wait_started):
        wait_seconds = max(0.0, self._clock() - wait_started)
        self._total_wait_seconds += wait_seconds
        self._wait_count += 1
        self._waiters -= 1
        self._drop_active_owner_wait_locked(owner_id, wait_started)
        if owner_id is not None:
            timing = self._owner_timing.setdefault(
                owner_id, {"wait_seconds": 0.0, "wait_count": 0}
            )
            timing["wait_seconds"] += wait_seconds
            timing["wait_count"] += 1
        return wait_seconds

    def _track_active_owner_wait_locked(self, owner_id, wait_started):
        if owner_id is None:
            return
        self._active_owner_waits.setdefault(owner_id, []).append(wait_started)

    def _drop_active_owner_wait_locked(self, owner_id, wait_started):
        if owner_id is None:
            return
        starts = self._active_owner_waits.get(owner_id)
        if not starts:
            return
        try:
            starts.remove(wait_started)
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
        wait_started = self._clock()
        with self._condition:
            self._waiters += 1
            self._track_active_owner_wait_locked(owner_id, wait_started)
        try:
            yield
        finally:
            with self._condition:
                self._record_wait_locked(owner_id, wait_started)

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
        wait_started = None
        announce_wait = False

        while True:
            try:
                cancelled = self._cancel_requested(cancel_check)
            except BaseException:
                if wait_started is not None:
                    with self._condition:
                        self._record_wait_locked(owner_id, wait_started)
                raise
            if cancelled:
                if wait_started is not None:
                    with self._condition:
                        self._record_wait_locked(owner_id, wait_started)
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
                            self._record_wait_locked(owner_id, wait_started)
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
                    if wait_started is not None:
                        wait_seconds = self._record_wait_locked(
                            owner_id, wait_started,
                        )
                    return ResourceLease(
                        self,
                        cpu_permits=cpu_permits,
                        lanes=request.lanes,
                        wait_seconds=wait_seconds,
                        label=request.label,
                        cpu_reserve=request.cpu_reserve,
                    )

                if wait_started is None:
                    wait_started = self._clock()
                    self._waiters += 1
                    self._track_active_owner_wait_locked(
                        owner_id, wait_started,
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
            active_starts = self._active_owner_waits.get(owner_id)
            if active_starts:
                now = self._clock()
                for start in active_starts:
                    result["wait_seconds"] += max(0.0, now - start)
                result["wait_count"] += len(active_starts)
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
