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


@pytest.fixture(autouse=True)
def _posix_shaped_inputs_stay_posix(monkeypatch):
    """Keep ``/Volumes/...``, ``/mnt/...`` and ``//server/share`` literals
    POSIX-shaped on every host.

    On Windows ``os.path.abspath("/mnt/archive")`` becomes ``D:\\mnt\\archive``,
    which would add a spurious ``D:/`` drive candidate and double every probe
    in these tests. Real Windows inputs (``tmp_path``, ``C:/...``) are left to
    the real functions.
    """
    import posixpath

    # Capture originals first: on POSIX ``posixpath`` *is* ``os.path``, so
    # calling ``posixpath.normpath`` after patching would recurse.
    real_abspath, real_normpath = os.path.abspath, os.path.normpath
    posix_normpath = posixpath.normpath

    def abspath(p):
        p = os.fspath(p)
        return posix_normpath(p) if p.startswith("/") else real_abspath(p)

    def normpath(p):
        p = os.fspath(p)
        if p.startswith("//"):
            return "//" + posix_normpath(p[2:]).lstrip("/")
        return posix_normpath(p) if p.startswith("/") else real_normpath(p)

    monkeypatch.setattr(vr.os.path, "abspath", abspath)
    monkeypatch.setattr(vr.os.path, "normpath", normpath)


class _FakeScandir:
    def __init__(self, names):
        self._it = iter(names)

    def __enter__(self):
        return self._it

    def __exit__(self, *exc):
        return False


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


