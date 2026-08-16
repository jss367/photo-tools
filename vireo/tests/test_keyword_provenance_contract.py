"""Structural guard for the ``photo_keywords.source`` provenance contract.

``retire_builtin_wildlife_genre()`` deletes keyword associations it reads as
generated. The only thing standing between a hand-added keyword and that
delete is ``photo_keywords.source = 'manual'``, so the contract cannot rest on
every future author remembering to stamp it.

Two mechanisms enforce it, and this module pins both:

1. ``Database.tag_photo`` defaults ``source`` to ``'manual'``. The failure
   modes are asymmetric — a wrongly-manual generated tag is a stale keyword
   the user can delete, while a wrongly-unknown manual tag is silent,
   unrecoverable data loss — so the default is the fail-safe one and a call
   site that forgets provenance degrades to "do not delete this".
2. Declining manual authorship is therefore an explicit, reviewable act. Only
   the sidecar readers may do it, and the allowlist below is the whole
   inventory. Adding a new provenance-neutral writer fails this test.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

VIREO_DIR = Path(__file__).resolve().parent.parent

# (module relative to vireo/, enclosing function) → why this writer cannot
# claim manual authorship. A sidecar term is genuinely ambiguous: the user may
# have typed it in Lightroom, or Vireo may have written it out itself.
PROVENANCE_NEUTRAL_WRITERS = {
    ("sync.py", "sync_from_xmp"): "XMP reconcile cannot tell user terms from Vireo's own",
    ("scanner.py", "_import_keywords_for_photo"): "scanned sidecar terms have ambiguous authorship",
}

# Writers that neither claim nor decline authorship: they move an association
# between rows and pass through whatever provenance it already carried. The
# value is computed, so this test can't read it statically — the allowlist is
# the review gate instead.
PROVENANCE_CARRYING_WRITERS = {
    ("db.py", "_apply_winner_loser_merge"): "duplicate merge carries the losers' stamps",
}


def _production_modules():
    """Every shipped .py under vireo/, excluding tests and test helpers."""
    for path in sorted(VIREO_DIR.rglob("*.py")):
        rel = path.relative_to(VIREO_DIR)
        if rel.parts[0] in {"tests", "testing"}:
            continue
        yield rel, path


def _enclosing_function_name(tree, target):
    """Return the innermost function enclosing ``target``, or '<module>'."""
    name = "<module>"
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is target:
                # Deeper matches overwrite shallower ones because ast.walk
                # visits outer functions first.
                name = node.name
    return name


def _tag_photo_calls():
    """Yield (relpath, function, lineno, source_kwarg_node_or_MISSING)."""
    for rel, path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "tag_photo":
                continue
            source = next(
                (kw.value for kw in node.keywords if kw.arg == "source"), None,
            )
            # A positional third argument is also a source declaration.
            if source is None and len(node.args) >= 3:
                source = node.args[2]
            yield (
                str(rel),
                _enclosing_function_name(tree, node),
                node.lineno,
                source,
            )


def _declared_source(node):
    """Resolve a ``source=`` argument to 'manual', None, or 'unrecognized'."""
    if node is None:
        return "<omitted>"
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return {
            "KEYWORD_SOURCE_MANUAL": "manual",
            "KEYWORD_SOURCE_UNKNOWN": None,
        }.get(node.id, "<unrecognized>")
    if isinstance(node, ast.Attribute):
        return {
            "KEYWORD_SOURCE_MANUAL": "manual",
            "KEYWORD_SOURCE_UNKNOWN": None,
        }.get(node.attr, "<unrecognized>")
    return "<unrecognized>"


def test_tag_photo_defaults_to_manual_provenance():
    """The unstamped path must degrade to 'never delete this', not the reverse."""
    from db import KEYWORD_SOURCE_MANUAL, KEYWORD_SOURCE_UNKNOWN, Database

    assert KEYWORD_SOURCE_MANUAL == "manual"
    assert KEYWORD_SOURCE_UNKNOWN is None

    sig = inspect.signature(Database.tag_photo)
    assert sig.parameters["source"].default == KEYWORD_SOURCE_MANUAL, (
        "tag_photo's source default is the fail-safe for every call site that "
        "forgets to declare provenance; it must stay 'manual'."
    )


def test_only_sidecar_readers_decline_manual_provenance():
    """A new provenance-neutral tag_photo() call site must be a deliberate act."""
    neutral = set()
    carrying = set()
    for rel, func, lineno, node in _tag_photo_calls():
        declared = _declared_source(node)
        if declared == "<unrecognized>":
            assert (rel, func) in PROVENANCE_CARRYING_WRITERS, (
                f"{rel}:{lineno} ({func}) passes a source= value this contract "
                f"cannot read statically. Use KEYWORD_SOURCE_MANUAL or "
                f"KEYWORD_SOURCE_UNKNOWN, or — if it forwards an existing "
                f"row's stamp — add it to PROVENANCE_CARRYING_WRITERS."
            )
            carrying.add((rel, func))
            continue
        assert declared in ("manual", None, "<omitted>"), (
            f"{rel}:{lineno} ({func}) passes source={declared!r}, which is not "
            f"a known provenance value."
        )
        if declared is None:
            neutral.add((rel, func))

    assert carrying == set(PROVENANCE_CARRYING_WRITERS), (
        "A listed provenance-carrying writer no longer computes its source. "
        "Drop it from PROVENANCE_CARRYING_WRITERS so the static check applies "
        f"again.\n  found:    {sorted(carrying)}\n"
        f"  expected: {sorted(PROVENANCE_CARRYING_WRITERS)}"
    )

    assert neutral == set(PROVENANCE_NEUTRAL_WRITERS), (
        "Provenance-neutral tagging is limited to the sidecar readers. A "
        "writer that leaves source NULL produces associations that "
        "retire_builtin_wildlife_genre() reads as generated and deletes. If "
        "the new site really cannot know authorship, add it to "
        "PROVENANCE_NEUTRAL_WRITERS with a reason; otherwise let it take the "
        "manual default.\n"
        f"  found:    {sorted(neutral)}\n"
        f"  expected: {sorted(PROVENANCE_NEUTRAL_WRITERS)}"
    )


def test_raw_photo_keywords_inserts_carry_a_source_column():
    """Bypassing tag_photo() must not bypass the provenance stamp."""
    offenders = []
    for rel, path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            lowered = node.value.lower()
            # Check the INSERT's own column list, not the whole statement — an
            # ``ON CONFLICT ... SET source = ...`` clause in the same literal
            # would otherwise mask a column list that omits it.
            for columns in re.findall(
                r"into\s+photo_keywords\s*\(([^)]*)\)", lowered,
            ):
                if "source" not in columns:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "These raw INSERTs write photo_keywords rows without a source column, "
        "so the associations land as NULL ('unknown') no matter who asked for "
        "them. Route them through tag_photo() or carry the source explicitly: "
        + ", ".join(offenders)
    )


@pytest.fixture()
def prov_db(tmp_path):
    from db import Database

    db = Database(str(tmp_path / "prov.db"))
    try:
        yield db
    finally:
        db.close()


def test_unstamped_tag_photo_is_recorded_as_manual(prov_db):
    """A call site that forgets provenance still produces a protected row."""
    fid = prov_db.add_folder("/photos", name="photos")
    pid = prov_db.add_photo(
        folder_id=fid, filename="a.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    kid = prov_db.add_keyword("Wildlife", kw_type="genre")

    prov_db.tag_photo(pid, kid)

    row = prov_db.conn.execute(
        "SELECT source FROM photo_keywords WHERE photo_id = ? AND keyword_id = ?",
        (pid, kid),
    ).fetchone()
    assert row["source"] == "manual"


def test_sidecar_import_leaves_provenance_unknown(prov_db):
    """The allowlisted neutral writers really do write NULL, not 'manual'."""
    from db import KEYWORD_SOURCE_UNKNOWN

    fid = prov_db.add_folder("/photos", name="photos")
    pid = prov_db.add_photo(
        folder_id=fid, filename="b.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    kid = prov_db.add_keyword("Wildlife", kw_type="genre")

    prov_db.tag_photo(pid, kid, source=KEYWORD_SOURCE_UNKNOWN)

    row = prov_db.conn.execute(
        "SELECT source FROM photo_keywords WHERE photo_id = ? AND keyword_id = ?",
        (pid, kid),
    ).fetchone()
    assert row["source"] is None


def test_duplicate_merge_carries_manual_provenance_to_the_winner(prov_db):
    """Moving an association between photo rows must move its authorship too."""
    from db import KEYWORD_SOURCE_UNKNOWN

    fid = prov_db.add_folder("/photos", name="photos")
    winner = prov_db.add_photo(
        folder_id=fid, filename="w.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    loser = prov_db.add_photo(
        folder_id=fid, filename="l.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    hand_added = prov_db.add_keyword("Backlit", kw_type="general")
    scanned = prov_db.add_keyword("Imported", kw_type="general")
    prov_db.tag_photo(loser, hand_added)
    prov_db.tag_photo(loser, scanned, source=KEYWORD_SOURCE_UNKNOWN)

    prov_db._apply_winner_loser_merge(winner, [loser])

    stamps = {
        row["keyword_id"]: row["source"]
        for row in prov_db.conn.execute(
            "SELECT keyword_id, source FROM photo_keywords WHERE photo_id = ?",
            (winner,),
        ).fetchall()
    }
    assert stamps[hand_added] == "manual", (
        "A duplicate merge must not launder a hand-added keyword into an "
        "unattributed association that retirement reads as generated."
    )
    assert stamps[scanned] is None


def test_setting_a_photo_location_records_manual_provenance(prov_db):
    """Assigning a place is a person's explicit act, not generated metadata."""
    fid = prov_db.add_folder("/photos", name="photos")
    pid = prov_db.add_photo(
        folder_id=fid, filename="loc.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    leaf = prov_db.add_keyword("Yosemite", kw_type="location")

    prov_db.set_photo_location(pid, leaf)

    row = prov_db.conn.execute(
        "SELECT source FROM photo_keywords WHERE photo_id = ? AND keyword_id = ?",
        (pid, leaf),
    ).fetchone()
    assert row["source"] == "manual"


def test_manual_stamp_survives_a_later_neutral_retag(prov_db):
    """A sidecar rescan must not downgrade authorship the user established."""
    from db import KEYWORD_SOURCE_MANUAL, KEYWORD_SOURCE_UNKNOWN

    fid = prov_db.add_folder("/photos", name="photos")
    pid = prov_db.add_photo(
        folder_id=fid, filename="c.jpg", extension=".jpg",
        file_size=10, file_mtime=1.0,
    )
    kid = prov_db.add_keyword("Wildlife", kw_type="genre")

    prov_db.tag_photo(pid, kid, source=KEYWORD_SOURCE_MANUAL)
    prov_db.tag_photo(pid, kid, source=KEYWORD_SOURCE_UNKNOWN)

    row = prov_db.conn.execute(
        "SELECT source FROM photo_keywords WHERE photo_id = ? AND keyword_id = ?",
        (pid, kid),
    ).fetchone()
    assert row["source"] == "manual"
