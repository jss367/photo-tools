# Process Review page: complete product and interaction design

**Date:** 2026-08-16<br>
**Status:** Draft for product and maintainer review<br>
**Scope:** Define the complete Process Review page, including its layout,
views, encounter and burst structure, triage, species evidence, multi-model and
multi-species decisions, comparison tools, structural editing, persistence,
performance, accessibility, and failure behavior. Large review sets can be
narrowed without creating alternate groupings or disabling edits; complete
encounters remain intact in every view.

## Summary

Process Review has one canonical set of results: the output of the latest
Process run. Collections, triage filters, species-review filters, and search are
views over those results. They never recompute, trim, or replace the canonical
encounters.

When a photo-based view is active, the user chooses how complete encounters
qualify:

- **Any photo** — include the complete encounter when at least one of its
  photos matches.
- **Every photo** — include the complete encounter only when all of its photos
  match.

Both modes render the full encounter, including all bursts and photos. Matching
photos are identified visually so the reason an encounter is present remains
clear. Every view is editable; there is no read-only Workspace or Collection
mode.

The page loads and renders complete encounters incrementally. Mutations target
stable encounter, burst, or photo identities and return only changed data.
Neither page navigation nor a single review action transfers, clones, or saves
the full review result.

## Problem

Process Review currently conflates two different operations:

1. defining which photos are analyzed and grouped; and
2. choosing which existing results the user wants to see.

Selecting Workspace or Collection currently calls the live regrouping endpoint
with a different photo set. That produces an alternate set of encounters. An
encounter crossing the collection boundary can be truncated, split, merged
differently, or assigned a different array index. Because several mutations
rewrite the saved pipeline cache or address groups by array index, the page
then blocks editing to avoid replacing the saved review with the scoped subset.

This is safe but not useful: Vireo offers views in which the primary review
actions cannot be performed.

The same design scales poorly. A process run containing 15,418 photos currently
produces a roughly 43 MB serialized cache. A species confirmation reloads and
rewrites that cache, returns the full encounter list, and the browser deep-clones
the full result again. The page also builds one large HTML string and replaces
the entire encounter container. In a real review this produced consistent
1.7–2.1 second confirmation requests and a multi-gigabyte WebKit content
process.

## Goals

- Make every Process Review view fully editable.
- Preserve the canonical encounter and burst context while narrowing the view.
- Let the user choose inclusive or strict encounter matching.
- Make the reason each encounter matched visible and understandable.
- Represent species evidence without collapsing different subjects, photos,
  models, or label lists into one misleading prediction.
- Support more than one species in a photo or encounter while retaining a fast
  confirmation path for the common single-species case.
- Keep work proportional to the encounters on screen, not the complete run.
- Prevent a scoped view or stale browser from overwriting unrelated results.
- Preserve review state when switching views, sorting, searching, or loading
  additional pages.
- Keep existing photo-, burst-, and encounter-level workflows available.

## Non-goals

- Changing how Process initially computes encounters or bursts.
- Making a Collection view run missing classification, detection, or grouping
  work. Photos not present in the latest process result remain outside that
  review.
- Allowing a view filter to create a new saved process result.
- Splitting a single encounter across pages or virtualized windows.
- Replacing the dedicated Rapid Review page.
- Retraining or changing the inference behavior of classifier models. This
  design does change how their evidence is retained, combined, and reviewed.
- Redesigning the quality-scoring or grouping algorithms.

## Product principles and terminology

This document is the product contract for the complete Process Review page,
not only its filtering or persistence layer. An implementation is incomplete
if the data model is correct but a control, state, or workflow described here
is absent or behaves differently without an explicit design revision.

### Product principles

1. **Preserve context.** Narrowing a view never hides members from inside an
   included encounter.
2. **Make targets explicit.** Every action names whether it affects subjects,
   photos, a burst, an encounter, matching photos, or all photos in shown
   encounters.
3. **Separate evidence from decisions.** Model output remains immutable and
   explainable; human review creates separate assertions and triage decisions.
4. **Never restructure implicitly.** Species confirmation, filtering, and
   triage do not change burst or encounter membership. Structural changes use
   dedicated commands with a preview.
5. **Keep the fast path fast.** A consistent single-species burst can be
   triaged and confirmed with a few keys or one action, while disagreement and
   multi-species cases remain fully inspectable.
6. **Scale by what is visible.** Loading, rendering, and mutations are
   proportional to the current page and action target, not the full Process
   result.
7. **Allow recovery.** Every review mutation is durable, auditable, and
   undoable until superseded by an incompatible new Process run.

### Object hierarchy

- **Process run:** one computation snapshot and its initial grouping of a
  defined set of photos.
- **Encounter:** one continuous photographic event or sighting. It contains
  one or more bursts and is the indivisible display and pagination unit.
- **Burst:** a chronologically ordered comparison and culling unit within an
  encounter, usually a rapid sequence of similar frames.
- **Photo:** one library image. It belongs to exactly one burst and one
  encounter in a review revision.
- **Subject:** one detected biological subject or one user-created subject
  target inside a photo. A photo may contain several subjects.
- **Prediction source:** one classifier-model and label-list version, plus its
  declared exclusive or multi-label behavior.
- **Review assertion:** a human decision about a subject or photo, such as a
  confirmed species, Not identifiable, or False detection.
- **Triage state:** the effective Keep, Review, or Reject decision for a photo.
  It is distinct from the automated recommendation that suggested it.
- **Review view:** an editable projection of complete encounters from the
  canonical Process result.

### Structural verbs

The interface uses one verb for one effect. Labels may include the affected
counts, but must not substitute “split” and “detach” for one another.

| Command | Exact effect | Encounter changes? | Burst changes? |
|---|---|---:|---:|
| **Split burst here** | Cut one burst at the selected gap into the photos before and after that gap | No | One burst becomes two |
| **Extract photos to new burst** | Remove an arbitrary selected set from its burst and create one or more new bursts in the same encounter | No | Membership changes |
| **Merge adjacent bursts** | Combine two neighboring bursts in the same encounter, preserving chronological photo order | No | Two bursts become one |
| **Split encounter here** | Cut one encounter at a burst boundary; the selected burst and every later burst form a new encounter | One becomes two | No |
| **Separate burst as encounter** | Remove exactly one complete burst and make it a standalone encounter | One gains a sibling | No |
| **Merge encounters** | Combine explicitly selected encounters and retain their bursts in chronological order | Several become one | No |

<figure class="design-diagram" aria-labelledby="structural-ops-caption">
<div class="diagram-scroll">
<svg viewBox="0 0 960 700" role="img" aria-labelledby="structural-ops-title structural-ops-desc">
  <title id="structural-ops-title">Structural editing operations</title>
  <desc id="structural-ops-desc">Before and after examples distinguish operations that change burst boundaries within one encounter from operations that create or combine encounters.</desc>
  <text class="diagram-title" x="28" y="34">Change bursts inside one encounter</text>
  <rect class="diagram-accent" x="22" y="50" width="448" height="420" rx="16"/>
  <text class="diagram-label" x="44" y="82">SPLIT BURST HERE</text>
  <rect class="diagram-surface" x="42" y="96" width="174" height="78" rx="10"/>
  <rect class="diagram-blue" x="54" y="112" width="150" height="46" rx="8"/>
  <text class="diagram-small" x="72" y="140">A · B · C · D</text>
  <path class="diagram-line-accent" d="M226 135 L270 135"/><path d="M262 127 L272 135 L262 143" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <rect class="diagram-surface" x="282" y="96" width="166" height="78" rx="10"/>
  <rect class="diagram-blue" x="294" y="112" width="66" height="46" rx="8"/>
  <rect class="diagram-blue" x="370" y="112" width="66" height="46" rx="8"/>
  <text class="diagram-small" x="307" y="140">A · B</text><text class="diagram-small" x="383" y="140">C · D</text>
  <text class="diagram-small" x="44" y="194">One burst becomes two. The encounter is unchanged.</text>

  <line class="diagram-divider" x1="42" y1="218" x2="448" y2="218"/>
  <text class="diagram-label" x="44" y="250">EXTRACT PHOTOS TO NEW BURST</text>
  <rect class="diagram-surface" x="42" y="264" width="174" height="104" rx="10"/>
  <rect class="diagram-blue" x="54" y="280" width="150" height="54" rx="8"/>
  <rect class="diagram-amber" x="98" y="286" width="62" height="42" rx="7"/>
  <text class="diagram-small" x="66" y="352">select C + D</text>
  <path class="diagram-line-accent" d="M226 316 L270 316"/><path d="M262 308 L272 316 L262 324" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <rect class="diagram-surface" x="282" y="264" width="166" height="104" rx="10"/>
  <rect class="diagram-blue" x="294" y="280" width="42" height="48" rx="7"/>
  <rect class="diagram-amber" x="346" y="280" width="42" height="48" rx="7"/>
  <rect class="diagram-blue" x="398" y="280" width="38" height="48" rx="7"/>
  <text class="diagram-small" x="299" y="350">AB</text><text class="diagram-small" x="351" y="350">CD</text><text class="diagram-small" x="402" y="350">EF</text>
  <text class="diagram-small" x="44" y="392">Selected and unselected chronological runs become bursts.</text>
  <rect class="diagram-panel" x="44" y="414" width="402" height="36" rx="8"/>
  <text class="diagram-small" x="62" y="438">Merge adjacent bursts is the explicit inverse boundary operation.</text>
  <text class="diagram-title" x="506" y="34">Change encounter boundaries</text>
  <rect class="diagram-blue" x="490" y="50" width="448" height="420" rx="16"/>
  <text class="diagram-label" x="512" y="82">SPLIT ENCOUNTER HERE</text>
  <rect class="diagram-surface" x="510" y="96" width="174" height="78" rx="10"/>
  <rect class="diagram-accent" x="522" y="112" width="150" height="46" rx="8"/>
  <text class="diagram-small" x="538" y="140">B1 · B2 · B3</text>
  <path class="diagram-line-accent" d="M694 135 L738 135"/><path d="M730 127 L740 135 L730 143" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <rect class="diagram-surface" x="750" y="96" width="166" height="78" rx="10"/>
  <rect class="diagram-accent" x="762" y="112" width="54" height="46" rx="8"/>
  <rect class="diagram-accent" x="826" y="112" width="78" height="46" rx="8"/>
  <text class="diagram-small" x="778" y="140">B1</text><text class="diagram-small" x="842" y="140">B2 · B3</text>
  <text class="diagram-small" x="512" y="194">A boundary cut creates two encounters.</text>

  <line class="diagram-divider" x1="510" y1="218" x2="916" y2="218"/>
  <text class="diagram-label" x="512" y="250">SEPARATE BURST AS ENCOUNTER</text>
  <rect class="diagram-surface" x="510" y="264" width="174" height="104" rx="10"/>
  <rect class="diagram-accent" x="522" y="280" width="150" height="54" rx="8"/>
  <rect class="diagram-amber" x="572" y="286" width="46" height="42" rx="7"/>
  <text class="diagram-small" x="538" y="352">choose middle B2</text>
  <path class="diagram-line-accent" d="M694 316 L738 316"/><path d="M730 308 L740 316 L730 324" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <rect class="diagram-surface" x="750" y="264" width="166" height="104" rx="10"/>
  <rect class="diagram-accent" x="762" y="280" width="86" height="48" rx="8"/>
  <rect class="diagram-amber" x="858" y="280" width="46" height="48" rx="8"/>
  <text class="diagram-small" x="778" y="350">B1 · B3</text><text class="diagram-small" x="870" y="350">B2</text>
  <text class="diagram-small" x="512" y="392">One complete burst becomes its own encounter; it is not split.</text>
  <rect class="diagram-panel" x="512" y="414" width="402" height="36" rx="8"/>
  <text class="diagram-small" x="530" y="438">Merge encounters combines selected encounters without merging bursts.</text>

  <rect class="diagram-surface" x="22" y="494" width="916" height="178" rx="16"/>
  <text class="diagram-label" x="48" y="526">INVARIANT ACROSS EVERY STRUCTURAL COMMAND</text>
  <circle class="diagram-number-circle" cx="62" cy="558" r="13"/><text class="diagram-number" x="62" y="558">1</text>
  <text class="diagram-text" x="86" y="563">No photos or evidence are discarded</text>
  <circle class="diagram-number-circle" cx="62" cy="600" r="13"/><text class="diagram-number" x="62" y="600">2</text>
  <text class="diagram-text" x="86" y="605">Species and triage stay with subjects/photos</text>
  <circle class="diagram-number-circle" cx="510" cy="558" r="13"/><text class="diagram-number" x="510" y="558">3</text>
  <text class="diagram-text" x="534" y="563">Derived summaries are recomputed</text>
  <circle class="diagram-number-circle" cx="510" cy="600" r="13"/><text class="diagram-number" x="510" y="600">4</text>
  <text class="diagram-text" x="534" y="605">Preview first, then apply with undo</text>
