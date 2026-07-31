"""Tests for the idle-sleep blocker that keeps long jobs running.

See issue #1397: a multi-hour import on battery gets suspended by macOS
idle sleep, and an SMB-over-Tailscale mount does not survive the
sleep/DarkWake cycling.
"""

import sys

import pytest

from power import SleepBlocker, start_platform_inhibitor


class FakeInhibitor:
    """Stand-in for the OS-level inhibitor process/handle."""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self, reason):
        self.started += 1
        self.reason = reason
        return self

    def stop(self):
        self.stopped += 1


def test_first_acquire_starts_the_inhibitor():
    fake = FakeInhibitor()
    blocker = SleepBlocker(start_inhibitor=fake.start)

    blocker.acquire()

    assert fake.started == 1
    assert blocker.active is True


def test_last_release_stops_the_inhibitor():
    fake = FakeInhibitor()
    blocker = SleepBlocker(start_inhibitor=fake.start)

    blocker.acquire()
    blocker.release()

    assert fake.stopped == 1
    assert blocker.active is False


def test_concurrent_holders_share_one_inhibitor():
    """Two jobs running at once must not start two inhibitors, and the
    first to finish must not stop the one the second still needs."""
    fake = FakeInhibitor()
    blocker = SleepBlocker(start_inhibitor=fake.start)

    blocker.acquire()
    blocker.acquire()
    assert fake.started == 1

    blocker.release()
    assert fake.stopped == 0
    assert blocker.active is True

    blocker.release()
    assert fake.stopped == 1
    assert blocker.active is False


def test_release_without_acquire_is_a_no_op():
    """An unbalanced release must not drive the count negative, or the
    next real acquire would fail to start the inhibitor."""
    fake = FakeInhibitor()
    blocker = SleepBlocker(start_inhibitor=fake.start)

    blocker.release()

    assert fake.stopped == 0

    blocker.acquire()
    assert fake.started == 1
    assert blocker.active is True


def test_inhibitor_failure_does_not_propagate():
    """A missing caffeinate/systemd-inhibit must not take the job down
    with it. Keeping the machine awake is best-effort; running the job
    is not."""
    def boom(reason):
        raise OSError("caffeinate not found")

    blocker = SleepBlocker(start_inhibitor=boom)

    blocker.acquire()

    assert blocker.active is False
    # Must still balance cleanly, or the count leaks and a later real
    # inhibitor never starts.
    blocker.release()
    assert blocker.active is False


def test_stop_failure_does_not_propagate():
    """A handle whose stop() raises must still be dropped, otherwise the
    blocker stays permanently 'active' and never inhibits again."""
    class ExplodingHandle:
        def stop(self):
            raise OSError("process already reaped")

    blocker = SleepBlocker(start_inhibitor=lambda reason: ExplodingHandle())

    blocker.acquire()
    blocker.release()

    assert blocker.active is False


def test_concurrent_acquire_release_balances():
    """The runner acquires and releases from many worker threads."""
    import threading

    fake = FakeInhibitor()
    blocker = SleepBlocker(start_inhibitor=fake.start)
    barrier = threading.Barrier(8)

    def churn():
        barrier.wait()
        for _ in range(50):
            blocker.acquire()
            blocker.release()

    threads = [threading.Thread(target=churn) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert blocker.active is False
    assert fake.started == fake.stopped


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS caffeinate")
def test_macos_inhibitor_runs_and_stops_caffeinate():
    """Real process, not a mock: the whole point is that the OS call
    actually works, and a mocked Popen would prove nothing."""
    handle = start_platform_inhibitor("Vireo test")
    try:
        assert handle is not None
        assert handle.proc.poll() is None, "caffeinate should be running"
    finally:
        handle.stop()

    assert handle.proc.poll() is not None, "caffeinate should be reaped"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS caffeinate")
def test_macos_inhibitor_asserts_idle_sleep_only():
    """-i inhibits idle sleep. It must NOT pass -s (which would also
    block sleep on battery in ways the user did not ask for) and must
    watch our pid so a crashed Vireo cannot strand caffeinate forever."""
    import os

    handle = start_platform_inhibitor("Vireo test")
    try:
        argv = handle.proc.args
        assert "-i" in argv
        assert "-s" not in argv
        assert "-d" not in argv
        assert argv[argv.index("-w") + 1] == str(os.getpid())
    finally:
        handle.stop()
