import errno
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import volume_reachability as vr


@pytest.fixture(autouse=True)
def _clear_shared_gate():
    vr.get_shared().clear()
    yield
    vr.get_shared().clear()


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_mount_root_candidates_recognizes_mount_shaped_prefixes():
    assert vr.mount_root_candidates("/Volumes/Photography/Raw Files/2026") == [
        "/Volumes/Photography"
    ]
    assert vr.mount_root_candidates("/mnt/nas/photos") == ["/mnt/nas"]
    assert vr.mount_root_candidates("/media/julius/card/DCIM") == [
        "/media/julius/card"
    ]
    assert vr.mount_root_candidates("//server/share/photos") == ["//server/share"]


def test_mount_root_candidates_empty_for_ordinary_local_paths(tmp_path):
    if sys.platform == "win32":
        pytest.skip("every absolute Windows path has a drive-letter candidate")
    assert vr.mount_root_candidates(str(tmp_path / "shoot")) == []


def test_is_offline_error_classifies_volume_loss_not_missing_files():
    assert vr.is_offline_error(OSError(errno.ENOTCONN, "Socket is not connected"))
    assert vr.is_offline_error(OSError(errno.EIO, "Input/output error"))
    assert not vr.is_offline_error(OSError(errno.ENOENT, "No such file"))
    assert not vr.is_offline_error(OSError(errno.EACCES, "Permission denied"))
    assert not vr.is_offline_error(RuntimeError("not an OSError"))


def test_check_skips_probe_for_paths_without_mount_root(tmp_path):
    if sys.platform == "win32":
        pytest.skip("every absolute Windows path has a drive-letter candidate")
    probes = []
    gate = vr.VolumeReachability(probe=lambda root: probes.append(root) or True)
    assert gate.check(str(tmp_path)) == (None, True)
    assert probes == []


def test_check_caches_reachable_verdict_within_ttl():
    probes = []
    clock = _FakeClock()

    def probe(root):
        probes.append(root)
        return True

    gate = vr.VolumeReachability(ttl_seconds=30, probe=probe, clock=clock)
    assert gate.check("/Volumes/NAS/a") == ("/Volumes/NAS", True)
    assert gate.check("/Volumes/NAS/b") == ("/Volumes/NAS", True)
    assert probes == ["/Volumes/NAS"], "second check within TTL must not re-probe"
    clock.now += 31
    gate.check("/Volumes/NAS/c")
    assert probes == ["/Volumes/NAS", "/Volumes/NAS"], "expired verdict re-probes"


def test_check_reports_offline_root_and_caches_it_for_backoff_window():
    probes = []
    clock = _FakeClock()

    def probe(root):
        probes.append(root)
        return False

    gate = vr.VolumeReachability(offline_ttl_seconds=30, probe=probe, clock=clock)
    assert gate.check("/Volumes/NAS/a") == ("/Volumes/NAS", False)
    assert gate.check("/Volumes/NAS/a") == ("/Volumes/NAS", False)
    assert len(probes) == 1
    clock.now += 31
    assert gate.check("/Volumes/NAS/a") == ("/Volumes/NAS", False)
    assert len(probes) == 2


def test_mark_offline_short_circuits_subsequent_checks_without_probing():
    probes = []
    gate = vr.VolumeReachability(probe=lambda root: probes.append(root) or True)
    gate.mark_offline("/Volumes/NAS")
    assert gate.check("/Volumes/NAS/shoot") == ("/Volumes/NAS", False)
    assert probes == []


def test_mark_offline_ignores_missing_root():
    gate = vr.VolumeReachability(probe=lambda root: True)
    gate.mark_offline(None)
    gate.mark_offline("")
    assert gate.check("/Volumes/NAS/x") == ("/Volumes/NAS", True)


def test_probe_exception_is_treated_as_offline():
    def probe(root):
        raise RuntimeError("boom")

    gate = vr.VolumeReachability(probe=probe)
    assert gate.check("/mnt/nas/x") == ("/mnt/nas", False)


def test_clear_forgets_verdicts():
    calls = []
    gate = vr.VolumeReachability(probe=lambda root: calls.append(root) or True)
    gate.check("/mnt/nas/x")
    gate.clear()
    gate.check("/mnt/nas/x")
    assert len(calls) == 2


def test_generic_probe_times_out_as_offline(monkeypatch):
    import threading

    release = threading.Event()

    def slow_isdir(path):
        release.wait(5)
        return True

    monkeypatch.setattr(vr.os.path, "isdir", slow_isdir)
    try:
        assert vr._probe_root_generic("/mnt/stuck", timeout=0.05) is False
    finally:
        release.set()


def test_generic_probe_returns_isdir_result(tmp_path):
    assert vr._probe_root_generic(str(tmp_path), timeout=1) is True
    assert vr._probe_root_generic(str(tmp_path / "nope"), timeout=1) is False


def test_app_reexports_probe_machinery_by_historical_names():
    """``app`` and ``pipeline_job`` keep their private aliases so existing
    call sites and tests continue to see one shared probe registry."""
    import app as app_module
    import pipeline_job

    assert app_module._network_root_reachable is vr.network_root_reachable
    assert app_module._NETWORK_PROBES is vr._NETWORK_PROBES
    assert app_module._NETWORK_PROBE_LOCK is vr._NETWORK_PROBE_LOCK
    assert pipeline_job._archive_mount_root_candidates is vr.mount_root_candidates