</svg>
</div>
<figcaption id="structural-ops-caption"><strong>Structural operations.</strong> Split and Extract change burst structure inside an encounter. Split encounter and Separate burst change encounter membership. Merge is always explicit and preserves the lower-level objects unless the user is specifically merging them.</figcaption>
</figure>

“Detach photo” is replaced in visible copy by **Extract to new burst**.
“Detach burst” is replaced by **Separate as encounter**. The command and event
names in the implementation should follow the visible semantics rather than
preserving the current ambiguous names.

Structural changes never discard photos, species assertions, triage state, or
model evidence. They only change membership and derived summaries. The page
previews the before/after structure, names inherited state, and supports undo.

## Core concepts

### Process result

The latest completed Process run creates a canonical result with a stable
`review_run_id` and two monotonically increasing counters: `revision`, which
advances on any review mutation, and `structural_revision`, which advances
only when encounter or burst membership changes. It defines:

- the photos included in that run;
- encounter and burst boundaries;
- computed classifications and quality recommendations; and
- the configuration and feature versions used to compute them.

The result may cover the whole workspace or only the scope selected on the
Process page. Process Review must state this coverage, but it must not silently
expand it by recomputing against other workspace photos.

Only these operations may intentionally change canonical grouping:

- completing a new Process run;
- applying grouping settings to the canonical result; or
- an explicit structural review action such as splitting, extracting, merging,
  or separating a group.

Changing a view never changes canonical grouping.

### Review view

A review view is a query over the canonical result. It contains:

- an optional collection;
- a photo-status filter: All, Keep, Review, Reject, or Species conflicts;
- species-review state filtering;
- species or filename search;
- encounter match mode: Any photo or Every photo;
- encounter sort; and
- a paging cursor.

The view is ephemeral. It may be represented in the URL and restored on reload,
but it is never saved as a pipeline result.

### Complete encounter

An encounter is the indivisible unit of display and pagination. When an
encounter qualifies, the response and page contain:

- every photo in the encounter;
- every burst and its complete membership;
- the encounter and burst species evidence;
- current confirmation and flag state; and
- the trace and metadata needed by visible review controls.

A filter may identify matching photos inside the encounter, but it must not
remove the nonmatching photos. Explicit structural commands are the only
review interactions that change group membership.

## Page structure

On a desktop-width window, Process Review has a collapsible left sidebar and a
single primary reading column. The primary column is ordered as follows:

1. page header and navigation;
2. process coverage and health;
3. canonical and current-view summary counts;
4. sticky review toolbar and active-filter summary;
5. paged encounter list;
6. page-loading sentinel or end-of-view summary; and
7. nonblocking mutation and undo notifications.

The Species evidence panel, Group Review, structural preview, and photo detail
views appear as overlays or drawers without replacing the current view state.
Returning from any overlay restores the originating encounter, scroll
position, expanded state, selection, and keyboard focus.

On a narrow window, the sidebar becomes an **Advanced controls** drawer and
toolbar filters become a **Refine view** sheet. The encounter hierarchy and
available actions do not change. Horizontal clipping is never required for
ordinary review; wide evidence matrices may scroll within their own panel.

<figure class="design-diagram" aria-labelledby="page-anatomy-caption">
<div class="diagram-scroll">
<svg viewBox="0 0 960 570" role="img" aria-labelledby="page-anatomy-title page-anatomy-desc">
  <title id="page-anatomy-title">Process Review page anatomy</title>
  <desc id="page-anatomy-desc">A desktop layout with advanced controls on the left and, on the right, the page header, summary, review toolbar, complete encounter cards, burst rows, and photo cards.</desc>
  <rect class="diagram-panel" x="20" y="22" width="218" height="522" rx="16"/>
  <text class="diagram-label" x="44" y="55">ADVANCED CONTROLS</text>
  <rect class="diagram-surface" x="42" y="76" width="174" height="78" rx="10"/>
  <text class="diagram-text" x="58" y="104">View explanation</text>
  <text class="diagram-small" x="58" y="128">Matches + context</text>
  <rect class="diagram-surface" x="42" y="170" width="174" height="88" rx="10"/>
  <text class="diagram-text" x="58" y="199">Scoring suggestions</text>
  <text class="diagram-small" x="58" y="223">Preview before apply</text>
  <rect class="diagram-surface" x="42" y="274" width="174" height="106" rx="10"/>
  <text class="diagram-text" x="58" y="303">Grouping settings</text>
  <text class="diagram-small" x="58" y="327">Preview regrouping</text>
  <text class="diagram-small" x="58" y="350">Complete result only</text>
  <rect class="diagram-surface" x="42" y="396" width="174" height="122" rx="10"/>
  <text class="diagram-text" x="58" y="425">Focused trace</text>
  <path class="diagram-line-accent" d="M62 484 L94 454 L126 470 L158 438 L194 458"/>

  <rect class="diagram-blue" x="266" y="22" width="674" height="76" rx="16"/>
  <circle class="diagram-number-circle" cx="292" cy="48" r="13"/><text class="diagram-number" x="292" y="48">1</text>
  <text class="diagram-title" x="318" y="53">Process Review</text>
  <text class="diagram-small" x="318" y="77">Coverage · run health · destinations · run details</text>

  <rect class="diagram-surface" x="266" y="114" width="674" height="68" rx="12"/>
  <circle class="diagram-number-circle" cx="292" cy="138" r="13"/><text class="diagram-number" x="292" y="138">2</text>
  <text class="diagram-text" x="318" y="143">Process totals</text>
  <text class="diagram-small" x="318" y="166">Triage · species review · current view · pending sidecars</text>

  <rect class="diagram-accent" x="266" y="198" width="674" height="82" rx="12"/>
  <circle class="diagram-number-circle" cx="292" cy="223" r="13"/><text class="diagram-number" x="292" y="223">3</text>
  <text class="diagram-text" x="318" y="228">Editable review toolbar</text>
  <text class="diagram-small" x="318" y="253">Triage · species · collection · Any/Every · search · sort · display</text>

  <rect class="diagram-surface" x="266" y="296" width="674" height="226" rx="16"/>
  <circle class="diagram-number-circle" cx="292" cy="322" r="13"/><text class="diagram-number" x="292" y="322">4</text>
  <text class="diagram-title" x="318" y="328">Complete encounter</text>
  <text class="diagram-small" x="760" y="328">8 photos · 2 bursts · 3 match</text>
  <line class="diagram-divider" x1="286" y1="348" x2="920" y2="348"/>
  <text class="diagram-label" x="292" y="374">BURST 1</text>
  <rect class="diagram-match" x="292" y="391" width="92" height="68" rx="9"/>
  <rect class="diagram-context" x="396" y="391" width="92" height="68" rx="9"/>
  <rect class="diagram-match" x="500" y="391" width="92" height="68" rx="9"/>
  <text class="diagram-small" x="314" y="482">Match</text>
  <text class="diagram-small" x="413" y="482">Context</text>
  <text class="diagram-small" x="522" y="482">Match</text>
  <rect class="diagram-panel" x="620" y="370" width="286" height="126" rx="12"/>
  <text class="diagram-label" x="642" y="397">BURST 2</text>
  <rect class="diagram-photo" x="642" y="414" width="70" height="52" rx="8"/>
  <rect class="diagram-photo" x="724" y="414" width="70" height="52" rx="8"/>
  <rect class="diagram-photo" x="806" y="414" width="70" height="52" rx="8"/>

  <rect class="diagram-panel" x="266" y="536" width="674" height="18" rx="9"/>
</svg>
</div>
<figcaption id="page-anatomy-caption"><strong>Page anatomy.</strong> Advanced computation controls stay out of the routine review path. The main column moves from run context to view controls to complete encounter cards; overlays preserve the user's place.</figcaption>
</figure>

### Page header and destinations

The page title is **Process Review**. The header includes:

- **Back to Process**, which returns to Process configuration without
  discarding completed review work;
- **Rapid Review**, for the separate one-photo-at-a-time workflow;
- **Review misses**, shown with a count only when missed-subject candidates
  exist; and
- the latest Process completion time and a **Run details** disclosure showing
  the run ID, configuration name, input scope, model/list-pair inventory, and
  feature availability.

The page does not use “Pipeline Review” in visible copy. Process is the
user-facing workflow name; pipeline and cache identifiers remain diagnostic
implementation terms only.

### Coverage summary

The top of the page describes the canonical result rather than the current
view. Examples:

> Latest process result · 15,418 photos · completed August 15 at 10:16 PM

For a partial process run:

> Latest process result includes 2,140 of 15,418 workspace photos · Processed
> from collection “August shorebirds”

If a selected collection contains photos outside the process result, the view
reports the intersection explicitly:

> 380 collection photos match this process result · 24 collection photos were
> not part of the latest process run

The user may return to Process to include those photos. Process Review does not
compute them implicitly.

If optional enhancing stages were unavailable, the coverage area shows one
dismissible health banner such as:

> Review is available, but 420 photos have no species predictions and 18 have
> no subject masks. Re-run missing stages from Process.

The banner names which features are affected and which page capabilities are
degraded. It never describes missing model evidence as a negative prediction.

### Summary strip

The summary strip separates canonical totals from the current view:

- **Process result:** total photos, encounters, and bursts in the run;
- **Triage:** effective Keep, Review, Reject, and protected-photo counts;
- **Species review:** resolved subjects or photo-level targets, unresolved
  targets, active disagreements, and multi-species photos; and
- **Current view:** matching photos, complete encounters, and context photos.

Canonical counts remain stable as filters change, except after a mutation.
View counts update for the active query. Every number has a label and tooltip
that states its unit; the page never displays a bare “42 confirmed” when the
number could refer to photos, subjects, bursts, or encounters.

### Pending sidecar synchronization

When committed review changes have not yet been written to Extensible Metadata
Platform (XMP) sidecar files, a nonblocking banner appears below the summary:

> 18 saved Vireo changes are waiting to be written to sidecar files

**Review sidecar changes** opens a preview grouped by photo and change type,
including keyword additions/removals, triage flags, ratings, locations, and
other pending metadata. Each row shows the current sidecar value and the value
Vireo will write. Thumbnail size and change-type filters affect the preview
only.

**Write sidecars** starts an asynchronous job for all pending changes.
**Write selected change types** writes only the visible checked types. Progress
reports photos completed and the current filename; closing the preview does not
cancel the job. A failure retains unsynchronized entries and offers retry.

The current label “Discard” is not used because it can sound like undoing the
review decision. **Keep only in Vireo** removes a selected pending sidecar write
after confirming that the saved Vireo triage/species decision remains. Undoing
the decision itself must happen through Edit history. Sidecar synchronization
is never required before continuing review or leaving the page.

