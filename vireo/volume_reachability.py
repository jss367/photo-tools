"""Bounded reachability checks for mounted volumes.

Vireo's catalog usually lives on an SMB/NFS share. When that share drops,
plain filesystem calls stop being safe: a stale mount can hang an
``os.stat`` for minutes, ``os.path.isdir`` can answer ``True`` from cached
parent metadata while every read raises ``ENOTCONN``/``EIO``, and a
background walk that trips over that turns into a failed job with a
traceback rather than a "volume offline" state the user can act on.

This module is the single place that answers *"is the volume under this
path reachable right now?"*:

* :func:`mount_root_candidates` extracts the mount-shaped prefix of a path
  (``/Volumes/<name>``, ``/mnt/<name>``, ``/media/<user>/<name>``, a drive
  letter, or a UNC share).
* :func:`network_root_reachable` is a bounded, out-of-process ``stat`` of a
  mount root (macOS). A timed-out probe is killed and reaped on a daemon
  thread so an uninterruptible filesystem call can never hold the caller.
* :class:`VolumeReachability` caches those answers for a short window so
  the navbar's polls and the new-images walk consult one gate instead of
  each touching the share, and lets a walk that hits an offline error mark
  the root offline for everyone else immediately.
* :func:`is_offline_error` classifies an ``OSError`` as "the volume went
  away" (as opposed to a per-file permission or corruption problem).

Both the pipeline (``pipeline_job``) and the Flask app (``app``) import the
moved helpers under their historical private names, so their existing call
sites and tests are unaffected.
"""
import contextlib
import errno
import logging
import os
import subprocess
import sys
import threading
import time

from proc import no_window_kwargs

log = logging.getLogger(__name__)

MOUNT_QUERY_TIMEOUT_SECS = 5
_MAX_NETWORK_PROBES = 8
_NETWORK_PROBE_RESERVED = object()
_NETWORK_PROBE_LOCK = threading.Lock()
_NETWORK_PROBES = {}

# ``OSError`` errnos that mean the *volume* is gone, not that one file is
# unreadable. ``ENOTCONN`` ("Socket is not connected") is what macOS raises
# from ``scandir`` on a dropped SMB share; ``EIO``/``ESTALE`` are the NFS and
# stale-handle shapes; the ``EHOST*``/``ENET*`` family covers the transport
# dying under an open mount. ``ENOENT`` is deliberately absent — a missing
# folder is the folder-health loop's business, not an outage.
OFFLINE_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "EIO", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "ENXIO", None),
        getattr(errno, "ENODEV", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if code is not None
)


def is_offline_error(exc):
    """Return True when ``exc`` is an ``OSError`` that means the volume
    holding the path is unreachable rather than a single entry being bad."""
    return isinstance(exc, OSError) and exc.errno in OFFLINE_ERRNOS


