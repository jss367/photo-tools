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