### Review toolbar

The sticky toolbar has two rows on wide screens.

The first row defines work to review:

- **Triage:** All, Keep, Review, or Reject.
- **Species review:** All, Needs review, Resolved, or Conflicts.
- **Collection:** All processed photos or one collection.
- **Encounter match:** Any photo or Every photo.
- **Search:** species, scientific name, filename, model name, label-list name,
  or photo ID. Match-case and whole-word options live inside the search menu.

The second row controls presentation:

- **Sort:** chronological, newest first, oldest first, most/fewest photos, most
  bursts, unresolved first, or conflict severity;
- **Thumbnail size:** a persistent compact-to-large control;
- **Prediction threshold:** a display-only minimum confidence control that
  never changes stored model evidence or resolution state;
- **Show automated triage labels:** off by default when manual decisions are
  present, so recommendations cannot be mistaken for decisions; and
- **Collapse all / Expand loaded:** changes only loaded encounter cards.

“Latest review,” “Workspace,” and “Collection” are removed as mutually
exclusive analysis scopes. “All processed photos” is the unfiltered view of
the latest canonical result; Collection is an optional view filter.

The encounter-match choice is a two-option segmented control rather than a
checkbox named “Keep encounters intact,” because both choices preserve
encounters. If the platform requires a checkbox, use **Require every photo in
the encounter to match**; unchecked means Any photo.

The setting defaults to **Any photo**, persists as a Process Review view
preference, and remains visible whenever a photo-based filter is active.

Active criteria appear as removable chips below the toolbar, followed by one
**Clear view filters** action. The current view definition is encoded in the
URL and restored on reload. Display-only preferences such as thumbnail size
and sidebar state are stored separately and do not alter a shared URL.

Changing a filter keeps the old list visible with an updating treatment until
the first new page arrives. The page does not blank, jump to the top, or lose
the user's focused encounter unless that encounter no longer qualifies.

### Advanced controls sidebar

The sidebar is collapsed by default for routine review and remembers its
state. It has three sections.

**View explanation** shows why the focused encounter is present, which photos
matched the current query, and which were included as context. It also shows
the focused encounter's grouping trace when trace data is current.

**Scoring recommendations** adjusts how automated Keep, Review, and Reject
recommendations are derived. Controls include hard-reject floors for crop
completeness, focus, clipped highlights, and composite quality, plus the
quality-versus-diversity balance and maximum selected photos for burst- and
encounter-level selection. Changes affect recommendations across the complete
canonical result, never just the current view. Manual triage decisions remain
untouched. **Reset recommendations** restores the run settings; **Apply**
previews changed recommendation counts before committing.

**Grouping settings** exposes the advanced encounter and burst parameters used
by Process: time, subject-similarity, whole-image-similarity, species, and
capture-metadata weights; hard and soft encounter-cut thresholds; adjacent
encounter merge thresholds; and burst time/similarity thresholds. Each control
has a plain-language explanation, current value, run value, and valid range.
Changing these settings does nothing until **Preview regrouping** is selected.
The preview reports encounters and bursts added, removed, split, or merged.
**Apply regrouping to complete result** creates a new canonical revision after
confirmation. **Save as Process defaults** is a separate action and never
occurs implicitly.

If the focused encounter was manually restructured, the grouping trace labels
the computed boundaries and manual overrides separately. It never presents an
outdated trace as the explanation for current membership.

### Encounter card

Every loaded encounter is one collapsible card. Its header remains visible
when the body is collapsed and contains:

- expand/collapse control and view-local encounter ordinal;
- time range, location when available, photo count, and burst count;
- confirmed species roster or unresolved suggestion summary;
- subject/photo resolution progress;
- badges for model disagreement, label-list disagreement, multiple species,
  group variation, missing evidence, missing timestamps, or protected photos;
- `N of M photos match view` when context photos are present;
- **Review species** or the eligible one-click confirmation action;
- an effective triage summary and explicit **Reject all photos** or **Clear
  rejects** action; and
- an overflow menu containing structural actions and **Copy encounter ID**.

Clicking the chevron or noninteractive header background expands the card.
Clicking a badge opens the relevant explanation. Clicking an action never also
toggles expansion. Long encounter cards repeat the decision summary in a
compact footer so the user need not scroll back to the header.

The expanded body renders every burst in chronological order. Context photos
that did not match the view remain fully interactive and carry a text-and-icon
**Context** badge. Matching photos may carry a **Matches view** badge. Neither
state is communicated by opacity alone.

### Burst row

Each burst row contains:

- stable burst label, chronological position, time span, and photo count;
- resolved species roster or suggestion and its model support;
- triage counts for its member photos;
- active disagreement or multi-species badges;
- **Review burst**, which opens Group Review;
- **Reject burst** or **Clear rejects** using every member photo; and
- an overflow menu with Split burst, Extract photos, Merge with previous/next,
  Separate as encounter, and Split encounter before/after when eligible.

Unavailable structural actions remain absent rather than disabled without an
explanation. For example, the first burst has no Split encounter before action
if that would create an empty encounter, and a one-photo burst has no internal
split point.

### Photo card

A photo card contains the thumbnail and only decision-relevant overlays:

- filename and capture time on demand;
- effective manual Keep or Reject state;
- optional automated triage recommendation;
- composite quality score and protected-photo reason when available;
- matching/context marker;
- species roster or primary suggestion, subject count, and active conflict;
- species-representative marker; and
- missing-file or unavailable-preview state.

Clicking a photo opens Group Review for its complete burst with that photo
selected. Right-click or the overflow button offers **Open in Browse**,
**Highlight for species**, **Set as species representative**, **Extract to new
burst**, and photo-level triage actions. Destructive-looking `×` controls are
not used for structural edits because they do not communicate where the photo
will go.

Thumbnail loading is lazy. A missing thumbnail does not remove the photo card
or its actions. The card shows filename, metadata, and **Locate file** when the
underlying library item is unavailable.

### Paged encounter list

The page requests whole encounters. Skeletons reserve approximate card height
only for the next page; they never mimic partial encounter content. An end
marker reports:

> End of view · 94 complete encounters · 612 photos shown

The page may evict distant expanded bodies to bound memory, but retains a
lightweight placeholder with stable height, expansion state, and current
revision so back-scrolling does not jump.

## Encounter matching

Let `S` be the set of photos in the canonical process result that satisfy the
active photo-based criteria, and let `E` be the complete member set of one
canonical encounter.

- **Any photo:** include the encounter when `E ∩ S` is not empty.
- **Every photo:** include the encounter when `E` is a subset of `S`.

The complete set `E` is returned and rendered in either case.

### Example

An encounter contains photos A, B, C, and D. A collection contains B and C.

| Encounter match | Result |
|---|---|
| Any photo | Show A, B, C, and D; identify B and C as matches |
| Every photo | Do not show the encounter |

If the collection later contains all four photos, both modes show the same
complete encounter.

<figure class="design-diagram" aria-labelledby="encounter-match-caption">
<div class="diagram-scroll">
<svg viewBox="0 0 960 390" role="img" aria-labelledby="encounter-match-title encounter-match-desc">
  <title id="encounter-match-title">Any photo and Every photo encounter matching</title>
  <desc id="encounter-match-desc">An encounter contains photos A through D, while only B and C match the filter. Any photo includes the complete encounter with A and D as context. Every photo excludes the encounter because not every member matches.</desc>
  <text class="diagram-label" x="34" y="34">CANONICAL ENCOUNTER</text>
  <rect class="diagram-panel" x="28" y="50" width="904" height="92" rx="14"/>
  <rect class="diagram-context" x="178" y="68" width="112" height="56" rx="9"/>
  <rect class="diagram-match" x="306" y="68" width="112" height="56" rx="9"/>
  <rect class="diagram-match" x="434" y="68" width="112" height="56" rx="9"/>
  <rect class="diagram-context" x="562" y="68" width="112" height="56" rx="9"/>
  <text class="diagram-text" x="226" y="102">A</text>
  <text class="diagram-text" x="354" y="102">B</text>
  <text class="diagram-text" x="482" y="102">C</text>
  <text class="diagram-text" x="610" y="102">D</text>
  <text class="diagram-small" x="704" y="91">B + C match filter</text>
  <text class="diagram-small" x="704" y="113">A + D are context</text>

  <path class="diagram-line-accent" d="M352 146 L264 188"/>
  <path class="diagram-line" d="M608 146 L704 188"/>

  <rect class="diagram-accent" x="28" y="188" width="430" height="172" rx="16"/>
  <text class="diagram-title" x="52" y="220">Any photo</text>
  <text class="diagram-small" x="52" y="244">At least one member matches → include all four</text>
  <rect class="diagram-context" x="52" y="266" width="80" height="52" rx="8"/>
  <rect class="diagram-match" x="144" y="266" width="80" height="52" rx="8"/>
  <rect class="diagram-match" x="236" y="266" width="80" height="52" rx="8"/>
  <rect class="diagram-context" x="328" y="266" width="80" height="52" rx="8"/>
  <text class="diagram-text" x="84" y="298">A</text><text class="diagram-text" x="176" y="298">B</text>
  <text class="diagram-text" x="268" y="298">C</text><text class="diagram-text" x="360" y="298">D</text>
  <text class="diagram-label" x="52" y="342">SHOWN · COMPLETE + EDITABLE</text>

  <rect class="diagram-surface" x="486" y="188" width="446" height="172" rx="16"/>
  <text class="diagram-title" x="510" y="220">Every photo</text>
  <text class="diagram-small" x="510" y="244">A and D do not match → exclude the encounter</text>
  <rect class="diagram-context" x="510" y="266" width="86" height="52" rx="8"/>
  <rect class="diagram-match" x="608" y="266" width="86" height="52" rx="8"/>
  <rect class="diagram-match" x="706" y="266" width="86" height="52" rx="8"/>
  <rect class="diagram-context" x="804" y="266" width="86" height="52" rx="8"/>
  <line x1="510" y1="258" x2="890" y2="326" stroke="#bd6b63" stroke-width="4"/>
  <text class="diagram-label" x="510" y="342">NOT SHOWN · NEVER PARTIALLY RENDERED</text>
</svg>
</div>
<figcaption id="encounter-match-caption"><strong>Encounter matching.</strong> Any photo is inclusive and adds context; Every photo is strict. Neither mode removes individual photos from an encounter that is shown.</figcaption>
</figure>

### Combining filters

Photo-based criteria are combined first to form `S`. For example, Collection
“August shorebirds” plus Status “Review” means photos that are both in the
collection and currently marked Review. The encounter match rule is then
applied once to that combined set.

Species and filename search includes a complete encounter when the query
matches any visible encounter label, burst label, qualifying prediction, or
filename. Search does not trim the encounter. Search options such as case
matching and whole-word matching retain their current meaning.

Species-review filtering operates at the encounter level:

- **Needs review** includes encounters with at least one unresolved evidence
  target and renders the complete encounter, including resolved subjects and
  bursts.
- **Resolved** includes encounters with no unresolved evidence targets.

It never removes resolved bursts from inside a mixed encounter. That
would fragment the context the design is intended to preserve.

### Explaining the view

The toolbar reports both the query match and expanded context:

> 273 photos matched · 94 complete encounters · 612 photos shown with context

In Any photo mode, matching photos receive a subtle “Matches view” treatment.
Nonmatching context photos remain fully interactive and are not presented as
disabled. The treatment must not rely on color alone.

In Every photo mode, every photo matches, so per-photo match decoration is
unnecessary.

## Species evidence and disagreement

Process Review must preserve where every species claim came from. A runner-up
from one classifier, two classifiers disagreeing about one subject, two
different animals in one image, and two species appearing in different photos
of an encounter are four different situations. They must not be flattened into
one confidence-weighted species list.

