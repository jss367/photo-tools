import os
import subprocess
from types import SimpleNamespace

import source_scan_policy


def test_darwin_groups_sources_by_volume_and_bounds_by_storage_type():
    mount_output = "\n".join([
        "/dev/disk3s1s1 on / (apfs, local, read-only)",
        "//user@nas/Photography on /Volumes/Photography (smbfs, nodev)",
        "/dev/disk4s1 on /Volumes/CARD (exfat, local)",
    ])

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=mount_output)

    policies = source_scan_policy.classify_sources([
        "/Users/me/Pictures/A",
        "/Users/me/Pictures/B",
        "/Volumes/Photography/2026/one",
        "/Volumes/Photography/2026/two",
        "/Volumes/CARD/DCIM",
    ], platform="darwin", run=run)

    assert [policy["storage"] for policy in policies] == [
        "local", "local", "network", "network", "removable",
    ]
    assert [policy["max_parallel"] for policy in policies] == [2, 2, 1, 1, 1]
    assert policies[0]["volume_key"] == policies[1]["volume_key"]
    assert policies[2]["volume_key"] == policies[3]["volume_key"]
    assert policies[0]["volume_key"] != policies[2]["volume_key"]
    assert "user" not in policies[2]["volume_key"]


def test_darwin_mount_failure_falls_back_to_one_shared_serial_lane():
    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("mount", 3)

    policies = source_scan_policy.classify_sources(
        ["/one", "/two"], platform="darwin", run=run,
    )
    assert {policy["volume_key"] for policy in policies} == {"unknown-volume"}
    assert {policy["max_parallel"] for policy in policies} == {1}


def test_linux_uses_mount_identity_and_unescapes_mount_points(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("\n".join([
        "20 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw",
        "21 20 0:44 / /mnt/Photo\\040Share rw - cifs //nas/photos rw",
        "22 20 8:17 / /media/me/CARD rw - vfat /dev/sdb1 rw",
    ]))
    policies = source_scan_policy.classify_sources([
        "/home/me/photos/a",
        "/mnt/Photo Share/2026/a",
        "/mnt/Photo Share/2026/b",
        "/media/me/CARD/DCIM",
    ], platform="linux", mountinfo_path=str(mountinfo))

    assert [policy["storage"] for policy in policies] == [
        "local", "network", "network", "removable",
    ]
    assert [policy["max_parallel"] for policy in policies] == [2, 1, 1, 1]
    assert policies[1]["volume_key"] == policies[2]["volume_key"] == "linux:0:44"


def test_windows_distinguishes_fixed_remote_and_removable_drives():
    drive_types = {"C:\\": 3, "Z:\\": 4, "E:\\": 2}
    policies = source_scan_policy.classify_sources([
        "C:\\Photos\\A", "Z:\\Photos\\B", "E:\\DCIM",
    ], platform="win32", get_drive_type=lambda root: drive_types[root],
        get_remote_share=lambda _local: None)

    assert [policy["storage"] for policy in policies] == [
        "local", "network", "removable",
    ]
    assert [policy["max_parallel"] for policy in policies] == [2, 1, 1]


def test_windows_groups_a_mapped_drive_with_its_unc_share():
    """Z:\\ and \\\\nas\\share are one share: selecting both spellings must
    share a lane, or the per-share serialization is defeated."""
    drive_types = {"Z:\\": 4, "\\\\NAS\\Share\\": 4}
    policies = source_scan_policy.classify_sources([
        "Z:\\Photos\\2026",
        "\\\\NAS\\Share\\Photos\\2027",
    ], platform="win32",
        get_drive_type=lambda root: drive_types[root],
        get_remote_share=lambda local: (
            "\\\\nas\\share" if local == "Z:" else None))

    assert [policy["storage"] for policy in policies] == ["network", "network"]
    assert policies[0]["volume_key"] == policies[1]["volume_key"]
    assert policies[0]["volume_key"] == "windows:unc:nas/share"


def test_windows_mapped_drive_keeps_its_own_lane_when_lookup_fails():
    policies = source_scan_policy.classify_sources([
        "Z:\\Photos",
    ], platform="win32", get_drive_type=lambda _root: 4,
        get_remote_share=lambda _local: None)

    assert policies[0]["storage"] == "network"
    assert policies[0]["volume_key"] == "windows:drive:z:"


def test_windows_only_consults_the_mapping_table_for_remote_drives():
    def refuse(_local):
        raise AssertionError(
            "local drives must not trigger a remote-share lookup")

    policies = source_scan_policy.classify_sources([
        "C:\\Photos\\A",
    ], platform="win32", get_drive_type=lambda _root: 3,
        get_remote_share=refuse)

    assert policies[0]["storage"] == "local"
    assert policies[0]["volume_key"] == "windows:drive:c:"


def test_classify_sources_does_not_stat_the_selected_paths(monkeypatch):
    """Path classification must stay purely lexical: a stale NAS mount
    (`os.path.realpath` blocks on stat) would otherwise be the exact
    failure mode this API exists to keep the UI out of."""
    mount_output = "//user@nas/Photography on /Volumes/Photography (smbfs, nodev)"

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=mount_output)

    def _refuse(*_args, **_kwargs):
        raise AssertionError("classify_sources must not stat the selected paths")

    monkeypatch.setattr(os.path, "realpath", _refuse)
    monkeypatch.setattr(os, "stat", _refuse)
    monkeypatch.setattr(os, "lstat", _refuse)

    policies = source_scan_policy.classify_sources(
        ["/Volumes/Photography/2026/one"], platform="darwin", run=run,
    )
    assert policies[0]["storage"] == "network"
