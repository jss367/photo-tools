# Per-File Import Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose which files to import from the preview grid, and make the import job honor and report on that choice without ever falsely declaring a camera card safe to erase.

**Architecture:** The preview grid gains checkboxes whose state is *derived* from a set of user-deselected paths rather than seeded per render. The page sends `include_paths` + two counts to `/api/jobs/import-photos`; the job filters its discovered file list against them, keeps `discovered` meaning "files on the card", and adds explicit conditions to both card-safety verdicts. Backend first, then API, then frontend — each layer is testable before the next depends on it.

**Tech Stack:** Python 3 / Flask, SQLite, vanilla JS in Jinja2 templates, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-07-27-import-file-selection-design.md`

---

## ⚠️ Read this before starting

This feature can cause permanent data loss. After an import, Vireo tells the user whether their camera card is safe to format. If that verdict is wrong, they erase the only copy of their photos.

Five review rounds found **three** separate data-loss paths in the design, all from the same mistake: assuming the ledger equality `(copied + skipped_duplicate) == discovered` would catch a problem *emergently* instead of stating it as an explicit condition.

Two rules follow, and they are not negotiable:

1. **Every safety-relevant intent gets its own condition.** Do not remove a condition because you can prove the arithmetic already covers it. It covers it until two errors cancel.
2. **Every safety change lands in FOUR places** — two verdict blocks (`safe_to_format` and `unverified_duplicates_only`) in each of two copy paths (`run_import_job` and `_run_remote_import_job`). Patching one block per path is how the second data-loss path got in.

| | `safe_to_format` | `unverified_duplicates_only` |
|---|---|---|
| local (`run_import_job`) | `import_job.py:3312-3320` | `3321-3329` |
| remote (`_run_remote_import_job`) | `1924-1933` | `1934-1943` |

Line numbers drift as you edit. Locate the blocks by their contents, not by number.

**Use one shared expression across all four**, so "patched one, missed three" is structurally impossible rather than review-dependent. Task 2 defines:

```python
def _selection_blocks_format(deselected, vanished_paths):
    """True when the user's selection means the card is NOT fully archived.

    Deliberately separate from the ``(copied + skipped_duplicate) ==
    discovered`` ledger check. That equality catches these cases *usually*,
    and three data-loss bugs came from trusting "usually":
      - deselect X, then X also vanishes -> discovered shrinks too, equality
        balances, nothing is wrong arithmetically, and a file the user
        excluded is reported as archived.
    Do not delete either condition because the other "already covers it".
    """
    return deselected > 0 or bool(vanished_paths)
```

### Writing tests that actually guard `unverified_duplicates_only`

This block's **first** condition is `unverified_duplicate > 0`. A test where no unverified duplicates occur asserts nothing — the verdict is already `False`, and an implementer who patches only `safe_to_format` passes it. A guard for this block must satisfy **both**:

- `unverified_duplicate > 0` — requires a catalog row matching filename + size + capture-time with *different bytes*, plus `trust_likely_duplicates=True` and `verify_by_hash=False` (`import_job.py:913`). Model on `test_trust_likely_duplicates_skips_metadata_match_without_byte_check` (`test_import_job.py:619`), which asserts the verdict is `True` on the baseline.
- **the ledger equality still holds** — otherwise the equality already forces `False` and the new condition is untested. This rules out a plain deselection and means the guard case is *deselect-then-vanish*.

**In the remote path this block is currently unreachable**, and no test can change that: `unverified_duplicate` only increments when `not params.verify_by_hash` (913), while `remote_unverified = not params.verify_by_hash` (1910) is itself a condition of the block. The two requirements are mutually exclusive. Wire the conditions there anyway — the mutual exclusion is incidental, not guaranteed, and a future change to either flag would silently re-open it. Do not "simplify" the remote block on the grounds that it can't fire.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `vireo/import_job.py` | `ImportParams` fields; filter; verdict conditions; `unsafe_files`; drift; progress denominator — in **both** copy paths | Modify |
| `vireo/app.py` | Request validation (shape + containment) and wiring into `ImportParams` | Modify |
| `vireo/templates/import.html` | Selection state, checkbox rendering, Start gating, readouts, in-place hiding, request body | Modify |
| `vireo/tests/test_import_job.py` | Job-level behavior, incl. the card-safety regression guards | Modify (exists, 6156 lines) |
| `vireo/tests/test_jobs_api.py` | Request validation | Modify |
| `tests/e2e/test_import_page.py` | Browser behavior | Modify |

No new files. Existing test harnesses cover everything:

- `vireo/tests/test_import_job.py` has `_make_card(tmp_path, [(name, mtime, color), ...])`, `_run_import(tmp_path, params)`, `_make_job()`, `FakeRunner`, and for remote: `_remote_archive_for(tmp_path)`, `_install_fake_remote_rsync(monkeypatch, calls, verify=)`, `_remote_calls(ra)`. Model new tests on `test_fresh_import_is_safe_to_format` (line 330) and `test_remote_import_rsyncs_to_remote_and_catalogs_at_mount` (line 3874).
- `tests/e2e/test_import_page.py` uses `live_server` + `page` fixtures, `page.goto(f"{url}/import")`, and overrides `window.fetch` to stub `/api/import/folder-preview`.

**Run the full import suite after every backend task:**
```bash
python -m pytest vireo/tests/test_import_job.py -q
```
It is slow (~2 min) but it is the regression net for card safety.

---

## Task 1: `ImportParams` fields and the discovery filter (local path)

**Files:**
- Modify: `vireo/import_job.py` (`ImportParams` ~line 202; `run_import_job` ~line 2117)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing test**

Append to `vireo/tests/test_import_job.py`:

```python
def test_include_paths_imports_only_selected_files(tmp_path):
    """include_paths restricts the copy set; discovered still counts the card."""
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
        ("DSC_0003.jpg", datetime(2026, 7, 3, 12, 0, 0), "blue"),
    ])
    keep = {str(card / "DSC_0001.jpg"), str(card / "DSC_0003.jpg")}

    db, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths=keep, previewed_count=3, checked_count=2,
    ))

    assert result["copied"] == 2
    # discovered stays the full card — it backs the card-safety verdict.
    assert result["discovered"] == 3
    assert {r["filename"] for r in _photo_rows(db)} == {
        "DSC_0001.jpg", "DSC_0003.jpg",
    }


def test_include_paths_absent_imports_everything(tmp_path):
    """No selection means no opinion — current behavior is unchanged."""
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
    ))
    assert result["copied"] == 2
    assert result["discovered"] == 2
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest vireo/tests/test_import_job.py::test_include_paths_imports_only_selected_files -q
```
Expected: `TypeError: ImportParams.__init__() got an unexpected keyword argument 'include_paths'`

- [ ] **Step 3: Add the fields**

In `vireo/import_job.py`, inside the `ImportParams` dataclass (after `recursive`):

```python
    # Per-file selection (copy mode only — see the import-file-selection spec).
    # ``include_paths`` is NOT the set of checked boxes: it is
    # ``previewed - user-deselected`` and deliberately still contains files the
    # UI rendered as unchecked duplicates, so the duplicate checker can see,
    # skip and COUNT them. Dropping them here makes them land in no ledger
    # bucket and falsely reports a fully-archived card as unsafe to format.
    include_paths: set | None = None
    # Size of the previewed set and the count the UI showed as checked. Both
    # are transport for values the job cannot reconstruct; ``previewed_count``
    # additionally gates a card-safety condition, so it is not just reporting.
    previewed_count: int | None = None
    checked_count: int | None = None
```

- [ ] **Step 4: Add the filter**

In `run_import_job`, immediately after `discovered = len(files)`:

```python
    discovered = len(files)
    # Snapshot BEFORE filtering — drift is measured against what the card
    # actually holds, and computing it post-filter makes files-appeared zero.
    discovered_paths = {str(f) for f in files}
    if params.include_paths is not None:
        files = [f for f in files if str(f) in params.include_paths]
    queued = len(files)