<figure class="design-diagram" aria-labelledby="species-flow-caption">
<div class="diagram-scroll">
<svg viewBox="0 0 960 650" role="img" aria-labelledby="species-flow-title species-flow-desc">
  <title id="species-flow-title">Species evidence from subjects and prediction sources to human assertions</title>
  <desc id="species-flow-desc">Subject and photo-level evidence remain separate. Multiple label lists from one model consolidate into one vote. Independent models can disagree. Multi-label photo predictions are evaluated per taxon. Human resolution creates assertions without deleting model evidence.</desc>
  <text class="diagram-label" x="24" y="34">1 · EVIDENCE TARGETS</text>
  <text class="diagram-label" x="252" y="34">2 · MODEL / LABEL-LIST SOURCES</text>
  <text class="diagram-label" x="570" y="34">3 · CONSOLIDATE</text>
  <text class="diagram-label" x="766" y="34">4 · REVIEW DECISION</text>

  <rect class="diagram-blue" x="20" y="52" width="198" height="518" rx="16"/>
  <text class="diagram-title" x="42" y="86">Photo 812</text>
  <rect class="diagram-surface" x="42" y="108" width="154" height="154" rx="12"/>
  <rect class="diagram-accent" x="58" y="126" width="58" height="96" rx="10"/>
  <rect class="diagram-amber" x="126" y="148" width="52" height="72" rx="10"/>
  <text class="diagram-small" x="58" y="246">Two subject boxes</text>
  <rect class="diagram-surface" x="42" y="286" width="154" height="72" rx="10"/>
  <text class="diagram-text" x="58" y="316">Subject 1</text>
  <text class="diagram-small" x="58" y="340">single animal</text>
  <rect class="diagram-surface" x="42" y="374" width="154" height="72" rx="10"/>
  <text class="diagram-text" x="58" y="404">Subject 2</text>
  <text class="diagram-small" x="58" y="428">separate animal</text>
  <rect class="diagram-violet" x="42" y="462" width="154" height="82" rx="10"/>
  <text class="diagram-text" x="58" y="493">Photo-level target</text>
  <text class="diagram-small" x="58" y="517">no subject association</text>

  <path class="diagram-line" d="M218 320 L246 126"/>
  <path class="diagram-line" d="M218 320 L246 222"/>
  <path class="diagram-line" d="M218 320 L246 318"/>
  <path class="diagram-line" d="M218 500 L246 450"/>

  <rect class="diagram-panel" x="246" y="52" width="294" height="518" rx="16"/>
  <rect class="diagram-accent" x="266" y="74" width="254" height="78" rx="10"/>
  <text class="diagram-text" x="282" y="104">Model A · Birds list</text>
  <text class="diagram-small" x="282" y="130">Subject 1 → Robin · 0.91</text>
  <rect class="diagram-accent" x="266" y="168" width="254" height="78" rx="10"/>
  <text class="diagram-text" x="282" y="198">Model A · Vertebrates list</text>
  <text class="diagram-small" x="282" y="224">Subject 1 → Robin · 0.88</text>
  <path d="M526 84 L534 84 L534 236 L526 236" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <text class="diagram-small" x="382" y="263">same independence group</text>
  <rect class="diagram-amber" x="266" y="278" width="254" height="78" rx="10"/>
  <text class="diagram-text" x="282" y="308">Model B · Birds list</text>
  <text class="diagram-small" x="282" y="334">Subject 1 → Sparrow · 0.82</text>
  <rect class="diagram-violet" x="266" y="386" width="254" height="134" rx="10"/>
  <text class="diagram-text" x="282" y="416">Model C · multi-label image</text>
  <text class="diagram-small" x="282" y="443">Photo → Robin present · 0.89</text>
  <text class="diagram-small" x="282" y="469">Photo → Mallard present · 0.86</text>
  <text class="diagram-small" x="282" y="498">Independent presence claims</text>
  <path class="diagram-line-accent" d="M540 154 L564 154"/><path d="M556 146 L566 154 L556 162" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <path class="diagram-line-accent" d="M540 316 L564 316"/><path d="M556 308 L566 316 L556 324" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <path class="diagram-line-accent" d="M540 452 L564 452"/><path d="M556 444 L566 452 L556 460" fill="none" stroke="#3a7d62" stroke-width="3"/>

  <rect class="diagram-surface" x="566" y="52" width="176" height="518" rx="16"/>
  <rect class="diagram-accent" x="584" y="82" width="140" height="104" rx="10"/>
  <text class="diagram-text" x="600" y="112">Model A</text>
  <text class="diagram-small" x="600" y="138">one Robin vote</text>
  <text class="diagram-small" x="600" y="162">not two votes</text>
  <rect class="diagram-amber" x="584" y="210" width="140" height="86" rx="10"/>
  <text class="diagram-text" x="600" y="240">Model B</text>
  <text class="diagram-small" x="600" y="266">one Sparrow vote</text>
  <rect class="diagram-violet" x="584" y="326" width="140" height="158" rx="10"/>
  <text class="diagram-text" x="600" y="356">Photo presence</text>
  <text class="diagram-small" x="600" y="384">Robin: supported</text>
  <text class="diagram-small" x="600" y="410">Mallard: supported</text>
  <text class="diagram-small" x="600" y="444">evaluated per taxon</text>
  <text class="diagram-small" x="600" y="468">no subject invented</text>
  <text class="diagram-small" x="586" y="532">No cross-model</text>
  <text class="diagram-small" x="586" y="552">confidence average</text>
  <path class="diagram-line-accent" d="M742 150 L762 150"/><path d="M754 142 L764 150 L754 158" fill="none" stroke="#3a7d62" stroke-width="3"/>
  <path class="diagram-line-accent" d="M742 404 L762 404"/><path d="M754 396 L764 404 L754 412" fill="none" stroke="#3a7d62" stroke-width="3"/>

  <rect class="diagram-panel" x="764" y="52" width="176" height="518" rx="16"/>
  <rect class="diagram-amber" x="782" y="82" width="140" height="112" rx="10"/>
  <text class="diagram-text" x="798" y="112">Subject 1</text>
  <text class="diagram-small" x="798" y="138">Model split</text>
  <text class="diagram-small" x="798" y="162">explicit review</text>
  <rect class="diagram-accent" x="782" y="218" width="140" height="102" rx="10"/>
  <text class="diagram-text" x="798" y="248">Human assertion</text>
  <text class="diagram-small" x="798" y="274">Subject 1 = Robin</text>
  <text class="diagram-small" x="798" y="298">dissent retained</text>
  <rect class="diagram-violet" x="782" y="344" width="140" height="122" rx="10"/>
  <text class="diagram-text" x="798" y="374">Photo assertions</text>
  <text class="diagram-small" x="798" y="400">Robin present</text>
  <text class="diagram-small" x="798" y="426">Mallard present</text>
  <rect class="diagram-blue" x="782" y="490" width="140" height="54" rx="10"/>
  <text class="diagram-text" x="798" y="522">2-species roster</text>
</svg>
</div>
<figcaption id="species-flow-caption"><strong>Species evidence flow.</strong> Evidence stays attached to a subject or the photo. Label lists from one underlying model cannot multiply its vote; independent models may split; multi-label image evidence can support several taxa without inventing subjects. Human assertions resolve work while preserving dissent for audit.</figcaption>
</figure>

### Evidence hierarchy

Species evidence has four explicit levels:

1. **Subject** — one detected subject in one photo, identified by its detection
   ID and bounding box. A photo with no usable detections may instead have one
   photo-level evidence target.
2. **Prediction source** — one classifier model and label-list version applied
   to that subject. Its identity is the pair
   `(classifier_model, labels_fingerprint)`. User-facing copy uses the model and
   label list display names; raw fingerprints appear only in diagnostics.
3. **Photo** — the set of resolved or suggested species across that photo's
   subjects. More than one subject may produce more than one species.
4. **Burst or encounter** — a coverage summary over its photos and subjects,
   not a new independent prediction.

The serialized review contract preserves subject ID, prediction-source ID,
candidate rank, confidence, taxon identity, and label-list coverage. The
current flattened `species_top5` shape may be read during migration, but it is
not sufficient as the canonical contract because it loses subject and label
list identity and caps rows across otherwise independent sources.

Each source also declares its prediction mode. An **exclusive** source returns
ranked alternatives for one biological subject; only one incompatible leaf
taxon can be correct. A **multi-label presence** source returns independently
qualified taxon-presence claims for a photo or region and may support several
species at once. Process Review must not infer the mode from the shape of a
top-k response.

### Prediction source identity

A label list is part of the prediction source, not incidental metadata. The
same model run against “North American birds” and “All vertebrates” produces
two model/list-pair sources. The process result snapshots the exact source
inventory used for that run:

```json
{
  "source_id": "source-17",
  "classifier_model": "timm-inat21-eva02-l",
  "model_display_name": "iNaturalist EVA-02 Large",
  "labels_fingerprint": "7f83b165...",
  "labels_display_name": "North American birds",
  "label_count": 1240,
  "prediction_mode": "exclusive",
  "independence_group": "timm-inat21-eva02-l"
}
```

Only sources explicitly included in the canonical Process run contribute to
that review. Stale predictions from an older label fingerprint do not appear,
and a source that failed or did not run is reported as missing evidence rather
than treated as a vote against a species.

The default independence group is the underlying classifier model. Multiple
label lists applied to the same model do not create multiple independent model
votes. A saved Process definition may supply explicit source weights or a
different independence grouping, but Process Review displays that policy; it
does not silently infer independence from the number of prediction rows.

### Taxon normalization and compatible predictions

Predictions are compared by canonical taxon ID when available, not display
spelling. Common names, scientific names, aliases, and hierarchy leaves that
refer to the same taxon agree.

Ancestor/descendant predictions are **compatible refinements**, not direct
conflicts. For example, “warbler” and “Yellow Warbler” may support a shared
lineage while differing in specificity. The UI shows the most specific
well-supported suggestion and discloses that another source stopped at a
broader rank. Predictions on incompatible taxonomy branches are disagreements.
Unmapped free-text labels can agree by normalized text but are marked as less
certain than taxon-linked evidence.

### Label-list coverage

A prediction source can only disagree about a taxon its label list was capable
of choosing. If a list does not contain a taxon or a compatible ancestor, that
source **abstains** for that comparison; it contributes neither a zero score nor
a negative vote.

This distinction is visible:

- **Agrees** — the source could express the candidate and selected it or a
  compatible refinement.
- **Disagrees** — the source could express the competing taxa and credibly
  selected an incompatible one.
- **Not covered** — the label list could not express the candidate.
- **Uncertain** — the source ran but did not clear its confidence and margin
  requirements.
- **Unavailable** — the configured source did not produce a usable result.

The label fingerprint registry therefore needs queryable taxon coverage, not
only a display name and label count. Coverage may be stored as a normalized
taxon set or a reference to the immutable label-list manifest used by the run.

### Exclusive alternatives and multi-label presence

An exclusive prediction source returns a ranked candidate list for one
subject. Only a qualified leading candidate participates in automatic
consensus. Lower-ranked candidates communicate uncertainty and remain
selectable during manual review; they are never interpreted as additional
animals or additional species present in the photo.

A leading candidate is qualified using that source's configured confidence and
margin requirements. Model-specific calibration may override global defaults.
Raw confidence values from different models or different label lists are shown
but are not averaged into a probability unless those sources have an explicit
shared calibration.

A source explicitly configured as multi-label presence may emit more than one
qualified taxon for a photo or multi-subject region. Those taxa become separate
presence suggestions, each with its own score and threshold result. They do not
claim how many subjects are present and do not create subject assignments. A
qualified absence counts against a taxon only when the source covers that taxon
and has a versioned, calibrated negative threshold; otherwise the source
abstains.

