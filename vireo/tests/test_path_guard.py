"""Unit tests for the case-aware containment helper.

Extracted from the import endpoint's destination-inside-source guard
(PR #1107). The endpoint-level tests in test_jobs_api.py remain the
behavior-preservation net; these pin the helper's own contract.
"""
import os
import sys

import pytest
from path_guard import (
    contains_resolved,
    fs_is_case_insensitive,
    make_case_folded_check,
    path_contains,
)


def test_contains_equal_and_nested(tmp_path):
    root = str(tmp_path / "card")
    os.makedirs(root)
    assert path_contains(root, root)
    assert path_contains(root, os.path.join(root, "DCIM", "IMG_0001.NEF"))
    assert not path_contains(root, str(tmp_path / "archive"))


def test_contains_prefix_is_not_containment(tmp_path):
    # /card-extra is NOT inside /card even though the string is a prefix.
    root = str(tmp_path / "card")
    os.makedirs(root)
    assert not path_contains(root, str(tmp_path / "card-extra" / "x.jpg"))


def test_contains_follows_symlinks(tmp_path):
    root = tmp_path / "card"
    root.mkdir()
    link = tmp_path / "alias"
    try:
        os.symlink(str(root), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this filesystem")
    assert path_contains(str(root), str(link / "IMG_0001.NEF"))


@pytest.mark.skipif(
    sys.platform in ("darwin", "win32"),
    reason="Linux-only probe: darwin/win32 always case-fold",
)
def test_inconclusive_probe_casefolds(tmp_path):
    # Numeric-only entries: the probe cannot case-swap, so it must fall
    # back to case-insensitive (the strict direction) and the case-swapped
    # child is treated as contained. Mirrors the PR #1107 endpoint test.
    root = tmp_path / "Card-BAR"
    root.mkdir()
    (root / "100").mkdir()
    assert fs_is_case_insensitive(str(root)) is True
    assert contains_resolved(str(root), str(tmp_path / "card-bar" / "x"))


@pytest.mark.skipif(
    sys.platform in ("darwin", "win32"),
    reason="Linux-only: needs a genuinely case-sensitive filesystem",
)
def test_case_sensitive_fs_distinguishes(tmp_path):
    root = tmp_path / "CardABC"
    root.mkdir()
    (root / "alpha.txt").write_text("x")
    if fs_is_case_insensitive(str(root)):
        pytest.skip("tmp filesystem is case-insensitive")
    assert not contains_resolved(str(root), str(tmp_path / "cardabc" / "x"))


def test_darwin_always_casefolds(tmp_path):
    if sys.platform not in ("darwin", "win32"):
        pytest.skip("case-insensitive-platform path")
    root = tmp_path / "Card"
    root.mkdir()
    swapped = str(tmp_path / "card" / "IMG.NEF")
    assert contains_resolved(str(root), swapped)


def test_path_contains_null_byte_is_strict(tmp_path):
    root = str(tmp_path / "card")
    os.makedirs(root)
    assert path_contains(root, root + "/x\x00y") is True


def test_probe_permission_error_is_inconclusive(tmp_path, monkeypatch):
    # exists() would collapse EACCES into "case-sensitive" (the
    # non-strict direction); the probe must treat an unreadable
    # case-swapped name as inconclusive → case-insensitive.
    root = tmp_path / "CardABC"
    root.mkdir()
    (root / "alpha.txt").write_text("x")
    import path_guard as pg
    real_stat = os.stat

    def denying_stat(p, *a, **kw):
        if str(p).endswith("Alpha.txt"):
            raise PermissionError(13, "denied", str(p))
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(pg.os, "stat", denying_stat)
    assert pg.fs_is_case_insensitive(str(root)) is True


def test_probe_caches_only_the_strict_result(tmp_path, monkeypatch):
    # Only True (case-insensitive, the strict answer) is cached: st_dev
    # identifies the backing device, not a mount generation, so a card
    # swapped through the same reused device node can keep the cache
    # key. A stale True merely over-folds; a stale False would run
    # case-sensitive comparisons against a FAT card.
    import path_guard as pg
    root = tmp_path / "CacheProbe"
    root.mkdir()
    pg._probe_cache.clear()
    calls = []

    def fake_probe(path):
        calls.append(str(path))
        return True

    monkeypatch.setattr(pg, "_probe_uncached", fake_probe)
    assert pg.fs_is_case_insensitive(str(root)) is True
    assert pg.fs_is_case_insensitive(str(root)) is True
    assert len(calls) == 1  # strict result served from cache


def test_make_case_folded_check_probes_once(tmp_path, monkeypatch):
    # Tight-loop consumers (scan/delete) call the containment check
    # thousands of times per run. contains_resolved reprobes the root's
    # filesystem for every False result (the module cache stores only
    # True), so the bound helper must lift the probe to once per run.
    import path_guard as pg
    root = tmp_path / "BoundCheck"
    root.mkdir()
    (root / "photos").mkdir()
    pg._probe_cache.clear()
    probe_calls = []

    def counting_probe(path):
        probe_calls.append(str(path))
        # Return False so the module cache never intercepts a repeat.
        return False

    monkeypatch.setattr(pg, "_probe_uncached", counting_probe)
    # Force the probe path: darwin/win32 short-circuit before probing,
    # so without this the probe count is 0 there, not 1.
    monkeypatch.setattr(pg, "is_case_insensitive_platform", lambda: False)
    check = make_case_folded_check(str(root))
    for _ in range(10):
        check(str(root / "photos" / "IMG.NEF"))
    assert len(probe_calls) == 1


def test_make_case_folded_check_agrees_with_contains_resolved(tmp_path):
    # Behavioral parity with the plain call — the bound helper must
    # decide the same containment for the same inputs.
    root = tmp_path / "card"
    root.mkdir()
    root_real = os.path.realpath(str(root))
    check = make_case_folded_check(root_real)
    child_inside = os.path.realpath(str(root / "DCIM" / "IMG.NEF"))
    child_outside = os.path.realpath(str(tmp_path / "elsewhere" / "x"))
    assert check(child_inside) == contains_resolved(root_real, child_inside)
    assert check(child_outside) == contains_resolved(root_real, child_outside)


def test_probe_never_caches_case_sensitive(tmp_path, monkeypatch):
    import path_guard as pg
    root = tmp_path / "NoCacheProbe"
    root.mkdir()
    pg._probe_cache.clear()
    calls = []

    def fake_probe(path):
        calls.append(str(path))
        return False

    monkeypatch.setattr(pg, "_probe_uncached", fake_probe)
    assert pg.fs_is_case_insensitive(str(root)) is False
    assert pg.fs_is_case_insensitive(str(root)) is False
    assert len(calls) == 2  # non-strict result re-probed every call