def mount_root_candidates(path: str, _report_confidence=None) -> list[str]:
    """Return the plausible mount-root prefix(es) for ``path``, if any.

    Extracts the mount-root component under each OS's mount conventions —
    the first entry under ``/Volumes/`` or ``/mnt/`` (SMB/NFS style), the
    first user/name pair under ``/media/<user>/``, a Windows drive letter
    (``Z:/...`` — mapped SMB drives use this), or a UNC share
    (``//server/share/...``). These shapes strongly imply the user
    intended the location as a mount point; the caller decides what state
    to require of it (missing entirely vs. present but not actually
    mounted).

    Windows mapped drives and UNC paths (Codex #1388 P2 r3663816324) are
    documented storage layouts in ``docs/WINDOWS_SUPPORT.md``; without
    detecting them, a disconnected SMB share on Windows would fall
    through to folder-scoped skips and classify would keep reissuing
    reads across the dead share instead of pausing for reconnection.

    Both the raw expanded path and the normalized absolute form are
    checked so relative or ``~``-prefixed paths still match. Duplicates
    are collapsed, and paths not shaped like a mount root return no
    candidates.
    """
    def _candidate(posix_path: str) -> str | None:
        parts = posix_path.split("/")
        if len(parts) >= 3 and parts[0] == "" and parts[1] in {"Volumes", "mnt"}:
            return f"/{parts[1]}/{parts[2]}"
        if len(parts) >= 4 and parts[0] == "" and parts[1] == "media":
            return f"/media/{parts[2]}/{parts[3]}"
        # UNC share: ``\\server\share\...`` after backslash-normalization
        # becomes ``//server/share/...``, so ``parts`` starts with two
        # empty strings.
        if (
            len(parts) >= 4
            and parts[0] == ""
            and parts[1] == ""
            and parts[2]
            and parts[3]
        ):
            return f"//{parts[2]}/{parts[3]}"
        # Windows drive letter: ``Z:\...`` after normalization becomes
        # ``Z:/...``. ``os.path.ismount("Z:")`` (no separator) returns
        # False even for a real mounted drive because Windows treats
        # ``Z:`` as a relative path on drive Z, so return with a trailing
        # separator that ismount accepts.
        if (
            parts
            and len(parts[0]) == 2
            and parts[0][1] == ":"
            and parts[0][0].isalpha()
        ):
            return f"{parts[0].upper()}/"
        return None

    raw_posix = os.path.expanduser(path).replace("\\", "/")
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    normalized_posix = normalized.replace("\\", "/")
    # Also probe the symlink-resolved form so a catalog alias like
    # ``/photos`` pointing into ``/Volumes/NAS/photos`` retains its
    # mount-shaped prefix (Codex #1388 P2 r3664891998). Resolution is
    # *lexical up to the mount root*: components are followed one at a time
    # and the walk stops as soon as the accumulated prefix is mount-shaped,
    # so no ``lstat`` ever crosses into the share itself. ``os.path.realpath``
    # would resolve every component — including ones on a dead SMB mount —
    # and can block indefinitely there, ahead of the bounded probe this
    # module exists to provide.
    # A mount-shaped prefix that is itself a symlink (``/mnt/archive`` ->
    # ``/mnt/NAS``) still has to yield the *real* mount, or the pipeline's
    # mounted-to-unmounted guards never see it. Inspecting that prefix is the
    # one lookup that may land on a dead filesystem, so the resolver does it
    # through a time-bounded helper rather than a bare ``lstat``.
    inconclusive: list[str] = []
    resolved = _resolve_symlinks_until_mount_shaped(
        normalized, _candidate, inconclusive=inconclusive,
    )
    resolved_posix = (
        resolved.replace("\\", "/") if resolved else None
    )

    seen: list[str] = []
    for source in (raw_posix, normalized_posix, resolved_posix):
        if source is None:
            continue
        cand = _candidate(source)
        if cand and cand not in seen:
            seen.append(cand)
    if _report_confidence is not None:
        _report_confidence.append(not inconclusive)
    return seen


def mount_root_resolution_conclusive(path):
    """True when every mount-shaped prefix of ``path`` could be inspected in
    time (or had a cached answer). Callers that already hold the candidate
    list use this to decide whether to trust it; False means fail closed."""
    return mount_root_candidates_checked(path)[1]


def mount_root_candidates_checked(path):
    """``(candidates, conclusive)`` — like :func:`mount_root_candidates`, but
    also says whether every mount-shaped prefix could be inspected in time.
    ``conclusive`` is False when a prefix probe timed out or the probe
    registry was saturated and no cached answer existed; the gate treats that
    as offline rather than trusting a possibly-truncated resolution."""
    confidence: list[bool] = []
    candidates = mount_root_candidates(path, _report_confidence=confidence)
    return candidates, (confidence[0] if confidence else True)



_MAX_SYMLINK_HOPS = 40


_BOUNDED_LINK_LOCK = threading.Lock()
_BOUNDED_LINK_PROBES = {}
_MAX_BOUNDED_LINK_PROBES = _MAX_NETWORK_PROBES
# Returned by ``_bounded_link_target`` when it could not get an answer (probe
# timed out, a probe for the path is still wedged, or the registry is full).
# Distinct from ``None`` ("conclusively not a symlink") so callers can fail
# closed instead of mistaking saturation for a plain directory.
INCONCLUSIVE = object()
# path -> last *conclusive* answer (target string or None). Mount-prefix
# symlink layout does not change while the process runs, so an answer
# obtained at a healthy moment stands in when a later probe is inconclusive
# — preserving the real mount behind an alias even under saturation.
_LINK_TARGET_CACHE = {}


