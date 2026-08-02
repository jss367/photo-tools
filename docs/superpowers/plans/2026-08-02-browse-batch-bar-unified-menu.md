# Browse Slim Batch Bar + Unified Action Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slim the browse batch bar to high-frequency verbs and make `buildPhotoContextMenu()` the single complete action surface, opened both by right-click and a new More ▾ button.

**Architecture:** All changes live in `vireo/templates/browse.html` (inline HTML + JS, per this codebase's one-file-per-page pattern). The context menu builder gains the five batch-bar-only actions plus the missing purple chip; the batch bar HTML is rewritten to 12 controls + More ▾; More ▾ re-uses the builder via `openContextMenu` with synthetic coordinates. Backend untouched.

**Tech Stack:** Flask/Jinja2 template, vanilla JS, pytest (Flask `test_client` HTML assertions), Playwright for live verification.

**Spec:** `docs/superpowers/specs/2026-08-02-browse-batch-bar-unified-menu-design.md`

---

### Task 1: Failing template test

**Files:**
- Test: `vireo/tests/test_app.py` (append after `test_browse_page`, ~line 61)

- [ ] **Step 1: Write the failing test**

Follow the existing `test_browse_page` pattern (`app_and_db` fixture, string assertions on the rendered page):

```python
def test_browse_slim_batch_bar_and_unified_menu(app_and_db):
    """The batch bar keeps only high-frequency verbs plus More; the photo
    context menu is the complete action surface (spec
    docs/superpowers/specs/2026-08-02-browse-batch-bar-unified-menu-design.md)."""
    app, _ = app_and_db
    html = app.test_client().get('/browse').get_data(as_text=True)
    # More button opens the unified menu
    assert 'id="batchMoreBtn"' in html
    assert 'function openBatchMoreMenu(' in html
    # Bar-only buttons that moved into the menu are gone from the bar
    for removed in ('id="resolveGpsSelectedBtn"', 'id="bestBatchBtn"',
                    'id="prepareFullResolutionBtn"', 'id="developBtn"'):
        assert removed not in html
    # The five former bar-only actions now exist as menu items
    for label in ("label: 'Review on Map'", "label: 'Develop'",
                  "label: 'Send to iNaturalist'", "label: 'Make Offline'",
                  "label: 'Export\\u2026'"):
        assert label in html
    # Purple joins the menu's color chip row
    assert "colorChip('purple'" in html
    # Kept on the bar: Compare, Review Burst, Export, Delete
    for kept in ('id="compareBtn"', 'id="burstReviewBtn"',
                 'onclick="openExportModal()"', 'onclick="batchDelete()"'):
        assert kept in html
```

Note: `"label: 'Export\\u2026'"` is Python for the literal JS source text
`label: 'Export\u2026'` — the menu source spells the ellipsis as the
six characters `\u2026`, matching the existing `'Add Keyword\u2026'` at
browse.html:8700. Do NOT write a literal `…` character in the JS source
(the test asserts on the escaped source text).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest vireo/tests/test_app.py::test_browse_slim_batch_bar_and_unified_menu -v`
Expected: FAIL on `assert 'id="batchMoreBtn"' in html`

- [ ] **Step 3: Commit the failing test? No — commit test + implementation together per task; proceed to Task 2.**

### Task 2: Context menu becomes the complete surface

**Files:**
- Modify: `vireo/templates/browse.html:8664-8713` (`buildPhotoContextMenu` return value)

- [ ] **Step 1: Add the purple chip**

In the color chip row (currently lines 8666-8672), append after the blue chip:

```js
      colorChip('purple', '\u25CF', 'Purple'),
```

- [ ] **Step 2: Add "Review on Map" after "View on Map"**

After the `View on Map` item (lines 8681-8682) insert:

```js
    { label: 'Review on Map',
      onClick: function() { reviewLocationsForSelection(); } },
```

- [ ] **Step 3: Move "Prepare Full Resolution" out of the review group**

Delete the `Prepare Full Resolution` item from its current spot (lines
8689-8690, directly after `Review Burst`). It reappears in the new final
group in Step 5.

- [ ] **Step 4: Add "Develop" after "Edit Photo"**

After the `Edit Photo` item (lines 8692-8693) insert:

```js
    { label: 'Develop',
      onClick: function() { developSelected(); } },
```

- [ ] **Step 5: Add the send/prepare group before Delete**

Replace the tail (currently):

```js
    { label: 'Adjust Capture Time\u2026', onClick: function() { openCaptureTimeModal(); } },
    { separator: true },
    { label: 'Delete', onClick: function() { batchDelete(); } },
  ]);
