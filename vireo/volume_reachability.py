"""Bounded reachability checks for mounted volumes.

Vireo's catalog usually lives on an SMB/NFS share. When that share drops,
plain filesystem calls stop being safe: a stale mount can hang an
``os.stat`` for minutes, ``os.path.isdir`` can answer ``True`` from cached
parent metadata while every read raises ``ENOTCONN``/``EIO``, and a
background walk that trips over that turns into a failed job with a
traceback rather than a "volume offline" state the user can act on.

This module is the single place that answers *"is the volume under this
path reachable right now?"*:

* :func:`mount_root_candidates` extracts a path's mount prefix from safe OS
  mount metadata, durable observations, or conventional mount shapes
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
import json
import logging
import ntpath
import os
import posixpath
import subprocess
import sys
import threading
import time

from image_loader import is_excluded_scan_dir
from proc import no_window_kwargs

log = logging.getLogger(__name__)

# ``os.path`` is the shared ``ntpath`` module on Windows. Tests and callers
# may monkeypatch ``os.path.normpath`` for path-shape isolation, so retain the
# native UNC normalizer before that shared module can be modified.
_NT_NORMPATH = ntpath.normpath

MOUNT_QUERY_TIMEOUT_SECS = 5
_MAX_NETWORK_PROBES = 8
_NETWORK_PROBE_RESERVED = object()
_NETWORK_PROBE_LOCK = threading.Lock()
_NETWORK_PROBE_CONDITION = threading.Condition(_NETWORK_PROBE_LOCK)
_NETWORK_PROBES = {}
_ABANDONED_NETWORK_PROBES = set()

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
# Native Windows filesystem errors whose POSIX ``errno`` translation can be
# ENOENT/EINVAL even though the real failure is a disconnected mapped drive or
# UNC share. Values are WinError.h's ERROR_BAD_NETPATH, ERROR_DEV_NOT_EXIST,
# ERROR_UNEXP_NET_ERR, ERROR_NETNAME_DELETED, and ERROR_BAD_NET_NAME.
OFFLINE_WINERRORS = frozenset({53, 55, 59, 64, 67})


def is_offline_error(exc):
    """Return True when ``exc`` is an ``OSError`` that means the volume
    holding the path is unreachable rather than a single entry being bad."""
    return isinstance(exc, OSError) and (
        exc.errno in OFFLINE_ERRNOS
        or getattr(exc, "winerror", None) in OFFLINE_WINERRORS
    )


def _mount_shaped_candidate(posix_path: str) -> str | None:
    """Mount-shaped root implied by ``posix_path`` (see :func:`mount_root_candidates`)."""
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


_MOUNT_TABLE_LOCK = threading.Lock()
_MOUNT_TABLE_CACHE = (float("-inf"), frozenset())
_MOUNT_TABLE_TTL_SECONDS = 30.0


def _unescape_linux_mount_path(value):
    """Decode the pathname escapes used by ``/proc/self/mountinfo``."""
    for escaped, literal in (
        ("\\040", " "), ("\\011", "\t"), ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, literal)
    return value


def _linux_mount_table_roots(mountinfo_path="/proc/self/mountinfo"):
    """Read live mount points from kernel metadata, never from the mounts."""
    roots = set()
    try:
        with open(mountinfo_path, encoding="utf-8") as mountinfo:
            for line in mountinfo:
                left = line.rstrip("\n").partition(" - ")[0].split()
                if len(left) >= 5:
                    roots.add(_unescape_linux_mount_path(left[4]))
    except OSError:
        return set()
    return roots


def _darwin_mount_table_roots(run=subprocess.run):
    """Read macOS's mount table with a timeout and return its mount points."""
    try:
        result = run(
            ["/sbin/mount"], capture_output=True, text=True,
            timeout=MOUNT_QUERY_TIMEOUT_SECS, **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    # Keep the platform-specific parser in one place. Importing lazily avoids
    # adding the NAS setup module to startup paths that never query mounts.
    import remote_setup

    return {
        row["mount_point"]
        for row in remote_setup.parse_mount_table(result.stdout or "")
        if row.get("mount_point")
    }


def _system_mount_roots(clock=time.monotonic, force_refresh=False):
    """Cached live mount roots obtained without touching mounted filesystems."""
    global _MOUNT_TABLE_CACHE

    now = clock()
    with _MOUNT_TABLE_LOCK:
        recorded_at, roots = _MOUNT_TABLE_CACHE
        if not force_refresh and now - recorded_at <= _MOUNT_TABLE_TTL_SECONDS:
            return set(roots)
    if sys.platform.startswith("linux"):
        fresh = _linux_mount_table_roots()
    elif sys.platform == "darwin":
        fresh = _darwin_mount_table_roots()
    else:
        fresh = set()
    # The root filesystem is not a useful boundary: treating it as one would
    # classify every ordinary absolute path as a volume-backed location.
    fresh = {
        posixpath.normpath(root.replace("\\", "/"))
        for root in fresh
        if isinstance(root, str) and root and root != "/"
    }
    with _MOUNT_TABLE_LOCK:
        _MOUNT_TABLE_CACHE = (now, frozenset(fresh))
    return fresh


def _known_mount_boundaries():
    """Live and historical custom mount roots safe to use as boundaries."""
    # Drive letters, UNC shares, and junctions are handled by Windows-native
    # logic below. POSIX-normalizing their persisted spellings would turn
    # ``Z:/`` into the invalid root ``Z:`` and create a duplicate candidate.
    if sys.platform == "win32":
        return set()
    live = _system_mount_roots()
    with _MOUNT_BASELINE_LOCK:
        for root in live:
            _MOUNT_BASELINE[root] = True
        known = {
            posixpath.normpath(root.replace("\\", "/"))
            for root, mounted in _MOUNT_BASELINE.items()
            if mounted and isinstance(root, str) and root
        }
    return live | known


def _custom_mount_candidate(posix_path, boundaries):
    normalized = posixpath.normpath(posix_path)
    return normalized if normalized in boundaries else None


def _deepest_custom_mount(posix_path, boundaries):
    normalized = posixpath.normpath(posix_path)
    matches = [
        root for root in boundaries
        if normalized == root or normalized.startswith(root.rstrip("/") + "/")
    ]
    return max(matches, key=len, default=None)


def _normalize_candidate_source(source):
    """Lexically normalize while preserving a UNC path's share anchor."""
    posix_source = source.replace("\\", "/")
    if posix_source.startswith("//"):
        # POSIX normalization allows ``..`` to climb from //server/share to
        # //server, inventing a sibling share. Windows keeps the share as the
        # anchor, which is the only meaningful interpretation of a UNC path.
        return _NT_NORMPATH(posix_source).replace("\\", "/")
    return posixpath.normpath(posix_source)


class MountRootCandidates(list):
    """Mount-root candidates plus whether the resolution was conclusive.

    A plain ``list`` for every existing caller; ``conclusive`` is False when a
    mount-shaped prefix could not be inspected in time and no cached answer
    existed. Carrying the flag on the same object means callers never pair
    candidates from one resolution with confidence from another.
    """

    conclusive = True


def mount_root_candidates(path: str) -> "MountRootCandidates":
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

    Custom POSIX locations are also matched against the OS mount table and
    durable roots previously observed live, so paths such as ``/srv/photos``
    or ``~/mnt/photos`` receive the same bounded treatment. Both the raw
    expanded path and the normalized absolute form are checked so relative
    or ``~``-prefixed paths still match. Duplicates are collapsed.
    """
    expanded = os.path.expanduser(path)
    raw_posix = expanded.replace("\\", "/")
    if raw_posix.startswith("//"):
        normalized_posix = _normalize_candidate_source(raw_posix)
        normalized = normalized_posix
    else:
        normalized = os.path.normpath(os.path.abspath(expanded))
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
    boundaries = _known_mount_boundaries()

    def candidate(source):
        return (
            _mount_shaped_candidate(source)
            or _custom_mount_candidate(source, boundaries)
        )

    inconclusive: list[str] = []
    resolved = _resolve_symlinks_until_mount_shaped(
        normalized, candidate, inconclusive=inconclusive,
    )
    resolved_posix = (
        resolved.replace("\\", "/") if resolved else None
    )

    seen = MountRootCandidates()
    for source in (raw_posix, normalized_posix, resolved_posix):
        if source is None:
            continue
        # The raw expanded form is retained to preserve UNC / drive syntax,
        # but it may contain ``.`` or ``..``. Collapse those lexically before
        # extracting roots so an unrelated volume named before ``..`` is not
        # probed (and cannot make a valid folder look offline).
        source = _normalize_candidate_source(source)
        for cand in (
            _mount_shaped_candidate(source),
            _deepest_custom_mount(source, boundaries),
        ):
            if cand and cand not in seen:
                seen.append(cand)
    # A timeout on a prefix not present in mount metadata is itself the only
    # safe candidate: carrying it lets every caller fail closed instead of
    # interpreting an empty list as an ordinary reachable local path.
    if not seen:
        for unresolved_prefix in inconclusive:
            if unresolved_prefix not in seen:
                seen.append(unresolved_prefix)
    seen.conclusive = not inconclusive
    return seen


def resolve_alias_lexically(path):
    """Symlink-resolved form of ``path`` that never looks below a mount root.

    Local components are followed like ``realpath`` would; a known mount
    prefix is inspected only through the bounded probe and everything after
    it is appended untouched. Callers use this to apply *name-based* rules
    (bundle exclusion) to an alias such as ``~/PhotoLib`` that points into a
    share, without any filesystem access on the share itself. Falls back to
    the normalized input when nothing could be resolved.
    """
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    boundaries = _known_mount_boundaries()
    resolved = _resolve_symlinks_until_mount_shaped(
        normalized,
        lambda source: (
            _mount_shaped_candidate(source)
            or _custom_mount_candidate(source, boundaries)
        ),
    )
    return resolved or normalized


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
    candidates = mount_root_candidates(path)
    return list(candidates), getattr(candidates, "conclusive", True)



_MAX_SYMLINK_HOPS = 40


_BOUNDED_LINK_LOCK = threading.Lock()
_BOUNDED_LINK_CONDITION = threading.Condition(_BOUNDED_LINK_LOCK)
_BOUNDED_LINK_PROBES = {}
_ABANDONED_LINK_PROBES = set()
_MAX_BOUNDED_LINK_PROBES = _MAX_NETWORK_PROBES
# Returned by ``_bounded_link_target`` when it could not get an answer (probe
# timed out, a probe for the path is still wedged, or the registry is full).
# Distinct from ``None`` ("conclusively not a symlink") so callers can fail
# closed instead of mistaking saturation for a plain directory.
INCONCLUSIVE = object()
# path -> last *conclusive* answer (target string or None). An answer obtained
# at a healthy moment stands in when a later probe is inconclusive, preserving
# the real mount behind an alias even under saturation.
_LINK_TARGET_CACHE = {}


def _bounded_link_target(path, timeout=MOUNT_QUERY_TIMEOUT_SECS):
    """``readlink(path)`` if ``path`` is a symlink, else ``None`` — bounded.

    Every non-UNC path prefix uses this helper. Known mount metadata is not
    guaranteed to be available, and a custom mount may already be stalled
    before this process first observes it, so assuming an unknown prefix is
    local would put an unbounded ``lstat`` ahead of the reachability gate.
    One probe per path stays registered while it is alive so repeated calls
    against a wedged prefix fail fast instead of stacking threads. UNC
    prefixes never reach here (callers skip them).
    """
    with _BOUNDED_LINK_CONDITION:
        existing = _BOUNDED_LINK_PROBES.get(path)
        if existing is not None:
            if existing.is_alive():
                deadline = time.monotonic() + max(0, timeout)
                while _BOUNDED_LINK_PROBES.get(path) is existing:
                    if path in _ABANDONED_LINK_PROBES:
                        return INCONCLUSIVE
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return INCONCLUSIVE
                    _BOUNDED_LINK_CONDITION.wait(remaining)
                return _LINK_TARGET_CACHE.get(path, INCONCLUSIVE)
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
                if _is_link_or_junction(path):
                    outcome["target"] = os.readlink(path)
                else:
                    outcome["target"] = None
            except OSError:
                outcome["inconclusive"] = True
            finally:
                with _BOUNDED_LINK_CONDITION:
                    if not outcome.get("inconclusive"):
                        _LINK_TARGET_CACHE[path] = outcome.get("target")
                    if _BOUNDED_LINK_PROBES.get(path) is thread:
                        del _BOUNDED_LINK_PROBES[path]
                    _ABANDONED_LINK_PROBES.discard(path)
                    _BOUNDED_LINK_CONDITION.notify_all()

        thread = threading.Thread(
            target=worker, name="vireo-mount-link-probe", daemon=True,
        )
        _BOUNDED_LINK_PROBES[path] = thread
        thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with _BOUNDED_LINK_CONDITION:
            if _BOUNDED_LINK_PROBES.get(path) is thread:
                _ABANDONED_LINK_PROBES.add(path)
                _BOUNDED_LINK_CONDITION.notify_all()
        return INCONCLUSIVE
    if outcome.get("inconclusive"):
        return INCONCLUSIVE
    target = outcome.get("target")
    return target


def _is_drive_root(prefix):
    """``C:`` / ``C:/`` — a local Windows drive root."""
    p = prefix.replace("\\", "/").rstrip("/")
    return len(p) == 2 and p[1] == ":" and p[0].isalpha()


_DRIVE_REMOTE = 4  # winbase.h DRIVE_REMOTE


def _drive_is_remote(drive):
    """True when a Windows drive letter is a mapped network drive (or its type
    cannot be determined). Uses ``GetDriveTypeW``, which asks the mount
    manager and does not touch the drive itself. Always False off Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        root = drive.replace("/", "\\").rstrip("\\") + "\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(root)  # type: ignore[attr-defined]
    except Exception:
        return True
    # DRIVE_UNKNOWN (0) / DRIVE_NO_ROOT_DIR (1) are not provably local:
    # fail closed and treat them like a remote mount.
    return kind in (0, 1, _DRIVE_REMOTE)


def _is_link_or_junction(path):
    """``islink`` that also recognises Windows directory junctions, which
    ``os.path.islink`` does not report but ``os.readlink`` can follow."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _normalize_link_target(target):
    """Strip the Windows extended-path prefix from a ``readlink`` result.

    A junction target comes back as the extended form (question-mark prefix,
    optionally followed by ``UNC``); the resolver wants plain ``C:/...`` or
    ``//server/share/...``.
    """
    t = target.replace("\\", "/")
    if t.startswith("//?/UNC/"):
        return "//" + t[len("//?/UNC/"):]
    if t.startswith("//?/"):
        return t[len("//?/"):]
    return t


def _contains_excluded_scan_component(path):
    """True when ``path`` names an app-managed bundle component."""
    return any(
        is_excluded_scan_dir(part)
        for part in path.replace("\\", "/").split("/")
        if part
    )


def _resolve_symlinks_until_mount_shaped(path, candidate, inconclusive=None):
    """Follow symlinks in ``path`` component by component, stopping early.

    Returns the (partially) resolved absolute path, or ``None`` when nothing
    could be resolved. Each accumulated prefix is tested with ``candidate``
    *before* it is ``lstat``-ed. Every non-UNC component is inspected only
    through :func:`_bounded_link_target`, so a symlinked alias such as
    ``/mnt/archive -> /mnt/NAS`` is still followed to the real mount while a
    dead mount point cannot hang the caller; once a mount-shaped prefix is
    not a link the walk stops and the remainder is appended untouched. UNC
    ``//server`` prefixes are never touched. Hop count is bounded so a
    symlink loop cannot spin.

    When a prefix could not be inspected in time, ``inconclusive`` (a list,
    if given) receives that prefix so the caller can fail closed. A cached
    target may still complete alias resolution, but never upgrades the
    current lookup's confidence.
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
        if _is_drive_root(nxt):
            prefix = nxt
            if _drive_is_remote(nxt):
                # A mapped network drive (``Z:``) *is* the mount root. Stop
                # here: every component below it lives on the share, and
                # even an ``lstat`` there can block while the server is
                # away. The drive type comes from the mount manager, not
                # from touching the drive.
                break
            # A local drive (``C:``) is where traversal *starts*, not a
            # terminal mount: junctions below it (``C:\Photos`` ->
            # ``\\server\share``) still have to be followed so the real
            # remote root is reported. Drive roots themselves are never
            # links, so no lookup is needed here.
            continue
        # Never lstat an app-managed bundle or anything below it. Merely
        # touching these components can trigger macOS's recurring TCC prompt.
        # This also catches components introduced by a symlink target because
        # target resolution restarts this same component-by-component loop.
        if _contains_excluded_scan_component(nxt):
            prefix = nxt
            break
        cand = candidate(nxt.replace("\\", "/"))
        target = _bounded_link_target(nxt)
        if target is INCONCLUSIVE:
            if inconclusive is not None:
                inconclusive.append(nxt)
            with _BOUNDED_LINK_LOCK:
                target = _LINK_TARGET_CACHE.get(nxt, INCONCLUSIVE)
        if target is INCONCLUSIVE:
            prefix = nxt
            break
        if target is None:
            prefix = nxt
            if cand is not None and not _is_drive_root(cand):
                break
            continue
        target = _normalize_link_target(target)
        hops += 1
        if hops > _MAX_SYMLINK_HOPS:
            return None
        if not (os.path.isabs(target) or target.startswith("//") or _is_drive_root(target[:2])):
            target = os.path.normpath(os.path.join(os.path.dirname(nxt), target)).replace("\\", "/")
        # Restart from the target: its own components may be links too.
        target_parts = [part for part in target.replace("\\", "/").split("/") if part]
        remaining = target_parts + remaining
        prefix = "//" if target.startswith("//") else ("/" if target.startswith("/") else "")
    if remaining:
        tail = "/".join(remaining)
        prefix = prefix + tail if prefix.endswith("/") else prefix + "/" + tail
    return prefix or None


def _reserve_network_probe(root, wait_timeout=0):
    """Reserve a slot, waiting boundedly for healthy same-root contention."""
    deadline = time.monotonic() + max(0, wait_timeout)
    with _NETWORK_PROBE_CONDITION:
        while root in _NETWORK_PROBES:
            # A timed-out process may stay in uninterruptible I/O while its
            # reaper waits. It already proved the root unhealthy, so retries
            # fail immediately instead of waiting another timeout.
            if root in _ABANDONED_NETWORK_PROBES:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _NETWORK_PROBE_CONDITION.wait(remaining)
        if len(_NETWORK_PROBES) >= _MAX_NETWORK_PROBES:
            return False
        _NETWORK_PROBES[root] = _NETWORK_PROBE_RESERVED
        return True


def _release_network_probe(root, owner):
    """Release ``root`` only when it is still owned by this probe."""
    with _NETWORK_PROBE_CONDITION:
        if _NETWORK_PROBES.get(root) is owner:
            _NETWORK_PROBES.pop(root, None)
            _ABANDONED_NETWORK_PROBES.discard(root)
            _NETWORK_PROBE_CONDITION.notify_all()


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
    with _NETWORK_PROBE_CONDITION:
        if _NETWORK_PROBES.get(root) is process:
            _ABANDONED_NETWORK_PROBES.add(root)
            _NETWORK_PROBE_CONDITION.notify_all()
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
    return _bounded_process_probe(
        ["/usr/bin/stat", "-f", "%HT", root], root, timeout,
        accept=lambda out: (out or "").strip() == "Directory",
        run=run, popen=popen,
    )


def _bounded_process_probe(argv, root, timeout, accept, run=None,
                           popen=subprocess.Popen):
    """Run ``argv`` out of process with the probe registry's bounds and return
    ``accept(stdout)`` only if it completed in time with exit status 0."""
    if run is not None:
        try:
            result = run(
                argv, capture_output=True, text=True, timeout=timeout,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool(accept(result.stdout))
    try:
        root_key = os.path.normcase(os.path.normpath(os.fspath(root)))
    except (TypeError, ValueError):
        return False
    if not _reserve_network_probe(root_key, wait_timeout=timeout):
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
    return process.returncode == 0 and bool(accept(stdout))



_GENERIC_PROBE_LOCK = threading.Lock()
_GENERIC_PROBES = {}
_MAX_GENERIC_PROBES = _MAX_NETWORK_PROBES
# root -> True if the root was a real mount point the last time it was seen
# online. A Linux mount point directory survives the share detaching, so a
# bare ``isdir`` would call a dead ``/mnt/NAS`` healthy; remembering that it
# *used to be* a mount lets the stub be recognised. Roots that were never a
# mount (an ordinary local ``/mnt/photos`` folder) stay plain directories.
_MOUNT_BASELINE = {}
_MOUNT_BASELINE_LOCK = threading.Lock()

# Historical db_meta key used by pipeline mount guards. The reachability gate
# now shares it so a new process can distinguish a readable detached stub from
# a legitimate empty local directory without inventing a second persistence
# format.
KNOWN_MOUNT_ROOTS_KEY = "known_archive_mount_roots"


def load_known_mount_roots(db) -> set[str]:
    """Return mount roots previously observed live in this catalog."""
    try:
        row = db.conn.execute(
            "SELECT value FROM db_meta WHERE key = ?",
            (KNOWN_MOUNT_ROOTS_KEY,),
        ).fetchone()
    except Exception:
        return set()
    if row is None or row["value"] is None:
        return set()
    try:
        entries = json.loads(row["value"])
    except (TypeError, ValueError):
        return set()
    if not isinstance(entries, list):
        return set()
    return {str(entry) for entry in entries if isinstance(entry, str)}


def record_known_mount_roots(db, baseline: dict[str, bool]) -> None:
    """Persist newly observed live roots, preserving all prior evidence."""
    fresh = {root for root, live in baseline.items() if live}
    if not fresh:
        return
    transaction_started = False
    try:
        # This helper has always committed at the end, so preserving pending
        # caller DML is part of its existing contract. End an implicit legacy
        # sqlite3 transaction before opening the explicit writer transaction.
        if db.conn.in_transaction:
            db.conn.commit()
        # Acquire SQLite's writer reservation before reading the JSON value.
        # Concurrent Database connections then serialize the complete
        # read/merge/write sequence instead of both reading the same old set
        # and letting the later UPSERT discard the other's newly seen root.
        db.conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        existing = load_known_mount_roots(db)
        merged = existing | fresh
        if merged == existing:
            db.conn.commit()
            return
        db.conn.execute(
            "INSERT INTO db_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (KNOWN_MOUNT_ROOTS_KEY, json.dumps(sorted(merged))),
        )
        db.conn.commit()
    except Exception:
        if transaction_started:
            with contextlib.suppress(Exception):
                db.conn.rollback()
        log.debug(
            "Could not persist known mount roots (%r); cold-start detached "
            "stub detection will lack that evidence next run.",
            sorted(fresh), exc_info=True,
        )


def seed_known_mount_roots(roots) -> None:
    """Install durable mounted-root evidence before bounded probes run."""
    with _MOUNT_BASELINE_LOCK:
        for root in roots:
            if isinstance(root, str) and root:
                _MOUNT_BASELINE[root] = True


def was_observed_mounted(root) -> bool:
    """Whether ``root`` is durably known or was mounted in this process."""
    with _MOUNT_BASELINE_LOCK:
        return bool(_MOUNT_BASELINE.get(root))


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
    # Read the directory: this is the one operation here that has to reach
    # the server. ``isdir``/``ismount`` can both succeed from cached metadata
    # on an NFS/SMB share whose server is gone; a listing cannot. It runs
    # inside the bounded probe thread, so a hang becomes a timeout (offline)
    # and an ``EIO``/``ENOTCONN`` becomes offline directly.
    try:
        with os.scandir(root) as it:
            next(it, None)
    except OSError:
        return False
    with _MOUNT_BASELINE_LOCK:
        if is_mount:
            _MOUNT_BASELINE[root] = True
            return True
        if _MOUNT_BASELINE.get(root):
            return False
        # With no durable or in-process mount history, a readable unmounted
        # directory is a valid local root even when empty. Emptiness alone
        # cannot distinguish it from a detached mount stub.
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


def _darwin_listing_is_directory(stdout):
    """``ls -1f`` prints ``.``/``..`` for a directory; a file prints itself."""
    lines = {line.strip() for line in (stdout or "").splitlines()}
    return "." in lines or ".." in lines


def _probe_root_darwin(root, timeout=MOUNT_QUERY_TIMEOUT_SECS,
                       run=None, popen=subprocess.Popen):
    """Out-of-process, time-bounded *directory read* of a mount root.

    ``stat`` alone (see :func:`network_root_reachable`) can succeed from
    cached attributes on a disconnected SMB mount without reaching the
    server. Enumerating the directory cannot, so the probe runs ``ls -1f``
    on the root and accepts only a listing that shows it is a directory.
    Same kill-and-reap machinery, per-root reuse, and global cap as the
    ``stat`` probe.
    """
    return _bounded_process_probe(
        ["/bin/ls", "-1", "-f", root], root, timeout,
        accept=_darwin_listing_is_directory, run=run, popen=popen,
    )


def probe_root(root, timeout=MOUNT_QUERY_TIMEOUT_SECS):
    """Return True when the mount root answers a bounded liveness check."""
    if sys.platform == "darwin":
        if not _probe_root_darwin(root, timeout=timeout):
            return False
        if not was_observed_mounted(root):
            return True
        live_roots = _system_mount_roots(force_refresh=True)
        normalized_root = posixpath.normpath(root.replace("\\", "/"))
        return normalized_root in live_roots
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
        returned so callers can name it. On success the deepest resolved
        candidate is returned, so a later mid-walk outage invalidates the
        canonical mount rather than only the alias used for this path.
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
        return candidates[-1], True

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