def _bounded_link_target(path, timeout=MOUNT_QUERY_TIMEOUT_SECS):
    """``readlink(path)`` if ``path`` is a symlink, else ``None`` — bounded.

    Used only for mount-shaped prefixes (``/mnt/archive``, ``/Volumes/NAS``):
    if the prefix is a symlink it lives on the local root filesystem and the
    ``lstat`` is instant; if it is a real mount point the ``lstat`` may hang
    on a dead server, so it runs on a daemon thread and we give up after
    ``timeout`` (answering "not a link", which is also the right answer for a
    real mount). One probe per path stays registered while it is alive so
    repeated calls against a wedged mount fail fast instead of stacking
    threads. UNC prefixes never reach here (callers skip them).
    """
    with _BOUNDED_LINK_LOCK:
        existing = _BOUNDED_LINK_PROBES.get(path)
        if existing is not None:
            if existing.is_alive():
                return INCONCLUSIVE
            del _BOUNDED_LINK_PROBES[path]
        # Reap finished probes for other paths, then apply the global cap so
        # many distinct wedged prefixes cannot accumulate threads either.
        for other, thread_ in list(_BOUNDED_LINK_PROBES.items()):
            if not thread_.is_alive():
                del _BOUNDED_LINK_PROBES[other]
        if len(_BOUNDED_LINK_PROBES) >= _MAX_BOUNDED_LINK_PROBES:
            return INCONCLUSIVE
        outcome = {}

        def worker():
            try:
                if os.path.islink(path):
                    outcome["target"] = os.readlink(path)
                else:
                    outcome["target"] = None
            except OSError:
                outcome["target"] = None
            finally:
                with _BOUNDED_LINK_LOCK:
                    if _BOUNDED_LINK_PROBES.get(path) is thread:
                        del _BOUNDED_LINK_PROBES[path]

        thread = threading.Thread(
            target=worker, name="vireo-mount-link-probe", daemon=True,
        )
        _BOUNDED_LINK_PROBES[path] = thread
        thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return INCONCLUSIVE
    target = outcome.get("target")
    with _BOUNDED_LINK_LOCK:
        _LINK_TARGET_CACHE[path] = target
    return target


def _resolve_symlinks_until_mount_shaped(path, candidate, inconclusive=None):
    """Follow symlinks in ``path`` component by component, stopping early.

    Returns the (partially) resolved absolute path, or ``None`` when nothing
    could be resolved. Each accumulated prefix is tested with ``candidate``
    *before* it is ``lstat``-ed. Local components (``/``, ``/Volumes``,
    ``/photos``) are inspected directly. A mount-shaped prefix is inspected
    only through :func:`_bounded_link_target`, so a symlinked alias such as
    ``/mnt/archive -> /mnt/NAS`` is still followed to the real mount while a
    dead mount point cannot hang the caller; once a mount-shaped prefix is
    not a link the walk stops and the remainder is appended untouched. UNC
    ``//server`` prefixes are never touched. Hop count is bounded so a
    symlink loop cannot spin.

    When a mount-shaped prefix could not be inspected in time and no earlier
    conclusive answer is cached, the prefix is kept as-is and
    ``inconclusive`` (a list, if given) receives that prefix so the caller
    can fail closed rather than trust a possibly-incomplete resolution.
    """
    remaining = [part for part in path.replace("\\", "/").split("/") if part]
    if path.startswith("//"):
        prefix = "//"
    elif path.startswith("/"):
        prefix = "/"
    else:
        prefix = ""
    hops = 0
    while remaining:
        step = remaining.pop(0)
        nxt = prefix + step if prefix.endswith("/") or not prefix else prefix + "/" + step
        if nxt.startswith("//"):
            # UNC: ``//server`` and ``//server/share`` are remote lookups and
            # never symlinks; classify lexically and stop at the share.
            prefix = nxt
            if candidate(nxt.replace("\\", "/")) is not None:
                break
            continue
        if candidate(nxt.replace("\\", "/")) is not None:
            # Mount-shaped: the only lookup allowed here is the bounded one.
            target = _bounded_link_target(nxt)
            if target is INCONCLUSIVE:
                with _BOUNDED_LINK_LOCK:
                    target = _LINK_TARGET_CACHE.get(nxt, INCONCLUSIVE)
            if target is INCONCLUSIVE:
                if inconclusive is not None:
                    inconclusive.append(nxt)
                prefix = nxt
                break
            if target is None:
                prefix = nxt
                break
        else:
            try:
                is_link = os.path.islink(nxt)
            except OSError:
                is_link = False
            if not is_link:
                prefix = nxt
                continue
            try:
                target = os.readlink(nxt)
            except OSError:
                prefix = nxt
                continue
        hops += 1
        if hops > _MAX_SYMLINK_HOPS:
            return None
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(os.path.dirname(nxt), target))
        # Restart from the target: its own components may be links too.
        target_parts = [part for part in target.replace("\\", "/").split("/") if part]
        remaining = target_parts + remaining
        prefix = "//" if target.startswith("//") else ("/" if target.startswith("/") else "")
    if remaining:
        tail = "/".join(remaining)
        prefix = prefix + tail if prefix.endswith("/") else prefix + "/" + tail
    return prefix or None


