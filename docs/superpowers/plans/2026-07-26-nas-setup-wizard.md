# NAS Setup Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A guided Settings-page wizard that takes a user from a Finder-mounted NAS share to a tested, saved remote target — auto-detecting mounts, installing an SSH key, and proving the NAS-side path with a nonce probe — without Terminal or hand-typed absolute paths.

**Architecture:** One new pure-logic module `vireo/remote_setup.py` with an injectable command-runner seam (mirroring `move.py`), six new synchronous `/api/remote-setup/*` endpoints in `app.py`, a wizard modal inline in `settings.html` (project convention: one file per page, inline JS), and a deep-link from the Import page's "Move to NAS unavailable" hint. Spec: `docs/superpowers/specs/2026-07-26-nas-setup-wizard-design.md`.

**Tech Stack:** Python 3 / Flask, `pty` + `select` for the password-driven key install, vanilla JS + existing `safeFetch`, pytest with the `app_and_db` fixture (`vireo/tests/conftest.py`).

**One deliberate refinement vs. the spec:** the key install drives plain `ssh` through a pty (running an idempotent `authorized_keys` append snippet) instead of `ssh-copy-id`. Same mechanism, same UX, but it reuses the already-configurable `ssh` binary (`move.resolve_ssh_bin`) — one fewer external tool to locate, and the e2e/stub seam is the binary path itself. The spec is amended alongside this plan.

**Working rules for every task:** TDD (test first, watch it fail, minimal code, watch it pass, commit). Run tests with `python -m pytest <file> -q` from the repo root. All new backend code goes through the injectable runner — no test may spawn a real `ssh`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `vireo/remote_setup.py` | Create | Mount parsing, friendly-name resolution, Vireo key management, pty key install, nonce share-locate, remote dir listing. Pure logic, injectable runner, no Flask imports. |
| `vireo/tests/test_remote_setup.py` | Create | Unit tests for the module (fake runners, stub pty script). |
| `vireo/tests/test_remote_setup_api.py` | Create | Endpoint tests via `app_and_db` fixture + monkeypatched `remote_setup`. |
| `vireo/app.py` | Modify | Six `/api/remote-setup/*` routes + loopback guard (near `api_remote_target_test`, ~line 22134). |
| `vireo/templates/settings.html` | Modify | "Set up from mounted volume…" button + wizard modal + inline JS (near the remote-targets section, ~line 867, and the editor JS, ~line 1566). |
| `vireo/templates/import.html` | Modify | Turn the "Move to NAS unavailable…" hint (`updateAfterMoveUI`, ~line 1467) into a link to the wizard. |
| `tests/e2e/test_nas_setup_wizard.py` | Create | Wizard walk-through with `remote_setup` monkeypatched in the in-process server. |

### Runner seam (used everywhere)

`remote_setup.py` functions take `run=subprocess.run` keyword args (the `move.py` pattern: real subprocess in production, a fake callable in tests returning `subprocess.CompletedProcess`-shaped objects). The pty driver takes `spawn_argv` (list) so tests point it at a stub script instead of real ssh.

---

### Task 1: Mount enumeration — parsing

**Files:**
- Create: `vireo/remote_setup.py`
- Create: `vireo/tests/test_remote_setup.py`

- [ ] **Step 1: Write failing tests** for `parse_mount_output(text)` → list of dicts `{fs_type, host, share, mount_point, user}`. Fixtures (real `mount` output shapes):