When a multi-label source is attached to a box known to contain exactly one
subject, incompatible qualified taxa are treated as disagreement about that
subject, not as proof that the subject has multiple species. When the region
may contain several subjects, the suggestions remain photo- or region-level
until a user associates them with subjects or confirms photo-level presence.

### Combining label lists from the same model

For one subject, sources in the same independence group are consolidated before
cross-model consensus:

1. Discard candidates below their source-specific confidence or margin floor.
2. Normalize candidates to canonical taxa and determine whether their label
   lists make them mutually comparable.
3. If all comparable, qualified sources agree or are compatible refinements,
   the model contributes one vote for the most specific supported taxon.
4. If comparable sources select incompatible taxa, mark **Label-list
   disagreement** and make that model abstain from the automatic consensus.
5. If sources cover disjoint taxonomic areas and cannot be compared, show
   **Different label coverage** and do not manufacture a winner. A configured
   routing rule from the Process definition may select the applicable source;
   absent such a rule, the model abstains.

This prevents running the same model against three overlapping lists from
outvoting a genuinely independent second model.

For multi-label presence sources, consolidation runs independently for each
taxon. One independence group contributes at most one support, calibrated
negative, or abstention for that taxon, even when several overlapping label
lists include it. Qualified presence claims for different taxa may coexist;
they are not forced through the exclusive single-winner rule.

### Combining different models for one subject

After same-model sources are consolidated, each independence group contributes
at most one vote for the subject.

- **Unanimous suggestion:** every non-abstaining model agrees or gives a
  compatible refinement.
- **Majority suggestion:** a strict majority agrees, with dissent shown.
- **Model split:** no strict majority exists. In particular, two models that
  choose different species produce no automatic winner.
- **Single-model suggestion:** only one model supplies qualified evidence. It
  may be offered as a suggestion but is labeled as single-model evidence.
- **Insufficient evidence:** every model abstains or is unavailable.

The interface reports votes and abstentions—for example, “2 of 3 models agree;
1 uncertain”—alongside each source's original confidence. It does not display
an opaque aggregate percentage that implies the models are calibrated or
independent.

For photo- or multi-subject-region multi-label evidence, cross-model consensus
also runs per taxon. Several taxa can each receive supported-presence
suggestions. Incompatible taxa at this level do not conflict merely because
they differ: multiple species can share an image. A disagreement exists when
models provide qualified opposing evidence about the same taxon's presence,
or when incompatible predictions have been assigned to one subject known to
represent a single animal.

A majority suggestion is eligible for the fast confirmation action, but any
dissent remains visible. A model split, same-model label-list disagreement, or
incompatible high-confidence alternative requires explicit review and is
included by the Species conflicts filter while unresolved.

### Multiple subjects and multiple species in one photo

Each detected subject retains its own prediction-source matrix. The photo
inspector and Group Review modal show subject chips and bounding boxes; choosing
a subject updates the evidence panel for that subject.

Two credible subjects supporting different taxa mean **multiple species in the
photo**, not model disagreement. The photo displays a species roster such as:

> American Wigeon · subject 1, 2 models<br>
> Mallard · subject 2, 2 models

Two subjects supporting the same taxon display one species with “2 subjects.”
Overlapping duplicate detections must be reconciled by detector identity or a
deduplication rule before they can inflate subject or species counts.

A whole-image classifier result with no subject association is photo-level
evidence. An exclusive source contributes alternatives for the image. A true
multi-label presence source may contribute several species suggestions, but it
cannot establish subject count or associate a species with a particular box.
When subject-specific and photo-level evidence disagree, subject-specific
evidence is shown first and the photo-level result is labeled as contextual
evidence.

### Species variation across a burst or encounter

Burst and encounter summaries aggregate resolved subject evidence by taxon and
report coverage rather than averaging every candidate row. Each species row
includes:

- confirmed and suggested subject counts;
- number of photos and bursts containing that species;
- agreeing, dissenting, abstaining, and unavailable model counts; and
- whether the evidence is subject-specific or photo-level only.

Different taxa across different photos are **group variation**. The page
distinguishes likely explanations:

- Species occur on different subjects in the same frames: present a
  multi-species encounter roster.
- Species align with different complete bursts: show a prominent **Possible
  split by species** action at the burst boundary.
- A credible species appears only in isolated photos: mark those photos and
  require review; do not silently erase the minority evidence.
- The disagreement is only taxonomic specificity: show compatible refinement,
  not a split warning.

An encounter may still show a primary suggestion for speed, but its label is a
summary such as “American Robin on 8 of 10 photos,” never an assertion that the
other two photos agree. Additional credible species appear beside it rather
than being hidden under a single winner.

### Species confirmation model

Confirmation is a positive species-presence assertion, not an exclusive
single-species field.

- **Subject confirmation** assigns one canonical taxon to one detected subject.
- **Photo confirmation** confirms that a taxon is present in a photo when no
  reliable subject target exists.
- **Burst or encounter confirmation** confirms that a taxon is present across
  the explicitly named target photos. It does not remove other confirmed
  species from those photos.
- **Replace species** is a separate, explicit action that names the old and new
  assignments and previews removals.
- **Not identifiable** resolves a subject without assigning a taxon.
- **False detection** resolves and excludes a detected subject.

Effective photo keywords are the union of confirmed subject- and photo-level
species assertions. This naturally permits several species keywords on one
photo. A group-level confirmation expands to auditable per-photo assertions so
later splitting, undo, and partial replacement remain precise.

For the common case—one consistent species across one subject per photo—the
header retains a one-click **Confirm [species] for encounter** action. It shows
the exact number of photos and unresolved subjects it will affect. When
credible multi-species evidence or a material disagreement exists, the header
uses **Review species** instead of presenting a misleading one-click winner.

### Completion state

Confirmation is tracked per reviewable subject or photo-level evidence target,
not by one `species_confirmed` boolean on the encounter.

A subject is resolved when it has a confirmed taxon, is marked Not
identifiable, or is marked False detection. A photo is resolved when all of its
reviewable subjects are resolved and any material photo-level conflict has been
acknowledged. A burst or encounter is resolved when all member photos are
resolved.

Resolving a disagreement never edits or deletes the model outputs. Confirming a
taxon records the human assertion and marks the target resolved; dissenting
evidence remains visible under **Resolved disagreement** with the reviewer,
time, and command provenance. The default Species conflicts view contains
unresolved material conflicts. A secondary **Include resolved disagreements**
option supports auditing without returning adjudicated encounters to the active
work queue.

The Needs review view includes a complete encounter when any member is
unresolved. Resolved includes encounters with no unresolved members. This
definition supports multi-species photos and prevents confirming one dominant
species from hiding unresolved minority subjects.

### Species evidence interface

The collapsed encounter header shows only decision-relevant information:

- confirmed species roster, or the primary suggestion plus additional-species
  count;
- photo coverage, such as “8 of 10 photos”;
- model support, such as “2 of 3 models”; and
- badges for Model split, Label-list disagreement, Multiple species, Group
  variation, or Insufficient evidence.

Expanding the species control reveals a matrix organized by subject and source.
Rows are subjects or photo-level targets; columns are prediction sources
labeled with both model and label list. Cells show the leading taxon,
confidence, and state (agrees, disagrees, not covered, uncertain, unavailable).
Selecting a cell reveals lower-ranked alternatives and source diagnostics.

The Group Review modal keeps its per-photo strip. Multi-subject photos expose
subject chips and boxes, while the evidence panel shows the selected subject's
source matrix and the burst-level species coverage summary. Users can select
subjects or photos and assign, confirm, reject, or mark evidence unresolved in
bulk.

## Editing behavior

There are no read-only review views. The current message “Collection scope is
view-only. Switch to Latest review to make changes.” is removed once all
mutations below use canonical identities.

### Action targets

- A photo action affects that photo.
- A burst action affects every photo in the complete displayed burst.
- An encounter action affects every photo in the complete displayed encounter.
- A bulk action must say whether it targets **matching photos** or **all photos
  in shown encounters**. It may not infer that distinction from which cards
  happened to be rendered.

Because Any photo mode intentionally includes context outside the collection or
status match, encounter- and burst-level controls can affect context photos.
Their labels and confirmations must use complete target counts, for example:

> Reject encounter · 8 photos

Matching-only bulk actions are allowed when explicitly named, for example:

> Reject 14 matching photos

The default target for an action physically located on an encounter or burst
is the complete object, even in Any photo mode. Page-level bulk actions have no
default and require an explicit target choice in their confirmation sheet.
The sheet previews matching photos, context photos, protected photos, and any
photos whose current manual decision would be replaced.

### Automated recommendations and manual triage

Process produces an automated Keep, Review, or Reject recommendation. Review
creates an effective manual decision:

- **Keep** protects the photo from rejection and marks it as selected.
- **Review** clears a manual Keep or Reject decision and returns the photo to a
  neutral review state. It does not mean “accept the automated Review label.”
- **Reject** marks the photo for rejection in downstream workflows.

The UI always distinguishes **Recommended: Reject** from **Rejected**. A manual
decision wins over the recommendation until explicitly cleared. Recomputing
scores may change the recommendation but never overwrites manual triage.

Photo actions apply immediately and optimistically, then reconcile with the
server. `P` sets Keep, `X` sets Reject, and `Space` clears to Review when focus
is in a review surface and not in a text field.

Burst and encounter actions use complete membership:

- **Reject burst · N photos** rejects every member not already rejected;
- **Clear rejects · N photos** clears only rejected members and never clears a
  Keep decision;
- **Keep selected · N photos** is offered from Group Review, not as an
  indiscriminate keep-entire-encounter action; and
- protected photos require an explicit second confirmation before Reject can
  replace their Keep/protected state.

The page-level bulk menu offers **Set matching photos to Keep/Review/Reject**
and **Set all photos in shown encounters to Keep/Review/Reject**. The chosen
target appears in the action label, confirmation, progress message, undo
message, and audit record.

### Species confirmation

Species confirmation uses the assertion model above. The request names the
target subjects or photos, taxon, operation (add, replace, Not identifiable, or
False detection), canonical run, and expected revision. The server returns the
changed photos and encounters, updated completion and summary counts, the new
`revision`, and the unchanged `structural_revision`; whether an active
pagination cursor survives follows the per-view rules in the Pagination
section. It does not return the complete encounter list.

Confirmation never changes structure merely because another species is
present: multi-species photos and encounters are valid. When credible species
variation aligns with burst boundaries, the interface may suggest **Split
encounter here** or **Separate burst as encounter**, but the user must preview
and request that structural command separately.

### Flags and quality decisions

Photo flags remain database-backed and update optimistically. Encounter and
burst flag controls submit explicit complete member IDs resolved from the
canonical group. Their responses contain changed photo state and updated
counts only.

### Structural editing

Every structural action opens a preview with a compact before/after diagram,
photo and burst counts, affected species summaries, and the resulting position
in chronological order. The user may cancel without mutation. Apply uses the
canonical run, expected revision, stable target IDs, and an explicit ordered
membership result; it never uses a filtered-array index.

#### Split burst here

The user chooses a gap between two chronologically adjacent photos. The left
photos remain in the original burst and the right photos form a new adjacent
burst in the same encounter. Neither side may be empty. Photo triage, subject
assertions, photo assertions, and evidence stay attached to their photos.
Burst summaries are derived again from each side.

If the original burst carried a legacy burst-level species decision, the
preview expands it into the auditable photo/subject assertions it represents
before splitting. No hidden exclusive burst field is copied onto both sides.

#### Extract photos to new burst

