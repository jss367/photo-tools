# Card cleanup gets its own page — design

**Date:** 2026-08-08
**Status:** Approved in discussion with maintainer (relocation follow-up to
PR #1436); amended 2026-08-11 to replace the workspace-wide audit affordance
with targeted matching-copy verification. Runtime behavior is governed by
`2026-08-07-card-cleanup-design.md`.
**Scope:** Move the "Free up card space" UI from a collapsed section on the
import page to a dedicated page with a navbar entry; add an inline
targeted-verification affordance; keep the post-import entry point as a link.
Scan/delete behavior remains governed by the parent design.

## Problem

The tool lives inside the import page although its task is the opposite of
importing, so a user arriving cold (days after an import) doesn't find it.
Worse, its enabling dependency — the integrity audit that stamps
`hash_status='ok'` — lives on the audit page, so the first-run experience
on a pre-existing archive dead-ends: everything lands in "kept — run the
integrity audit" with no way to do that from where you're standing
(observed in production 2026-08-08: 5,523/5,523 files kept for exactly
this reason).

## Design

1. **New page** `vireo/templates/card_cleanup.html`, served at
   `/card-cleanup` (route follows the existing page-route pattern in
   app.py). Standalone page in the codebase's one-file-per-page style
   (includes `_navbar.html`, inline CSS/JS). Contains the entire
   scan → preview → delete flow moved from import.html, preserving the
   existing ids/function names (`card-cleanup-*` / `cardCleanup*`) and
   ALL user-facing copy verbatim — including the byte-exact confirmation
   dialog from the parent spec, the incomplete-preview banner, and the
   honest summary states. The section is no longer collapsed: on its own
   page it renders expanded.
2. **Support code the section leaned on in import.html** (folder browser,
   `formatBytes`, progress/`field-error`/modal CSS): the new page brings
   its OWN minimal copies scoped to its needs — a single-select folder
   browser (the card-cleanup `browserMode` variant), `formatBytes`, and
   the small CSS set. Deliberate duplication over a shared-include
   refactor of import.html: the parent PR's reviewers accepted mirrored
   code at this scale, and un-inlining import.html's browser is exactly
   the kind of unrelated refactor the parent spec declined.
3. **Inline targeted-verification affordance.** When the rendered preview's
   kept bucket contains `KEEP_NOT_VERIFIED`, show how many card files and
   unique hashes need verification. **Verify matching archive copies** calls
   `POST /api/card-cleanup/verify` for this scan. It reads at most one viable
   archive copy per unique pending hash, never unrelated workspace files,
   then reloads the atomically refreshed manifest. No second card scan is
   required.
4. **Navbar**: add "Card cleanup" to `_navbar.html` in the tools/pages
   list (match existing nav idiom and ordering conventions).
5. **Import page**: remove the moved section and its JS; keep the
   card-safety-pill entry point, now a link to
   `/card-cleanup?source=<path>` (URL-encoded). The new page pre-fills
   its source input from the `source` query parameter. Multi-source
   imports pass the first source; the new page keeps the existing
   "remaining sources" hint behavior.
6. **Scoped API addition.** The verify endpoint and `card-cleanup-verify`
   job are tied to a completed scan manifest and cannot overlap verification
   or deletion for that manifest.

## Testing

- Page route test: `GET /card-cleanup` → 200, contains the section
  markup (mirrors existing page-route tests).
- Existing card-cleanup API tests unchanged and green.
- Import page test (if any asserts the section's presence) updated; a
  test asserts import.html no longer contains the section container and
  DOES contain the pill link.
- JS of both changed templates re-checked with node --check; no
  duplicate ids on either served page.
- Manual visual QA remains pending for the human (both themes), as with
  the parent PR.
