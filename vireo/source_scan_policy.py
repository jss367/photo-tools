"""Storage-aware concurrency policy for import source discovery.

Classification reads the operating system's mount metadata rather than the
selected folder.  That distinction matters for a stale NAS mount: statting the
folder just to decide whether it is safe to stat can block the request that was
supposed to keep the UI responsive.
"""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import posixpath
import subprocess
import sys

import remote_setup

_FAST_LOCAL_FS = frozenset({
    "apfs", "btrfs", "ext2", "ext3", "ext4", "hfs", "hfsplus",
    "ntfs", "refs", "tmpfs", "xfs", "zfs",
})
_REMOVABLE_OR_OPTICAL_FS = frozenset({
    "cd9660", "exfat", "fat", "fat32", "msdos", "udf", "vfat",
})
_NETWORK_FS = frozenset({
    "9p", "afpfs", "cifs", "davfs", "fuse.sshfs", "nfs", "nfs4",
    "smb3", "smbfs", "sshfs", "webdav",
})


def _policy(path, volume_key, storage, max_parallel):
    return {
        "path": path,
        "volume_key": volume_key,
        "storage": storage,
        "max_parallel": max_parallel,
    }


def _unknown(paths):
    return [_policy(path, "unknown-volume", "unknown", 1) for path in paths]


def _unescape_mount_path(value):
    for escaped, literal in (
        ("\\040", " "), ("\\011", "\t"), ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, literal)
    return value


def _path_candidates(path, *, windows=False):
    # Purely lexical: normpath / abspath / expanduser do not stat path
    # components, so a stale NAS mount can't block this call. Resolving
    # symlinks with os.path.realpath would defeat that guarantee.
    path_module = ntpath if windows else posixpath
    try:
        expanded = path_module.expanduser(path) if not windows else path
        normalized = path_module.normpath(path_module.abspath(expanded))
    except (OSError, TypeError, ValueError):
        return []
    return [normalized]


def _path_under_mount(path, mount_point, *, case_insensitive=False):
    def normalize(value):
        return posixpath.normpath(value).rstrip("/") or "/"

    candidate = normalize(path)
    root = normalize(mount_point)
    if case_insensitive:
        candidate = candidate.casefold()
        root = root.casefold()
    return root == "/" or candidate == root or candidate.startswith(root + "/")


def _best_mount(path, mounts, *, case_insensitive=False):
    best = None
    for candidate in _path_candidates(path):
        for mount in mounts:
            if not _path_under_mount(
                candidate, mount["mount_point"],
                case_insensitive=case_insensitive,
            ):
                continue
            if best is None or len(mount["mount_point"]) > len(best["mount_point"]):
                best = mount
    return best


def _policy_for_filesystem(path, volume_key, fs_type):
    normalized = (fs_type or "").strip().lower()
    if normalized in _NETWORK_FS:
        return _policy(path, volume_key, "network", 1)
    if normalized in _FAST_LOCAL_FS:
        return _policy(path, volume_key, "local", 2)
    if normalized in _REMOVABLE_OR_OPTICAL_FS:
        return _policy(path, volume_key, "removable", 1)
    return _policy(path, volume_key, "unknown", 1)