```

**Placement is safety-critical.** The filter must sit *after* `discovered = len(files)` and *before* the `if params.skip_duplicates:` block. Filtering above `discovered` shrinks it, satisfies `(copied + skipped_duplicate) == discovered`, and reports **safe to format** on a card whose deselected originals were never copied.

- [ ] **Step 5: Run to verify it passes**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k include_paths
```
Expected: 2 passed

- [ ] **Step 6: Run the full import suite for regressions**

```bash
python -m pytest vireo/tests/test_import_job.py -q
```
Expected: no new failures

- [ ] **Step 7: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "feat(import): filter the copy set by include_paths"
```

---

## Task 2: Card-safety conditions (local path)

The core of the feature. Both verdicts must fail closed on a deselection or a vanished in-scope file.

**Files:**
- Modify: `vireo/import_job.py` (`run_import_job`, both verdict blocks)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_deselection_makes_card_unsafe_to_format(tmp_path):
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg")},
        previewed_count=2, checked_count=1,
    ))
    assert result["safe_to_format"] is False
    assert result["unverified_duplicates_only"] is False


def test_full_selection_of_card_with_duplicates_is_safe_to_format(tmp_path):
    """THE duplicate-accounting regression guard.

    Duplicates stay in include_paths, so the checker counts them as
    skipped_duplicate and the ledger balances. If someone "fixes"
    include_paths to mean the checked boxes, this goes false and Vireo
    tells the user not to format a card that is fully archived.
    """
    from import_job import ImportParams

    archive = tmp_path / "archive"
    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    # First import puts both in the archive.
    _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(archive),
    ))
    # Second import of the same card: everything is a duplicate.
    all_paths = {str(card / "DSC_0001.jpg"), str(card / "DSC_0002.jpg")}
    _, _, result = _run_import(
        tmp_path, ImportParams(
            sources=[str(card)], destination=str(archive),
            include_paths=all_paths, previewed_count=2, checked_count=0,
        ),
    )
    assert result["copied"] == 0
    assert result["skipped_duplicate"] == 2
    assert result["safe_to_format"] is True


def test_vanished_in_scope_file_makes_card_unsafe(tmp_path):
    """The ledger equality still balances here — 1 processed of 1 discovered —
    so this needs its own condition."""
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    gone = str(card / "DSC_0002.jpg")  # previewed, then deleted
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg"), gone},
        previewed_count=2, checked_count=2,
    ))
    assert result["copied"] == 1
    assert result["discovered"] == 1
    assert result["safe_to_format"] is False
    assert result["unverified_duplicates_only"] is False


def test_deselected_then_vanished_file_makes_card_unsafe(tmp_path):
    """Deselect X, then X disappears before the job.

    discovered=1, queued=1, copied=1 → the equality balances. vanished_paths
    is empty because X was never in include_paths. Only the explicit
    ``deselected == 0`` condition catches this.
    """
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg")},
        previewed_count=2, checked_count=1,   # DSC_0002 previewed, deselected, gone
    ))
    assert result["copied"] == 1
    assert result["discovered"] == 1
    assert result["safe_to_format"] is False
    assert result["unverified_duplicates_only"] is False


def _seed_likely_twin(tmp_path, db, name="IMG_0400.jpg"):
    """Card file + a catalog row matching name/size/capture-time with
    DIFFERENT bytes. With trust_likely_duplicates=True this drives
    unverified_duplicate > 0 — the precondition that makes
    unverified_duplicates_only reachable at all. Lifted from
    test_trust_likely_duplicates_skips_metadata_match_without_byte_check.
    """
    from PIL.ExifTags import Base as ExifBase

    dt = datetime(2026, 5, 1, 10, 15, 30)
    card = tmp_path / "card"
    card.mkdir(exist_ok=True)
    card_file = card / name
    img = Image.new("RGB", (16, 16), "red")
    exif = img.getexif()
    exif[ExifBase.DateTimeOriginal] = dt.strftime("%Y:%m:%d %H:%M:%S")
    img.save(str(card_file), exif=exif)
    card_bytes = card_file.read_bytes()

    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    (library / name).write_bytes(
        card_bytes[:-1] + bytes([card_bytes[-1] ^ 0xFF]))

    fid = db.conn.execute(
        "INSERT INTO folders (path, name, status) VALUES (?, ?, 'ok')",
        (str(library), "library"),
    ).lastrowid
    db.conn.execute(
        "INSERT INTO photos (folder_id, filename, extension, file_size,"
        " timestamp) VALUES (?, ?, '.jpg', ?, ?)",
        (fid, name, len(card_bytes), "2026-05-01T10:15:30"),
    )
    db.conn.commit()
    return card, card_file


def test_deselected_then_vanished_also_blocks_the_amber_verdict(tmp_path):
    """NON-VACUOUS guard for the second verdict block.

    unverified_duplicates_only's FIRST condition is unverified_duplicate > 0,
    so any test without a likely-duplicate asserts nothing — the verdict is
    already False and a patch to safe_to_format alone would pass. This setup
    makes it True on the baseline, and the ledger equality HOLDS (0 copied +
    1 skipped == 1 discovered), so only the explicit deselected == 0
    condition can flip it.

    The amber pill it renders says "keep the card until likely duplicates are
    verified" — asserting duplicate verification is the SOLE blocker. Its
    remedy is a re-run with verify_by_hash, on which the deselected file is
    gone from the preview too, everything verifies, and the pill goes GREEN
    over a card that was never fully archived.
    """
    from import_job import ImportParams, run_import_job

    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    card, card_file = _seed_likely_twin(tmp_path, db)

    result = run_import_job(
        _make_job(), FakeRunner(), db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=str(tmp_path / "archive"),
            trust_likely_duplicates=True,
            include_paths={str(card_file)},
            previewed_count=2, checked_count=1,  # a 2nd file was deselected, then vanished
        ),
    )
    # Preconditions: without these the assertions below are vacuous.
    assert result["unverified_duplicate"] == 1
    assert result["copied"] + result["skipped_duplicate"] == result["discovered"]

    assert result["safe_to_format"] is False
    assert result["unverified_duplicates_only"] is False


def test_vanished_file_also_blocks_the_amber_verdict(tmp_path):
    """Same non-vacuous shape, for the vanished_paths condition."""
    from import_job import ImportParams, run_import_job

    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    card, card_file = _seed_likely_twin(tmp_path, db)

    result = run_import_job(
        _make_job(), FakeRunner(), db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=str(tmp_path / "archive"),
            trust_likely_duplicates=True,
            include_paths={str(card_file), str(card / "GONE.jpg")},
            previewed_count=2, checked_count=2,
        ),
    )
    assert result["unverified_duplicate"] == 1
    assert result["copied"] + result["skipped_duplicate"] == result["discovered"]

    assert result["safe_to_format"] is False
    assert result["unverified_duplicates_only"] is False
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k "unsafe or safe_to_format"
```
Expected: the three "unsafe" tests fail with `assert True is False`

- [ ] **Step 3: Compute the signals**

In `run_import_job`, after the filter from Task 1:

```python
    # Selection drift. Computed against the pre-filter snapshot.
    deselected = 0
    vanished_paths = set()
    appeared = 0
    if params.include_paths is not None and params.previewed_count is not None:
        deselected = params.previewed_count - len(params.include_paths)
        vanished_paths = params.include_paths - discovered_paths
        appeared = max(0, len(discovered_paths) - params.previewed_count)
```

- [ ] **Step 4: Add the shared helper and apply it to BOTH verdict blocks**

Define `_selection_blocks_format` (see the header section) at module level, then add this **one line** to `safe_to_format` **and** to `unverified_duplicates_only`:

```python
        and not _selection_blocks_format(deselected, vanished_paths)
```

One shared expression means a future condition added to the helper lands in all four blocks automatically.

`unverified_duplicates_only` renders an amber pill reading *"Import complete — keep the card until likely duplicates are verified"* (`import.html:2613`), which asserts duplicate verification is the **sole** remaining blocker. Patching only `safe_to_format` leaves that path live.

