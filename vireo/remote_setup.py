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


def vireo_key_paths():
    """Paths of the dedicated wizard-managed keypair. Lives under ~/.vireo
    (not ~/.ssh) so setup never touches or clobbers user-managed keys."""
    priv = os.path.join(os.path.expanduser("~"), ".vireo", "ssh",
                        "vireo_ed25519")
    return priv, priv + ".pub"


def ensure_vireo_key(run=subprocess.run, ssh_keygen_bin="ssh-keygen"):
    """Generate the Vireo keypair if missing; return (private, public) paths.

    No passphrase: background rsync jobs must run unattended. The key never
    leaves this machine and grants only what the NAS account grants.
    """
    priv, pub = vireo_key_paths()
    key_dir = os.path.dirname(priv)
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    os.chmod(key_dir, 0o700)  # makedirs mode is ignored for existing dirs
    if os.path.exists(priv) and os.path.exists(pub):
        return priv, pub
    r = run([ssh_keygen_bin, "-t", "ed25519", "-N", "", "-f", priv,
             "-C", "vireo"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not os.path.exists(priv):
        detail = (getattr(r, "stderr", "") or "").strip()
        raise RuntimeError(f"ssh-keygen failed: {detail or 'unknown error'}")
    os.chmod(priv, 0o600)
    return priv, pub


def _ssh_option_args(port, key, batch=True):
    """Mirror of move.ssh_base_args for wizard probes (kept local so this
    module stays import-light; see that docstring for the option rationale).
    ``batch=False`` drops BatchMode so a password prompt can reach a pty."""
    args = []
    if batch:
        args += ["-o", "BatchMode=yes"]
    args += ["-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10"]
    try:
        if int(port or 22) != 22:
            args += ["-p", str(int(port))]
    except (TypeError, ValueError):
        pass
    if key:
        args += ["-i", key]
    return args


def key_auth_works(host, user, port, key, ssh_bin, run=subprocess.run):
    """True when passwordless (key) SSH login works right now."""
    argv = ([ssh_bin] + _ssh_option_args(port, key)
            + [f"{user}@{host}", "echo vireo_ok"])
    try:
        r = run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "vireo_ok" in (r.stdout or "")


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
