"""Unit tests for the case-aware containment helper.

Extracted from the import endpoint's destination-inside-source guard
(PR #1107). The endpoint-level tests in test_jobs_api.py remain the
behavior-preservation net; these pin the helper's own contract.
"""
import os
import sys

import pytest
from path_guard import contains_resolved, fs_is_case_insensitive, path_contains


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