- [ ] **Step 5: Run to verify they pass**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k "unsafe or safe_to_format or amber_verdict"
```
Expected: all pass

- [ ] **Step 6: Prove the amber guard is not vacuous**

This is the step that catches the failure mode the whole plan is organized around.

Temporarily remove the new condition from `unverified_duplicates_only` only (leave `safe_to_format` patched), then:

```bash
python -m pytest vireo/tests/test_import_job.py -q -k amber_verdict
```
Expected: **FAIL**. If it passes, the tests are asserting nothing — check that `unverified_duplicate == 1` and that the ledger equality holds in the assertions above. Restore the condition and re-run to green before continuing.

- [ ] **Step 7: Full suite**

```bash
python -m pytest vireo/tests/test_import_job.py -q
```

- [ ] **Step 8: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "feat(import): fail card-safety closed on deselection or vanished files"
```

---

## Task 3: `unsafe_files` entries (local path)

A red "Do NOT format" pill with an empty list tells the user nothing.

**Files:**
- Modify: `vireo/import_job.py`
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing test**

```python
def _unsafe_paths(result):
    return {u["path"] for u in result["unsafe_files"]}


def test_deselection_explains_itself_on_the_result_card(tmp_path):
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg")},
        previewed_count=2, checked_count=1,
    ))
    assert "Deselected files" in _unsafe_paths(result)
    entry = next(u for u in result["unsafe_files"]
                 if u["path"] == "Deselected files")
    assert "1 files you deselected were not copied" in entry["reason"]
    # Must NOT claim the card holds the only copies — false when the
    # deselected file is byte-identical to a selected one.
    assert "only copies" not in entry["reason"]


def test_vanished_file_explains_itself_on_the_result_card(tmp_path):
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg"), str(card / "GONE.jpg")},
        previewed_count=2, checked_count=2,
    ))
    assert "Files missing at import time" in _unsafe_paths(result)
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k explains_itself
```
Expected: `KeyError` / assertion on missing entry

- [ ] **Step 3: Append the entries**

Next to the existing `unverified_duplicate` entry (search for `"path": "Likely duplicates"`):

```python
    if deselected > 0:
        unsafe_files.append({
            "path": "Deselected files",
            "reason": f"{deselected} files you deselected were not copied",
        })
    if vanished_paths:
        unsafe_files.append({
            "path": "Files missing at import time",
            "reason": f"{len(vanished_paths)} files were in scope but had "
                      "disappeared from the source when the import ran",
        })
    if appeared > 0:
        unsafe_files.append({
            "path": "Files added after preview",
            "reason": f"at least {appeared} files arrived after your preview "
                      "and were not imported — re-preview to include them",
        })
```

Entries render as `li.textContent = u.path + ' — ' + u.reason` (`import.html:2636-2638`) — plain text, no markup. They are also mirrored into `result["errors"]`.

Note these entries *attribute* the ledger gap without fully explaining it: deselect X, have X vanish, and have a new file Y arrive, and the only line rendered is about X while the unimported file is Y. Do not write copy claiming the list is exhaustive.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k explains_itself
```

- [ ] **Step 5: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "feat(import): explain why a partial import blocks card formatting"
```

---

## Task 4: Drift reporting and progress denominator (local path)

**Files:**
- Modify: `vireo/import_job.py`
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing test**

```python
def test_drift_signals_and_progress_denominator(tmp_path):
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
        ("DSC_0003.jpg", datetime(2026, 7, 3, 12, 0, 0), "blue"),
    ])
    runner = FakeRunner()
    _run_import(
        tmp_path,
        ImportParams(
            sources=[str(card)], destination=str(tmp_path / "archive"),
            include_paths={str(card / "DSC_0001.jpg")},
            previewed_count=3, checked_count=1,
        ),
        runner=runner,
    )
    totals = {d.get("total") for _, kind, d in runner.events
              if kind == "progress" and d.get("total")}
    # Progress runs on the queued workload (1), never the full card (3) —
    # otherwise a finished import sits at 33% and looks hung.
    assert 3 not in totals


def test_ordinary_deselection_reports_no_files_appeared(tmp_path):
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg")},
        previewed_count=2, checked_count=1,
    ))
    assert result["files_appeared"] == 0
    assert result["files_vanished"] == 0


def test_mixed_appear_and_vanish_never_reports_a_negative_count(tmp_path):
    """files_appeared is a net delta clamped at zero. Without the clamp, more
    vanishing than arriving renders "-3 files were added"."""
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    _, _, result = _run_import(tmp_path, ImportParams(
        sources=[str(card)], destination=str(tmp_path / "archive"),
        include_paths={str(card / "DSC_0001.jpg"),
                       str(card / "GONE_A.jpg"), str(card / "GONE_B.jpg")},
        previewed_count=3, checked_count=3,
    ))
    assert result["files_appeared"] == 0
    assert result["files_vanished"] == 2


def test_step_summary_selected_figure_comes_from_checked_count(tmp_path):
    """Not len(include_paths) — that set retains unchecked duplicates and
    would overstate what the user chose."""
    from import_job import ImportParams

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    runner = FakeRunner()
    _run_import(
        tmp_path,
        ImportParams(
            sources=[str(card)], destination=str(tmp_path / "archive"),
            include_paths={str(card / "DSC_0001.jpg"),
                           str(card / "DSC_0002.jpg")},
            previewed_count=2, checked_count=1,
        ),
        runner=runner,
    )
    summaries = [kw.get("summary", "") for _, _, kw in runner.step_updates]
    assert any("1 selected of 2 discovered" in s for s in summaries)
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k "drift_signals or files_appeared"
```

- [ ] **Step 3: Add the result keys and switch the denominator**

Add to the `result` dict:

```python
        "files_appeared": appeared,
        "files_vanished": len(vanished_paths),
```

Change the `_emit(...)` calls in the copy loop from `discovered` to `queued`, and update the step summary:

```python
        summary=(
            f"{copied} copied, {skipped_duplicate} already present, "
            f"{failed} failed of {discovered} discovered"
        ),
```
becomes:
```python
        summary=(
            (f"{params.checked_count} selected of {discovered} discovered, "
             if params.checked_count is not None else "")
            + f"{copied} copied, {skipped_duplicate} already present, "
              f"{failed} failed"
        ),
```

The selected figure comes from `checked_count`, **not** `len(include_paths)` — that set contains unchecked duplicates and would overstate what the user chose.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest vireo/tests/test_import_job.py -q
```

- [ ] **Step 5: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "feat(import): report selection drift and scale progress to the selection"
```

---

## Task 5: Mirror everything into the remote copy path

**A half-wired remote path is the silent failure mode.** `run_import_job` delegates to `_run_remote_import_job` (`import_job.py:523`) at line 1987 whenever `params.remote_target` is set — the import page reaches it whenever the user picks a saved NAS target. That function has its own discovery loop, its own `discovered = len(files)` (line 646), its own `checker.prepare(files)` (653), its own `_emit` calls (806, 830, 904, 1847), its own step summary (1879), and its own two verdict blocks (1924-1933, 1934-1943).

**Files:**
- Modify: `vireo/import_job.py` (`_run_remote_import_job`)
- Test: `vireo/tests/test_import_job.py`

- [ ] **Step 1: Write the failing test**

Model the setup on `test_remote_import_rsyncs_to_remote_and_catalogs_at_mount` (line 3874).

