"""Discovery + one-time setup helpers behind the NAS setup wizard.

Pure logic with an injectable command-runner seam (the ``move.py``
pattern): production passes nothing and gets ``subprocess``; tests pass
fakes. Nothing here imports Flask or touches the database.
"""

import concurrent.futures
import ipaddress
import os
import re
import socket
import subprocess
import urllib.parse

# `mount` line: "<source> on <mount point> (<fstype>, opt, ...)".
# The source is space-free for the network filesystems we accept (smbfs/afp
# URL-encode spaces; nfs is host:/path), while the mount point may contain
# spaces — so split at the FIRST " on " (non-greedy source).
_MOUNT_RE = re.compile(r"^(?P<src>\S+?) on (?P<mp>.+) \((?P<opts>[^()]*)\)$")
# smbfs/afp source: //[user@]host/share  (URL-encoded)
_SMB_SRC_RE = re.compile(r"^//(?:(?P<user>[^@/]+)@)?(?P<host>[^/]+)/(?P<share>.+)$")
# nfs source: host:/export/path
_NFS_SRC_RE = re.compile(r"^(?P<host>[^:/]+):(?P<path>/.*)$")

_NETWORK_FS = ("smbfs", "nfs", "afpfs", "webdav")


def parse_mount_output(text):
    """Parse ``mount`` output into network-share rows the wizard can offer."""
    rows = []
    for line in text.splitlines():
        m = _MOUNT_RE.match(line.strip())
        if not m:
            continue
        fs_type = (m.group("opts").split(",")[0] or "").strip()
        if fs_type not in _NETWORK_FS:
            continue
        src, mount_point = m.group("src"), m.group("mp")
        if fs_type == "nfs":
            n = _NFS_SRC_RE.match(src)
            if not n:
                continue
            rows.append({
                "fs_type": fs_type, "host": n.group("host"),
                "share": os.path.basename(n.group("path").rstrip("/")),
                "mount_point": mount_point, "user": "",
            })
            continue
        s = _SMB_SRC_RE.match(src)
        if not s:
            continue
        unq = urllib.parse.unquote
        rows.append({
            "fs_type": fs_type, "host": unq(s.group("host")),
            "share": unq(s.group("share").rstrip("/")),
            "mount_point": mount_point, "user": unq(s.group("user") or ""),
        })
    return rows


def _reverse_dns(ip):
    """Default resolver: PTR lookup with a hard 2s cap — gethostbyaddr has
    no timeout parameter and can hang for many seconds on networks that
    drop PTR queries, which would stall the wizard's mounts endpoint."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(socket.gethostbyaddr, ip)
        return fut.result(timeout=2)[0]


def friendly_host_name(host, resolver=None):
    """Reverse-resolve an IP host to a name (Tailscale MagicDNS, mDNS, DNS);
    non-IP hosts and failed lookups pass through unchanged."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    try:
        name = (resolver or _reverse_dns)(host)
    except Exception:
        return host
    return (name or host).rstrip(".") or host


def list_network_mounts(run=subprocess.run, resolver=None):
    """Enumerate mounted network shares, each with a display-friendly host."""
    try:
        r = run(["mount"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = parse_mount_output(r.stdout or "")
    for row in rows:
        row["friendly_host"] = friendly_host_name(row["host"], resolver=resolver)
        row["display_name"] = row["friendly_host"].split(".", 1)[0]
    return rows