```

with:

```js
    { label: 'Adjust Capture Time\u2026', onClick: function() { openCaptureTimeModal(); } },
    { separator: true },
    { label: 'Send to iNaturalist', onClick: function() { batchSubmitInat(); } },
    { label: 'Make Offline', onClick: function() { makeAvailableOffline(); } },
    { label: 'Prepare Full Resolution',
      onClick: function() { prepareFullResolutionSelection(photoIds); } },
    { label: 'Export\u2026', onClick: function() { openExportModal(); } },
    { separator: true },
    { label: 'Delete', onClick: function() { batchDelete(); } },
  ]);
```

All five handlers are the exact functions the batch bar buttons call today
(`browse.html:1587-1596`); they operate on the current selection, which the
right-click path has already coerced to match `photoIds`.

- [ ] **Step 6: Run the new test — expect it to still FAIL (bar not yet slimmed), but the menu-label assertions now pass**

Run: `python -m pytest vireo/tests/test_app.py::test_browse_slim_batch_bar_and_unified_menu -v`
Expected: FAIL, now on `assert 'id="resolveGpsSelectedBtn"' not in html` (or `batchMoreBtn`)

### Task 3: Slim the batch bar + More ▾

**Files:**
- Modify: `vireo/templates/browse.html:1579-1599` (batch bar HTML)
- Modify: `vireo/templates/browse.html` (add `openBatchMoreMenu` next to `buildPhotoContextMenu`, ~line 8714)

- [ ] **Step 1: Rewrite the batch bar HTML**

Replace lines 1579-1599 with (keeps each button's existing inline style
idiom; adds thin separator spans between groups; `Clear` keeps
`margin-left:auto`):

```html
    <div id="batchBar" style="display:none;background:var(--bg-secondary);padding:8px 20px;border-bottom:1px solid var(--border-primary);align-items:center;gap:10px;flex-shrink:0;">
      <span id="batchCount" style="font-size:12px;color:var(--accent);font-weight:600;"></span>
      <button onclick="batchSetRating(1)" style="background:var(--bg-tertiary);color:var(--warning);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">&#9733;1</button>
      <button onclick="batchSetRating(3)" style="background:var(--bg-tertiary);color:var(--warning);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">&#9733;3</button>
      <button onclick="batchSetRating(5)" style="background:var(--bg-tertiary);color:var(--warning);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">&#9733;5</button>
      <button onclick="batchSetFlag('flagged')" style="background:var(--bg-tertiary);color:var(--accent);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">Flag</button>
      <button onclick="batchSetFlag('rejected')" style="background:var(--bg-tertiary);color:var(--danger);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">Reject</button>
      <span style="width:1px;height:20px;background:var(--border-primary);"></span>
      <button onclick="batchAddKeyword()" style="background:var(--bg-tertiary);color:var(--text-secondary);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">+ Keyword</button>
      <button onclick="addToCollection()" style="background:var(--bg-tertiary);color:var(--info);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">+ Collection</button>
      <span style="width:1px;height:20px;background:var(--border-primary);"></span>
      <button id="compareBtn" onclick="openBrowseCompare()" title="Compare two selected photos side by side" style="display:none;background:var(--bg-tertiary);color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">Compare</button>
      <button id="burstReviewBtn" onclick="openSelectedInBurstReview()" title="Open selected photos in burst review" style="display:none;background:var(--bg-tertiary);color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">Review Burst</button>
      <span style="width:1px;height:20px;background:var(--border-primary);"></span>
      <button onclick="openExportModal()" style="background:var(--bg-tertiary);color:var(--text-secondary);border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;">Export</button>
      <button onclick="batchDelete()" style="background:var(--danger);color:white;border:none;border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;" title="Delete selected photos">Delete</button>
      <button id="batchMoreBtn" onclick="openBatchMoreMenu(this)" title="All actions for the selected photos — same menu as right-click" style="background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border-secondary);border-radius:3px;padding:4px 8px;font-size:12px;cursor:pointer;font-weight:600;">More &#9662;</button>
      <button onclick="clearSelection()" style="background:none;color:var(--text-faint);border:none;padding:4px 8px;font-size:12px;cursor:pointer;margin-left:auto;">Clear</button>
    </div>