The user selects one or more photos from one source burst. Selected photos are
removed from that burst and placed into new burst membership inside the same
encounter. The source sequence is partitioned into maximal selected and
unselected chronological runs, and every run becomes a burst. For example,
extracting two middle photos from a six-photo burst produces an unselected
two-photo burst, an extracted two-photo burst, and a final unselected two-photo
burst. Disjoint selections therefore create separate extracted bursts and do
not create overlapping time ranges. Resulting bursts are ordered by first
capture time, with deterministic filename/ID fallback for missing timestamps.

Extracting every photo is invalid because it would only rename the burst. A
one-photo source burst offers Separate as encounter or Merge, not Extract.
This operation replaces the current ambiguous “Detach photo” action.

#### Merge adjacent bursts

Only adjacent bursts in the same encounter can be merged directly. Their
photos are placed in chronological order and their source IDs are recorded in
the command provenance. Conflicting human species assertions remain attached
to their subjects/photos and therefore produce a multi-species or disagreement
summary rather than being overwritten. The preview warns if the merged burst
will no longer have a single eligible quick-confirm action.

#### Split encounter here

The user chooses a boundary between bursts. Bursts before the boundary remain
in the original encounter; the selected burst and later bursts form a new
encounter. Both sides must contain at least one burst. Burst membership is
unchanged. Encounter summaries, time ranges, and view qualification are
recomputed.

#### Separate burst as encounter

The selected complete burst is removed from its encounter and becomes a new
one-burst encounter. This is useful when a middle burst is unrelated and a
simple boundary split would put neighboring bursts on the wrong side. The
command does not split the burst and does not silently merge it into a nearby
encounter, even when species and time appear compatible. This operation
replaces the current ambiguous “Detach burst” action.

Separating the sole burst of an encounter is a no-op and is not offered.

#### Merge encounters

The user selects two or more encounters. The default chooser offers adjacent
encounters in chronological order; an advanced search may target a nonadjacent
encounter but requires a warning because it can create a discontinuous event.
Existing bursts remain intact and are ordered chronologically. Confirmed
species and triage decisions are preserved. The result may legitimately be a
multi-species encounter.

#### Structural results

A successful command returns removed, added, and updated encounter/burst
records plus the new revision. The page re-evaluates the current view. If a new
encounter does not match, the success message still states where it went and
offers **Show result**. Structural changes invalidate only affected cursors and
group traces where possible; a canonical regroup may invalidate all cursors.

### Group Review

The Group Review modal always opens a complete canonical burst. It remains
editable from every view. Applying changes sends command data for that burst;
it never posts the page's complete `pipelineResults` object back to the server.

Group Review is the primary high-resolution comparison surface. It contains:

1. **Picks, Candidates, and Rejects lanes.** Every burst photo appears exactly
   once unless staged for extraction. Lane state reflects live manual triage,
   not a stale Process snapshot.
2. **Large comparison preview.** The selected photo is shown at a selectable
   preview resolution with fit, 1:1, and up-to-5× zoom. Hovering or locking a
   location compares the same region across selected frames.
3. **Photo controls.** Thumbnail size, per-photo pan offsets, reset offsets,
   selected-region sharpness, subject chips/boxes, mask visibility, and image
   metadata.
4. **Species evidence.** The selected subject's source matrix, photo-level
   evidence, burst coverage summary, lower-ranked exclusive alternatives, and
   multi-label presence suggestions.
5. **Commit footer.** Staged triage count, staged structural count, species
   resolution count, and one **Apply changes** button whose label and tooltip
   enumerate the writes.

Single click selects one photo. Command-click toggles photos and Shift-click
selects a chronological range. Right-click keeps the selection and opens the
photo menu. The active selected photo drives the large preview; multi-photo
actions target the selected set.

Inside Group Review:

- `P` moves selected photos to Picks;
- `X` moves them to Rejects;
- `Space` moves them to Candidates/Review;
- Up and Down move one lane at a time;
- Left and Right change the active photo;
- `Z` toggles the preview between fit and 1:1;
- Delete stages **Extract photos to new burst**, never deletion from the
  library; and
- Escape requests close, warning first when staged changes exist.

The footer has independently reviewable sections for **Apply triage**,
**Apply species decisions**, and **Apply structural changes**. A section with
changes is selected automatically but may be deselected before Apply. The
button preview states consequences such as:

> Keep 2 photos, reject 4, confirm American Wigeon on 3 subjects, and extract
> 1 photo to a new burst

Closing without Apply discards staged modal changes only. It never reverses
previously committed page actions. While Apply is pending, the modal remains
open, targets are locked, and retry is possible without duplicating commands.

### Species evidence panel

The panel can open from an encounter, burst, photo, subject, or conflict badge.
It preserves that target in the URL while open. The left side lists photo and
subject targets; the right side shows prediction sources and human assertions.
Changing subjects updates highlighted boxes in the photo preview. Filters can
show all sources, dissent only, unavailable sources, or resolved
disagreements.

The panel supports Confirm, Replace, Not identifiable, False detection,
acknowledge photo-level conflict, and undo. For a multi-label photo suggestion,
each taxon has an independent Confirm presence action. The panel never turns a
runner-up from an exclusive classifier into a presence checkbox.

### Photo detail fallback

Photos with burst membership open Group Review. A legacy photo with no burst
opens a photo detail drawer containing its preview, masks, effective and
recommended triage, rejection reasons, quality-score components, species
evidence, encounter filmstrip, and raw diagnostic features. The fallback is a
migration aid, not a separate long-term review experience.

### Undo and redo

Undo and redo operate on server-recorded review commands. They return the same
changed-record envelope as the original mutation. The browser is not the
source of truth for restoring a previous full cache snapshot.

After a mutation, a persistent notification states the effect and offers Undo.
The page also exposes an **Edit history** panel with ordered commands, actor,
time, target, and affected counts. Undo creates a compensating command; redo
reapplies an undone command only when its expected targets still exist. If a
later structural edit makes exact reversal impossible, history explains why
instead of offering a destructive approximation.

Keyboard Command-Z and Shift-Command-Z invoke page undo and redo only when a
text field or native editing surface is not active.

### Pending, concurrency, and failure behavior

Actions show pending state within 100 ms. Overlapping writes to the same photo,
subject, burst, or encounter are serialized. Unrelated actions may continue.
Double activation sends one idempotent command ID and cannot duplicate a
split, assertion, or keyword.

On a network failure, optimistic state rolls back and the page retains focus.
On `409 Conflict`, the page fetches changed target records, explains that the
review revision advanced, and asks the user to retry after showing the updated
target. It never silently retargets by position. Offline mode is read-only only
because persistence is unavailable, not because of the selected view; the
page labels this as **Offline—changes cannot be saved**.

### View movement after an edit

An edit may cause an encounter to stop matching the current view—for example,
resolving the last unresolved subject while viewing Needs review. Apply
the change immediately, briefly retain the updated encounter with a completion
state, then remove it after the normal success feedback. Focus moves to the
next encounter without jumping to the top of the page.

## Grouping and scoring controls

Scoring changes that do not alter encounter membership apply to the canonical
result and return changed labels/counts. The current view is then re-evaluated.

Grouping changes can alter every encounter in the canonical result. They are
therefore explicit full-result operations:

- The sidebar states **Applies to the complete latest process result**.
- While a grouping change runs, structural review actions are disabled and
  the current view remains visible with a progress state.
- Completion installs a new canonical revision and reapplies the current view.
- A collection filter never causes grouping to run on only that collection.

If live grouping cannot meet the page's responsiveness and durability
requirements at large scale, replace automatic slider requests with an Apply
button. Do not solve that problem by producing an ephemeral collection-only
grouping.

## Navigation, focus, and shortcuts

The page has one visible focus target. A focused encounter has a non-color-only
outline and supplies the grouping trace. Within an expanded encounter, focus
may move to a burst or photo without changing the focused encounter.

Outside a text field or overlay:

- `J` and `K` move to the next and previous loaded encounter;
- Right Arrow expands the focused encounter and Left Arrow collapses it;
- `Enter` opens the focused burst or photo in Group Review;
- `P`, `X`, and Space apply Keep, Reject, and Review to the focused photo;
- Shift with `P`, `X`, or Space targets the focused complete burst after a
  labeled confirmation;
- `F` moves focus to Search;
- `C` opens the Collection chooser;
- `E` opens the Species evidence panel for the focused target;
- `?` opens the shortcut reference; and
- Escape closes the topmost menu, drawer, or overlay before changing page
  focus.

Encounter cards and pages that load later participate in the same logical
order. Shortcuts resolve stable target IDs at activation time. They never act
on an array position remembered before filtering, sorting, or structural
edits. Shortcut hints are discoverable from buttons and can be disabled in
preferences.

## Primary workflows

### Enter or resume review

1. Open Process Review from a completed Process run.
2. See the shell, coverage, health, and canonical counts immediately.
3. Restore the previous URL-defined view and personal display preferences.
4. Load the first page of complete encounters and focus the first unresolved
   encounter, or the previously focused encounter when it still qualifies.
5. Resume editing without an explicit save step; every committed action is
   already durable.

If a newer Process run exists, the page does not silently replace an open
review. It offers **Open newer result** and explains which run the current URL
references. Opening it installs the new canonical result and attempts to carry
forward photo/subject decisions that still map unambiguously.

### Narrow a large review while preserving encounters

1. Choose a Collection, Triage state, Species review state, and/or Search.
2. Leave Encounter match at Any photo to see complete context around any
   matching member, or choose Every photo for a strict all-members condition.
3. Read matching, encounter, and context counts before acting.
4. Inspect **Matches view** and **Context** markers inside each complete
   encounter.
5. Perform any photo, burst, encounter, species, or structural action normally.

Changing or clearing the view never recomputes grouping and never creates a
read-only page.

### Triage a burst

1. Open **Review burst** or click one of its photos.
2. Compare frames in Group Review at the needed resolution.
3. Move photos among Picks, Candidates, and Rejects by keyboard or pointer.
4. Optionally calculate sharpness for the same selected region across frames.
5. Review the exact staged counts and Apply triage.
6. Continue to the next unresolved burst without returning to the top of the
   encounter.

### Resolve a species disagreement

1. Open a conflict badge or **Review species**.
2. Select the disputed subject or photo-level target.
3. Compare each model/list-pair prediction, coverage state, confidence,
   alternatives, and independence group.
4. Confirm one taxon, choose a lower-ranked alternative, search for another
   taxon, mark Not identifiable/False detection, or leave unresolved.
5. Review which subject/photo assertions will be added or replaced.
6. Apply. Dissenting evidence remains under Resolved disagreement for audit.

### Confirm several species in one image

1. Select each subject box and confirm its taxon independently.
2. When a credible species has no reliable subject box, confirm photo-level
   presence instead.
3. Treat qualified multi-label results as separate presence suggestions; do
   not treat exclusive runner-ups as additional species.
4. Verify the photo roster and unresolved-target count.
5. Apply without removing species already confirmed on other subjects.

### Correct grouping

Use the smallest structural operation that expresses the correction:

- wrong boundary inside one burst → Split burst here;
- outlier photos inside a burst → Extract photos to new burst;
- two artificial neighboring bursts → Merge adjacent bursts;
- encounter boundary belongs between two bursts → Split encounter here;
- one unrelated middle burst → Separate burst as encounter; or
- two encounters are one event → Merge encounters.

The preview is part of the workflow, not an optional diagnostic. After Apply,
the page highlights resulting objects and offers Undo and Show result.

### Finish or leave

There is no page-wide Save button. The summary strip reports unresolved
targets and active conflicts. **Review next unresolved** moves through the
current view; **Review unresolved across all processed photos** clears only the
view criteria necessary to expose remaining work, after confirmation.

Leaving the page while a mutation is pending warns and waits or cancels the
navigation. Leaving with no pending request is safe. Staged, unapplied Group
Review changes require confirmation; committed actions do not.