```python
def test_remote_import_honors_include_paths_and_card_safety(
        tmp_path, monkeypatch):
    """Every assertion from Tasks 1-3, against the remote path."""
    from import_job import ImportParams, run_import_job

    ra = _remote_archive_for(tmp_path)
    calls = _remote_calls(ra)
    _install_fake_remote_rsync(monkeypatch, calls, verify=None)

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
        ("DSC_0002.jpg", datetime(2026, 7, 3, 11, 0, 0), "green"),
    ])
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    result = run_import_job(
        _make_job(), FakeRunner(), db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=ra["mount_base"],
            remote_target=ra, verify_by_hash=True,
            include_paths={str(card / "DSC_0001.jpg")},
            previewed_count=2, checked_count=1,
        ),
    )

    assert result["copied"] == 1
    assert result["discovered"] == 2
    assert "Deselected files" in {u["path"] for u in result["unsafe_files"]}
    # verify_by_hash=True so remote_unverified is False and this assertion
    # actually depends on the new condition rather than passing for free.
    assert result["safe_to_format"] is False


def test_remote_deselected_then_vanished_is_unsafe(tmp_path, monkeypatch):
    """The equality balances (1 copied of 1 discovered); only the explicit
    deselected condition catches it."""
    from import_job import ImportParams, run_import_job

    ra = _remote_archive_for(tmp_path)
    # verify=None is the "verified OK" sentinel. Any non-None value is treated
    # as a (name, detail) failure tuple and unpacked (import_job.py:1295), so
    # verify=True raises TypeError instead of failing an assertion.
    _install_fake_remote_rsync(monkeypatch, _remote_calls(ra), verify=None)

    card = _make_card(tmp_path, [
        ("DSC_0001.jpg", datetime(2026, 7, 3, 10, 0, 0), "red"),
    ])
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    result = run_import_job(
        _make_job(), FakeRunner(), db_path, db._active_workspace_id,
        ImportParams(
            sources=[str(card)], destination=ra["mount_base"],
            remote_target=ra, verify_by_hash=True,
            include_paths={str(card / "DSC_0001.jpg")},
            previewed_count=2, checked_count=1,
        ),
    )
    assert result["copied"] == 1
    assert result["discovered"] == 1
    assert result["safe_to_format"] is False
```

**Both remote tests must pass `verify_by_hash=True`.** Otherwise
`remote_unverified = not params.verify_by_hash` (`import_job.py:1910`) is `True`,
both verdicts are already `False`, and the tests pass on unmodified `main` —
Step 2's "verify it fails" would not fail.

Neither test asserts `unverified_duplicates_only`. Per the header, that block is
unreachable in the remote path (`unverified_duplicate` needs
`not verify_by_hash`; `remote_unverified` is that same flag), so an assertion
would be vacuous in both directions. Wire the condition anyway; do not test it
here.

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k remote_import_honors
```
Expected: `assert 2 == 1` on `copied` — the filter isn't wired

- [ ] **Step 3: Apply Tasks 1-4 to `_run_remote_import_job`**

Work through this checklist. Each item mirrors the local change:

- [ ] filter + `discovered_paths` + `queued`, after `discovered = len(files)` (line 646), before `checker.prepare(files)` (653)
- [ ] `deselected` / `vanished_paths` / `appeared` computation
- [ ] `and not _selection_blocks_format(deselected, vanished_paths)` on `safe_to_format` (1924-1933) — the **shared helper** from Task 2, not two inlined conditions; inlining defeats the whole point of the helper and lets a future condition land in only two of the four blocks
- [ ] the same single line on `unverified_duplicates_only` (1934-1943), even though that block is currently unreachable here (see the header) — do not skip it
- [ ] the three `unsafe_files` entries
- [ ] `files_appeared` / `files_vanished` in the result dict
- [ ] `_emit` denominator → `queued` at **806, 830, 904, 1847**
- [ ] step summary at **1879**

If the two functions now share substantial logic, extracting a helper is welcome — but only after these tests pass, so the refactor is covered.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest vireo/tests/test_import_job.py -q -k remote
```

- [ ] **Step 5: Full suite**

```bash
python -m pytest vireo/tests/test_import_job.py -q
```

- [ ] **Step 6: Commit**

```bash
git add vireo/import_job.py vireo/tests/test_import_job.py
git commit -m "feat(import): honor include_paths in the remote copy path"
```

---

## Task 6: Request validation

**Files:**
- Modify: `vireo/app.py` (`/api/jobs/import-photos`, ~line 23583)
- Test: `vireo/tests/test_jobs_api.py`

- [ ] **Step 1: Write the failing tests**

```python
def _import_body(tmp_path, **over):
    src = str(tmp_path / "card")
    os.makedirs(src, exist_ok=True)
    Image.new('RGB', (16, 16)).save(os.path.join(src, 'a.jpg'))
    body = {
        "sources": [src],
        "destination": str(tmp_path / "archive"),
        "include_paths": [os.path.join(src, "a.jpg")],
        "previewed_count": 1,
        "checked_count": 1,
    }
    body.update(over)
    return body


@pytest.mark.parametrize("over", [
    {"include_paths": []},                       # empty list
    {"include_paths": "not-a-list"},
    {"include_paths": [123]},
    {"include_paths": [""]},
    {"previewed_count": -1},
    {"previewed_count": True},                   # bool is an int in Python
    {"checked_count": True},
    {"previewed_count": None},                   # partial field set
    {"include_paths": None},                     # counts without paths
    {"checked_count": 5},                        # > len(include_paths)
    {"include_paths": ["/etc/passwd"]},          # outside sources
    {"include_paths": ["relative/path.jpg"]},    # commonpath ValueError -> 400
])
def test_import_photos_rejects_bad_selection(app_and_db, tmp_path, over):
    app, _ = app_and_db
    resp = app.test_client().post(
        '/api/jobs/import-photos', json=_import_body(tmp_path, **over),
    )
    assert resp.status_code == 400


def test_import_photos_accepts_a_valid_selection(app_and_db, tmp_path):
    app, _ = app_and_db
    resp = app.test_client().post(
        '/api/jobs/import-photos', json=_import_body(tmp_path),
    )
    assert resp.status_code == 200


def test_repeated_paths_do_not_inflate_the_deselected_count(app_and_db,
                                                            tmp_path):
    """include_paths is deduped at validation. Without it, a client repeating
    a path shrinks previewed_count - len(include_paths) and could hide a
    deselection from the card-safety verdict."""
    app, _ = app_and_db
    body = _import_body(tmp_path)
    body["include_paths"] = body["include_paths"] * 3
    resp = app.test_client().post('/api/jobs/import-photos', json=body)
    assert resp.status_code == 200


def test_import_in_place_ignores_include_paths(app_and_db, tmp_path):
    """That route is unchanged by this feature — it must not half-apply a
    selection it has no machinery to honor."""
    app, _ = app_and_db
    src = str(tmp_path / "card")
    os.makedirs(src, exist_ok=True)
    Image.new('RGB', (16, 16)).save(os.path.join(src, 'a.jpg'))
    resp = app.test_client().post('/api/jobs/import-in-place', json={
        "sources": [src],
        "include_paths": [os.path.join(src, "nonexistent.jpg")],
    })
    assert resp.status_code == 200


def test_import_photos_accepts_symlinked_file_inside_source(
        app_and_db, tmp_path):
    """Lexical containment admits it, matching today's ingest() behavior.

    Regression guard against someone "hardening" this with realpath, which
    would break imports that work today.
    """
    app, _ = app_and_db
    src = tmp_path / "card"
    src.mkdir()
    outside = tmp_path / "outside.jpg"
    Image.new('RGB', (16, 16)).save(str(outside))
    os.symlink(str(outside), str(src / "link.jpg"))

    resp = app.test_client().post('/api/jobs/import-photos', json={
        "sources": [str(src)],
        "destination": str(tmp_path / "archive"),
        "include_paths": [str(src / "link.jpg")],
        "previewed_count": 1,
        "checked_count": 1,
    })
    assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest vireo/tests/test_jobs_api.py -q -k selection
```

- [ ] **Step 3: Implement validation**

In `/api/jobs/import-photos`, after `sources` is validated:

