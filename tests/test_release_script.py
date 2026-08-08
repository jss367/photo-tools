"""Guards on the release process.

These tests exist because of the v0.32.3 release failure: `scripts/release.sh`
ran `cargo generate-lockfile`, which re-resolved every third-party crate to the
newest compatible version. That pulled in zune-core 0.5.2 — published three
hours earlier, broken, and yanked 35 minutes later — and the macOS build failed
after the tag had already been pushed.

The ordering assertions matter as much as the presence ones. A compile gate that
runs *after* `git tag` protects nothing: the tag is the point of no return, so a
guard that only checked for the command's existence would still pass while the
protection it describes had been silently lost.
"""
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
CARGO_LOCK = REPO_ROOT / "src-tauri" / "Cargo.lock"
PYPROJECT = REPO_ROOT / "pyproject.toml"

LOCKFILE_SYNC = r"^\s*\(cd src-tauri && cargo update --workspace\)"
DEPENDENCY_CHECK = r"^\s*\(cd src-tauri && cargo check --locked\)"
TAG_COMMAND = r'^\s*git tag "'
PUBLISH_GUARD = r"^if\s+\$PUBLISH\s*;\s*then\s*$"


def _code_lines():
    """release.sh with comments and blanks blanked out, line indices preserved.

    Blanking rather than dropping keeps index comparisons meaningful, and stops
    a command named in a comment from satisfying a presence assertion.
    """
    return [
        "" if (not line.strip() or line.lstrip().startswith("#")) else line
        for line in RELEASE_SH.read_text().splitlines()
    ]


def _sole_index(lines, pattern):
    """Index of the one line matching `pattern`, asserting it is unambiguous."""
    hits = [i for i, line in enumerate(lines) if re.search(pattern, line)]
    assert len(hits) == 1, (
        f"expected exactly one line in scripts/release.sh matching {pattern!r}, "
        f"found {len(hits)} (lines {[i + 1 for i in hits]})"
    )
    return hits[0]


def _publish_block_ranges(lines):
    """(start, end) index pairs for each `if $PUBLISH; then ... fi` block.

    Tracks if/fi nesting so a `$PUBLISH` block containing inner conditionals
    still resolves to its own `fi` rather than the first one encountered.
    """
    ranges = []
    open_blocks = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^if\s", stripped):
            open_blocks.append((i, line))
        elif stripped == "fi":
            assert open_blocks, f"unbalanced `fi` at scripts/release.sh:{i + 1}"
            start, opener = open_blocks.pop()
            if re.match(PUBLISH_GUARD, opener):
                ranges.append((start, i))
    assert not open_blocks, "unbalanced `if` in scripts/release.sh"
    return ranges


def test_release_does_not_regenerate_the_whole_lockfile():
    """The version bump must not re-resolve third-party crates.

    `cargo update --workspace` rewrites only the `vireo` entry;
    `cargo generate-lockfile` rewrites everything.
    """
    lines = _code_lines()
    offenders = [
        i + 1 for i, line in enumerate(lines) if "cargo generate-lockfile" in line
    ]
    assert not offenders, (
        "scripts/release.sh must not run `cargo generate-lockfile` — it bumps "
        "every dependency to the newest compatible version at tag time, with no "
        f"CI run in between. Use `cargo update --workspace`. Lines: {offenders}"
    )
    _sole_index(lines, LOCKFILE_SYNC)


def test_lockfile_sync_runs_before_tagging():
    """A lock synced after tagging would not be in the tagged commit."""
    lines = _code_lines()
    assert _sole_index(lines, LOCKFILE_SYNC) < _sole_index(lines, TAG_COMMAND), (
        "`cargo update --workspace` must run before `git tag` so the tagged "
        "commit contains the synced Cargo.lock"
    )


def test_dependency_check_gates_the_tag():
    """The compile gate is worthless unless it can still stop the tag.

    The publish path builds nothing locally, so this is the only thing standing
    between a broken dependency and a pushed tag.
    """
    lines = _code_lines()
    check = _sole_index(lines, DEPENDENCY_CHECK)
    tag = _sole_index(lines, TAG_COMMAND)

    assert check < tag, (
        "`cargo check --locked` must run before `git tag` — after the tag is "
        "pushed it cannot prevent a broken release, which is the whole point"
    )

    blocks = _publish_block_ranges(lines)
    assert blocks, "no `if $PUBLISH; then` block found in scripts/release.sh"
    assert any(start < check < end for start, end in blocks), (
        "`cargo check --locked` must sit inside an `if $PUBLISH; then` block. "
        "The non-publish path already does a full local build, so running it "
        "unconditionally just duplicates that compile."
    )


def test_cargo_lock_version_matches_pyproject():
    """A stale Cargo.lock means CI has to re-resolve during a tagged build."""
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    lock = CARGO_LOCK.read_text()
    match = re.search(
        r'\[\[package\]\]\nname = "vireo"\nversion = "([^"]+)"', lock
    )
    assert match, "no `vireo` package entry found in src-tauri/Cargo.lock"
    assert match.group(1) == expected, (
        f"src-tauri/Cargo.lock has vireo v{match.group(1)} but pyproject.toml "
        f"has {expected}. Run `cd src-tauri && cargo update --workspace`."
    )
