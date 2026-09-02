"""pytest plugin: ``--selected-tests FILE`` restricts a run to listed tests.

``scripts/select_tests.py`` writes a selection file with one entry per line:
a repo-relative test *file* (``vireo/tests/test_db.py``) runs in full, a
*node id* (``vireo/tests/test_db.py::test_x[param]``) runs alone. Blank lines
and ``#`` comments are ignored.

Two hooks do the work. ``pytest_ignore_collect`` skips test modules that
contribute nothing to the selection so collection stays fast, and
``pytest_collection_modifyitems`` deselects everything else. Node ids that
no longer exist (a PR renamed or deleted the test) simply match nothing,
and an empty selection exits 0 instead of pytest's "no tests collected" 5.

Registered by the repository-root ``conftest.py`` so it applies to both
``tests/`` and ``vireo/tests/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SELECTION_KEY = pytest.StashKey["_Selection"]()


class _Selection:
    def __init__(self, path: str):
        self.path = path
        self.files: set[str] = set()
        self.ids: set[str] = set()
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            (self.ids if "::" in line else self.files).add(line)
        # Modules that must still be collected because one of their tests is
        # listed individually.
        self.id_files = {nodeid.split("::", 1)[0] for nodeid in self.ids}

    def wants_module(self, rel: str) -> bool:
        return rel in self.files or rel in self.id_files

    def wants_item(self, nodeid: str) -> bool:
        return nodeid in self.ids or nodeid.split("::", 1)[0] in self.files


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--selected-tests",
        default=None,
        metavar="FILE",
        help="only run the test files / node ids listed in FILE (see scripts/select_tests.py)",
    )


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--selected-tests")
    if path:
        config.stash[SELECTION_KEY] = _Selection(path)


def _relative(config: pytest.Config, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.rootpath.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    selection = config.stash.get(SELECTION_KEY, None)
    if selection is None or collection_path.is_dir():
        return None
    if collection_path.suffix != ".py" or not collection_path.name.startswith("test_"):
        return None
    if selection.wants_module(_relative(config, collection_path)):
        return None
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selection = config.stash.get(SELECTION_KEY, None)
    if selection is None:
        return
    keep = [item for item in items if selection.wants_item(item.nodeid)]
    drop = [item for item in items if not selection.wants_item(item.nodeid)]
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_report_header(config: pytest.Config) -> str | None:
    selection = config.stash.get(SELECTION_KEY, None)
    if selection is None:
        return None
    return (
        f"selected tests: {len(selection.files)} whole files + "
        f"{len(selection.ids)} individual tests from {selection.path}"
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if SELECTION_KEY not in session.config.stash:
        return
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        # Every listed test was renamed or removed on this branch; the
        # branch's own test-file changes run separately, so this is a clean
        # "nothing applies", not an error.
        session.exitstatus = pytest.ExitCode.OK
