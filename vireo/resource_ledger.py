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

    def __post_init__(self):
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


class ResourceLease:
    """An idempotently releasable allocation returned by ``ResourceLedger``."""

    def __init__(self, ledger, *, cpu_permits, lanes, wait_seconds, label):
        self._ledger = ledger
        self.cpu_permits = cpu_permits
        self.lanes = lanes
        self.wait_seconds = wait_seconds
        self.label = label
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
        self._lane_allocated = {name: 0 for name in self._lane_capacities}
        self._condition = threading.Condition(threading.Lock())
        self._clock = clock or time.monotonic
        self._total_wait_seconds = 0.0
        self._wait_count = 0
        self._waiters = 0
        self._owner_timing = {}

    def _validate_request(self, request):
        if request.cpu is not None and request.cpu.minimum > self.cpu_capacity:
            raise ValueError(
                f"CPU request minimum {request.cpu.minimum} exceeds capacity "
                f"{self.cpu_capacity}"
            )
        unknown = [lane for lane in request.lanes if lane not in self._lane_capacities]
        if unknown:
            raise ValueError(f"Unknown resource lane(s): {', '.join(unknown)}")

    def _available_cpu(self):
        return self.cpu_capacity - self._cpu_allocated

    def _can_grant(self, request):
        if request.cpu is not None and self._available_cpu() < request.cpu.minimum:
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
        if owner_id is not None:
            timing = self._owner_timing.setdefault(
                owner_id, {"wait_seconds": 0.0, "wait_count": 0}
            )
            timing["wait_seconds"] += wait_seconds
            timing["wait_count"] += 1
        return wait_seconds

    def acquire(
        self, request, *, cancel_check=None, owner_id=None, on_wait=None,
    ):
        """Block until ``request`` can be allocated atomically.

        Cancellation callbacks run outside the ledger mutex.  They may call
        JobRunner or stage code without creating a lock-order cycle.
        """
        if not isinstance(request, ResourceRequest):
            raise TypeError("request must be a ResourceRequest")
        self._validate_request(request)
        owner_id = owner_id if owner_id is not None else _RESOURCE_OWNER.get()
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
                    available = self._available_cpu()
                    cpu_permits = 0
                    if request.cpu is not None:
                        cpu_permits = min(request.cpu.preferred, available)
                        self._cpu_allocated += cpu_permits
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
                    )

                if wait_started is None:
                    wait_started = self._clock()
                    self._waiters += 1
                    announce_wait = True
                    # Run the callback outside the ledger mutex before
                    # sleeping. The next loop rechecks availability first.
                    continue
                self._condition.wait(timeout=0.2)

    def _release(self, lease):
        with self._condition:
            self._cpu_allocated -= lease.cpu_permits
            for lane in lease.lanes:
                self._lane_allocated[lane] -= 1
            self._condition.notify_all()

    def owner_timing(self, owner_id, *, remove=False):
        with self._condition:
            timing = self._owner_timing.get(owner_id)
            result = dict(timing or {"wait_seconds": 0.0, "wait_count": 0})
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


def _set_resource_ledger_for_tests(ledger):
    """Replace the process-wide ledger; tests must restore it afterwards."""
    global _DEFAULT_LEDGER
    with _DEFAULT_LEDGER_LOCK:
        previous = _DEFAULT_LEDGER
        _DEFAULT_LEDGER = ledger
    return previous
