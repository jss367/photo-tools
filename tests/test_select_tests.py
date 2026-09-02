"""Tests for the PR test selector (scripts/select_tests.py) and its plugin.

Each test builds a throwaway git repo shaped like this one (``vireo/``,
``vireo/tests/``, ``tests/``), records a synthetic impact map with
``coverage.CoverageData`` and asks the selector what a follow-up commit
should run. The selector must only ever *narrow* a run, so the assertions
check both what is selected and what is deliberately left out.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import select_tests  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt" and not os.environ.get("CI"), reason="git fixtures assume POSIX shell")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

APP_SRC = textwrap.dedent(
    '''
    import os

    LIMIT = 3


    def alpha(x):
        return x + 1


    @staticmethod
    def beta(y):
        if y:
            return "b"
        return "c"


    def render_browse():
        return "browse.html"
    '''
).lstrip()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A committed repo plus an impact map that mirrors APP_SRC line by line."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "vireo/app.py", APP_SRC)
    _write(repo, "vireo/templates/browse.html", "{% include '_navbar.html' %}\n")
    _write(repo, "vireo/templates/_navbar.html", "<nav><a href='/settings'>settings.html</a></nav>\n")
    _write(repo, "vireo/templates/settings.html", "{% extends '_base.html' %}\n")
    _write(repo, "vireo/tests/test_comments.py", "# see app.py for the routes\ndef test_noop(): pass\n")
    _write(repo, "vireo/tests/contracts/routes.txt", "GET /\n")
    _write(repo, "vireo/tests/test_route_contract.py", "SNAPSHOT = 'contracts/routes.txt'\ndef test_routes(): pass\n")
    _write(repo, "vireo/static/js/browse.js", "// js\n")
    _write(repo, "vireo/tests/conftest.py", "")
    _write(repo, "vireo/tests/test_app.py", "def test_alpha(): pass\ndef test_beta(): pass\n")
    _write(repo, "vireo/tests/test_pages.py", "def test_browse(): pass\n")
    _write(repo, "tests/test_release.py", "SCRIPT = 'release.sh'\n")
    _write(repo, "scripts/release.sh", "echo hi\n")
    _write(repo, "docs/notes.md", "notes\n")
    base = _commit(repo, "base")

    # Line numbers in APP_SRC: LIMIT=3 -> line 3; alpha body lines 6-7;
    # beta decorator 10, def 11, body 12-14; render_browse 17-18.
    map_dir = repo / select_tests.MAP_DIR
    map_dir.mkdir()
    data = CoverageData(basename=str(map_dir / select_tests.MAP_DB))
    app = "vireo/app.py"
    data.set_context("")
    data.add_lines({app: [1, 3, 6, 10, 11, 17]})
    data.set_context("vireo/tests/test_app.py::test_alpha|run")
    data.add_lines({app: [7]})
    data.set_context("vireo/tests/test_app.py::test_beta|run")
    data.add_lines({app: [12, 13]})
    data.set_context("vireo/tests/test_app.py::test_beta|setup")
    data.add_lines({"vireo/tests/conftest.py": [1]})
    data.set_context("vireo/tests/test_pages.py::test_browse|run")
    data.add_lines({app: [18]})
    data.write()
    (map_dir / select_tests.MAP_META).write_text(json.dumps({"sha": base, "root": str(repo)}))
    return repo


def _select(repo: Path, **kwargs):
    impact, meta, problem = select_tests.load_map(repo / select_tests.MAP_DIR)
    assert problem is None, problem
    return select_tests.select(meta["sha"], "HEAD", impact, cwd=repo, **kwargs)


# --------------------------------------------------------------------------
# selection rules
# --------------------------------------------------------------------------


def test_change_inside_function_selects_only_its_tests(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("return x + 1", "return x + 2"))
    _commit(repo, "edit alpha")

    sel = _select(repo)

    assert sel.mode == "subset"
    assert sel.ids == {"vireo/tests/test_app.py::test_alpha"}
    assert sel.files == set()


def test_insertion_inside_function_uses_surrounding_lines(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace('        return "b"\n', '        print("x")\n        return "b"\n'))
    _commit(repo, "insert into beta")

    sel = _select(repo)

    assert sel.ids == {"vireo/tests/test_app.py::test_beta"}


def test_decorator_line_belongs_to_its_function(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("@staticmethod\n", "@classmethod\n"))
    _commit(repo, "swap decorator")

    sel = _select(repo)

    assert sel.ids == {"vireo/tests/test_app.py::test_beta"}


def test_module_level_change_selects_every_test_touching_the_file(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("LIMIT = 3", "LIMIT = 4"))
    _commit(repo, "bump constant")

    sel = _select(repo)

    assert sel.ids == {
        "vireo/tests/test_app.py::test_alpha",
        "vireo/tests/test_app.py::test_beta",
        "vireo/tests/test_pages.py::test_browse",
    }


def test_source_basename_mentions_in_tests_do_not_select(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("return x + 1", "return x + 2"))
    _commit(repo, "edit alpha")

    sel = _select(repo)

    # test_comments.py says "app.py" in a comment; the map is authoritative.
    assert "vireo/tests/test_comments.py" not in sel.files
    assert sel.ids == {"vireo/tests/test_app.py::test_alpha"}


def test_comment_only_change_selects_nothing_from_map(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("    return x + 1", "    # tweak\n    return x + 1"))
    _commit(repo, "comment")
    # Insertion of a comment line still attributes to the enclosing function
    # (its neighbours are code), but *modifying* a comment does not:
    sel_insert = _select(repo)
    assert sel_insert.ids == {"vireo/tests/test_app.py::test_alpha"}

    _write(repo, "vireo/app.py", APP_SRC.replace("    return x + 1", "    # tweaked\n    return x + 1"))
    _commit(repo, "reword comment")
    meta_path = repo / select_tests.MAP_DIR / select_tests.MAP_META
    meta = json.loads(meta_path.read_text())
    previous = subprocess.run(["git", "rev-parse", "HEAD^"], cwd=repo, capture_output=True, text=True).stdout.strip()
    impact, _, _ = select_tests.load_map(repo / select_tests.MAP_DIR)
    # Compare against the previous commit so the diff is the comment reword alone.
    sel = select_tests.select(previous, "HEAD", impact, cwd=repo)
    assert sel.mode == "none", sel.notes
    assert meta["sha"]  # map still intact


def test_insertion_between_nested_functions_picks_the_nested_one(repo):
    nested = textwrap.dedent(
        """
        def create_app():
            def route_a():
                return "a"

            def route_b():
                return "b"

            return route_a, route_b
        """
    ).lstrip()
    _write(repo, "vireo/nested.py", nested)
    _commit(repo, "nested base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    map_dir = repo / select_tests.MAP_DIR
    data = CoverageData(basename=str(map_dir / select_tests.MAP_DB))
    data.set_context("vireo/tests/test_nested.py::test_create_app|run")
    data.add_lines({"vireo/nested.py": [1, 2, 5, 8]})
    data.set_context("vireo/tests/test_nested.py::test_route_a|run")
    data.add_lines({"vireo/nested.py": [3]})
    data.set_context("vireo/tests/test_nested.py::test_route_b|run")
    data.add_lines({"vireo/nested.py": [6]})
    data.write()
    (map_dir / select_tests.MAP_META).write_text(json.dumps({"sha": base, "root": str(repo)}))

    # Insert a new route between route_a and route_b: the old-side
    # neighbours are the blank line 4 (create_app only) and line 5
    # (route_b's def). The smaller enclosing function wins.
    _write(repo, "vireo/nested.py", nested.replace("    def route_b", "    def route_new():\n        return 'n'\n\n    def route_b"))
    _commit(repo, "add route")

    sel = _select(repo)

    assert sel.ids == {"vireo/tests/test_nested.py::test_route_b"}


def test_test_asset_runs_the_tests_that_name_it(repo):
    _write(repo, "vireo/tests/contracts/routes.txt", "GET /\nGET /new\n")
    _commit(repo, "update contract")

    sel = _select(repo)

    assert sel.mode == "subset"
    assert sel.files == {"vireo/tests/test_route_contract.py"}


def test_template_hyperlinks_are_not_followed(repo):
    # _navbar.html mentions settings.html in an href, and browse.html
    # includes _navbar.html. Editing settings.html must not select browse.
    _write(repo, "vireo/templates/settings.html", "{% extends '_base.html' %}<p>x</p>\n")
    _commit(repo, "edit settings")

    sel = _select(repo)

    assert sel.mode == "none", sel.notes


def test_deleted_source_file_selects_everything_that_used_it(repo):
    (repo / "vireo/app.py").unlink()
    _commit(repo, "delete app")

    sel = _select(repo)

    assert sel.mode == "subset"
    assert len(sel.ids) == 3


def test_changed_test_file_runs_whole_file(repo):
    _write(repo, "vireo/tests/test_app.py", "def test_alpha(): pass\ndef test_beta(): pass\ndef test_new(): pass\n")
    _commit(repo, "add test")

    sel = _select(repo)

    assert sel.files == {"vireo/tests/test_app.py"}
    assert sel.ids == set()


def test_added_test_file_runs_whole_file_and_added_source_selects_nothing_else(repo):
    _write(repo, "vireo/newmod.py", "def f():\n    return 1\n")
    _write(repo, "vireo/tests/test_newmod.py", "def test_f(): pass\n")
    _commit(repo, "new module + test")

    sel = _select(repo)

    assert sel.files == {"vireo/tests/test_newmod.py"}
    assert sel.ids == set()


def test_deleted_test_file_is_not_selected(repo):
    (repo / "vireo/tests/test_pages.py").unlink()
    _commit(repo, "drop test file")

    sel = _select(repo)

    assert sel.mode == "none"
    assert sel.files == set() and sel.ids == set()


def test_template_change_selects_tests_that_rendered_it(repo):
    _write(repo, "vireo/templates/browse.html", "{% include '_navbar.html' %}<p>new</p>\n")
    _commit(repo, "edit template")

    sel = _select(repo)

    assert sel.ids == {"vireo/tests/test_pages.py::test_browse"}


def test_included_partial_is_chased_through_including_templates(repo):
    _write(repo, "vireo/templates/_navbar.html", "<nav>changed</nav>\n")
    _commit(repo, "edit navbar")

    sel = _select(repo)

    # _navbar.html -> browse.html -> render_browse() -> test_browse
    assert sel.ids == {"vireo/tests/test_pages.py::test_browse"}


def test_unreferenced_asset_selects_nothing(repo):
    _write(repo, "vireo/static/js/browse.js", "// changed\n")
    _commit(repo, "edit js nobody references")

    sel = _select(repo)

    assert sel.mode == "none"


def test_asset_mentioned_by_a_test_file_runs_that_file(repo):
    _write(repo, "scripts/release.sh", "echo changed\n")
    _commit(repo, "edit release script")

    sel = _select(repo)

    assert sel.files == {"tests/test_release.py"}


def test_docs_only_change_selects_nothing(repo):
    _write(repo, "docs/notes.md", "changed\n")
    _commit(repo, "docs")

    sel = _select(repo)

    assert sel.mode == "none"


def test_e2e_changes_are_ignored_for_unit_selection(repo):
    _write(repo, "tests/e2e/test_x.py", "def test_x(): pass\n")
    _commit(repo, "e2e")

    sel = _select(repo)

    assert sel.mode == "none"


@pytest.mark.parametrize(
    "path",
    [
        "vireo/tests/conftest.py",
        "tests/conftest.py",
        "pyproject.toml",
        ".github/workflows/test.yml",
        ".github/actions/setup-python-tests/action.yml",
        "vireo/data/taxonomy.json",
        "vireo/tests/wait.py",
        "vireo/tests/fixtures/sample.jpg",
        "scripts/select_tests.py",
    ],
)
def test_harness_changes_force_the_full_suite(repo, path):
    _write(repo, path, "changed\n")
    _commit(repo, f"touch {path}")

    sel = _select(repo)

    assert sel.mode == "full"
    assert path in sel.full_reason


def test_missing_map_forces_full_suite(tmp_path):
    impact, meta, problem = select_tests.load_map(tmp_path / "nowhere")

    assert impact is None
    assert "no map" in problem

    sel = select_tests.select("HEAD", "HEAD", None, cwd=tmp_path)
    assert sel.mode == "full"


def test_map_without_contexts_is_rejected(tmp_path):
    map_dir = tmp_path / select_tests.MAP_DIR
    map_dir.mkdir()
    data = CoverageData(basename=str(map_dir / select_tests.MAP_DB))
    data.add_lines({"vireo/app.py": [1]})
    data.write()
    (map_dir / select_tests.MAP_META).write_text(json.dumps({"sha": "abc"}))

    impact, _, problem = select_tests.load_map(map_dir)

    assert impact is None
    assert "no per-test contexts" in problem


def test_selection_file_lists_files_then_ids_and_drops_ids_covered_by_files(tmp_path):
    sel = select_tests.Selection()
    sel.files.add("vireo/tests/test_app.py")
    sel.ids.update({"vireo/tests/test_app.py::test_alpha", "vireo/tests/test_db.py::test_x"})
    out = tmp_path / "sel.txt"

    sel.write(out, "base", "head")

    body = [line for line in out.read_text().splitlines() if not line.startswith("#")]
    assert body == ["vireo/tests/test_app.py", "vireo/tests/test_db.py::test_x"]


# --------------------------------------------------------------------------
# diff / ast helpers
# --------------------------------------------------------------------------


def test_expand_to_functions_picks_innermost_span():
    spans = select_tests.function_spans(
        textwrap.dedent(
            """
            def outer():
                def inner():
                    return 1
                return inner
            """
        )
    )
    # Body lines only: a def line runs in the *enclosing* scope's context.
    assert select_tests.expand_to_functions({4}, spans) == {4}
    assert select_tests.expand_to_functions({3}, spans) == {4}
    assert select_tests.expand_to_functions({2}, spans) == {3, 4, 5}
    assert select_tests.expand_to_functions({1}, spans) is None


def test_function_spans_survive_syntax_errors():
    assert select_tests.function_spans("def broken(:\n") == []


# --------------------------------------------------------------------------
# build-map validation
# --------------------------------------------------------------------------


def _junit(path: Path, ran: int, skipped: int = 0) -> Path:
    cases = "".join(f'<testcase name="t{i}" time="0.1"/>' for i in range(ran))
    cases += "".join(f'<testcase name="s{i}"><skipped/></testcase>' for i in range(skipped))
    path.write_text(f'<testsuites><testsuite name="p">{cases}</testsuite></testsuites>')
    return path


def test_build_map_rejects_partial_coverage(repo, tmp_path):
    coverage_file = repo / select_tests.MAP_DIR / select_tests.MAP_DB
    junit = _junit(tmp_path / "junit.xml", ran=10, skipped=2)

    with pytest.raises(SystemExit, match="looks partial"):
        select_tests.build_map(coverage_file, tmp_path / "out", "sha", cwd=repo, junit_xml=junit)


def test_build_map_accepts_complete_coverage(repo, tmp_path):
    coverage_file = repo / select_tests.MAP_DIR / select_tests.MAP_DB
    junit = _junit(tmp_path / "junit.xml", ran=3, skipped=5)

    meta = select_tests.build_map(coverage_file, tmp_path / "out", "sha123", cwd=repo, junit_xml=junit)

    assert meta["sha"] == "sha123"
    assert meta["contexts"] == 3
    assert meta["executed_tests"] == 3
    written = json.loads((tmp_path / "out" / select_tests.MAP_META).read_text())
    assert written["sha"] == "sha123"


# --------------------------------------------------------------------------
# CLI + pytest plugin end to end
# --------------------------------------------------------------------------


def test_cli_writes_selection_and_prints_mode(repo):
    _write(repo, "vireo/app.py", APP_SRC.replace("return x + 1", "return x + 3"))
    _commit(repo, "edit alpha")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/select_tests.py"), "--repo", str(repo), "--explain"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "subset"
    assert "selected: 0 whole files + 1 individual tests" in result.stderr
    selection = (repo / select_tests.MAP_DIR / select_tests.SELECTION_FILE).read_text()
    assert "vireo/tests/test_app.py::test_alpha" in selection


def test_cli_falls_back_to_full_when_map_commit_is_unreachable(repo):
    meta_path = repo / select_tests.MAP_DIR / select_tests.MAP_META
    meta_path.write_text(json.dumps({"sha": "0" * 40, "root": str(repo)}))

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/select_tests.py"), "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "full"
    assert "not reachable" in result.stderr


def test_plugin_runs_only_listed_tests_and_ignores_missing_ids(tmp_path):
    tests_dir = tmp_path / "vireo" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_one.py").write_text("def test_a(): pass\ndef test_b(): pass\n")
    (tests_dir / "test_two.py").write_text("def test_c(): pass\n")
    (tests_dir / "test_three.py").write_text("def test_d(): pass\n")
    selection = tmp_path / "selected.txt"
    selection.write_text(
        "# mode: subset\nvireo/tests/test_one.py::test_b\nvireo/tests/test_two.py\nvireo/tests/test_one.py::test_gone\n"
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "selected_tests_plugin",
            "--rootdir", str(tmp_path), "-o", "addopts=", "-q", "-rA",
            "--selected-tests", str(selection), str(tests_dir),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED vireo/tests/test_one.py::test_b" in result.stdout
    assert "PASSED vireo/tests/test_two.py::test_c" in result.stdout
    assert "test_a" not in result.stdout.split("short test summary")[1]
    assert "test_three" not in result.stdout
    assert "2 passed, 1 deselected" in result.stdout


def test_plugin_exits_zero_when_nothing_matches(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_one.py").write_text("def test_a(): pass\n")
    selection = tmp_path / "selected.txt"
    selection.write_text("tests/test_one.py::test_gone\n")

    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "selected_tests_plugin",
            "--rootdir", str(tmp_path), "-o", "addopts=", "-q",
            "--selected-tests", str(selection), str(tests_dir),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 deselected" in result.stdout