## Downstream effects and page boundaries

- Keep, Review, and Reject update the library's effective triage state. Reject
  marks a photo for downstream culling; Process Review never deletes a file.
- Confirmed species assertions update effective taxonomy keywords through a
  provenance-bearing operation. Removing or replacing a species assertion
  removes only keyword attachment owned by that assertion when no other source
  still requires it.
- Library decisions are durable before sidecar synchronization. Writing XMP
  sidecars exports pending metadata; choosing Keep only in Vireo suppresses
  that export without reverting the underlying decision.
- Subject boxes, prediction evidence, and review resolution belong to Process
  Review state. They do not alter original image pixels or classifier output.
- Burst and encounter structure belongs to the canonical review revision. It
  does not move files, alter collection membership, or modify capture times.
- **Open in Browse** navigates to the same photo and current library decisions.
  Changes made in Browse are reflected when Process Review next reads the
  affected records.
- Rapid Review and Process Review operate on the same run, revisions, triage,
  and species assertions. Neither keeps a private browser-owned copy that can
  overwrite the other.
- Review misses is a separate workflow for likely missed subjects. Returning
  to Process Review preserves the view and focus when possible.
- Starting a new Process run is the only path that recomputes models and the
  initial grouping. Review controls do not start classification implicitly.

## Data and API design

### Stable identities and revisions

Every canonical review result has:

```json
{
  "review_run_id": "pipeline-1786837933532-107",
  "revision": 42,
  "structural_revision": 7
}
```

`revision` advances on every review mutation, including species confirmation,
triage, and flag changes. `structural_revision` advances only when encounter
or burst membership changes: a new Process run, applied regrouping, or an
explicit structural command such as Split, Extract, Merge, or Separate.
Decision-only mutations increment `revision` but preserve
`structural_revision` so focus and scroll position remain valid, and so
pagination cursors remain valid in views whose sort and filters do not read
decision state (the Pagination section defines the decision-dependent
cases).

Every encounter and burst has an opaque stable ID. IDs survive labels, flags,
sorting, filtering, and pagination. A structural regroup may replace IDs;
manual structural commands preserve unaffected IDs and create IDs for new
groups.

Mutation requests include `review_run_id`, the expected `revision`, and the
target IDs. If a new Process run or another structural mutation has made the
page stale, the server returns `409 Conflict` with the current run,
`revision`, and `structural_revision`. The page refreshes its view and
explains that the review changed; it never applies a command to whichever
group now occupies an old array index.

### Species evidence contract

The process snapshot stores the source inventory once and references compact
source IDs from subject evidence. A representative loaded photo is:

```json
{
  "id": 812,
  "subjects": [
    {
      "subject_id": "detection-4401",
      "box": {"x": 0.12, "y": 0.18, "w": 0.31, "h": 0.52},
      "evidence": [
        {
          "source_id": "source-17",
          "state": "available",
          "candidates": [
            {
              "taxon_id": 144814,
              "name": "American Wigeon",
              "rank": 1,
              "confidence": 0.91
            },
            {
              "taxon_id": 6930,
              "name": "Mallard",
              "rank": 2,
              "confidence": 0.06
            }
          ]
        }
      ],
      "consensus": {
        "state": "majority",
        "taxon_id": 144814,
        "agreeing_models": 2,
        "voting_models": 3,
        "abstaining_models": 1
      },
      "resolution": {"state": "unresolved"}
    }
  ],
  "confirmed_taxa": []
}
```

The response also includes the referenced source records and label-list
coverage metadata. Derived consensus is returned for display and query
performance, but the underlying source candidates remain available so the UI
can explain and audit it.

Species resolution records are separate from classifier predictions. A
representative assertion contains:

```json
{
  "assertion_id": "species-assertion-88",
  "photo_id": 812,
  "subject_id": "detection-4401",
  "taxon_id": 144814,
  "state": "confirmed",
  "origin": "manual",
  "command_id": "review-command-301"
}
```

Photo-level assertions use a null subject ID. Not identifiable and False
detection resolutions have no taxon ID. Assertions retain command provenance
so group confirmation, replacement, undo, and redo can reconstruct exactly
which photo/subject assignments changed.

### View query

A dedicated query accepts the complete view definition. A representative
request is:

```json
{
  "review_run_id": "pipeline-1786837933532-107",
  "collection_id": 197,
  "status": "REVIEW",
  "species_review": "needs_review",
  "encounter_match": "any",
  "search": "sandpiper",
  "search_options": {"match_case": false, "whole_word": false},
  "sort": "chronological",
  "cursor": null,
  "page_budget": {"encounters": 40, "photos": 600}
}
```

The server resolves collection membership and intersects it with the canonical
process result. It returns:

- canonical run, `revision`, and `structural_revision`;
- process-coverage counts;
- match, encounter, context-photo, and confirmation counts;
- a page of complete encounters;
- only photo records referenced by those encounters;
- matching photo IDs for explanation/highlighting; and
- an opaque next cursor.

Collection membership is evaluated on the server. The browser must not fetch a
large collection ID list and intersect it with a second large result payload.

### Pagination

Pagination is encounter-based and uses an opaque keyset cursor tied to the
run, `structural_revision`, sort, and filter fingerprint. The cursor encodes
the sort key and stable ID of the last returned encounter — never a bare
offset — so membership changes elsewhere in the result cannot silently skip a
successor.

Whether decision-only mutations preserve the cursor depends on the active
view:

- When the sort and filters read only snapshot data (chronological,
  newest/oldest first, most/fewest photos, most bursts, with
  decision-independent filters), decision-only mutations such as species
  confirmation, triage, and flags do not invalidate the cursor, so a large
  review can be edited while continuing to scroll from the loaded position.
- When the sort or a filter reads mutable decision state (`unresolved first`
  or conflict-severity sorts, triage-status or species-review filters), a
  decision can move an encounter across the cursor boundary or out of the
  result set, so cursor reuse could repeat or miss encounters. A decision
  mutation that changes a sort or filter key of the active view therefore
  marks the cursor stale, and the next page fetch obtains a fresh cursor
  anchored at the stable ID of the last loaded encounter. Already-loaded rows
  stay in place and update through the delta, and the client's ID-keyed maps
  drop any encounter a re-anchored page would repeat.

The server observes both a target encounter count and a soft target photo
count:

- never split an encounter;
- return at least one encounter even when it exceeds the photo target;
- otherwise stop before adding an encounter that would exceed the soft photo
  target; and
- invalidate the cursor when `structural_revision` or the query changes, or
  when a decision mutation changes a sort or filter key that the
  cursor-bound view depends on.

The client loads the next page as the user approaches the end of the current
window. Already loaded pages may be virtualized or discarded outside a bounded
window, provided focus, selection, and scroll restoration remain correct.
Collapsed encounter bodies should not create photo-card DOM until expanded.
Thumbnail loading remains lazy.

### Mutation response

All review mutations use a common delta envelope:

```json
{
  "ok": true,
  "review_run_id": "pipeline-1786837933532-107",
  "revision": 43,
  "structural_revision": 7,
  "updated_encounters": [],
  "removed_encounter_ids": [],
  "updated_photos": [],
  "summary_delta": {},
  "view_may_have_changed": true
}
```

The client compares the returned `structural_revision` with the one bound to
its active cursor: a bumped value means the next page fetch must use a fresh
cursor. When the value is unchanged, pagination remains valid unless the
active view depends on decision state and `view_may_have_changed` reports
that a sort or filter key changed, in which case the next page fetch
re-anchors as described in the Pagination section. The client applies the
delta to normalized maps keyed by stable IDs. It does not replace or
deep-clone the complete review result.

### Persistence model

Computed pipeline output and mutable review state must not require a complete
JSON rewrite for every click.

The intended separation is:

- an immutable or infrequently replaced computation snapshot for photo
  features, source inventory, subject-level ranked predictions,
  recommendations, original grouping, and trace data; and
- transactional mutable review records for flags, subject/photo species
  assertions, evidence resolutions, burst overrides, manual structure changes,
  and edit history.

The mutable layer may be normalized database tables or a compact revisioned
overlay, but it must support atomic command application and proportional
responses. The server composes the canonical view from the computation
snapshot and mutable state.

The existing full JSON cache can remain as a migration input and a rebuildable
derived artifact. It is no longer accepted from the browser as authoritative
state.

## Loading, empty, and error states

| State | Required presentation and action |
|---|---|
| Initial load | Render the page shell and lightweight run/coverage summary, then skeleton whole-encounter rows until the first page arrives |
| Process currently running | Show stage, progress, and **Open running Process**; retain the latest completed review as a clearly labeled older result when one exists |
| No completed Process result | Explain that Process Review needs a completed run and offer **Open Process** |
| Process failed | Show the last successful review separately from the failed run and link to its failure details |
| Optional feature missing | Keep review available, name affected photos/features, and offer to run the missing stage |
| View updating | Retain the prior list with an updating treatment until the first new page arrives; ignore stale responses by query generation |
| No matches | Say **No encounters match this view**, list active criteria, and offer **Clear view filters** |
| Collection outside run | State how many collection photos fall outside the Process result and offer **Open Process**; do not compute implicitly |
| Invalid/deleted collection | Remove only that filter, announce the reset, and keep the canonical review available |
| Empty or missing burst member | Preserve the encounter shell, label the missing record, and offer repair diagnostics; never silently renumber targets |
| Missing image file or thumbnail | Keep its card and metadata; show **Locate file** or library diagnostics |
| Encounter larger than page budget | Return that one complete encounter, warn that expansion may be slower, and virtualize its burst/photo content without splitting pagination |
| Page request failed | Keep already loaded encounters, show an inline retry at the failed boundary, and do not duplicate prior pages |
| Mutation failed | Roll back optimistic state, retain selection/focus, explain the affected action, and offer retry |
| Stale revision | Refresh affected targets, show the intervening change, and require explicit retry |
| Offline | Keep loaded data readable, disable mutation controls with an offline explanation, and retry connectivity without changing the view |
| End of view | Report complete encounter and shown-photo totals; do not use an infinite spinner |

## Performance requirements

For a canonical result of at least 15,000 photos and 1,000 encounters on a
supported local Mac:

- first useful encounter content should appear without downloading the full
  result;
- a typical first page should stay below 2 MB serialized;
- the document must not contain photo cards for unloaded or collapsed
  encounters;
- changing a view must not serialize or clone the full canonical result;
- a normal photo, burst, encounter, or species mutation must have payload and
  response sizes proportional to its target, not total review size;
- visible pending feedback appears within 100 ms of an action; and
- mutation acknowledgement should target under 500 ms at the 95th percentile
  when no external storage operation is required;
- opening Group Review should show its shell and cached thumbnails within 150
  ms, with live decisions and higher-resolution previews filling in
  incrementally; and
- keyboard navigation and lane movement should remain within a 16 ms frame
  budget for the loaded burst.

Automated performance coverage records response bytes, first-page encounter
and photo counts, mutation bytes, and browser DOM node counts. Timing thresholds
that are too host-sensitive for blocking tests are still recorded in a repeatable
benchmark.

## Accessibility and input behavior

- The encounter-match control has a visible label and keyboard-operable radio
  semantics.
- Page regions use landmarks for navigation, advanced controls, review tools,
  encounter results, and notifications. The page has one descriptive H1.
- Every encounter, burst, photo, subject, and overlay has a programmatic name
  containing its object type and useful count or filename.
- Match highlighting includes text or an icon with an accessible label and
  does not rely on color alone.
- Keep, Review, Reject, Context, Matches view, conflicts, protection, and
  resolution states never rely on color or hover alone.
- Collapsible controls expose `aria-expanded` and their controlled region.
- Segmented filters use radio semantics; toggle buttons expose pressed state;
  multi-selection exposes selected state and count.