```python
        include_paths = body.get("include_paths")
        previewed_count = body.get("previewed_count")
        checked_count = body.get("checked_count")

        # All three travel together or none do. A partial set would fabricate
        # drift figures and then blow up on None inside the job.
        provided = [v is not None
                    for v in (include_paths, previewed_count, checked_count)]
        if any(provided) and not all(provided):
            return json_error(
                "include_paths, previewed_count and checked_count must be "
                "sent together", 400,
            )

        if include_paths is not None:
            if not isinstance(include_paths, list) or not include_paths:
                return json_error("include_paths must be a non-empty list", 400)
            if any(not isinstance(p, str) or not p for p in include_paths):
                return json_error(
                    "include_paths must contain non-empty strings", 400,
                )
            # bool is a subclass of int — {"previewed_count": true} would
            # otherwise sail through as 1.
            for name, val in (("previewed_count", previewed_count),
                              ("checked_count", checked_count)):
                if type(val) is not int or val < 0:
                    return json_error(
                        f"{name} must be a non-negative integer", 400,
                    )
            include_paths = set(include_paths)
            if len(include_paths) > previewed_count:
                return json_error(
                    "include_paths cannot exceed previewed_count", 400,
                )
            if checked_count > len(include_paths):
                return json_error(
                    "checked_count cannot exceed include_paths", 400,
                )

            # Containment is LEXICAL by design. normpath catches the real
            # threat (a client naming files outside the chosen folders:
            # "/src/../etc/passwd" collapses and fails). It deliberately does
            # not resolve symlinks — discover_source_files returns symlinked
            # files inside a source and ingest() copies them today, so
            # realpath here would newly reject working imports. Do not
            # "harden" this without reading the spec's §4.
            norm_sources = [os.path.normpath(s) for s in sources]
            for p in include_paths:
                np = os.path.normpath(p)
                try:
                    ok = any(os.path.commonpath([np, s]) == s
                             for s in norm_sources)
                except ValueError:
                    # Mixed absolute/relative — a containment failure, not a 500.
                    ok = False
                if not ok:
                    return json_error(
                        f"include_paths contains a path outside the selected "
                        f"source folders: {p}", 400,
                    )
```

- [ ] **Step 4: Run to verify**

```bash
python -m pytest vireo/tests/test_jobs_api.py -q -k selection
```

- [ ] **Step 5: Commit**

```bash
git add vireo/app.py vireo/tests/test_jobs_api.py
git commit -m "feat(import): validate the per-file selection payload"
```

---

## Task 7: Wire the selection into `ImportParams`

**Files:**
- Modify: `vireo/app.py` (~line 24045)

- [ ] **Step 1: Write the failing test**

```python
def test_import_photos_passes_selection_to_the_job(app_and_db, tmp_path,
                                                   monkeypatch):
    app, _ = app_and_db
    captured = {}
    import import_job

    real = import_job.run_import_job

    def spy(job, runner, db_path, ws, params):
        captured["params"] = params
        return real(job, runner, db_path, ws, params)

    monkeypatch.setattr(import_job, "run_import_job", spy)

    resp = app.test_client().post(
        '/api/jobs/import-photos', json=_import_body(tmp_path),
    )
    assert resp.status_code == 200
    wait_for_job_via_client(app.test_client(), resp.get_json()['job_id'])

    params = captured["params"]
    assert params.include_paths is not None
    assert params.previewed_count == 1
    assert params.checked_count == 1
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest vireo/tests/test_jobs_api.py -q -k passes_selection
```

- [ ] **Step 3: Pass the fields**

In the `ImportParams(...)` construction:

```python
                include_paths=include_paths,
                previewed_count=previewed_count,
                checked_count=checked_count,
```

**Do not add `include_paths` to `job_config`.** The other params are recorded there by convention, but there is no re-run-from-config path and a 5,000-entry path list would bloat the job row.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest vireo/tests/test_jobs_api.py -q
```

- [ ] **Step 5: Commit**

```bash
git add vireo/app.py vireo/tests/test_jobs_api.py
git commit -m "feat(import): pass the selection through to the import job"
```

---

## Task 8: Selection state and derived checkboxes

Backend is complete and tested. Now the UI.

**Files:**
- Modify: `vireo/templates/import.html` (state ~line 490; `renderImportPreviewGrid` ~1927; CSS ~line 60)
- Test: `tests/e2e/test_import_page.py`

- [ ] **Step 1: Write the failing test**

```python
def _stub_preview(page, files, duplicates=None):
    """Stub folder-preview + check-duplicates, and put the page in COPY mode.

    Two things here are load-bearing:
      - #modeInPlace is `checked` by default (import.html:232). Without
        selecting copy mode, previewImport() returns early at the
        `if (!copyMode)` branch (2301), no duplicate stream runs, and per
        Task 11 the checkboxes are hidden entirely — every selection test
        would fail for the wrong reason.
      - check-duplicates is SSE, not newline-JSON. The client parses
        `buffer.split('\\n\\n')` + /^data: (.+)$/m (import.html:2330-2341).
        Frames must be `data: {...}\\n\\n`. Copied from the existing stub at
        tests/e2e/test_import_page.py:102-116.
    """
    page.locator("#modeCopy").check()
    page.evaluate(
        """
        ([files, dupes]) => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: files.length, total_size: 0,
                type_breakdown: {'.jpg': files.length},
                duplicate_count: 0, files: files,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (t && t.indexOf('/api/import/check-duplicates') === 0) {
              const frame = 'data: ' + JSON.stringify({
                duplicates: dupes, checked: files.length, total: files.length,
              }) + '\\n\\n' + 'data: ' + JSON.stringify({
                done: true, duplicate_count: dupes.length,
                checked: files.length, total: files.length,
              }) + '\\n\\n';
              return Promise.resolve(new Response(frame, {
                status: 200,
                headers: {'Content-Type': 'text/event-stream'},
              }));
            }
            return originalFetch(input, init);
          };
          window.pickDirectory = async () => ['/tmp/card'];
        }
        """,
        [files, duplicates or []],
    )


def _preview(page):
    """Add the stubbed source and wait for the grid to settle."""
    page.locator("[data-testid='import-source-browse-btn']").click()
    page.locator("#btnPreview").click()
    expect(page.locator("#importPreviewGrid")).to_be_visible()


def _files(n, prefix='/tmp/card/DSC_'):
    return [{"path": f"{prefix}{i:04d}.jpg", "filename": f"DSC_{i:04d}.jpg",
             "subfolder": "card", "size": 100, "extension": ".jpg",
             "mtime": 0, "thumb_url": ""} for i in range(n)]


def test_import_preview_files_are_checked_by_default(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    expect(boxes).to_have_count(3)
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()


def test_import_preview_duplicates_are_unchecked_and_disabled(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    _preview(page)

    dupe = page.locator(
        f".import-preview-thumb[data-path='{files[1]['path']}'] .thumb-check")
    expect(dupe).not_to_be_checked()
    expect(dupe).to_be_disabled()
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k "checked_by_default or unchecked_and_disabled"
```

- [ ] **Step 3: Add state**

Near the other module-level state (~line 490):

```js
// User intent ONLY: paths the user explicitly unchecked. Duplicate verdicts
// are a separate eligibility overlay and are never written in here — see the
// import-file-selection spec §1. Keeping these separate is what makes the
// checkbox state safe to re-derive on every render.
let importDeselected = new Set();
let importDuplicatePaths = new Set();
let importPreviewedPaths = [];
let importSelectionAnchor = null;
let importPreviewCapturedSignature = null;
let importPreviewInFlight = false;
let importDupStreamPending = false;
```

- [ ] **Step 4: Render checkboxes with derived state**

First, at the **top of `renderImportPreviewGrid`** (before the `Object.keys(groups)` loop). Task 9's folder-header code also needs `skipDupes`, and declaring it inside the per-file loop would put it out of scope there:

```js
  const skipDupes = document.getElementById('chkSkipDuplicates').checked;
```

Then inside the per-file loop, before appending `imgWrap`:

```js
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.className = 'thumb-check';
      // Derived, never seeded. The renderer runs up to three times per
      // preview and duplicate verdicts arrive late; recomputing from state
      // each pass means a late verdict just changes the answer.
      check.checked = !importDeselected.has(f.path)
        && !(isDuplicate && skipDupes);
      check.disabled = isDuplicate && skipDupes;
      check.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleImportSelection(f.path, check.checked, e.shiftKey);
      });
      card.appendChild(check);
```

Add the CSS next to the other `.import-preview-*` rules:

```css
.import-preview-thumb { position: relative; }
.import-preview-thumb .thumb-check { position: absolute; top: 4px; left: 4px; z-index: 2; }
.import-preview-thumb .thumb-check:disabled { opacity: 0.5; cursor: not-allowed; }
```

Add the toggle and the counting helpers. `updateImportSelectionUI` must land **here**, not in Task 10 — Task 9's tests click checkboxes, and a handler calling an undefined function throws `ReferenceError`:

```js
function toggleImportSelection(path, checked, shiftKey) {
  // Range handling arrives in Task 9.
  if (checked) importDeselected.delete(path);
  else importDeselected.add(path);
  importSelectionAnchor = path;
  updateImportSelectionUI();
}