def test_generic_probe_returns_isdir_result(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_MOUNT_BASELINE", {})
    (tmp_path / "photo.jpg").write_bytes(b"")
    assert vr._probe_root_generic(str(tmp_path), timeout=1) is True
    assert vr._probe_root_generic(str(tmp_path / "nope"), timeout=1) is False
    # An empty, unmounted directory is read as a detached share's stub.
    (tmp_path / "empty").mkdir()
    assert vr._probe_root_generic(str(tmp_path / "empty"), timeout=1) is False


def test_app_reexports_probe_machinery_by_historical_names():
    """``app`` and ``pipeline_job`` keep their private aliases so existing
    call sites and tests continue to see one shared probe registry."""
    import app as app_module
    import pipeline_job

    assert app_module._network_root_reachable is vr.network_root_reachable
    assert app_module._NETWORK_PROBES is vr._NETWORK_PROBES
    assert app_module._NETWORK_PROBE_LOCK is vr._NETWORK_PROBE_LOCK
    assert pipeline_job._archive_mount_root_candidates is vr.mount_root_candidates


def test_generic_probe_reuses_wedged_thread_instead_of_stacking(monkeypatch):
    """A root whose ``isdir`` never returns must not accumulate one thread per
    check: while the first probe is alive, later checks fail closed at once."""
    import threading

    release = threading.Event()
    started = []

    def slow_isdir(path):
        started.append(path)
        release.wait(5)
        return True

    monkeypatch.setattr(vr.os.path, "isdir", slow_isdir)
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    try:
        assert vr._probe_root_generic("/mnt/stuck", timeout=0.05) is False
        assert vr._probe_root_generic("/mnt/stuck", timeout=0.05) is False
        assert started == ["/mnt/stuck"], "second check must reuse the live probe"
        assert "/mnt/stuck" in vr._GENERIC_PROBES
        probe = vr._GENERIC_PROBES["/mnt/stuck"]
    finally:
        release.set()
    probe.join(1)
    assert "/mnt/stuck" not in vr._GENERIC_PROBES, "finished probe unregisters itself"


def test_generic_probe_global_cap_fails_closed(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(vr.os.path, "isdir", lambda p: release.wait(5) or True)
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    monkeypatch.setattr(vr, "_MAX_GENERIC_PROBES", 2)
    try:
        assert vr._probe_root_generic("/mnt/a", timeout=0.02) is False
        assert vr._probe_root_generic("/mnt/b", timeout=0.02) is False
        assert vr._probe_root_generic("/mnt/c", timeout=0.02) is False
        assert set(vr._GENERIC_PROBES) == {"/mnt/a", "/mnt/b"}, "third root never spawned"
    finally:
        release.set()


def test_mount_root_candidates_follows_local_alias_without_touching_share(tmp_path, monkeypatch):
    """An alias into a share resolves to the share's mount root, but resolution
    stops at the mount-shaped prefix — nothing under it is ever stat'ed."""
    if sys.platform == "win32":
        pytest.skip("POSIX symlinks")
    alias = tmp_path / "photos"
    alias.symlink_to("/Volumes/NAS/photos")

    touched = []
    real_islink = os.path.islink

    def spy_islink(p):
        touched.append(str(p))
        return real_islink(p)

    monkeypatch.setattr(vr.os.path, "islink", spy_islink)
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: None)
    assert vr.mount_root_candidates(str(alias / "2026" / "shoot")) == ["/Volumes/NAS"]
    assert not any(t.startswith("/Volumes/NAS") for t in touched), touched
    monkeypatch.setattr(vr.os.path, "realpath", lambda p: pytest.fail("realpath must not be used"))
    assert vr.mount_root_candidates(str(alias)) == ["/Volumes/NAS"]


def test_mount_root_candidates_symlink_loop_is_bounded(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX symlinks")
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    assert vr.mount_root_candidates(str(tmp_path / "a" / "x")) == []


def test_mount_root_candidates_only_bounded_lookups_on_mount_shaped_prefixes(monkeypatch):
    """Anything at or below a mount-shaped prefix may only be inspected via the
    time-bounded helper — never a bare ``islink``/``realpath`` (unbounded on
    a dead server). UNC prefixes are never inspected at all."""
    bounded = []
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: bounded.append(p))
    monkeypatch.setattr(vr.os.path, "realpath", lambda p: pytest.fail(f"realpath({p!r})"))
    direct = []
    real_islink = os.path.islink
    monkeypatch.setattr(vr.os.path, "islink", lambda p: direct.append(p) or real_islink(p))

    assert vr.mount_root_candidates("//server/share/photos/2026") == ["//server/share"]
    assert vr.mount_root_candidates("/Volumes/NAS/photos") == ["/Volumes/NAS"]
    assert vr.mount_root_candidates("/mnt/nas/photos") == ["/mnt/nas"]
    assert not any(p.startswith("//") for p in bounded), bounded
    assert set(bounded) == {"/Volumes/NAS", "/mnt/nas"}
    assert not any(
        p.startswith(("/Volumes/NAS", "/mnt/nas", "//")) for p in direct
    ), f"unbounded lookup on a mount-shaped path: {direct}"


def test_mount_root_candidates_follows_mount_shaped_alias_to_real_mount(monkeypatch):
    """``/mnt/archive -> /mnt/NAS``: both the alias and the real mount are
    reported, so the pipeline's mounted-to-unmounted guards see the mount
    that can actually detach."""
    links = {"/mnt/archive": "/mnt/NAS"}
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: links.get(p))
    assert vr.mount_root_candidates("/mnt/archive/photos/2026") == ["/mnt/archive", "/mnt/NAS"]


def test_bounded_link_target_reads_local_symlink_and_times_out_on_wedged_mount(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX symlinks")
    link = tmp_path / "archive"
    link.symlink_to("/mnt/NAS")
    assert vr._bounded_link_target(str(link)) == "/mnt/NAS"
    assert vr._bounded_link_target(str(tmp_path)) is None

    import threading
    release = threading.Event()
    monkeypatch.setattr(vr.os.path, "islink", lambda p: release.wait(5) or False)
    monkeypatch.setattr(vr, "_BOUNDED_LINK_PROBES", {})
    try:
        assert vr._bounded_link_target("/mnt/stuck", timeout=0.05) is vr.INCONCLUSIVE
        # Reused while alive: no second thread, immediate (inconclusive) answer.
        assert vr._bounded_link_target("/mnt/stuck", timeout=0.05) is vr.INCONCLUSIVE
        assert list(vr._BOUNDED_LINK_PROBES) == ["/mnt/stuck"]
    finally:
        release.set()


def test_resolver_skips_unc_server_prefix(monkeypatch):
    calls = []
    real = os.path.islink

    def spy(p):
        calls.append(str(p))
        return real(p)

    monkeypatch.setattr(vr.os.path, "islink", spy)
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: None)
    vr._resolve_symlinks_until_mount_shaped(
        "//server/share/photos",
        lambda p: "//x/y" if p.startswith("//") and len([q for q in p.split("/") if q]) >= 2 else None,
    )
    # ``os.path`` is process-global and daemon probe threads from earlier
    # tests may still be calling it, so assert only on UNC prefixes.
    unc = [c for c in calls if c.startswith("//")]
    assert unc == [], f"UNC server prefix was stat'ed: {unc}"