```

Removed buttons: `resolveGpsSelectedBtn` (Review on Map), `bestBatchBtn`,
`prepareFullResolutionBtn`, `developBtn`, iNaturalist, Make Offline. Their
JS helpers stay: `updateBestBatchButton` (browse.html:5427) and
`_setPrepareFullResolutionButton` (browse.html:9318) both null-guard, and
`reviewLocationsForSelection` / `developSelected` / `batchSubmitInat` /
`makeAvailableOffline` / `openBestBatch` are still called from the menu or
native Tauri menu.

- [ ] **Step 2: Add `openBatchMoreMenu`**

Directly after `buildPhotoContextMenu`'s closing brace (~line 8713), add:

```js
// The More button on the batch bar opens the same menu as right-click on a
// card, anchored under the button. openContextMenu only reads clientX/Y and
// clamps to the viewport, so a synthetic event object is enough.
function openBatchMoreMenu(btn) {
  var ids = getActiveSelection();
  if (!ids.length) return;
  var r = btn.getBoundingClientRect();
  openContextMenu({ clientX: r.left, clientY: r.bottom + 4 },
                  buildPhotoContextMenu(ids));
}
```

- [ ] **Step 3: Run the new test to verify it passes**

Run: `python -m pytest vireo/tests/test_app.py::test_browse_slim_batch_bar_and_unified_menu -v`
Expected: PASS

- [ ] **Step 4: Run the browse-page regression tests**

Run: `python -m pytest vireo/tests/test_app.py -q -k "browse"`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add vireo/templates/browse.html vireo/tests/test_app.py
git commit -m "Slim browse batch bar; unify actions in the photo context menu"
```

### Task 4: Full required suite

- [ ] **Step 1: Run the CLAUDE.md required suite**

Run: `python -m pytest tests/test_workspaces.py vireo/tests/test_db.py vireo/tests/test_app.py vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py vireo/tests/test_config.py -q`
Expected: all pass (note: the local machine has 4 known pre-existing failures
in the *full* `vireo/tests` sweep, but this required subset is expected
green; investigate anything that fails here).

### Task 5: Live verification (Playwright)

The user's Vireo instance is running (old code) and holds the
`~/.vireo/runtime.lock` single-instance slot. The lock and config paths hang
off `$HOME`, so launch the verify server with an isolated HOME and a copy of
the real DB:

- [ ] **Step 1: Stage an isolated instance**

```bash
mkdir -p /tmp/vireo-verify/.vireo
sqlite3 ~/.vireo/vireo.db ".backup /tmp/vireo-verify/.vireo/vireo.db"
HOME=/tmp/vireo-verify nohup python3 vireo/app.py --db /tmp/vireo-verify/.vireo/vireo.db --port 8199 > /tmp/vireo-verify/server.log 2>&1 &
for i in $(seq 1 45); do curl -sf http://localhost:8199/browse -o /dev/null && break; sleep 1; done
```

- [ ] **Step 2: Drive with Playwright (viewport 1400×1000)**

Script checklist (adapt `/tmp/annotate_browse.py` from this session):
1. Load `/browse`, wait for `.grid-card` count ≥ 4 (poll; first query takes ~4s). The copied DB carries the user's paused/filter state — if 0 cards, click `.vf-mute` (this is a throwaway DB copy, no restore needed).
2. Select 4 photos (click + shift-click) → `#batchBar` visible; assert the bar's right edge (Clear button) is inside a 1400px viewport.
3. Click `#batchMoreBtn` → `.vireo-ctx-menu` appears; assert it contains `Review on Map`, `Develop`, `Send to iNaturalist`, `Make Offline`, `Prepare Full Resolution`, `Export…`, `Delete`, and 6 color chips in the color row.
4. Press Escape; right-click a selected card → same menu appears.
5. Click `Export…` in the menu → `#exportOverlay` becomes visible.
6. Screenshot bar + open menu; check `console` for errors.

Expected: all assertions pass, screenshot shows the slim bar and unified menu.

- [ ] **Step 3: Tear down**

```bash
lsof -ti:8199 -sTCP:LISTEN | xargs -r kill
rm -rf /tmp/vireo-verify
```

### Task 6: PR

- [ ] **Step 1: Push and create the PR**

```bash
git push -u origin nashville
gh pr create --base main --title "Browse: slim batch bar, unified photo action menu" --body "<what changed + spec link + test results + screenshots>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

PR body must include: problem (overflow + drift), the More ▾ = right-click
unification, the five added menu items + purple chip, removed bar buttons,
test results, and the verification screenshot.