// Count off the RENDERED CARDS, never off importDuplicatePaths. That set is
// assigned only when the whole check-duplicates stream drains and is never
// cleared, so on a re-preview it still holds the PREVIOUS run's verdicts
// while the cards on screen carry none. Counters reading it under-report
// over a grid of ticked, enabled, badge-free boxes — and the numbers drive
// the select-all checkbox, so a stale count doesn't just print a wrong
// figure, it disables a live control with nothing on screen explaining why.
// Task 9 refactors this into importGridCards()/importSelectableCardPaths().
function importSelectablePaths() {
  const grid = document.getElementById('importPreviewGrid');
  if (!grid) return [];
  const skipDupes = document.getElementById('chkSkipDuplicates').checked;
  return Array.from(grid.querySelectorAll('.import-preview-thumb'))
    .filter(el => !(el.classList.contains('duplicate') && skipDupes))
    .map(el => el.dataset.path)
    .filter(p => !!p);
}

function importEligibleCount() {
  return importSelectablePaths().length;
}

function importCheckedCount() {
  return importSelectablePaths().filter(p => !importDeselected.has(p)).length;
}

function updateImportSelectionUI() {
  const el = document.getElementById('previewSelectedCount');
  if (el) {
    // The CHECKED count — what will actually be copied. Never the
    // include_paths count, which is larger because it retains duplicates.
    el.textContent = importCheckedCount().toLocaleString() + ' of '
      + importPreviewedPaths.length.toLocaleString() + ' selected';
  }
  const row = document.getElementById('selectAllRow');
  // The selectionEnabled check matters: without it a later call to this
  // function can re-show the select-all row in in-place mode after Task 11
  // hid it inside the renderer.
  const selectionEnabled = newImagesSnapshotId === null
    && selectedImportMode() === 'copy';
  if (row) {
    row.style.display =
      (selectionEnabled && importPreviewedPaths.length) ? '' : 'none';
  }
  if (typeof updateStartGate === 'function') updateStartGate();  // Task 10
}
```

- [ ] **Step 5: Capture duplicate verdicts**

In `previewImport`, where `duplicatePaths` is finalized before the render at line ~2357:

```js
    importDuplicatePaths = new Set(duplicatePaths);
```

And where the file list first arrives:

```js
    importPreviewedPaths = files.map(f => f.path);
```

- [ ] **Step 6: Run to verify**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k "checked_by_default or unchecked_and_disabled"
```

- [ ] **Step 7: Commit**

```bash
git add vireo/templates/import.html tests/e2e/test_import_page.py
git commit -m "feat(import): per-file checkboxes in the preview grid"
```

---

## Task 9: Folder headers, select-all, and shift-range

**Files:**
- Modify: `vireo/templates/import.html`
- Test: `tests/e2e/test_import_page.py`

- [ ] **Step 1: Write the failing test**

```python
def test_shift_click_selects_a_contiguous_range(live_server, page):
    page.goto(f"{live_server['url']}/import")
    files = _files(5)
    _stub_preview(page, files)
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    boxes.nth(1).click()                          # uncheck index 1
    boxes.nth(3).click(modifiers=["Shift"])       # range 1..3 unchecked
    for i, want in enumerate([True, False, False, False, True]):
        if want:
            expect(boxes.nth(i)).to_be_checked()
        else:
            expect(boxes.nth(i)).not_to_be_checked()


def test_folder_header_checkbox_toggles_its_subfolder(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.locator(".import-preview-folder-header .folder-check").first.click()
    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).not_to_be_checked()
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k "shift_click or folder_header"
```

- [ ] **Step 3: Restructure the folder header**

The header is currently `header.textContent = ...` (`import.html:1954`), so it must become child elements — appending is not enough:

```js
    const header = document.createElement('div');
    header.className = 'import-preview-folder-header';
    const folderCheck = document.createElement('input');
    folderCheck.type = 'checkbox';
    folderCheck.className = 'folder-check';
    const groupPaths = groups[subfolder]
      .filter(f => !(importDuplicatePaths.has(f.path) && skipDupes))
      .map(f => f.path);
    const onCount = groupPaths.filter(p => !importDeselected.has(p)).length;
    folderCheck.checked = onCount > 0;
    folderCheck.indeterminate = onCount > 0 && onCount < groupPaths.length;
    folderCheck.addEventListener('change', () => {
      groupPaths.forEach(p => {
        if (folderCheck.checked) importDeselected.delete(p);
        else importDeselected.add(p);
      });
      updateImportSelectionUI();
      rerenderImportPreviewGridSafe();
    });
    header.appendChild(folderCheck);
    const label = document.createElement('span');
    label.textContent = subfolder + ' (' + groups[subfolder].length + ')';
    header.appendChild(label);
```

Disabled duplicate cards are excluded from `groupPaths` — they cannot be deselected because they are already ineligible.

- [ ] **Step 4: Add shift-range**

Replace `toggleImportSelection`:

```js
function toggleImportSelection(path, checked, shiftKey) {
  const skipDupes = document.getElementById('chkSkipDuplicates').checked;
  // Range runs over VISIBLE render order, so a filter (e.g. Hide duplicates)
  // can't make a shift-click toggle cards the user cannot see.
  const visible = Array.from(
    document.querySelectorAll('.import-preview-thumb')).map(el => el.dataset.path);
  let targets = [path];
  if (shiftKey && importSelectionAnchor !== null) {
    const a = visible.indexOf(importSelectionAnchor);
    const b = visible.indexOf(path);
    if (a !== -1 && b !== -1) {
      targets = visible.slice(Math.min(a, b), Math.max(a, b) + 1);
    }
  }
  targets
    .filter(p => !(importDuplicatePaths.has(p) && skipDupes))
    .forEach(p => {
      if (checked) importDeselected.delete(p);
      else importDeselected.add(p);
    });
  importSelectionAnchor = path;
  updateImportSelectionUI();
  rerenderImportPreviewGridSafe();
}

// The hide-duplicates-checkbox branch adds rerenderImportPreviewGrid(). Until
// it merges, fall back to a targeted DOM refresh.
function rerenderImportPreviewGridSafe() {
  if (typeof rerenderImportPreviewGrid === 'function') {
    rerenderImportPreviewGrid();
    return;
  }
  const skipDupes = document.getElementById('chkSkipDuplicates').checked;
  document.querySelectorAll('.import-preview-thumb').forEach((el) => {
    const cb = el.querySelector('.thumb-check');
    if (!cb) return;
    const isDup = importDuplicatePaths.has(el.dataset.path);
    cb.checked = !importDeselected.has(el.dataset.path) && !(isDup && skipDupes);
    cb.disabled = isDup && skipDupes;
  });
}
```

- [ ] **Step 5: Add select-all**

Next to `#previewSummary` in the markup:

```html
<label class="preview-filter" id="selectAllRow" style="display:none;">
  <input type="checkbox" id="chkSelectAllImport" checked>
  <span id="previewSelectedCount"></span>
</label>
```

```js
document.getElementById('chkSelectAllImport').addEventListener('change', (e) => {
  const skipDupes = document.getElementById('chkSkipDuplicates').checked;
  importPreviewedPaths
    .filter(p => !(importDuplicatePaths.has(p) && skipDupes))
    .forEach(p => {
      if (e.target.checked) importDeselected.delete(p);
      else importDeselected.add(p);
    });
  updateImportSelectionUI();
  rerenderImportPreviewGridSafe();
});
```

- [ ] **Step 6: Run to verify**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k "shift_click or folder_header"
```

- [ ] **Step 7: Commit**

```bash
git add vireo/templates/import.html tests/e2e/test_import_page.py
git commit -m "feat(import): folder, select-all and shift-range selection"
```

---

## Task 10: Start gating and readouts

**Files:**
- Modify: `vireo/templates/import.html`
- Test: `tests/e2e/test_import_page.py`

- [ ] **Step 1: Write the failing test**

```python
def test_start_is_disabled_when_all_importable_files_are_unchecked(
        live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)

    page.locator("#chkSelectAllImport").click()   # select none
    expect(page.locator("#btnStart")).to_be_disabled()