def _reserve_network_probe(root):
    """Reserve a bounded probe slot, reusing one already active per root."""
    with _NETWORK_PROBE_LOCK:
        if root in _NETWORK_PROBES:
            return False
        if len(_NETWORK_PROBES) >= _MAX_NETWORK_PROBES:
            return False
        _NETWORK_PROBES[root] = _NETWORK_PROBE_RESERVED
        return True


def _release_network_probe(root, owner):
    """Release ``root`` only when it is still owned by this probe."""
    with _NETWORK_PROBE_LOCK:
        if _NETWORK_PROBES.get(root) is owner:
            _NETWORK_PROBES.pop(root, None)


def _reap_abandoned_network_probe(root, process):
    """Reap a timed-out probe away from the request path."""
    try:
        process.communicate()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        _release_network_probe(root, process)


def _abandon_network_probe(root, process):
    """Kill a timed-out probe without synchronously waiting for it."""
    with contextlib.suppress(OSError):
        process.kill()
    threading.Thread(
        target=_reap_abandoned_network_probe,
        args=(root, process),
        name="vireo-network-probe-reaper",
        daemon=True,
    ).start()


def network_root_reachable(root, timeout=MOUNT_QUERY_TIMEOUT_SECS,
                             run=None, popen=subprocess.Popen):
    """Bounded, out-of-process reachability probe for a mounted network root.

    ``mount`` listing a share and Finder reporting a file's absence are
    not sufficient signals that the underlying server is actually reachable:
    an SMB mount can remain in the kernel mount table while its server is
    unreachable, and Finder's ``exists`` query can then return false from
    cached parent metadata even though the photo will reappear on
    reconnect. Repeating the same Finder query does not detect this — it
    reuses the same cache. A ``stat`` on the mount root in a bounded
    subprocess is an *independent* signal: it does not touch Finder. On
    timeout the child is killed and reaped on a daemon thread so an
    uninterruptible filesystem call cannot hold the request thread in
    Python's usual synchronous kill-and-wait timeout cleanup. Active probes
    are reused per root and globally capped so retries cannot accumulate an
    unbounded number of stuck children and reaper threads.

    Returns ``True`` only when ``stat`` completed in time and reported the
    root as a directory. Any other outcome (timeout, non-zero exit, error)
    is treated as unreachable so the caller fails closed.
    """
    if sys.platform != "darwin":
        return False
    argv = ["/usr/bin/stat", "-f", "%HT", root]
    if run is not None:
        try:
            result = run(
                argv, capture_output=True, text=True, timeout=timeout,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            result.returncode == 0
            and (result.stdout or "").strip() == "Directory"
        )
    try:
        root_key = os.path.normcase(os.path.normpath(os.fspath(root)))
    except (TypeError, ValueError):
        return False
    if not _reserve_network_probe(root_key):
        return False

    process = None
    abandoned = False
    try:
        process = popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **no_window_kwargs(),
        )
        with _NETWORK_PROBE_LOCK:
            _NETWORK_PROBES[root_key] = process
        stdout, _stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        abandoned = True
        _abandon_network_probe(root_key, process)
        return False
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            abandoned = True
            _abandon_network_probe(root_key, process)
        else:
            _release_network_probe(root_key, _NETWORK_PROBE_RESERVED)
        return False
    finally:
        if process is not None and not abandoned:
            _release_network_probe(root_key, process)
    return process.returncode == 0 and (stdout or "").strip() == "Directory"



