# Browse: slim batch bar + unified action menu

Date: 2026-08-02
Status: Approved by user (mockup reviewed 2026-08-02)

## Problem

The browse page has two hand-maintained bulk-action surfaces that have drifted apart:

- The **batch bar** (`#batchBar`, `browse.html:1579-1599`) renders 18 controls in one
  non-wrapping row. At a 1680px-wide window, Make Offline is squeezed and
  Export / Delete / Clear are cut off entirely.
- The **photo context menu** (`buildPhotoContextMenu()`, `browse.html:8633`) has 9
  actions the bar lacks; the bar has 6 the menu lacks. Neither is a superset, so
  users must remember which surface holds which verb.

## Design

### 1. `buildPhotoContextMenu()` becomes the single complete action surface

Add the five batch-bar-only actions to the menu, reusing the existing handlers
verbatim (all already operate on the current selection):

| New menu item | Handler | Placement |
|---|---|---|
| `Review on Map` | `reviewLocationsForSelection()` | after `View on Map` |
| `Develop` | `developSelected()` | after `Edit Photo` |
| `Send to iNaturalist` | `batchSubmitInat()` | new final group, before Delete |
| `Make Offline` | `makeAvailableOffline()` | same group |
| `Export…` | `openExportModal()` | same group, after `Prepare Full Resolution` (which moves into this group) |

Resulting menu order: chip rows (rating / color / flag) · Find Similar, View on
Map, Review on Map, Compare, Best Batch, Review Burst · Edit Photo, Develop,
Open in Editor, Reveal in Finder/File Manager, Copy Path · Add Keyword…, Add to
Collection…, Highlights items, Representative items, Adjust Capture Time… ·
Send to iNaturalist, Make Offline, Prepare Full Resolution, Export… · Delete.

Also: add the missing **Purple** chip to the color row (`colorChip('purple', …)`).
Purple already exists in the detail panel, filter bar, and collection rules —
the context menu is the only surface missing it.

### 2. Batch bar slims to high-frequency verbs

New bar contents, left to right:

```
N selected · ★1 ★3 ★5 · Flag · Reject | + Keyword · + Collection |
Compare · Review Burst | Export · Delete · More ▾ · Clear (right-aligned)
```

Removed from the bar (all reachable via More ▾ / right-click): Review on Map,
Best Batch, Prepare Full Resolution, Develop, iNaturalist, Make Offline.
Review Burst stays on the bar by explicit user request. Thin visual separators
divide the groups shown above.

Existing conditional visibility is unchanged: `Compare` and `Review Burst`
still appear only for selections of ≥2 (`updateCompareButton`,
`updateBurstReviewButton`). `updateBestBatchButton` and
`_setPrepareFullResolutionButton` already null-guard their `getElementById`,
so removing those buttons is safe; Best Batch gating continues to apply to the
menu item unchanged, and prepare-full-resolution progress remains visible in
the bottom jobs panel.

### 3. More ▾ opens the same menu

The `More ▾` button calls `openContextMenu(syntheticEvent,
buildPhotoContextMenu(getActiveSelection()))`, where `syntheticEvent` is
`{clientX, clientY}` derived from the button's `getBoundingClientRect()`
(bottom-left corner) — `openContextMenu` only reads those two fields and
already clamps to the viewport. One builder, two entry points; the surfaces
cannot drift again.

Single-photo-only items (Find Similar, View on Map, Edit Photo, Open in
Editor, Reveal, Develop is selection-based so unaffected) keep their existing
`disabled` + `disabledHint` behavior when the selection has ≥2 photos.

## Not in scope

- Grouping the menu into submenus (separate suggestion, needs
  `openContextMenu` submenu support in `_navbar.html`).
- Changing the bar's ★1/★3/★5 to full 0–5 ratings.
- Keyboard shortcut for the purple color label.
- Label unification beyond the touched items (the menu keeps `Add Keyword…` /
  `Add to Collection…`; the bar keeps `+ Keyword` / `+ Collection`).
- The native Tauri menu (`_navbar.html`) — it dispatches to the same functions,
  which are all unchanged.

## Testing

- Backend is untouched; run the CLAUDE.md required suite to confirm no
  regressions.
- Playwright drive of the real app: select photos, verify the slimmed bar fits
  at 1400px, More ▾ opens the menu with the five added items and the purple
  chip, right-click shows the identical menu, and one added action
  (e.g. Export…) opens its modal from the menu.