def test_start_stays_enabled_when_every_file_is_a_duplicate(live_server, page):
    """Zero checked, zero eligible — the user must still be able to run the
    import to get the safe-to-format verdict on an already-archived card."""
    page.goto(f"{live_server['url']}/import")
    files = _files(2)
    _stub_preview(page, files, duplicates=[f["path"] for f in files])
    _preview(page)

    expect(page.locator("#btnStart")).to_be_enabled()


def test_changing_a_source_after_selecting_disables_start(live_server, page):
    """Toggling a signature input must gate Start immediately.

    #chkRecursive is wired into wireDestStructureInvalidation (import.html:733),
    which fires scheduleImportPreview() on a 350ms debounce. Suppress that
    entirely: if the auto-preview starts, updateStartGate checks
    importPreviewInFlight BEFORE staleness and the label reads "Previewing…"
    instead, making the assertion timing-dependent.
    """
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    page.evaluate("() => { window.scheduleImportPreview = () => {}; }")
    page.locator("#chkRecursive").click()   # invalidates the signature
    expect(page.locator("#btnStart")).to_be_disabled()
    expect(page.locator("#btnStart")).to_have_text(
        "Preview again before importing")


def test_start_is_disabled_while_a_preview_is_in_flight(live_server, page):
    """The window between clearImportPreviewGrid() and the render is the
    5,000-file hazard: state must not reset to 'no preview run' there."""
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    page.evaluate(
        """() => {
          const f = window.fetch;
          window.__release = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return new Promise((res) => { window.__release = res; });
            }
            return f(input, init);
          };
        }"""
    )
    page.locator("#btnPreview").click()
    expect(page.locator("#btnStart")).to_be_disabled()
    # The prior selection survives the in-flight window.
    assert page.evaluate("() => importDeselected.size") == 1


def test_start_is_disabled_while_the_duplicate_stream_is_draining(
        live_server, page):
    """Checkbox eligibility is not final until verdicts land, so submitting
    mid-stream would send an include_paths that doesn't match the screen."""
    page.goto(f"{live_server['url']}/import")
    page.locator("#modeCopy").check()
    page.evaluate(
        """(files) => {
          const f = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/import/folder-preview') === 0) {
              return Promise.resolve(new Response(JSON.stringify({
                total_count: files.length, total_size: 0,
                type_breakdown: {'.jpg': files.length},
                duplicate_count: 0, files: files,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (t && t.indexOf('/api/import/check-duplicates') === 0) {
              return new Promise(() => {});   // stream never drains
            }
            return f(input, init);
          };
          window.pickDirectory = async () => ['/tmp/card'];
        }""",
        _files(3),
    )
    _preview(page)
    expect(page.locator("#btnStart")).to_be_disabled()


def test_zero_file_preview_disables_start(live_server, page):
    """A completed preview that found nothing is NOT 'no preview run' —
    otherwise later arrivals would be imported unseen."""
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, [])
    _preview(page)
    expect(page.locator("#btnStart")).to_be_disabled()