```python
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import remote_setup


SMB = "//julius_admin@100.80.236.59/Photography on /Volumes/Photography (smbfs, nodev, nosuid, mounted by julius)"
SMB_SPACES = "//guest@My%20NAS._smb._tcp.local/Photo%20Library on /Volumes/Photo Library (smbfs, nodev, nosuid, mounted by julius)"
NFS = "truenas:/mnt/tank/photos on /Volumes/photos (nfs, nodev, nosuid, mounted by julius)"
LOCAL = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)"
AUTOFS = "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)"


def test_parse_smbfs_mount():
    rows = remote_setup.parse_mount_output(SMB + "\n" + LOCAL + "\n" + AUTOFS)
    assert rows == [{
        "fs_type": "smbfs", "host": "100.80.236.59",
        "share": "Photography", "mount_point": "/Volumes/Photography",
        "user": "julius_admin",
    }]


def test_parse_smbfs_url_encoding_and_spaces():
    (row,) = remote_setup.parse_mount_output(SMB_SPACES)
    assert row["share"] == "Photo Library"
    assert row["host"] == "My NAS._smb._tcp.local"
    assert row["mount_point"] == "/Volumes/Photo Library"
    assert row["user"] == "guest"


def test_parse_nfs_mount():
    (row,) = remote_setup.parse_mount_output(NFS)
    assert row == {"fs_type": "nfs", "host": "truenas",
                   "share": "photos", "mount_point": "/Volumes/photos",
                   "user": ""}


def test_parse_ignores_non_network_and_garbage():
    assert remote_setup.parse_mount_output(LOCAL + "\nnot a mount line\n") == []


def test_parse_afpfs_and_ipv6_hosts():
    afp = "//julius@mynas._afpovertcp._tcp.local/Media on /Volumes/Media (afpfs, nodev, nosuid, mounted by julius)"
    v6 = "//admin@[fe80::1%25en0]/Backup on /Volumes/Backup (smbfs, nodev, nosuid, mounted by julius)"
    rows = remote_setup.parse_mount_output(afp + "\n" + v6)
    assert rows[0]["fs_type"] == "afpfs" and rows[0]["share"] == "Media"
    assert rows[1]["host"] == "[fe80::1%en0]" and rows[1]["share"] == "Backup"
```

- [ ] **Step 2: Run** `python -m pytest vireo/tests/test_remote_setup.py -q` — expect FAIL (`ModuleNotFoundError` / `AttributeError`).

- [ ] **Step 3: Implement** in `vireo/remote_setup.py`:

```python
"""Discovery + one-time setup helpers behind the NAS setup wizard.

Pure logic with an injectable command-runner seam (the ``move.py``
pattern): production passes nothing and gets ``subprocess``; tests pass
fakes. Nothing here imports Flask or touches the database.
"""

import os
import re
import subprocess
import urllib.parse

# `mount` line: "<source> on <mount point> (<fstype>, opt, ...)".
# The mount point may contain spaces, so anchor on the LAST " on " before
# a trailing "(...)" group instead of splitting naively.
_MOUNT_RE = re.compile(r"^(?P<src>.+) on (?P<mp>.+) \((?P<opts>[^()]*)\)$")
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
```

- [ ] **Step 4: Run tests** — expect PASS.
- [ ] **Step 5: Commit** `feat: mount-output parsing for NAS setup wizard`.

---

### Task 2: Mount enumeration — listing + friendly names

**Files:** same two.

- [ ] **Step 1: Failing tests** for `list_network_mounts(run=...)` (invokes `["mount"]`, parses, attaches `friendly_host`) and `friendly_host_name(host, resolver=...)`:

```python
class FakeRun:
    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=self.stdout, stderr="")


def test_list_network_mounts_runs_mount_and_resolves():
    run = FakeRun(stdout=SMB)
    rows = remote_setup.list_network_mounts(
        run=run, resolver=lambda ip: "synology-nas.tail1234.ts.net")
    assert run.calls == [["mount"]]
    assert rows[0]["friendly_host"] == "synology-nas.tail1234.ts.net"
    assert rows[0]["display_name"] == "synology-nas"


def test_friendly_host_passthrough_for_hostnames_and_failed_reverse():
    # Non-IP hosts pass through; resolver failures fall back to the raw host.
    assert remote_setup.friendly_host_name("mynas.local", resolver=None) == "mynas.local"
    def boom(ip):
        raise OSError("no PTR")
    assert remote_setup.friendly_host_name("100.80.236.59", resolver=boom) == "100.80.236.59"
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** `friendly_host_name(host, resolver=None)`: if `host` doesn't parse as an IP (`ipaddress.ip_address`), return it. Otherwise call `resolver` (default: `socket.gethostbyaddr` wrapped in a 2-second `concurrent.futures` timeout — reverse DNS can hang) and return the resolved name stripped of a trailing dot, falling back to the IP on any exception. `list_network_mounts(run=subprocess.run, resolver=None)`: `run(["mount"], capture_output=True, text=True, timeout=10)`, parse, and for each row set `friendly_host` and `display_name` (first dot-component of `friendly_host`).
- [ ] **Step 4: Run — PASS.**  **Step 5: Commit** `feat: network mount listing with reverse-DNS friendly names`.

---

### Task 3: Vireo key management + key-auth probe

**Files:** same two.

- [ ] **Step 1: Failing tests:**

```python
def test_key_paths_under_vireo_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    priv, pub = remote_setup.vireo_key_paths()
    assert priv == str(tmp_path / ".vireo" / "ssh" / "vireo_ed25519")
    assert pub == priv + ".pub"