_GENERIC_PROBE_LOCK = threading.Lock()
_GENERIC_PROBES = {}
_MAX_GENERIC_PROBES = _MAX_NETWORK_PROBES
# root -> True if the root was a real mount point the last time it was seen
# online. A Linux mount point directory survives the share detaching, so a
# bare ``isdir`` would call a dead ``/mnt/NAS`` healthy; remembering that it
# *used to be* a mount lets the stub be recognised. Roots that were never a
# mount (an ordinary local ``/mnt/photos`` folder) stay plain directories.
_MOUNT_BASELINE = {}


def _classify_generic_root(root):
    """Return True when ``root`` is online, judged by mount state + history.

    Runs inside the bounded probe thread (both calls ``lstat``). A root that
    is a mount point now is online and recorded as such. A root that exists
    but is *not* a mount point is online only if it was never seen mounted —
    otherwise it is the stub left behind by a detached share (see
    ``pipeline_job._archive_mount_baseline`` for the same reasoning).
    """
    if not os.path.isdir(root):
        return False
    is_mount = os.path.ismount(root)
    if is_mount:
        _MOUNT_BASELINE[root] = True
        return True
    if _MOUNT_BASELINE.get(root):
        return False
    # No in-process history (cold start after the share already detached):
    # a mount-shaped directory that is not a mount point *and is empty* is
    # exactly what a detached share leaves behind, so read it as offline
    # rather than recording the stub as an ordinary local folder. A
    # populated non-mount directory is a real local root and stays online.
    try:
        with os.scandir(root) as it:
            populated = next(it, None) is not None
    except OSError:
        return False
    if not populated:
        log.warning(
            "Volume %s is an empty, unmounted mount-point directory; "
            "treating as a detached share", root,
        )
        return False
    _MOUNT_BASELINE.setdefault(root, False)
    return True


def _probe_root_generic(root, timeout):
    """Bounded mount-aware liveness check for hosts without the macOS probe.

    Runs the check on a daemon thread and gives up after ``timeout``. A
    thread stuck in an uninterruptible call cannot be killed, so it stays
    registered under its root until it returns: while it is alive the root is
    reported offline *without* spawning another thread, and the registry is
    capped globally so many wedged roots cannot pile up threads either. This
    mirrors the per-root reuse and global cap of the macOS subprocess probe.
    Non-macOS hosts hit the SMB-stall failure mode far less often, so this is
    a safety net rather than the primary path.
    """
    with _GENERIC_PROBE_LOCK:
        existing = _GENERIC_PROBES.get(root)
        if existing is not None:
            if existing.is_alive():
                return False
            del _GENERIC_PROBES[root]
        if len(_GENERIC_PROBES) >= _MAX_GENERIC_PROBES:
            return False
        outcome = {}

        def worker():
            try:
                outcome["ok"] = _classify_generic_root(root)
            except OSError:
                outcome["ok"] = False
            finally:
                with _GENERIC_PROBE_LOCK:
                    if _GENERIC_PROBES.get(root) is thread:
                        del _GENERIC_PROBES[root]

        thread = threading.Thread(
            target=worker, name="vireo-volume-probe", daemon=True,
        )
        _GENERIC_PROBES[root] = thread
        thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # Left registered: the next check for this root finds it alive and
        # fails closed instead of stacking another stuck thread.
        return False
    return bool(outcome.get("ok"))