def test_with_skip_duplicates_off_every_card_is_checked_and_enabled(
        live_server, page):
    """The duplicate stream returns early at import.html:2303 in this mode,
    so there are no verdicts and the derived-checked rule yields all-on."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[1]["path"]])
    page.locator("#chkSkipDuplicates").uncheck()
    _preview(page)

    boxes = page.locator(".import-preview-thumb .thumb-check")
    for i in range(3):
        expect(boxes.nth(i)).to_be_checked()
        expect(boxes.nth(i)).to_be_enabled()


def test_selected_count_readout(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(4))
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()

    expect(page.locator("#previewSelectedCount")).to_have_text("3 of 4 selected")
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k "start_is_disabled or stays_enabled or disables_start or count_readout"
```

- [ ] **Step 3: Add the single gate owner**

`#btnStart.disabled` is currently written from seven places (`import.html:2477, 2496, 2535, 2776, 2810, 2817, 2842`) — `finishJob` at 2535 re-enables it after every import. Route them all through one function or they will race:

```js
// SINGLE OWNER of #btnStart.disabled and its label. Every other site that
// touches the button must call this instead of assigning directly.
function updateStartGate() {
  const btn = document.getElementById('btnStart');
  if (!btn) return;
  const copyMode = newImagesSnapshotId === null
    && selectedImportMode() === 'copy';
  let reason = null;
  if (activeJobId) reason = 'Importing…';
  else if (copyMode && importPreviewInFlight) reason = 'Previewing…';
  else if (copyMode && importPreviewCapturedSignature !== null
           && importPreviewSignatureChanged(importPreviewCapturedSignature)) {
    reason = 'Preview again before importing';
  } else if (copyMode && importDupStreamPending) reason = 'Checking duplicates…';
  else if (copyMode && importEligibleCount() > 0 && importCheckedCount() === 0) {
    reason = 'No files selected';
  }
  btn.disabled = reason !== null;
  btn.textContent = reason
    || (importPreviewCapturedSignature !== null && copyMode
        ? 'Start import (' + importCheckedCount().toLocaleString() + ' files)'
        : 'Start import');
}

```

`importEligibleCount`, `importCheckedCount` and `updateImportSelectionUI` were defined in Task 8 — do not redefine them here.

The `importEligibleCount() > 0` qualifier is load-bearing: a card where every file is already archived renders zero checked through no choice of the user's, and blocking Start there would remove the ability to run the import for its safe-to-format verdict.

- [ ] **Step 4: Maintain the lifecycle flags**

In `previewImport`: set `importPreviewInFlight = true` at the top (and call `updateStartGate()`), set `importDupStreamPending = true` before the check-duplicates fetch and `false` when it drains, and on success set `importPreviewCapturedSignature = requestSignature`, reset **both** `importDeselected = new Set()` and `importDuplicatePaths = new Set()`, then `updateImportSelectionUI()`.

Resetting `importDuplicatePaths` matters: it is only assigned when the duplicate stream lands, so without a reset the first-render window of a new preview evaluates `importCheckedCount()` and the derived `checked`/`disabled` state against the *previous* preview's verdicts.

**Do not clear selection state at the top of `previewImport`.** `clearImportPreviewGrid()` runs at line 2238, *before* the slow disk walk. Clearing there parks the page in "no preview → import everything" for the whole walk with a signature that still matches, which is the exact 5,000-file hazard the gate exists to prevent. Replace state on success, not on start.

Per-exit behavior:

| Exit | State |
|---|---|
| zero files (`2286-2289`) | preview current, empty set, Start disabled |
| throws (`2366-2372`) | previous state retained, Start disabled (stale) |
| superseded (`2281`, `2350`, `2363`) | previous state retained; the newer run owns the transition |

Also call `updateStartGate()` from `wireDestStructureInvalidation` (731-751) so signature changes gate immediately.

- [ ] **Step 5: Run to verify**

```bash
python -m pytest tests/e2e/test_import_page.py -q
```

- [ ] **Step 6: Commit**

```bash
git add vireo/templates/import.html tests/e2e/test_import_page.py
git commit -m "feat(import): gate Start on selection and preview freshness"
```

---

## Task 11: Hide selection in in-place mode

**Files:**
- Modify: `vireo/templates/import.html`
- Test: `tests/e2e/test_import_page.py`

- [ ] **Step 1: Write the failing test**

```python
def test_in_place_mode_hides_selection_and_explains_why(live_server, page):
    page.goto(f"{live_server['url']}/import")
    _stub_preview(page, _files(3))
    page.locator("#modeInPlace").click()
    _preview(page)

    expect(page.locator(".import-preview-thumb .thumb-check")).to_have_count(0)
    expect(page.locator("#selectionUnavailableNote")).to_be_visible()
    expect(page.locator("#selectionUnavailableNote")).to_contain_text(
        "In-place import catalogs every file")
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k in_place_mode_hides
```

- [ ] **Step 3: Implement**

Add the note next to `#importPreviewGrid`:

```html
<div class="preview-summary" id="selectionUnavailableNote" style="display:none;">
  File selection is available when copying files. In-place import catalogs every
  file in the folder.
</div>
```

Guard the checkbox creation in `renderImportPreviewGrid` and the folder header on copy mode, and toggle the note:

```js
  const selectionEnabled = newImagesSnapshotId === null
    && selectedImportMode() === 'copy';
  document.getElementById('selectionUnavailableNote').style.display =
    selectionEnabled ? 'none' : '';
  document.getElementById('selectAllRow').style.display =
    selectionEnabled && files.length ? '' : 'none';
```

Hiding the controls without saying why leaves the user unable to tell whether selection is missing, broken, or gated behind a setting. The note states what the import *will* do.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k in_place_mode_hides
```

- [ ] **Step 5: Commit**

```bash
git add vireo/templates/import.html tests/e2e/test_import_page.py
git commit -m "feat(import): hide selection in in-place mode with an explanation"
```

---

## Task 12: Send the selection

**Files:**
- Modify: `vireo/templates/import.html` (`startImport`, ~line 2401)
- Test: `tests/e2e/test_import_page.py`

- [ ] **Step 1: Write the failing test**

```python
def test_start_import_sends_include_paths_with_duplicates_retained(
        live_server, page):
    """include_paths keeps a file the UI shows as an unchecked duplicate —
    the job needs it to count the duplicate and keep the ledger balanced."""
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[2]["path"]])
    page.evaluate(
        """() => {
          const f = window.fetch;
          window.__body = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-photos') === 0) {
              window.__body = JSON.parse(init.body);
              return Promise.resolve(new Response(
                JSON.stringify({job_id: 'import-x'}), {status: 200,
                headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
        }"""
    )
    # Fill the destination BEFORE previewing. #destInput is wired into
    # wireDestStructureInvalidation (import.html:733), so filling it after
    # selecting schedules a re-preview whose success path resets
    # importDeselected — the selection assertions would then flake.
    page.locator("#destInput").fill("/tmp/archive")
    _preview(page)
    page.locator(".import-preview-thumb .thumb-check").first.click()
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert set(body["include_paths"]) == {files[1]["path"], files[2]["path"]}
    assert body["previewed_count"] == 3
    assert body["checked_count"] == 1


def test_a_duplicate_unchecked_before_verdicts_still_reaches_the_job(
        live_server, page):
    """At first render (import.html:2290) verdicts have not arrived, so
    duplicate cards are still enabled and clickable. A click there writes the
    path into importDeselected and nothing removes it — the eligibleDeselections
    filter in startImport is what stops that path being dropped from
    include_paths, where it would land in no ledger bucket and falsely report
    a fully-archived card as unsafe to format.
    """
    page.goto(f"{live_server['url']}/import")
    files = _files(3)
    _stub_preview(page, files, duplicates=[files[2]["path"]])
    page.locator("#destInput").fill("/tmp/archive")   # before preview — see above
    _preview(page)
    page.evaluate(
        """() => {
          const f = window.fetch;
          window.__body = null;
          window.fetch = (input, init) => {
            const t = typeof input === 'string' ? input : input.url;
            if (t && t.indexOf('/api/jobs/import-photos') === 0) {
              window.__body = JSON.parse(init.body);
              return Promise.resolve(new Response(
                JSON.stringify({job_id: 'import-x'}), {status: 200,
                headers: {'Content-Type': 'application/json'}}));
            }
            return f(input, init);
          };
        }"""
    )
    # Simulate the pre-verdict click: the path IS a duplicate, but the user
    # unchecked it before the stream landed, so it sits in importDeselected.
    page.evaluate("(p) => importDeselected.add(p)", files[2]["path"])
    page.locator("#btnStart").click()

    body = page.evaluate("() => window.__body")
    assert files[2]["path"] in body["include_paths"]
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/e2e/test_import_page.py -q -k sends_include_paths
```

- [ ] **Step 3: Build the payload**

In `startImport()`, in the copy-mode branch:

```js
    if (importPreviewCapturedSignature !== null) {
      const skipDupes = document.getElementById('chkSkipDuplicates').checked;
      // include_paths is NOT the checked boxes. A deselection only counts if
      // the file was eligible in the first place, so a click landing on a
      // duplicate before its verdict arrived is discarded here. Duplicates
      // must reach the job or they land in no ledger bucket and a
      // fully-archived card is falsely reported unsafe to format.
      const eligibleDeselections = new Set(
        Array.from(importDeselected).filter(
          p => !(skipDupes && importDuplicatePaths.has(p))));
      body.include_paths = importPreviewedPaths.filter(
        p => !eligibleDeselections.has(p));
      body.previewed_count = new Set(importPreviewedPaths).size;
      body.checked_count = importCheckedCount();
    }
```

`previewed_count` is the **unique** path count: `/api/import/folder-preview` appends per source with no cross-source dedup (`app.py:17500-17506`), so nested sources like `/card` and `/card/DCIM` emit the same file twice.

- [ ] **Step 4: Run to verify**

```bash
python -m pytest tests/e2e/test_import_page.py -q
```

- [ ] **Step 5: Commit**

```bash
git add vireo/templates/import.html tests/e2e/test_import_page.py
git commit -m "feat(import): send the file selection to the import job"
```

---

## Task 13: Full verification

- [ ] **Step 1: Run the project's required suite**

```bash
python -m pytest tests/test_workspaces.py vireo/tests/test_db.py \
  vireo/tests/test_app.py vireo/tests/test_photos_api.py \
  vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py \
  vireo/tests/test_darktable_api.py vireo/tests/test_config.py -q
```

- [ ] **Step 2: Run the import and e2e suites**

```bash
python -m pytest vireo/tests/test_import_job.py vireo/tests/test_check_duplicates.py -q
python -m pytest tests/e2e/test_import_page.py -q
```

Four pre-existing failures exist in `vireo/tests` on some machines — confirm any failure is pre-existing on `main` before treating it as a regression.

- [ ] **Step 3: Drive the real app**

Use the @verify skill. Launch:

```bash
python vireo/app.py --db ~/.vireo/vireo.db --port 8080
```

Walk a real card through: preview → deselect a few → confirm the count and Start label → import → confirm the result card explains the formatting warning rather than showing a bare red pill.

- [ ] **Step 4: Create the PR**

Per `CLAUDE.md`, the spec ships with the implementation in one PR (matching #1314 and #1380).

```bash
gh pr create --base main \
  --title "Per-file selection in the import preview" \
  --body "$(cat <<'EOF'
Adds checkboxes to the import preview grid so files can be deselected or
cherry-picked. Scoped to copy mode (local + remote); in-place hides the
controls with an explanation rather than showing controls that do nothing.

Also fixes a pre-existing transparency gap: preview and import discovered
files independently, so the two could disagree with no acknowledgement.

## Card safety

The design review found three ways this could falsely report a card as
safe to format. All three are explicit conditions with named regression
tests, asserted across both verdict blocks in both copy paths:

- deselection (including when the deselected file then vanishes)
- files vanishing between preview and import
- duplicates being dropped from the job's scope

See `docs/superpowers/specs/2026-07-27-import-file-selection-design.md`.

## Tests

<!-- paste results -->
EOF
)"
```

---

## Notes for the implementer

**`include_paths` is the thing to get right.** It is `previewed − user-deselected`, **not** the set of checked boxes. Duplicates render unchecked and disabled but stay in the payload. The reflexive implementation — `include_paths = paths.filter(p => checked[p])` — reintroduces a data-loss bug that took three review rounds to find. Both the JS and the `ImportParams` field carry comments saying so; leave them there.

**Coordination with `hide-duplicates-checkbox`** (worktree `banjul`): that branch adds `lastImportPreviewRender` / `rerenderImportPreviewGrid()` and modifies the same two render functions. `rerenderImportPreviewGridSafe()` in Task 9 detects it. Whichever branch merges second must verify that toggling "Hide duplicates" preserves `importDeselected`, that `previewed_count` still reflects the endpoint response rather than the visible count, and that the early return at `import.html:2303` still holds — §1 and §2 depend on the duplicate stream not running when Skip duplicates is off.
