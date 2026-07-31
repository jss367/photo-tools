"""Keep the system awake while long-running jobs are in flight.

See issue #1397. A multi-hour import on battery is suspended by macOS idle
sleep roughly ten minutes in, and an SMB-over-Tailscale mount does not
survive the repeated sleep/DarkWake cycling — the job wakes to a share that
is no longer there.

Best-effort by design: if the platform tool is missing or fails, the job
still runs. Keeping the machine awake is a convenience; completing the
user's import is not.
"""

import logging
import os
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


class _ProcessInhibitor:
    """An inhibitor backed by a child process (caffeinate, systemd-inhibit)."""

    def __init__(self, argv):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class _WindowsInhibitor:
    """ES_CONTINUOUS | ES_SYSTEM_REQUIRED for the calling thread.

    Deliberately omits ES_DISPLAY_REQUIRED: we keep the machine computing,
    we do not keep the screen lit.
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self):
        import ctypes

        self._ctypes = ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED
        )

    def stop(self):
        self._ctypes.windll.kernel32.SetThreadExecutionState(
            self.ES_CONTINUOUS
        )


def start_platform_inhibitor(reason):
    """Start an OS-level idle-sleep inhibitor and return a handle.

    Raises on failure; ``SleepBlocker`` treats that as "run unprotected".

    Every platform blocks *idle* sleep only. Closing the lid, choosing
    Sleep from the menu, or a low-battery sleep all still work — the user
    asking the machine to sleep outranks a background job.
    """
    if sys.platform == "darwin":
        # -i: prevent idle system sleep. Not -s (that also covers the
        # on-AC case we do not need) and not -d (display).
        # -w <pid>: exit when Vireo exits, so a crash cannot strand a
        # caffeinate process holding the machine awake indefinitely.
        return _ProcessInhibitor(
            ["caffeinate", "-i", "-w", str(os.getpid())]
        )
    if sys.platform == "win32":
        return _WindowsInhibitor()
    # Linux and other POSIX. --what=idle leaves lid/power-button handling
    # alone. systemd-inhibit needs a command to supervise; sleep infinity
    # holds the lock until we terminate it.
    return _ProcessInhibitor([
        "systemd-inhibit",
        "--what=idle",
        "--who=Vireo",
        f"--why={reason}",
        "--mode=block",
        "sleep", "infinity",
    ])


class SleepBlocker:
    """Refcounted idle-sleep inhibitor.

    Holders come and go; the underlying OS inhibitor starts on the first
    acquire and stops on the last release.
    """

    def __init__(self, start_inhibitor):
        self._start_inhibitor = start_inhibitor
        self._handle = None
        self._count = 0
        # ``self._count += 1`` is load/add/store, not atomic. Jobs acquire
        # and release from their own worker threads, so a lost update
        # either strands the inhibitor on forever (machine never idles) or
        # drops it while a job is still running (the bug we are fixing).
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._handle is not None

    def acquire(self):
        with self._lock:
            self._count += 1
            if self._count != 1 or self._handle is not None:
                return
            try:
                self._handle = self._start_inhibitor(
                    "Vireo is running a job"
                )
            except Exception:
                # Missing caffeinate/systemd-inhibit, denied permission,
                # unsupported platform. Log once and carry on unprotected
                # rather than failing the job.
                log.warning(
                    "Could not inhibit idle sleep; a long job may be "
                    "suspended if this machine sleeps",
                    exc_info=True,
                )
                self._handle = None

    def release(self):
        with self._lock:
            if self._count == 0:
                # Unbalanced release. Clamping at zero matters: a negative
                # count would make the next real acquire a no-op and leave
                # a long job unprotected.
                return
            self._count -= 1
            if self._count != 0:
                return
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                handle.stop()
            except Exception:
                # Already reaped, or the platform call failed. The handle
                # is dropped either way — holding onto it would leave the
                # blocker permanently "active" and stop it ever inhibiting
                # again.
                log.warning(
                    "Failed to release idle-sleep inhibitor", exc_info=True,
                )
