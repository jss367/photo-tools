# vireo/tests/test_packaging.py
"""Guard the setuptools package-data declaration.

`[tool.setuptools.packages.find]` discovers Python packages only, so every
non-Python runtime asset under vireo/ ships in a wheel *only* because
`[tool.setuptools.package-data]` names it. When that declaration is missing
the failure is silent and total: `load_scientific_synonyms()` falls back to
`{}` (outdated binomials like "Bubulcus ibis" stop resolving and come back as
raw names and false model disagreements) and Flask has no templates at all,
with nothing in the build output to say so. These tests fail loudly instead.
"""

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import taxonomy as tax_mod  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

VIREO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(VIREO_DIR)
PYPROJECT = os.path.join(REPO_ROOT, "pyproject.toml")

# Directories under vireo/ that hold shipped non-Python assets. Anything here
# is needed at runtime, so package-data has to cover all of it.
ASSET_DIRS = ("data", "templates", "static")


def _declared_patterns():
    """The package-data globs pyproject declares for the `vireo` package."""
    if tomllib is None or not os.path.exists(PYPROJECT):
        pytest.skip("pyproject.toml unavailable (installed, not a checkout)")
    with open(PYPROJECT, "rb") as f:
        cfg = tomllib.load(f)
    package_data = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {})
    assert "vireo" in package_data, (
        "pyproject.toml declares no [tool.setuptools.package-data] for `vireo`; "
        "a wheel build would ship .py files only."
    )
    return package_data["vireo"]


def _packaged_files():
    """Paths (relative to vireo/) that the declared patterns would include.

    Mirrors what setuptools does with the patterns: recursive glob rooted at
    the package directory.
    """
    included = set()
    for pattern in _declared_patterns():
        for hit in glob.glob(os.path.join(VIREO_DIR, pattern), recursive=True):
            if os.path.isfile(hit):
                included.add(os.path.relpath(hit, VIREO_DIR).replace(os.sep, "/"))
    return included


def test_synonym_map_is_declared_as_package_data():
    """The synonym JSON must be packaged, or the fix it backs is a no-op."""
    rel = os.path.relpath(tax_mod.SCIENTIFIC_SYNONYMS_PATH, VIREO_DIR).replace(os.sep, "/")
    assert rel in _packaged_files(), (
        f"{rel} is not covered by [tool.setuptools.package-data]; a wheel "
        "install would load an empty synonym map and silently keep showing "
        "outdated scientific names."
    )


def test_all_shipped_assets_are_declared_as_package_data():
    """Every non-.py file under the asset dirs is covered by a pattern."""
    packaged = _packaged_files()
    missing = []
    for asset_dir in ASSET_DIRS:
        root = os.path.join(VIREO_DIR, asset_dir)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py") or name == ".DS_Store":
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), VIREO_DIR)
                rel = rel.replace(os.sep, "/")
                if rel not in packaged:
                    missing.append(rel)
    assert not missing, (
        "these runtime assets would be dropped from a wheel build; add a "
        f"pattern to [tool.setuptools.package-data]: {sorted(missing)}"
    )


def test_synonym_map_resolves_and_is_populated():
    """The path taxonomy.py resolves actually holds the map at runtime.

    Complements the pyproject assertions above: this one also fails if an
    install puts the file somewhere `SCIENTIFIC_SYNONYMS_PATH` cannot see.
    """
    assert os.path.exists(tax_mod.SCIENTIFIC_SYNONYMS_PATH), (
        f"synonym map missing at {tax_mod.SCIENTIFIC_SYNONYMS_PATH}"
    )
    assert tax_mod.load_scientific_synonyms(), "synonym map loaded empty"
