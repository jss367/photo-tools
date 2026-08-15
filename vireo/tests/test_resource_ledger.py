import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import resource_ledger
from resource_ledger import (
    CpuRequest,
    ResourceLedger,
    ResourceRequest,
    ResourceWaitCancelled,
    bind_resource_cancel_check,
    bind_resource_owner,
)


def test_automatic_capacity_reserves_interactive_cores():
    assert resource_ledger.automatic_cpu_capacity(physical_cores=16) == 12
    assert resource_ledger.automatic_cpu_capacity(physical_cores=8) == 6


def test_automatic_capacity_survives_unavailable_core_counts(monkeypatch):
    monkeypatch.setattr(resource_ledger, "detect_physical_core_count", lambda: None)
    monkeypatch.setattr(resource_ledger.os, "cpu_count", lambda: None)
    assert resource_ledger.automatic_cpu_capacity() == 1


def test_cpu_grants_preferred_then_available_above_minimum():
    ledger = ResourceLedger(cpu_capacity=6)
    outer_request = ResourceRequest(cpu=CpuRequest(2, 4, 6))
    inner_request = ResourceRequest(cpu=CpuRequest(2, 4, 4))

    with ledger.acquire(outer_request) as outer:
        assert outer.cpu_permits == 4
        with ledger.acquire(inner_request) as inner:
            assert inner.cpu_permits == 2
            assert ledger.snapshot()["cpu"] == {
                "capacity": 6, "allocated": 6, "available": 0,
            }
        assert ledger.snapshot()["cpu"]["allocated"] == 4
    assert ledger.snapshot()["cpu"]["allocated"] == 0


def test_cpu_and_lane_claim_waits_without_partial_allocation():
    ledger = ResourceLedger(cpu_capacity=2)
    lane_holder = ledger.acquire(ResourceRequest(lanes=("cpu_ml",)))
    waiting = threading.Event()
    acquired = threading.Event()

    def claim_both():
        request = ResourceRequest(
            cpu=CpuRequest(2, 2, 2), lanes=("cpu_ml",),
        )
        with ledger.acquire(request, on_wait=lambda _request: waiting.set()):
            acquired.set()

    thread = threading.Thread(target=claim_both)
    thread.start()
    assert waiting.wait(timeout=1.0)
    assert ledger.snapshot()["cpu"]["allocated"] == 0
    assert not acquired.is_set()

    with ledger.acquire(ResourceRequest(cpu=CpuRequest(2, 2, 2))):
        assert ledger.snapshot()["cpu"]["allocated"] == 2
    lane_holder.release()
    assert acquired.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_owner_wait_timing_uses_injected_clock():
    now = [10.0]
    ledger = ResourceLedger(cpu_capacity=1, clock=lambda: now[0])
    holder = ledger.acquire(ResourceRequest(cpu=CpuRequest(1, 1, 1)))
    waiting = threading.Event()
    acquired = threading.Event()

    def waiter():
        with bind_resource_owner("job-1"):
            with ledger.acquire(
                ResourceRequest(cpu=CpuRequest(1, 1, 1)),
                on_wait=lambda _request: waiting.set(),
            ):
                acquired.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=1.0)
    now[0] = 12.5
    holder.release()
    assert acquired.wait(timeout=1.0)
    thread.join(timeout=1.0)

    assert ledger.owner_timing("job-1") == {
        "wait_seconds": 2.5, "wait_count": 1,
    }
    assert ledger.snapshot()["wait_seconds"] == 2.5


def test_cancelled_wait_releases_waiter_accounting():
    ledger = ResourceLedger(cpu_capacity=1)
    holder = ledger.acquire(ResourceRequest(cpu=CpuRequest(1, 1, 1)))
    waiting = threading.Event()
    cancelled = threading.Event()
    outcome = []

    def waiter():
        try:
            ledger.acquire(
                ResourceRequest(cpu=CpuRequest(1, 1, 1)),
                cancel_check=cancelled.is_set,
                on_wait=lambda _request: waiting.set(),
            )
        except ResourceWaitCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=1.0)
    cancelled.set()
    # Wake the condition immediately; production cancellation otherwise gets
    # noticed by the bounded 200 ms poll.
    holder.release()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
    assert ledger.snapshot()["waiters"] == 0


def test_raising_cancel_check_releases_waiter_accounting():
    class CallerCancelled(RuntimeError):
        pass

    ledger = ResourceLedger(cpu_capacity=1)
    holder = ledger.acquire(ResourceRequest(cpu=CpuRequest(1, 1, 1)))
    waiting = threading.Event()
    cancel_now = threading.Event()
    outcome = []

    def cancel_check():
        if cancel_now.is_set():
            raise CallerCancelled("stop")
        return False

    def waiter():
        try:
            ledger.acquire(
                ResourceRequest(cpu=CpuRequest(1, 1, 1)),
                cancel_check=cancel_check,
                on_wait=lambda _request: waiting.set(),
            )
        except CallerCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=1.0)
    cancel_now.set()
    holder.release()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
    assert ledger.snapshot()["waiters"] == 0


@pytest.mark.parametrize(
    "values",
    [(0, 1, 1), (2, 1, 2), (1, 3, 2)],
)
def test_invalid_cpu_request_rejected(values):
    with pytest.raises(ValueError):
        CpuRequest(*values)


def test_bound_cancel_check_wakes_waiter_without_explicit_argument():
    """A job that binds its cancel probe cancels downstream ledger waits.

    Downstream inference sites (CPU classify/detect/mask/embed) claim the
    ``cpu_ml`` lane through ``acquire_inference_resources`` without seeing
    the job's cancellation callable. Binding the probe via
    ``bind_resource_cancel_check`` at the top of a job means those waits
    still wake when the job is cancelled.
    """
    ledger = ResourceLedger(cpu_capacity=1)
    holder = ledger.acquire(ResourceRequest(cpu=CpuRequest(1, 1, 1)))
    waiting = threading.Event()
    cancelled = threading.Event()
    outcome = []

    def waiter():
        with bind_resource_cancel_check(cancelled.is_set):
            try:
                ledger.acquire(
                    ResourceRequest(cpu=CpuRequest(1, 1, 1)),
                    on_wait=lambda _request: waiting.set(),
                )
            except ResourceWaitCancelled:
                outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=1.0)
    cancelled.set()
    holder.release()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
    assert ledger.snapshot()["waiters"] == 0


def test_explicit_cancel_check_overrides_bound_probe():
    """A caller that passes cancel_check explicitly is trusted verbatim.

    Scanner hashing already threads its own probe and would otherwise be
    surprised by a bound probe silently taking over — the explicit
    argument must win, and ``None`` means "no cancellation" for callers
    that opted out on purpose.
    """
    ledger = ResourceLedger(cpu_capacity=1)
    holder = ledger.acquire(ResourceRequest(cpu=CpuRequest(1, 1, 1)))
    waiting = threading.Event()
    bound_cancelled = threading.Event()
    explicit_cancelled = threading.Event()
    outcome = []

    def waiter():
        with bind_resource_cancel_check(bound_cancelled.is_set):
            try:
                ledger.acquire(
                    ResourceRequest(cpu=CpuRequest(1, 1, 1)),
                    cancel_check=explicit_cancelled.is_set,
                    on_wait=lambda _request: waiting.set(),
                )
            except ResourceWaitCancelled:
                outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=1.0)
    # Flip the bound probe first. If bind took precedence, this would
    # cancel the waiter; the assertions below prove it did not.
    bound_cancelled.set()
    explicit_cancelled.set()
    holder.release()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