def test_generic_probe_recognises_detached_mount_stub(monkeypatch):
    """Linux keeps the mount-point directory after a share detaches. A root
    seen as a real mount once must read as offline when it later exists only
    as a plain directory; a root that was never a mount stays online."""
    monkeypatch.setattr(vr, "_MOUNT_BASELINE", {})
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    monkeypatch.setattr(vr.os.path, "isdir", lambda p: True)
    mounted = {"/mnt/NAS": True}
    monkeypatch.setattr(vr.os.path, "ismount", lambda p: mounted.get(p, False))
    monkeypatch.setattr(vr.os, "scandir", lambda p: _FakeScandir(["photo.jpg"]))

    assert vr._probe_root_generic("/mnt/NAS", timeout=1) is True
    assert vr._probe_root_generic("/mnt/photos", timeout=1) is True, "never a mount: plain dir is fine"
    mounted["/mnt/NAS"] = False  # share detached, stub directory remains
    assert vr._probe_root_generic("/mnt/NAS", timeout=1) is False
    assert vr._probe_root_generic("/mnt/photos", timeout=1) is True
    mounted["/mnt/NAS"] = True  # reconnected
    assert vr._probe_root_generic("/mnt/NAS", timeout=1) is True


def test_generic_probe_missing_directory_is_offline(monkeypatch):
    monkeypatch.setattr(vr, "_MOUNT_BASELINE", {})
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    monkeypatch.setattr(vr.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(vr.os.path, "ismount", lambda p: pytest.fail("ismount on a missing dir"))
    assert vr._probe_root_generic("/mnt/gone", timeout=1) is False


def test_bounded_link_target_global_cap(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(vr.os.path, "islink", lambda p: release.wait(5) or False)
    monkeypatch.setattr(vr, "_BOUNDED_LINK_PROBES", {})
    monkeypatch.setattr(vr, "_MAX_BOUNDED_LINK_PROBES", 2)
    try:
        assert vr._bounded_link_target("/mnt/a", timeout=0.02) is vr.INCONCLUSIVE
        assert vr._bounded_link_target("/mnt/b", timeout=0.02) is vr.INCONCLUSIVE
        assert vr._bounded_link_target("/mnt/c", timeout=0.02) is vr.INCONCLUSIVE
        assert set(vr._BOUNDED_LINK_PROBES) == {"/mnt/a", "/mnt/b"}, "third path never spawned"
    finally:
        release.set()


def test_saturated_link_probes_fall_back_to_cached_real_mount(monkeypatch):
    """Under saturation the alias's real mount must still be reported when an
    earlier conclusive answer exists — never silently downgraded to "plain
    directory" (which would let an import write into a detached stub)."""
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: vr.INCONCLUSIVE)
    # Both prefixes answered conclusively earlier: full resolution, confident.
    monkeypatch.setattr(vr, "_LINK_TARGET_CACHE", {"/mnt/archive": "/mnt/NAS", "/mnt/NAS": None})
    cands, conclusive = vr.mount_root_candidates_checked("/mnt/archive/photos")
    assert cands == ["/mnt/archive", "/mnt/NAS"]
    assert conclusive is True
    # Only the alias is cached: the real mount is still reported (so the
    # pipeline guards see it) but the result is not confident, because the
    # real mount's own prefix could not be inspected.
    monkeypatch.setattr(vr, "_LINK_TARGET_CACHE", {"/mnt/archive": "/mnt/NAS"})
    cands, conclusive = vr.mount_root_candidates_checked("/mnt/archive/photos")
    assert cands == ["/mnt/archive", "/mnt/NAS"]
    assert conclusive is False


def test_inconclusive_prefix_without_cache_fails_closed_in_gate(monkeypatch):
    monkeypatch.setattr(vr, "_LINK_TARGET_CACHE", {})
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: vr.INCONCLUSIVE)
    cands, conclusive = vr.mount_root_candidates_checked("/mnt/archive/photos")
    assert cands == ["/mnt/archive"]
    assert conclusive is False
    probes = []
    gate = vr.VolumeReachability(probe=lambda root: probes.append(root) or True)
    assert gate.check("/mnt/archive/photos") == ("/mnt/archive", False)
    assert probes == [], "no point probing a root whose prefix could not be inspected"


