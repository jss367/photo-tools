# Workspaces Design

## Problem

Vireo currently operates as a single global instance — one database, one config, all photos mixed together. Users who organize their photography by trip or location (e.g., "Kenya 2025" vs. "USA backyard") have no way to scope their workflow to a subset of their library. They also can't maintain separate classification pipelines (different models, thresholds, label sets) for different projects.

## Core Model

A **workspace** is a named scope that defines which folders you're working with and holds your workflow state for those folders. Every Vireo instance always has an active workspace.

### Default Workspace

On first launch (or for existing users upgrading), Vireo creates an implicit workspace called "Default" containing the existing scan roots. The user never needs to know it exists — Vireo behaves exactly as it does today. The concept only surfaces when you create a second workspace.

### What a Workspace Owns

- A name (e.g., "Kenya 2025", "Default")
- A set of folder paths (the workspace's scope)
- Predictions, collections, and pending changes for photos in those folders
- Config overrides (classification threshold, active label sets, active model) — falls back to global defaults if not set
- UI state (last active page, scroll position, filters)

### What's Shared Globally

- The photo cache — metadata, thumbnails, embeddings, sharpness scores, perceptual hashes. These are properties of image files and computed once regardless of how many workspaces reference them.
- The keyword tree — hierarchical keywords are global since they ultimately sync to XMP.

### Folder Overlap

A folder can appear in multiple workspaces. The photo cache is shared, but each workspace has its own predictions for those photos.

## Data Architecture

### Global Database (`~/.vireo/vireo.db`)

Keeps the shared photo cache, largely unchanged from today:

- `folders`, `photos`, `keywords`, `photo_keywords` — as they exist now
- Thumbnails, embeddings, sharpness, phashes — stay here
- New `workspaces` table: `id, name, folder_paths (JSON), config_overrides (JSON), ui_state (JSON), created_at, last_opened_at`

### Per-Workspace Database (`~/.vireo/workspaces/<name>/workspace.db`)

Holds workflow state:

- `predictions` — moved out of the global DB
- `pending_changes` — per-workspace sync queue
- `collections` — per-workspace saved filters
- `job_history` — per-workspace job log

### On Workspace Open

1. Load the workspace's folder list
2. Check if any folders need scanning (new/modified files)
3. Reconcile XMP state — read sidecar files and compare against the global `photo_keywords` table, flagging drift
4. Load the workspace's predictions and pending changes

### On Workspace Creation

If the selected folders are already scanned (from another workspace), no re-scan or re-embedding needed. Only the workspace-level state (predictions DB) is initialized fresh.

### Migration for Existing Users

The current `vireo.db` becomes the global DB. A "Default" workspace is created pointing at the existing scan roots. Current `predictions`, `pending_changes`, `collections`, and `job_history` rows are moved into its workspace DB.

## UI & Navigation

### Landing Page

Shown when no workspace is active, or via "Switch workspace":

- List of workspaces sorted by `last_opened_at`
- Each card shows: name, folder count, photo count, last opened date
- "New Workspace" button — name and folder picker
- Clicking a workspace opens it and restores UI state

### Navbar

- Workspace name appears in the navbar (left side, near the Vireo logo)
- Click opens a dropdown: other workspaces for quick switching, "Manage Workspaces" link
- Visual indicator of active workspace

### Settings

- New "Workspaces" section for managing the workspace list (rename, delete)
- Workspace-specific settings appear as overrides with a "using global default" indicator when not set
- Global settings remain as the fallback

### Page Scoping

All existing pages (browse, classify, review, cull, stats) are automatically scoped to the active workspace's folders. Stats show workspace-level numbers.

## Edge Cases

### Deleting a Workspace

Removes the workspace DB (predictions, collections, pending changes). Does NOT touch the global photo cache or XMP files. Vireo warns if there are unsynced pending changes or unaccepted predictions.

### Removing a Folder from a Workspace

The folder's photos stop appearing in views. Orphaned predictions in the workspace DB are left in place (harmless) and cleaned up lazily.

### Adding an Already-Scanned Folder

Instant — photo cache is already populated. The workspace starts with zero predictions for those photos.

### XMP Drift Reconciliation

On workspace open, if XMP keywords changed since last seen (e.g., synced from another workspace), drift is surfaced via the existing audit mechanism. The user decides whether to update the DB cache.

### The Default Workspace

Behaves identically to every other workspace. Can be renamed. Cannot be deleted if it's the only workspace.

### Running Jobs

Jobs run in the context of the active workspace. Scan updates the global photo cache for the workspace's folders. Classification writes predictions to the workspace's DB.