- Group Review lanes have descriptive names and keyboard operations that do
  not require drag and drop. Dragging is an enhancement only.
- Subject boxes have corresponding list controls, so precise pointer placement
  is not required to choose a subject.
- The loupe, masks, and score visualizations have text equivalents. Raw
  confidence and quality values remain available.
- Pending, success, error, view-count, and focus-movement messages use
  appropriately polite live regions and do not repeatedly announce every
  thumbnail load.
- Loading additional pages does not steal keyboard focus.
- When an edited encounter leaves the view, focus moves predictably to the next
  encounter and is announced.
- Existing keyboard shortcuts act on the focused canonical target, not an
  array position inferred from the current page.
- Focus is trapped only inside modal dialogs, returns to the invoking control,
  and remains visible against every state background.
- All actions remain usable at 200% zoom and in a 320 CSS-pixel-wide viewport.
- Motion for card removal, regroup previews, and success feedback respects the
  reduced-motion preference.
- Thumbnail and preview images use meaningful filename/position alternatives
  where the image contributes to navigation; decorative duplicates use empty
  alternatives.

## Migration and delivery

Implement behind one internal feature boundary and expose it only when the
complete editable flow is ready. Do not ship another read-only scoped mode as
an intermediate state.

1. **Canonical identity:** add run/revision metadata and stable encounter and
   burst identities; migrate existing caches when loaded.
2. **Species evidence:** replace the flattened review contract with
   subject-level predictions keyed by model/list-pair source; snapshot source
   inventory and label-list taxon coverage; add multi-species assertions and
   resolution state. Legacy predictions remain readable during migration.
3. **Projection query:** implement server-side collection/status matching,
   Any photo and Every photo semantics, complete-encounter responses, counts,
   and cursors.
4. **Incremental page state:** normalize client records, render paged complete
   encounters, remove full-result cloning, and persist the view definition.
5. **Command mutations:** move triage, species, Split burst, Extract photos,
   Merge bursts, Split encounter, Separate burst, Merge encounters, Group
   Review, undo, and redo paths to stable targets and delta responses.
6. **Mutable persistence:** remove full-cache writes from per-click paths and
   retire browser-authoritative `/api/pipeline/save-cache` usage.
7. **Toolbar cutover:** replace Latest/Workspace/Collection modes with the
   editable view controls and remove all scoped-view guards and read-only copy.
8. **Grouping cutover:** make grouping an explicit canonical-result operation
   that reapplies, rather than redefines, the current view.
9. **Complete page cutover:** install the specified header, summaries, toolbar,
   card hierarchy, Group Review, Species evidence panel, structural previews,
   keyboard model, empty/error states, and responsive drawers as one coherent
   workflow.

Existing endpoint behavior used by other pages, including collection-scoped
pipeline analysis for Cull, may remain. Process Review stops using those
analysis endpoints for view filtering.

## Alternatives considered

### Remove the read-only guard

Rejected. Current scoped results contain alternate encounter arrays and can be
posted back as the complete cache. Enabling edits without changing mutation
identity and persistence risks silent loss of out-of-scope results.

### Filter the existing full result only in the browser

Rejected as the final architecture. It could preserve encounters, but the page
would still download, parse, clone, retain, and repeatedly scan the full result.
It does not solve the observed large-review performance problem.

### Continue regrouping each collection

Rejected for Process Review. It changes encounter boundaries at the moment the
user is trying to review them and makes edits ambiguous. Collection-scoped
analysis can remain a processing workflow elsewhere.

### Offer a “matching photos only” mode

Rejected from this design. Removing individual photos from a displayed
encounter hides the context needed to assess species, burst quality, and group
structure. Users can inspect or act on matching photos while the complete
encounter remains visible.

### Label the control “Keep encounters intact”

Rejected because it implies the other choice fragments encounters. The actual
choice is whether any or every member must match; encounter integrity is an
invariant in both modes.

## Acceptance criteria

- Selecting a collection never calls live regrouping and never changes an
  encounter's membership.
- Every Collection, status, species-review-state, and search view remains
  editable.
- Any photo mode includes the complete encounter when one member matches and
  identifies the matching member.
- Every photo mode excludes an encounter when even one member does not match;
  qualifying encounters remain complete.
- Combining Collection and Status applies the encounter-match rule to their
  intersection.
- Species-review filtering never removes an individual burst from a displayed
  mixed encounter.
- The toolbar reports matching photos, complete encounters, and total context
  photos separately.
- Photos outside the latest Process run are reported and never silently
  computed or grouped by a view change.
- Photo, burst, encounter, species-confirmation, every named structural
  command, Group Review, undo, and redo work from a collection-filtered view.
- Page header destinations, Process health/coverage, canonical summary counts,
  current-view counts, and active filters are visible and use unambiguous
  units.
- Pending sidecar synchronization is presented as an export of already saved
  decisions; Keep only in Vireo cannot be mistaken for undo.
- Encounter cards, burst rows, and photo cards expose the content and actions
  defined by this document without ambiguous `×` structural controls.
- Automated recommendations and effective manual triage are visibly distinct;
  recomputing recommendations never replaces manual decisions.
- Group Review opens a complete burst, exposes live triage and species state,
  supports multi-selection and comparison, previews every staged write, and
  does not commit unchecked sections.
- Split burst changes only a burst boundary; Extract photos creates burst
  membership in the same encounter; Separate burst creates a new encounter.
- Merge burst and encounter operations preserve photo decisions, assertions,
  evidence, and chronological order.
- Prediction evidence retains detection/subject, classifier model, label-list
  fingerprint, candidate rank, confidence, and canonical taxon identity.
- A model run against multiple label lists contributes at most one independent
  model vote by default; repeated lists cannot outvote another model.
- A source whose label list cannot express a candidate is shown as Not covered
  and is not counted as disagreement or zero support.
- Compatible ancestor/descendant predictions are shown as refinements; only
  incompatible credible taxa count as disagreement.
- Two models that credibly choose incompatible taxa produce Model split and no
  automatic winner; a strict majority may produce a suggestion while retaining
  visible dissent.
- Lower-ranked alternatives from one source are never counted as additional
  species present in a photo.
- A source must declare exclusive or multi-label presence semantics; qualified
  multi-label photo predictions may suggest several species without inventing
  subject assignments.
- Different credible taxa on different subjects are shown as a multi-species
  photo and can be confirmed independently.
- Human adjudication resolves the target without deleting dissenting model
  evidence; resolved disagreements remain available for audit.
- Confirming one species never removes another species assertion unless the
  user explicitly chooses Replace species.
- An encounter is Resolved only when every reviewable subject or photo-level
  evidence target is resolved; confirming a dominant species cannot hide an
  unresolved minority subject.
- Species variation across bursts offers an explicit split but never changes
  grouping automatically.
- Encounter- and burst-level actions name and mutate the complete target;
  matching-only bulk actions are explicitly labeled.
- Mutation endpoints reject stale run or revision targets rather than applying
  them by array index.
- No review action accepts or returns the complete canonical review payload.
- No encounter is split across pages or virtualized windows.
- The 15,000-photo performance requirements are covered by an automated
  fixture and benchmark.
- Existing supported triage, inspection, species, and Group Review workflows
  remain correct for small results, and Rapid Review observes the same
  canonical revisions without a private cache.

## Testing

### Backend

- Any photo and Every photo truth tables, including mixed collection/status
  criteria.
- Collection intersection with partial canonical process coverage.
- Complete encounter and burst membership in every returned page.
- Cursor stability across decision-only mutations and invalidation after
  structural revisions.
- Stable-ID targeting for every mutation type.
- Stale run and stale revision conflict responses.
- Delta correctness for single-species and multi-species confirmation.
- Membership, derived-summary, provenance, revision, and undo truth tables for
  Split burst, Extract photos, Merge adjacent bursts, Split encounter,
  Separate burst as encounter, and Merge encounters.
- Empty-side, all-photo extraction, sole-burst separation, nonadjacent merge,
  missing-timestamp ordering, protected-photo, and stale-target validation.
- Source inventory snapshotting and exclusion of stale label fingerprints.
- Model/list-pair coverage states: agrees, disagrees, not covered, uncertain,
  and unavailable.
- Same-model overlapping-list agreement and disagreement, ensuring the model
  contributes no more than one independent vote.
- Cross-model unanimous, strict-majority, two-way split, single-model, and
  all-abstain consensus cases.
- Exclusive top-k versus multi-label presence behavior, including calibrated
  negative evidence and a multi-label result with several qualified taxa.
- Canonical-taxon alias agreement, compatible ancestor refinement, and
  incompatible branch conflict.
- Multi-subject same-species, multi-subject multi-species, photo-level-only,
  and duplicate-detection cases.
- Positive add versus explicit replacement semantics for photos already
  carrying one or more confirmed species.
- Resolution rollups from subject to photo, burst, and encounter.
- Transactional undo and redo of structural and nonstructural changes.
- Existing collection-scoped regrouping tests remain green for non-review
  consumers.

### Browser integration

- Switching collections and match modes does not call live regrouping.
- Matching decoration and the three distinct counts are correct.
- All review actions work in a collection-filtered view.
- Any photo shows context photos; Every photo drops partially matching
  encounters.
- A mutation that removes the current encounter from the view preserves scroll
  position and moves focus predictably.
- Infinite loading and back-scrolling preserve complete encounters and current
  state.
- Keyboard shortcuts target stable visible entities after sorting and paging.
- Page-level and Group Review shortcuts have identical target semantics and do
  not fire from text inputs or assistive controls.
- No scoped-view warning or disabled review action remains.
- Structural previews show the correct before/after membership, and Split
  burst, Extract, Separate, and Merge lead to observably different results.
- Group Review close warns about staged changes, Apply enumerates selected
  sections, failure is retryable, and a successful apply preserves focus.
- Sidecar preview groups accurate before/after changes, filters write targets,
  reports progress, retains failed entries, and keeps Vireo decisions when an
  export entry is suppressed.
- The species evidence matrix labels both model and label list, exposes
  alternatives on demand, and differentiates disagreement from missing
  coverage.
- Resolving a model split removes it from the active conflict queue while
  preserving a visible, auditable Resolved disagreement state.
- Multi-subject photos expose independently selectable subject boxes and retain
  several confirmed species on the same photo.
- Model split, Label-list disagreement, Multiple species, Group variation, and
  Insufficient evidence badges route to the relevant evidence.

### Performance

- Seed at least 15,000 photos across more than 1,000 encounters.
- Assert bounded first-page response size and DOM card count.
- Assert a species confirmation returns only changed entities.
- Assert filter changes do not trigger a full-result transfer or deep clone.
- Assert Group Review shell/thumbnail latency and 60-frame-per-second keyboard
  lane movement with a representative large burst.
- Record first useful content, view-change, and mutation timings.

## Open implementation decisions

- Whether mutable structural overrides live in normalized database tables or a
  compact revisioned overlay.
- Whether loaded encounter pages use DOM virtualization or bounded page
  eviction; either is acceptable if accessibility and scroll restoration meet
  this design.
- Whether scoring changes can be expressed as per-photo deltas cheaply enough
  for live sliders or require an explicit Apply action.
- The exact first-page encounter/photo budgets after measuring representative
  small, medium, and unusually large encounters.
- Where immutable label-list taxon coverage and taxonomy-version mappings are
  stored and how they are indexed for comparison queries.
- How confidence, margin, and calibrated negative thresholds are versioned for
  each prediction source so an older review remains reproducible.
- Which detector identity, overlap, and embedding rules reconcile duplicate
  detections before subject-level evidence is aggregated.
- Which advanced Process configurations may override default model
  independence groups or source weights, and what validation prevents one
  underlying model from being accidentally counted several times.
