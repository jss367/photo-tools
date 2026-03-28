# Pipeline Burst Group Review

## Summary

Add the burst group review mode (GRM) to the pipeline review page, so users can view all photos in a burst simultaneously with zoom/pan sync, while also seeing full pipeline metadata.

## Trigger & Routing

- Click a photo card in pipeline review:
  - Burst has **2+ photos** → open GRM overlay with all burst photos
  - Burst has **1 photo** → open existing inspect panel (unchanged)
- The clicked photo is pre-selected in the GRM loupe.

## GRM Layout

Reuses the same structure as the review page GRM:

- **Left — Strips panel**: Three zones (PICKS / CANDIDATES / REJECTS). All burst photos start in CANDIDATES. Compact cards: thumbnail, species, confidence, Q score, sharpness, AI BEST badge.
- **Right — Loupe panel**: Large preview photo with crosshairs, zoom/pan sync (1-12x), click-to-lock.
- **Below loupe photo — Pipeline metadata** for selected photo:
  - Triage label (KEEP/REVIEW/REJECT)
  - Reject reasons
  - Species predictions per model with confidences
  - Score waterfall (Focus/Exposure/Composition/Area/Noise with weights)
  - Raw features table (subject tenengrad, bg tenengrad, crop completeness, bg separation, highlight/shadow clip, median luminance, subject size, detection confidence)
- **Footer**: Species input, remove from group, hint text, Apply & Close.

## Implementation Approach

1. Add GRM markup, CSS, and JS directly to `pipeline_review.html` (inline, matching existing template pattern).
2. Modify `openInspect()` to check burst size — route to GRM for 2+ photos, keep inspect panel for single photos.
3. Extend GRM loupe info area to render full pipeline metadata when opened from pipeline review.
4. Wire Apply & Close to save pick/reject decisions back to `pipelineResults` state and re-render encounter cards.
5. No new API endpoints — read from existing `pipelineResults.photos` local state.

## Trade-offs

- Code duplication between `review.html` and `pipeline_review.html` GRM. Acceptable because extracting shared partials for inline JS/CSS would be a larger refactor. Can consolidate later.
