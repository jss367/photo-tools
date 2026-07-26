import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import remote_setup


SMB = "//julius_admin@100.80.236.59/Photography on /Volumes/Photography (smbfs, nodev, nosuid, mounted by julius)"
SMB_SPACES = "//guest@My%20NAS._smb._tcp.local/Photo%20Library on /Volumes/Photo Library (smbfs, nodev, nosuid, mounted by julius)"
NFS = "truenas:/mnt/tank/photos on /Volumes/photos (nfs, nodev, nosuid, mounted by julius)"
LOCAL = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)"
AUTOFS = "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)"


class FakeRun:
    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=self.stdout, stderr="")


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


# --- Vireo key management -------------------------------------------------


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


def test_ensure_vireo_key_raises_on_keygen_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    run = FakeRun(returncode=1)
    with pytest.raises(RuntimeError):
        remote_setup.ensure_vireo_key(run=run)


def test_key_auth_works_builds_batchmode_ssh(tmp_path):
    run = FakeRun(stdout="vireo_ok\n")
    ok = remote_setup.key_auth_works(
        host="nas", user="admin", port=2222, key="/k", ssh_bin="/usr/bin/ssh",
        run=run)
    assert ok is True
    argv = run.calls[0]
    assert argv[0] == "/usr/bin/ssh"
    assert "BatchMode=yes" in argv
    assert "-p" in argv and "2222" in argv and "-i" in argv and "/k" in argv
    assert argv[-2:] == ["admin@nas", "echo vireo_ok"]


def test_key_auth_works_false_on_denied(tmp_path):
    run = FakeRun(stdout="", returncode=255)
    assert remote_setup.key_auth_works(
        host="nas", user="admin", port=22, key="", ssh_bin="ssh",
        run=run) is False


def test_port_reachable(monkeypatch):
    class FakeSock:
        def close(self):
            pass

    monkeypatch.setattr(remote_setup.socket, "create_connection",
                        lambda addr, timeout: FakeSock())
    assert remote_setup.port_reachable("nas", 22) is True

    def refuse(addr, timeout):
        raise OSError("refused")

    monkeypatch.setattr(remote_setup.socket, "create_connection", refuse)
    assert remote_setup.port_reachable("nas", 22) is False


# --- Password-driven key install (pty) ------------------------------------


STUB = r'''#!/usr/bin/env python3
import sys
mode = sys.argv[1]  # ok | wrongpw | nopw | hostkey
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


def test_install_key_password_auth_disabled(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "nopw"), password="x", timeout=15)
    assert res["ok"] is False and res["error"] == "password_auth_disabled"


def test_install_key_host_key_rejected(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "hostkey"), password="x", timeout=15)
    assert res["ok"] is False and res["error"] == "host_key"


def test_password_never_in_result_text(tmp_path):
    res = remote_setup.install_key_with_password(
        spawn_argv=_stub(tmp_path, "wrongpw"), password="hunter2", timeout=15)
    assert "hunter2" not in repr(res)


# --- Nonce share-locate + remote dir listing ------------------------------


def test_locate_share_writes_nonce_probes_and_cleans_up(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    seen = {}

    def fake_run(argv, **kw):
        seen["cmd"] = argv[-1]
        # Server-side: pretend /volume1/Photography contains the nonce.
        return subprocess.CompletedProcess(
            argv, 0, stdout="FOUND:/volume1/Photography\n", stderr="")

    path = remote_setup.locate_share(
        mount_point=str(mount), share="Photography",
        host="nas", user="admin", port=22, key="/k", ssh_bin="ssh",
        run=fake_run)
    assert path == "/volume1/Photography"
    assert ".vireo-probe-" in seen["cmd"] and "/volume*/" in seen["cmd"]
    assert list(mount.iterdir()) == []          # nonce cleaned up


def test_locate_share_no_match_returns_none_and_cleans_up(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    fake_run = FakeRun(stdout="")               # no candidate matched
    assert remote_setup.locate_share(
        mount_point=str(mount), share="Photography", host="nas",
        user="admin", port=22, key="/k", ssh_bin="ssh", run=fake_run) is None
    assert list(mount.iterdir()) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory write bits")
def test_locate_share_readonly_mount_raises(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    mount.chmod(0o500)          # no write bit — nonce write must fail cleanly
    try:
        with pytest.raises(remote_setup.MountNotWritable):
            remote_setup.locate_share(
                mount_point=str(mount), share="S", host="h", user="u",
                port=22, key="", ssh_bin="ssh", run=FakeRun())
    finally:
        mount.chmod(0o700)      # let pytest clean tmp_path up


def test_list_remote_dirs_parses_and_quotes(tmp_path):
    run = FakeRun(stdout="Raw Files\nExports\n")
    dirs = remote_setup.list_remote_dirs(
        path="/volume1/My Photos", host="nas", user="admin", port=22,
        key="", ssh_bin="ssh", run=run)
    assert dirs == ["Exports", "Raw Files"]
    assert "'/volume1/My Photos'" in run.calls[0][-1]


def test_list_remote_dirs_empty_on_error():
    assert remote_setup.list_remote_dirs(
        path="/nope", host="h", user="u", port=22, key="", ssh_bin="ssh",
        run=FakeRun(returncode=255)) == []


def test_build_install_argv_no_batchmode_and_idempotent_append():
    argv = remote_setup.build_install_argv(
        host="nas", user="admin", port=22,
        key_pub_line="ssh-ed25519 AAAA vireo", ssh_bin="/usr/bin/ssh")
    assert argv[0] == "/usr/bin/ssh"
    assert "BatchMode=yes" not in argv          # password prompt must reach the pty
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[-2] == "admin@nas"
    snippet = argv[-1]
    assert "umask 077" in snippet and "mkdir -p ~/.ssh" in snippet
    assert "grep -qxF" in snippet and "authorized_keys" in snippet
    assert "ssh-ed25519 AAAA vireo" in snippet