def test_ensure_vireo_key_generates_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        priv, pub = remote_setup.vireo_key_paths()
        open(priv, "w").write("KEY")
        open(pub, "w").write("ssh-ed25519 AAAA vireo")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    priv, pub = remote_setup.ensure_vireo_key(run=fake_run)
    assert calls and calls[0][0] == "ssh-keygen"
    assert "-N" in calls[0] and "-t" in calls[0]
    assert oct(os.stat(os.path.dirname(priv)).st_mode & 0o777) == "0o700"
    # Second call: key exists, no regeneration.
    remote_setup.ensure_vireo_key(run=fake_run)
    assert len(calls) == 1


def test_key_auth_works_builds_batchmode_ssh(tmp_path):
    run = FakeRun(stdout="vireo_ok\n")
    ok = remote_setup.key_auth_works(
        host="nas", user="admin", port=2222, key="/k", ssh_bin="/usr/bin/ssh",
        run=run)
    assert ok is True
    argv = run.calls[0]
    assert argv[0] == "/usr/bin/ssh"
    assert ["-o", "BatchMode=yes"] == argv[1:3] or "BatchMode=yes" in argv
    assert "-p" in argv and "2222" in argv and "-i" in argv and "/k" in argv
    assert argv[-2:] == ["admin@nas", "echo vireo_ok"]
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** `vireo_key_paths()` → `~/.vireo/ssh/vireo_ed25519(.pub)` via `os.path.expanduser`. `ensure_vireo_key(run=subprocess.run, ssh_keygen_bin="ssh-keygen")`: mkdir `~/.vireo/ssh` mode 0700 (and `os.chmod` it — pre-existing dirs), return early if both files exist; else `run([ssh_keygen_bin, "-t", "ed25519", "-N", "", "-f", priv, "-C", "vireo"], ...)`, raise `RuntimeError` on nonzero rc, `os.chmod(priv, 0o600)`. `key_auth_works(...)`: build argv exactly like `move.ssh_base_args` does (BatchMode, accept-new, ConnectTimeout, `-p` when ≠22, `-i` when key given) and return `rc == 0 and "vireo_ok" in stdout`. Import nothing from Flask; reuse `move.py` helpers only if importable without cycles — otherwise keep the 6-line argv builder local with a comment pointing at `move.ssh_base_args`.
- [ ] **Step 4: Run — PASS.**  **Step 5: Commit** `feat: vireo-managed SSH key generation and key-auth probe`.

---

### Task 4: Password-driven key install (pty)

**Files:** same two, plus a stub script written by the test into `tmp_path`.

The production call runs, through a pty: `ssh <base args, NO BatchMode> user@host "<append snippet>"` where the snippet is

```
umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF <quoted pubkey line> ~/.ssh/authorized_keys || echo <quoted pubkey line> >> ~/.ssh/authorized_keys
```

(idempotent; `shlex.quote` the pubkey line once and reuse).

- [ ] **Step 1: Failing tests** using a Python stub as the "ssh" binary:

```python
STUB = r'''#!/usr/bin/env python3
import sys
mode = sys.argv[1]  # test passes: ok | wrongpw | nopw | hostkey
if mode == "nopw":
    sys.stderr.write("admin@nas: Permission denied (publickey).\n")
    sys.exit(255)
if mode == "hostkey":
    sys.stderr.write("Host key verification failed.\n")
    sys.exit(255)
sys.stderr.write("admin@nas's password: ")
sys.stderr.flush()
pw = input()
if mode == "ok" and pw == "sekret":
    sys.exit(0)
sys.stderr.write("Permission denied, please try again.\nadmin@nas's password: ")
sys.stderr.flush()
sys.exit(255)
'''

def _stub(tmp_path, mode):
    p = tmp_path / "fake_ssh.py"
    p.write_text(STUB)
    p.chmod(0o755)
    return [sys.executable, str(p), mode]


def test_install_key_success(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "ok"), password="sekret", timeout=15)
    assert res == {"ok": True, "error": None}


def test_install_key_wrong_password(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "wrongpw"), password="nope", timeout=15)
    assert res["ok"] is False and res["error"] == "wrong_password"


def test_install_key_host_key_rejected(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "hostkey"), password="x", timeout=15)
    assert res["ok"] is False and res["error"] == "host_key"


def test_install_key_password_auth_disabled(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "nopw"), password="x", timeout=15)
    assert res["ok"] is False and res["error"] == "password_auth_disabled"


def test_password_never_in_result_or_exception_text(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "wrongpw"), password="hunter2", timeout=15)
    assert "hunter2" not in repr(res)
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `install_key_with_password(spawn_argv, password, timeout=30)`:
  - `pty.openpty()`; `subprocess.Popen(spawn_argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)`; close slave in parent.
  - Loop: `select.select([master], [], [], deadline-remaining)`; accumulate decoded output (replace errors). Count password prompts (`re.findall(r"[Pp]assword[^\n]*:", output)`): on the first prompt → write `password + "\n"`; a second prompt at any point (live read OR post-EOF accumulated output) → wrong password: kill process if alive, return `{"ok": False, "error": "wrong_password"}`.
  - On EOF/exit: rc 0 → ok. Classify from the **accumulated output**, not just the live reads (the stub prints its second prompt and exits immediately, so prompt+EOF can arrive in one read): ≥2 password prompts observed → `wrong_password`; `Permission denied (publickey` with no password prompt ever shown → `password_auth_disabled`; `Host key verification failed` → `host_key`; anything else → `{"ok": False, "error": "ssh_failed", "detail": <output tail, max ~400 chars>}`.
  - Timeout → kill, `{"ok": False, "error": "timeout"}`.
  - Never put the password into logs, exceptions, or the returned dict. `del password` before returning is cosmetic — the real rule is simply never to log it; note this in a comment.
  - A small pure helper `build_install_argv(host, user, port, key_pub_line, ssh_bin)` assembles the argv (unit-testable without pty): base ssh args **without** BatchMode (password prompts must reach the pty) but **with** `accept-new`, plus the quoted append snippet. Add a direct test asserting BatchMode is absent and the snippet greps before appending.
- [ ] **Step 4: Run — PASS** (also on a second consecutive run; pty tests can flake if reads race — use the deadline loop, not fixed sleeps).
- [ ] **Step 5: Commit** `feat: pty-driven authorized_keys install for NAS wizard`.

---

### Task 5: Nonce share-locate + remote dir listing

**Files:** same two.

- [ ] **Step 1: Failing tests:**

```python
def test_locate_share_writes_nonce_probes_and_cleans_up(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    seen = {}
    def fake_run(argv, **kw):
        seen["cmd"] = argv[-1]
        # Server-side: pretend /volume1/Photography contains the nonce.
        return subprocess.CompletedProcess(argv, 0,
            stdout="FOUND:/volume1/Photography\n", stderr="")
    path = remote_setup.locate_share(
        mount_point=str(mount), share="Photography",
        host="nas", user="admin", port=22, key="/k", ssh_bin="ssh",
        run=fake_run)
    assert path == "/volume1/Photography"
    assert ".vireo-probe-" in seen["cmd"] and "/volume*/" in seen["cmd"]
    assert list(mount.iterdir()) == []          # nonce cleaned up


def test_locate_share_no_match_returns_none_and_cleans_up(tmp_path):
    mount = tmp_path / "mnt"; mount.mkdir()
    fake_run = FakeRun(stdout="")               # no candidate matched
    assert remote_setup.locate_share(
        mount_point=str(mount), share="Photography", host="nas",
        user="admin", port=22, key="/k", ssh_bin="ssh", run=fake_run) is None
    assert list(mount.iterdir()) == []


def test_locate_share_readonly_mount_raises(tmp_path):
    mount = tmp_path / "mnt"; mount.mkdir()
    mount.chmod(0o500)          # no write bit — nonce write must fail cleanly
    with pytest.raises(remote_setup.MountNotWritable):
        remote_setup.locate_share(
            mount_point=str(mount), share="S", host="h", user="u",
            port=22, key="", ssh_bin="ssh", run=FakeRun())


def test_list_remote_dirs_parses_and_quotes(tmp_path):
    run = FakeRun(stdout="Raw Files\nExports\n")
    dirs = remote_setup.list_remote_dirs(
        path="/volume1/My Photos", host="nas", user="admin", port=22,
        key="", ssh_bin="ssh", run=run)
    assert dirs == ["Exports", "Raw Files"]
    assert "'/volume1/My Photos'" in run.calls[0][-1]
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
  - `class MountNotWritable(RuntimeError)`.
  - `locate_share(...)`: nonce = `".vireo-probe-" + secrets.token_hex(8)`; write it inside `mount_point` (wrap `OSError`/`PermissionError` → `MountNotWritable`); `try/finally` unlink (missing-ok). Cleanup is mount-side only — a deliberate narrowing of the spec's "best-effort over SSH too": it's the same file either way, and the mount is by definition writable once the nonce was written. One ssh command with `S=<shlex.quote(share)>` and candidates `/volume*/"$S" /share/"$S" /shares/"$S" /mnt/*/"$S" /srv/*/"$S"`, `[ -e "$p/<nonce>" ] && { echo "FOUND:$p"; break; }` — parse the `FOUND:` line. `timeout=20` on the run.
  - `list_remote_dirs(...)`: `find <quoted path> -mindepth 1 -maxdepth 1 -type d -exec basename {} \;` (avoids `ls -p` symlink ambiguity), sorted casefolded; return `[]` on nonzero rc.
- [ ] **Step 4: Run — PASS.**  **Step 5: Commit** `feat: nonce-verified share location and remote dir listing`.

---

### Task 6: Flask endpoints

**Files:**
- Modify: `vireo/app.py` (immediately after `api_remote_target_test`, ~line 22168)
- Create: `vireo/tests/test_remote_setup_api.py`

- [ ] **Step 1: Failing tests** (pattern: `app_and_db` fixture; monkeypatch `remote_setup` functions — endpoints stay thin):

```python
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_mounts_endpoint_returns_parsed_mounts(app_and_db, monkeypatch):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "list_network_mounts", lambda **kw: [
        {"fs_type": "smbfs", "host": "1.2.3.4", "friendly_host": "nas.ts.net",
         "display_name": "nas", "share": "Photos",
         "mount_point": "/Volumes/Photos", "user": "admin"}])
    res = app.test_client().get("/api/remote-setup/mounts")
    assert res.status_code == 200
    assert res.get_json()["mounts"][0]["share"] == "Photos"


def test_ssh_check_reports_port_auth_and_pubkey(app_and_db, monkeypatch, tmp_path):
    # ssh-check ensures the Vireo key exists (idempotent, local-only) and
    # returns its public line — the Terminal-fallback expander needs it
    # WITHOUT ever calling install-key (no password submitted).
    app, _db = app_and_db
    import remote_setup
    pub = tmp_path / "vireo_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAA vireo\n")
    monkeypatch.setattr(remote_setup, "port_reachable", lambda host, port, timeout=5: True)
    monkeypatch.setattr(remote_setup, "ensure_vireo_key",
                        lambda **kw: (str(tmp_path / "vireo_ed25519"), str(pub)))
    monkeypatch.setattr(remote_setup, "key_auth_works", lambda **kw: False)
    res = app.test_client().post("/api/remote-setup/ssh-check",
        json={"host": "nas", "port": 22, "user": "admin"})
    body = res.get_json()
    assert body["port_open"] is True and body["key_auth_ok"] is False
    assert body["pub_key_line"] == "ssh-ed25519 AAAA vireo"


def test_ssh_check_validates_input(app_and_db):
    app, _db = app_and_db
    res = app.test_client().post("/api/remote-setup/ssh-check", json={"host": ""})
    assert res.status_code == 400


def test_install_key_rejects_non_loopback(app_and_db):
    app, _db = app_and_db
    res = app.test_client().post(
        "/api/remote-setup/install-key",
        json={"host": "nas", "user": "a", "port": 22, "password": "x"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert res.status_code == 403


def test_install_key_happy_path_never_echoes_password(app_and_db, monkeypatch):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "ensure_vireo_key", lambda **kw: ("/k", "/k.pub"))
    monkeypatch.setattr(remote_setup, "build_install_argv", lambda **kw: ["ssh"])
    monkeypatch.setattr(remote_setup, "install_key_with_password",
                        lambda **kw: {"ok": True, "error": None})
    monkeypatch.setattr(remote_setup, "key_auth_works", lambda **kw: True)
    res = app.test_client().post("/api/remote-setup/install-key",
        json={"host": "nas", "user": "a", "port": 22, "password": "hunter2"})
    body = res.get_json()
    assert body["ok"] is True
    assert "hunter2" not in res.get_data(as_text=True)


def test_locate_share_endpoint(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "locate_share",
                        lambda **kw: "/volume1/Photos")
    res = app.test_client().post("/api/remote-setup/locate-share",
        json={"mount_path": str(tmp_path), "share": "Photos",
              "host": "nas", "user": "a", "port": 22})
    assert res.get_json()["remote_path"] == "/volume1/Photos"


def test_locate_share_readonly_mount_is_clean_error(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup
    def boom(**kw):
        raise remote_setup.MountNotWritable("read-only")
    monkeypatch.setattr(remote_setup, "locate_share", boom)
    res = app.test_client().post("/api/remote-setup/locate-share",
        json={"mount_path": str(tmp_path), "share": "P", "host": "n",
              "user": "a", "port": 22})
    body = res.get_json()
    assert res.status_code == 400 and "writable" in body["error"].lower()
```

- [ ] **Step 2: Run — FAIL** (404s).
- [ ] **Step 3: Implement the six routes** in `app.py`. Shared shape:
  - `_remote_setup_loopback_guard()` helper: `request.remote_addr not in ("127.0.0.1", "::1")` → `json_error("remote setup is only available from this machine", 403)`. Applied to **all** `/api/remote-setup/*` routes (cheapest consistent rule; the app binds loopback via waitress anyway — this is defense in depth).
  - `GET /api/remote-setup/mounts` → `{"mounts": remote_setup.list_network_mounts()}` (platform gate: non-darwin returns `{"mounts": [], "unsupported_platform": True}`).
  - `POST /api/remote-setup/ssh-check` `{host, port, user}` (validate host/user non-empty, port int 1–65535) → `{"port_open", "key_auth_ok", "pub_key_line"}`. This endpoint calls `remote_setup.ensure_vireo_key()` (idempotent, local-only, no network) and reads the public line — the Terminal-fallback expander builds its one-liner from `pub_key_line` and must work without install-key ever being called. `port_open` via new `remote_setup.port_reachable(host, port, timeout=5)` (plain `socket.create_connection`; unit test with a monkeypatched socket). `ssh_bin` resolved via `move_mod.resolve_ssh_bin(effective_cfg.get("ssh_bin", ""))` exactly like `api_remote_target_test`; `key_auth_ok` is `False` with `"ssh_missing": True` when no ssh binary resolves.
  - `POST /api/remote-setup/install-key` `{host, port, user, password}` → ensure key, `build_install_argv`, `install_key_with_password`, then verify with `key_auth_works`; response `{"ok", "error", "fingerprint"}` (fingerprint via `ssh-keygen -lf pub`, best-effort). The password variable is request-scoped; never logged (request-timing logs only record the URL — confirm no body logging on this path).
  - `GET /api/remote-setup/disk-free?path=` → `{"free_bytes": shutil.disk_usage(path).free}` for the archive-root step's free-space readout; 400 on non-absolute or missing path. One test: create a tmp dir, assert `free_bytes > 0`.
  - `POST /api/remote-setup/locate-share` `{mount_path, share, host, port, user}` → validate `mount_path` is an absolute existing dir; map `MountNotWritable` → 400 with a "mount is not writable" message; result `{"remote_path": ... or None}`.
  - `POST /api/remote-setup/list-remote-dirs` `{path, host, port, user}` → `{"dirs": [...]}` for the fallback browser.
- [ ] **Step 4: Run — PASS.** Also run the neighbors: `python -m pytest vireo/tests/test_app.py -q` (no route collisions).
- [ ] **Step 5: Commit** `feat: /api/remote-setup endpoints for the NAS wizard`.

---

### Task 7: Settings wizard UI

**Files:**
- Modify: `vireo/templates/settings.html`

No unit tests (vanilla JS in template, covered by Task 9 e2e). Structure — follow the page's existing inline-style idiom:

- [ ] **Step 1: Button.** Next to `+ Add remote target` (~line 874): `Set up from mounted volume…` (primary accent styling), `onclick="openNasWizard()"`.
- [ ] **Step 2: Modal skeleton** appended near the page's other overlays: `#nasWizard` fixed overlay, one step container per spec step (`data-step="volume|ssh|share|archive|review"`), Back/Next footer, close ×. Step state machine in JS: `_nwState = {step, mounts, mount, host, port, user, keyExists, keyAuthOk, remotePath, archiveRoot, name}`.
- [ ] **Step 3: Step logic**, one function per step:
  - `nwLoadMounts()` → `GET /api/remote-setup/mounts`; zero mounts → guidance ("connect in Finder ⌘K") + Refresh; one → preselect and advance enabled.
  - `nwSshStep()` → editable user/host/port prefilled from mount (`friendly_host` preferred, IP shown as hint); on entry call `ssh-check`; port closed → Synology-aware help text (detect via `display_name`/`share` heuristic `/synology|diskstation/i`, generic otherwise) + Retry. `key_auth_ok` already true → skip password UI, auto-advance ("This Mac is already authorized ✓"). Password form posts `install-key`; map `error` codes to messages (`wrong_password` → retry with field cleared; `password_auth_disabled` → point at the Terminal expander; `timeout`/`ssh_failed` → show detail). Terminal expander shows the equivalent one-liner (built client-side from state: `ssh user@host` + append snippet with the actual pubkey, taken from `ssh-check`'s `pub_key_line` — available before and without any password submission) and a **Verify** button that re-runs `ssh-check` until `key_auth_ok`.
  - `nwShareStep()` → auto-runs `locate-share`; found → green "Verified: /volume1/Photography" + Next; not found → fallback mini-browser (`list-remote-dirs`, drill-down list starting at `/`, plus a manual text input); read-only-mount 400 → its own error text.
  - `nwArchiveStep()` → folder picker on `/api/browse` + `/api/browse/mkdir` (compact list: current path, subdirs, "New folder" inline input), default suggestion `<home>/Pictures/Vireo Archive` (home = the `path` returned by parameterless `/api/browse`); "create it" action; free-space line from `GET /api/remote-setup/disk-free?path=` (built in Task 6). Validation mirror: absolute, not inside mount (client-side check copying `collectRemoteTargets`' spirit; server re-validates on save anyway).
  - `nwReviewStep()` → assembled card (name defaulted from `display_name` or share; `bwlimit_kbps: 0`; `ssh_key`: the wizard key path); auto-POST `/api/remote-targets/test` with the assembled target; per-check lines from its response (`ssh`, `remote_path_writable`, `rsync_ok`, `remote_rsync_ok`, mount presence); failures link back ("Fix connection" → step 2, "Fix path" → step 3). Save = push into `_remoteTargetsState` + `renderRemoteTargets()` + `saveConfig()` (the existing config-save path), close, scroll to the new card.
- [ ] **Step 4: Deep link.** On settings page load: `location.hash === "#nas-setup"` → `openNasWizard()`.
- [ ] **Step 5: Manual smoke** (see Task 10) then **commit** `feat: NAS setup wizard modal on the settings page`.

---

### Task 8: Import-page entry point

**Files:**
- Modify: `vireo/templates/import.html` (`updateAfterMoveUI`, ~line 1461–1471)

- [ ] **Step 1:** Where `afterMoveUnavailable` text is set, append a link: `Set up remote target…` → `href="/settings#nas-setup"` (build via DOM nodes, matching how `remotePreviewWarn` builds elements — the hint currently uses `textContent`; switch this one message to element children, keeping the text copy identical plus the trailing link).
- [ ] **Step 2:** Run the import-page e2e neighbors that assert on this hint if any exist: `grep -rn "not inside any remote target" tests/e2e/` first; update matching assertions.
- [ ] **Step 3: Commit** `feat: link the unavailable-NAS-move hint to the setup wizard`.

---

### Task 9: Wizard e2e

**Files:**
- Create: `tests/e2e/test_nas_setup_wizard.py`

The e2e server runs in-process (`tests/e2e/conftest.py` uses `make_server`), so monkeypatch `remote_setup` module functions in the fixture — no real ssh anywhere.

- [ ] **Step 1:** Fixture: patch `list_network_mounts` (one smbfs row), `port_reachable` (True), `ensure_vireo_key` (must return a path to a **real** pub file on disk in the test home — the ssh-check route reads it for `pub_key_line`), `build_install_argv`/`install_key_with_password` (success)/`key_auth_works` (False until install called, True after — a tiny stateful fake), `locate_share` ("/volume1/Photography"), and `move.test_remote_connection` (all-green dict). Follow the existing e2e patching idiom in that conftest.
- [ ] **Step 2:** Test A — happy path: open `/settings`, click "Set up from mounted volume…", walk all five steps (select mount → password `sekret` → verified share → pick archive root via the picker into a tmp dir → review shows green → Save), assert the new target card renders in the remote-targets list and `GET /api/config` now contains the target with `ssh_key` = wizard key path and `bwlimit_kbps` 0.
- [ ] **Step 3:** Test B — Terminal fallback branch: stateful fake starts with `install_key_with_password` unused; expand the fallback, flip the fake's auth state, click Verify, assert the wizard advances without ever calling `install_key_with_password`.
- [ ] **Step 4:** Run `python -m pytest tests/e2e/test_nas_setup_wizard.py -v` — PASS. **Commit** `test: e2e coverage for the NAS setup wizard`.

---

### Task 10: Full suite, manual verify, PR

- [ ] **Step 1:** Required suite (CLAUDE.md): `python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py vireo/tests/test_remote_setup.py vireo/tests/test_remote_setup_api.py -q` — all green.
- [ ] **Step 2:** Manual verify against the real app (`python vireo/app.py --db ~/.vireo/vireo.db --port 8080`): walk the wizard against the real mounted share `/Volumes/Photography` (this machine has the real thing — the ultimate fixture). Confirm: mount detected with friendly name `synology-nas`, ssh-check reports port open, key install path exercised (key already authorized → skip branch), nonce probe returns the real `/volume1/...` path, target saves and Test connection is green.
- [ ] **Step 3:** `gh pr create --base main` with summary, spec/plan links, and test results per CLAUDE.md.

---

## Out of scope (per spec)

Windows/Linux mount enumeration and key install; multi-key/passphrase/ssh-agent; wizard-based editing of saved targets; NAS-brand APIs.