def test_conclusive_link_answers_are_cached(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX symlinks")
    monkeypatch.setattr(vr, "_LINK_TARGET_CACHE", {})
    monkeypatch.setattr(vr, "_BOUNDED_LINK_PROBES", {})
    link = tmp_path / "archive"
    link.symlink_to("/mnt/NAS")
    assert vr._bounded_link_target(str(link)) == "/mnt/NAS"
    assert vr._bounded_link_target(str(tmp_path)) is None
    assert {str(link): "/mnt/NAS", str(tmp_path): None} == vr._LINK_TARGET_CACHE


def test_generic_probe_cold_start_treats_empty_unmounted_stub_as_offline(monkeypatch):
    """No in-process history (Vireo started after the share detached): an
    empty, unmounted mount-point directory is the detached share's stub and
    reads offline; a populated unmounted directory is a real local root."""
    monkeypatch.setattr(vr, "_MOUNT_BASELINE", {})
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    monkeypatch.setattr(vr.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(vr.os.path, "ismount", lambda p: False)
    contents = {"/mnt/NAS": [], "/mnt/photos": ["2026"]}
    monkeypatch.setattr(vr.os, "scandir", lambda p: _FakeScandir(contents[p]))

    assert vr._probe_root_generic("/mnt/NAS", timeout=1) is False
    assert "/mnt/NAS" not in vr._MOUNT_BASELINE, "a stub must not be recorded as a local dir"
    assert vr._probe_root_generic("/mnt/photos", timeout=1) is True
    assert vr._MOUNT_BASELINE["/mnt/photos"] is False


def test_generic_probe_requires_readable_listing_even_when_mounted(monkeypatch):
    """A share still in the mount table but with a dead server: ``isdir`` and
    ``ismount`` succeed from cached metadata, the listing does not. Only the
    listing counts."""
    import errno

    monkeypatch.setattr(vr, "_MOUNT_BASELINE", {})
    monkeypatch.setattr(vr, "_GENERIC_PROBES", {})
    monkeypatch.setattr(vr.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(vr.os.path, "ismount", lambda p: True)

    def dead_scandir(p):
        raise OSError(errno.EIO, "Input/output error", p)

    monkeypatch.setattr(vr.os, "scandir", dead_scandir)
    assert vr._probe_root_generic("/mnt/NAS", timeout=1) is False
    assert "/mnt/NAS" not in vr._MOUNT_BASELINE


def test_mount_root_candidates_carry_confidence_on_the_same_object(monkeypatch):
    """Candidates and confidence must come from one resolution: the returned
    list carries ``conclusive`` so callers cannot pair a truncated candidate
    list with a later, luckier confidence check."""
    monkeypatch.setattr(vr, "_LINK_TARGET_CACHE", {})
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: vr.INCONCLUSIVE)
    cands = vr.mount_root_candidates("/mnt/archive/photos")
    assert isinstance(cands, list) and cands == ["/mnt/archive"]
    assert cands.conclusive is False
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: None)
    assert vr.mount_root_candidates("/mnt/archive/photos").conclusive is True


def test_darwin_probe_requires_a_directory_listing():
    """``stat`` can answer from cached attributes on a dead SMB mount; the
    macOS probe now enumerates the root and accepts only a directory
    listing (``.``/``..`` present)."""
    import subprocess
    from types import SimpleNamespace

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=".\n..\nphotos\n")

    assert vr._probe_root_darwin("/Volumes/NAS", run=fake_run) is True
    assert calls[0][:3] == ["/bin/ls", "-1", "-f"] and calls[0][3] == "/Volumes/NAS"

    file_like = lambda argv, **k: SimpleNamespace(returncode=0, stdout="/Volumes/NAS\n")  # noqa: E731
    assert vr._probe_root_darwin("/Volumes/NAS", run=file_like) is False
    failed = lambda argv, **k: SimpleNamespace(returncode=1, stdout="")  # noqa: E731
    assert vr._probe_root_darwin("/Volumes/NAS", run=failed) is False

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    assert vr._probe_root_darwin("/Volumes/NAS", run=timed_out) is False


