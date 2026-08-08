"""Guards on the release process.

Both tests exist because of the v0.32.3 release failure: `scripts/release.sh`
ran `cargo generate-lockfile`, which re-resolved every third-party crate to the
newest compatible version. That pulled in zune-core 0.5.2 — published three
hours earlier, broken, and yanked 35 minutes later — and the macOS build failed
after the tag had already been pushed.
"""
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
CARGO_LOCK = REPO_ROOT / "src-tauri" / "Cargo.lock"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _uncommented_lines(text):
    return [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_release_does_not_regenerate_the_whole_lockfile():
    """The version bump must not re-resolve third-party crates.

    `cargo update --workspace` rewrites only the `vireo` entry;
    `cargo generate-lockfile` rewrites everything.
    """
    code = _uncommented_lines(RELEASE_SH.read_text())
    offenders = [line for line in code if "cargo generate-lockfile" in line]
    assert not offenders, (
        "scripts/release.sh must not run `cargo generate-lockfile` — it bumps "
        "every dependency to the newest compatible version at tag time, with no "
        "CI run in between. Use `cargo update --workspace`. Offending lines: "
        f"{offenders}"
    )
    assert any("cargo update --workspace" in line for line in code), (
        "scripts/release.sh must sync the workspace version into Cargo.lock "
        "with `cargo update --workspace`"
    )


def test_release_compiles_dependencies_before_tagging():
    """The publish path builds nothing locally, so it needs a compile gate."""
    code = _uncommented_lines(RELEASE_SH.read_text())
    assert any("cargo check --locked" in line for line in code), (
        "scripts/release.sh must run `cargo check --locked` before tagging so a "
        "dependency that does not compile is caught before the tag is pushed"
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