def probe_root(root, timeout=MOUNT_QUERY_TIMEOUT_SECS):
    """Return True when the mount root answers a bounded liveness check."""
    if sys.platform == "darwin":
        return network_root_reachable(root, timeout=timeout)
    return _probe_root_generic(root, timeout)


class VolumeReachability:
    """Short-lived cache of per-mount-root reachability answers.

    ``check(path)`` resolves the path's mount root, returns the cached
    verdict when it is fresh, and otherwise runs one bounded probe. A path
    with no mount-shaped prefix (an ordinary local folder) is always
    reachable — the walk itself is the right check there and probing would
    only add latency.

    Reachable answers are cached for ``ttl_seconds``; offline answers for
    ``offline_ttl_seconds`` (matching the new-images error backoff so a
    dropped share is retried at the same cadence as before). ``mark_offline``
    lets a caller that observed an offline error mid-walk publish it, so the
    next poll fails fast instead of walking into the same dead share.
    """

    def __init__(self, ttl_seconds=30.0, offline_ttl_seconds=30.0,
                 probe=probe_root, clock=time.monotonic):
        self._ttl = float(ttl_seconds)
        self._offline_ttl = float(offline_ttl_seconds)
        self._probe = probe
        self._clock = clock
        # root -> (reachable: bool, recorded_at)
        self._verdicts = {}
        self._lock = threading.Lock()
        # Serialize probes per root so two concurrent walks over the same
        # share run one ``stat`` instead of colliding on the global probe
        # slot (which would fail the loser closed and cache a false
        # "offline" verdict).
        self._root_locks = {}

    def _root_lock(self, root):
        with self._lock:
            lock = self._root_locks.get(root)
            if lock is None:
                lock = self._root_locks[root] = threading.Lock()
            return lock

    def _fresh_verdict(self, root):
        with self._lock:
            entry = self._verdicts.get(root)
            if entry is None:
                return None
            reachable, recorded_at = entry
            ttl = self._ttl if reachable else self._offline_ttl
            if self._clock() - recorded_at > ttl:
                del self._verdicts[root]
                return None
            return reachable

    def root_reachable(self, root):
        """Return the (cached or freshly probed) verdict for a mount root."""
        cached = self._fresh_verdict(root)
        if cached is not None:
            return cached
        with self._root_lock(root):
            cached = self._fresh_verdict(root)
            if cached is not None:
                return cached
            try:
                reachable = bool(self._probe(root))
            except Exception:
                log.exception("volume probe raised for %s; treating as offline", root)
                reachable = False
            with self._lock:
                self._verdicts[root] = (reachable, self._clock())
            if not reachable:
                log.warning("Volume %s is not reachable", root)
            return reachable

    def check(self, path):
        """Return ``(mount_root, reachable)`` for ``path``.

        ``mount_root`` is ``None`` (and ``reachable`` True) for paths that
        are not on a mount-shaped location. When several candidate roots
        apply (an alias resolving into a share), the first offline one is
        returned so callers can name it.
        """
        candidates, conclusive = mount_root_candidates_checked(path)
        if not candidates:
            return None, True
        if not conclusive:
            # A mount-shaped prefix could not even be inspected in time: the
            # only safe reading is that the volume is not reachable.
            log.warning(
                "Volume prefix of %s could not be inspected in time; "
                "treating as offline", path,
            )
            return candidates[0], False
        for root in candidates:
            if not self.root_reachable(root):
                return root, False
        return candidates[0], True

    def mark_offline(self, root):
        """Record that ``root`` was just observed offline (e.g. ``ENOTCONN``
        mid-walk) so subsequent checks fail fast within the backoff window."""
        if not root:
            return
        with self._lock:
            self._verdicts[root] = (False, self._clock())
        log.warning("Volume %s went offline during a filesystem walk", root)

    def clear(self):
        with self._lock:
            self._verdicts.clear()


_shared = VolumeReachability()


def get_shared():
    """Process-wide gate shared by the navbar probe, walks, and scans."""
    return _shared