def test_probe_root_uses_directory_read_on_darwin(monkeypatch):
    monkeypatch.setattr(vr.sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(vr, "_probe_root_darwin", lambda root, timeout=None: seen.append(root) or True)
    monkeypatch.setattr(vr, "network_root_reachable", lambda *a, **k: pytest.fail("stat-only probe must not decide"))
    assert vr.probe_root("/Volumes/NAS") is True
    assert seen == ["/Volumes/NAS"]


def test_resolver_follows_junction_below_windows_drive_root(monkeypatch):
    """``C:\\Photos -> \\\\server\\share`` (a junction): the drive root is a
    traversal start, not a terminal mount, so the UNC share is reported."""
    links = {"C:/Photos": "\\\\?\\UNC\\server\\share"}
    monkeypatch.setattr(vr, "_is_link_or_junction", lambda p: p in links)
    monkeypatch.setattr(vr.os, "readlink", lambda p: links[p])
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: pytest.fail(f"lookup on {p}"))
    resolved = vr._resolve_symlinks_until_mount_shaped("C:/Photos/2026", vr._mount_shaped_candidate)
    assert resolved == "//server/share/2026"
    # End to end, with abspath neutralised (POSIX host running a Windows-shaped path).
    monkeypatch.setattr(vr.os.path, "abspath", lambda p: p)
    monkeypatch.setattr(vr.os.path, "normpath", lambda p: p)
    cands = vr.mount_root_candidates("C:/Photos/2026")
    assert cands == ["C:/", "//server/share"]
    assert cands.conclusive is True


def test_normalize_link_target_strips_windows_prefixes():
    assert vr._normalize_link_target("\\\\?\\UNC\\server\\share\\x") == "//server/share/x"
    assert vr._normalize_link_target("\\\\?\\D:\\archive") == "D:/archive"
    assert vr._normalize_link_target("/mnt/NAS") == "/mnt/NAS"


def test_resolver_stops_at_mapped_network_drive_without_touching_it(monkeypatch):
    """``Z:`` mapped to a share: the drive root is the mount, and no component
    below it may be inspected (an lstat there can block while the server is
    away). Local ``C:`` keeps being traversed."""
    monkeypatch.setattr(vr, "_drive_is_remote", lambda d: d.upper().startswith("Z:"))
    monkeypatch.setattr(vr, "_is_link_or_junction", lambda p: pytest.fail(f"lookup under mapped drive: {p}"))
    monkeypatch.setattr(vr, "_bounded_link_target", lambda p, timeout=None: pytest.fail(f"lookup under mapped drive: {p}"))
    assert vr._resolve_symlinks_until_mount_shaped("Z:/Photos/2026", vr._mount_shaped_candidate) == "Z:/Photos/2026"
    monkeypatch.setattr(vr.os.path, "abspath", lambda p: p)
    monkeypatch.setattr(vr.os.path, "normpath", lambda p: p)
    cands = vr.mount_root_candidates("Z:/Photos/2026")
    assert cands == ["Z:/"] and cands.conclusive is True


def test_drive_is_remote_is_false_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows asks the mount manager")
    assert vr._drive_is_remote("Z:") is False