def _darwin_policies(paths, run):
    try:
        result = run(
            ["mount"], capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return _unknown(paths)
    if result.returncode != 0:
        return _unknown(paths)
    mounts = remote_setup.parse_mount_table(result.stdout or "")
    if not mounts:
        return _unknown(paths)
    policies = []
    for path in paths:
        mount = _best_mount(path, mounts, case_insensitive=True)
        if mount is None:
            policies.append(_policy(path, "unknown-volume", "unknown", 1))
            continue
        fs_type = mount["fs_type"]
        # Hash the mount source so aliases of one share share a lane without
        # sending a credential-bearing SMB source string to the browser.
        identity = hashlib.sha256(
            (fs_type + "\0" + mount["source"]).encode("utf-8", "surrogatepass")
        ).hexdigest()[:16]
        if remote_setup.mount_type_is_network_or_unknown(fs_type):
            policies.append(_policy(path, "darwin:" + identity, "network", 1))
        else:
            policies.append(_policy_for_filesystem(
                path, "darwin:" + identity, fs_type,
            ))
    return policies


def _linux_mounts(mountinfo_path):
    mounts = []
    try:
        with open(mountinfo_path, encoding="utf-8") as mountinfo:
            for line in mountinfo:
                try:
                    left, right = line.rstrip("\n").split(" - ", 1)
                except ValueError:
                    continue
                left_fields = left.split()
                right_fields = right.split()
                if len(left_fields) < 5 or not right_fields:
                    continue
                mounts.append({
                    "volume_key": "linux:" + left_fields[2],
                    "mount_point": _unescape_mount_path(left_fields[4]),
                    "fs_type": right_fields[0],
                })
    except OSError:
        return []
    return mounts


def _linux_policies(paths, mountinfo_path):
    mounts = _linux_mounts(mountinfo_path)
    if not mounts:
        return _unknown(paths)
    policies = []
    for path in paths:
        mount = _best_mount(path, mounts)
        if mount is None:
            policies.append(_policy(path, "unknown-volume", "unknown", 1))
        else:
            policies.append(_policy_for_filesystem(
                path, mount["volume_key"], mount["fs_type"],
            ))
    return policies


def _unc_volume_key(unc_path):
    normalized = ntpath.normpath(unc_path or "")
    if not normalized.startswith("\\\\"):
        return None
    parts = normalized.strip("\\").split("\\")
    if len(parts) < 2:
        return None
    return "windows:unc:" + parts[0].casefold() + "/" + parts[1].casefold()


def _windows_volume(path):
    normalized = ntpath.normpath(path)
    if normalized.startswith("\\\\"):
        unc_key = _unc_volume_key(normalized)
        if unc_key is not None:
            parts = normalized.strip("\\").split("\\")
            root = f"\\\\{parts[0]}\\{parts[1]}\\"
            return root, unc_key
    drive = ntpath.splitdrive(ntpath.abspath(normalized))[0]
    if drive:
        return drive + "\\", "windows:drive:" + drive.casefold()
    return None, "unknown-volume"


def _windows_remote_share(local_name):
    # WNetGetConnectionW reads the redirector's local connection table; it
    # does not touch the share itself, so a stale NAS mount can't block it.
    try:
        wnet_get_connection = ctypes.windll.mpr.WNetGetConnectionW
    except (AttributeError, OSError):
        return None
    length = ctypes.c_ulong(1024)
    buffer = ctypes.create_unicode_buffer(length.value)
    try:
        result = wnet_get_connection(
            ctypes.c_wchar_p(local_name), buffer, ctypes.byref(length),
        )
    except OSError:
        return None
    return buffer.value if result == 0 else None


def _windows_policies(paths, get_drive_type=None, get_remote_share=None):
    if get_drive_type is None:
        try:
            win_get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        except (AttributeError, OSError):
            return _unknown(paths)

        def get_drive_type(root):
            return win_get_drive_type(ctypes.c_wchar_p(root))
    if get_remote_share is None:
        get_remote_share = _windows_remote_share
    policies = []
    for path in paths:
        root, volume_key = _windows_volume(path)
        if root is None:
            policies.append(_policy(path, volume_key, "unknown", 1))
            continue
        try:
            drive_type = int(get_drive_type(root))
        except (OSError, TypeError, ValueError):
            drive_type = 0
        if drive_type == 4:  # DRIVE_REMOTE
            # A mapped drive (Z:\) and the UNC spelling of the same share
            # (\\nas\share) must land in one lane, or selecting both defeats
            # the per-share serialization the network policy exists for.
            # Group them under the share's UNC identity, like the Darwin
            # branch groups aliases by mount source.
            if len(root) == 3 and root[1] == ":":
                try:
                    remote_key = _unc_volume_key(get_remote_share(root[:2]))
                except (OSError, TypeError, ValueError):
                    remote_key = None
                if remote_key is not None:
                    volume_key = remote_key
            policies.append(_policy(path, volume_key, "network", 1))
        elif drive_type in {3, 6}:  # DRIVE_FIXED, DRIVE_RAMDISK
            policies.append(_policy(path, volume_key, "local", 2))
        elif drive_type in {2, 5}:  # DRIVE_REMOVABLE, DRIVE_CDROM
            policies.append(_policy(path, volume_key, "removable", 1))
        else:
            policies.append(_policy(path, volume_key, "unknown", 1))
    return policies


def classify_sources(
    paths, *, platform=None, run=subprocess.run,
    mountinfo_path="/proc/self/mountinfo", get_drive_type=None,
    get_remote_share=None,
):
    """Return a conservative per-volume concurrency policy for ``paths``."""
    clean_paths = [path for path in paths if isinstance(path, str) and path]
    platform = platform or sys.platform
    if platform == "darwin":
        return _darwin_policies(clean_paths, run)
    if platform.startswith("linux"):
        return _linux_policies(clean_paths, mountinfo_path)
    if platform == "win32":
        return _windows_policies(
            clean_paths, get_drive_type=get_drive_type,
            get_remote_share=get_remote_share,
        )
    return _unknown(clean_paths)
