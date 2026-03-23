# Photo Culling Pipeline Design

A hierarchical, subject-aware culling system for wildlife photography. Rather than one global clustering pass or one global "beauty" score, the pipeline isolates the bird, scores subject quality — not frame quality — and groups photos into encounters and bursts before selecting diverse winners.

## Pipeline Overview

```
Import
  → Feature Extraction         (per-image: detection, masking, embeddings, quality metrics)
  → Encounter Segmentation     (time-constrained cut + merge into subject encounters)
  → Burst Clustering           (tight near-duplicate groups within each encounter)
  → Subject-Aware Scoring      (subject sharpness, occlusion, exposure, composition)
  → Diverse Selection (MMR)    (quality × diversity within bursts and encounters)
  → Triage                     (keep / review / reject)
```

## Grouping Hierarchy

Three levels, because "same encounter?" and "best frame?" are different problems.

| Level          | Definition                                                                 | Example                                        |
|----------------|----------------------------------------------------------------------------|-------------------------------------------------|
| **Encounter**  | Contiguous run of images of the same subject in the same situation         | One warbler in one patch of branches over 90s   |
| **Burst**      | Tight subset within an encounter: rapid-fire near-duplicates / pose variants | 5 raptor frames in 1.2 seconds                  |
| **Winner set** | The 1-N frames kept from a burst or encounter                              | The sharpest eye-contact frame + one wing-spread |

---

## Stage 1: Feature Extraction

Compute and cache the following for every imported image. All downstream stages depend on these.

### 1.1 Metadata

- Timestamp (with sub-second precision)
- GPS coordinates (if present)
- Focal length, aperture, shutter speed, ISO
- Camera orientation
- Burst ID / sequence ID (if written by camera) — **privileged feature**, not optional decoration
- AF point / focus distance (if available) — **privileged feature**

### 1.2 Subject Detection and Mask

**v1 approach:** Use **MegaDetector** (already integrated) for initial bounding box detection. Refine the best box with **SAM2** (default: SAM2-Small, configurable via `pipeline.sam2_variant`) to get a pixel-level mask `M_i`. This avoids replacing a proven detector while gaining pixel-level masks.

**Future:** Evaluate Grounding DINO as a MegaDetector replacement for open-vocabulary detection with text prompts (e.g., `bird, raptor, hawk, eagle, owl, falcon`). The interface is designed so the detector is swappable.

The pixel mask is essential — downstream scoring (subject vs. background sharpness, occlusion, exposure on the bird not the frame) all depend on it.

**Working image:** Load the raw/source image at a working resolution of **longest edge 1536px** (`render_proxy`). This is large enough for SAM2 (which internally resizes to 1024×1024) and for quality feature computation. Each model performs its own final resize from this proxy (DINOv2 → 518×518, BioCLIP → model-specific).

From the mask, produce:
- **Subject crop**: bounding box with 10-15% margin
- **Masked crop**: pixels outside the bird mask neutralized via **heavy Gaussian blur** (radius 51px). Blur preserves rough color context while eliminating background detail, avoiding the artificial edges that black-fill or mean-color-fill would create in pHash and embedding computation.
- If the bird is small in frame, run detection on tiles or an upscaled proxy as well

**Storage:** Masks are saved as single-channel PNGs in `~/.vireo/masks/{photo_id}.png`.

### 1.3 Embeddings

| Embedding | Model | Input | Dimensions | Purpose |
|-----------|-------|-------|------------|---------|
| `s_i` | DINOv2 ViT-B/14 | subject crop (518×518) | 768 | **Primary semantic grouping feature** |
| `c_i` | BioCLIP | subject crop | model-dependent | Species classification + text-aligned semantics |
| `g_i` | DINOv2 ViT-B/14 | full working image (518×518) | 768 | Secondary scene-level context |

**DINOv2 variant** defaults to **ViT-B/14** (768-dim embeddings). Configurable via `pipeline.dinov2_variant` — users with powerful GPUs can use ViT-L/14 (1024-dim), users on CPU can drop to ViT-S/14 (384-dim). ViT-B/14 is the best balance of embedding quality and speed for fine-grained bird similarity.

`s_i` is the main grouping feature. DINOv2 is a stronger visual feature extractor for fine-grained similarity than CLIP (self-supervised training preserves visual structure better than text-image alignment).

`c_i` uses the existing **BioCLIP** integration rather than adding a separate CLIP model. BioCLIP already provides text-aligned embeddings and species classification in a single forward pass. This avoids running a redundant model.

`g_i` is secondary context — when the bird is small, the crop may be low-resolution, and the full image captures "same tree / same pond" signals that help grouping. It carries low weight (0.15 in encounter segmentation).

**Storage:** Embeddings are stored as BLOB columns in the photos table (`dino_subject_embedding`, `dino_global_embedding`), matching the existing `embedding` column pattern for BioCLIP.

**Deferred:** Part-aware / keypoint-aligned embedding (`k_i`). Useful for pose-robust grouping but requires a specialized model. Add when the keypoint model (see 1.6) is proven.

### 1.4 Species Classification

Use the existing **BioCLIP** classifier on the subject crop. Keep the **top-5 predictions with probabilities**, not just the top-1 label. This gives a lightweight species similarity signal for encounter grouping without the cost of a full 10,000-species posterior. No new model needed — this reuses the `c_i` forward pass from Section 1.3.

**Deferred:** Location/time prior (`q'_i(species) ∝ q_i(species) * p(species | lat, lon, date)`). Elegant refinement but adds a dependency on external range map data. Not needed for v1.

### 1.5 Perceptual Hashes

| Hash | Input | Purpose |
|------|-------|---------|
| `hF_i` | Full frame | General duplicate detection |
| `hC_i` | Masked subject crop | Subject-specific near-duplicate detection |

pHash is a **duplicate/near-duplicate feature**, not the main semantic cue. It is too brittle for "same bird across changed pose/background" but very effective for "these two frames are basically the same frame." Both hashes are trivially cheap to compute and useful as a fast pre-filter before expensive embedding comparisons.

Rough ranges for 64-bit crop pHash hamming distance (uncertain, crop-normalization dependent):

| Hamming | Interpretation |
|---------|----------------|
| 0-6 | Likely duplicate or near-duplicate |
| 7-12 | Often same burst, slightly different pose |
| 13+ | Often distinct pose or different framing |

### 1.6 Part / Pose Features

**Deferred for v1.** No good off-the-shelf bird keypoint model exists. Training data is available (CUB 15-keypoint, NABirds part annotations) but fine-tuning a model is a real project.

For v1, derive coarse pose signals from the SAM2 mask shape:
- Rough aspect ratio / elongation (flight vs. perched heuristic)
- Crop completeness (is the mask clipped by the frame edge?)
- Subject area fraction

**Future:** Fine-tune a keypoint model to estimate eye(s), beak, head center, wing tips, tail with visibility/confidence. This unlocks head box `H_i`, eye box `E_i`, head yaw, flight direction, wing phase, and proper mode classification (perched close, flight close, flight distant, environmental, silhouette/backlit).

### 1.7 Quality Features

Computed per image at extraction time:

- **Sharpness**: multi-scale Tenengrad on subject mask region and background ring, at original resolution
- **Relative sharpness ratio**: subject Tenengrad / background Tenengrad — detects the "sharp branches, blurry bird" misfocus failure mode
- **Exposure**: highlight and shadow clip fractions computed on the bird mask, not the whole frame
- **Subject area fraction**: mask pixels / frame pixels
- **Noise estimate**: from smooth background regions, or ISO from EXIF as proxy

**Deferred:** Global IQA models (BRISQUE, HyperIQA, NIMA). These estimate generic perceptual quality, not whether the bird itself is the sharp subject. At 8% weight in the composite score they are not worth three additional model passes for v1. Redistribute their weight to subject-aware signals.

### 1.8 Feature Storage

All computed features are stored in the existing `photos` table, extending the current column pattern:

| Feature | Column | Type | Notes |
|---------|--------|------|-------|
| DINOv2 subject embedding | `dino_subject_embedding` | BLOB | float32 array (768-dim for ViT-B/14) |
| DINOv2 global embedding | `dino_global_embedding` | BLOB | float32 array (768-dim for ViT-B/14) |
| Mask path | `mask_path` | TEXT | path to `~/.vireo/masks/{photo_id}.png` |
| Tenengrad (subject) | `subject_tenengrad` | REAL | multi-scale, original resolution |
| Tenengrad (background) | `bg_tenengrad` | REAL | background ring region |
| Crop completeness | `crop_complete` | REAL | 0-1, fraction of mask perimeter within frame |
| Background separation | `bg_separation` | REAL | background variance, normalized per encounter |
| Subject clip high | `subject_clip_high` | REAL | fraction of mask pixels > 250 |
| Subject clip low | `subject_clip_low` | REAL | fraction of mask pixels < 5 |
| Subject median luminance | `subject_y_median` | REAL | median Y of mask region |
| Crop pHash | `phash_crop` | TEXT | 64-bit hex, masked subject crop |

Existing columns (`embedding`, `quality_score`, `sharpness`, `subject_sharpness`, `subject_size`, `detection_box`, `detection_conf`, `phash`) are retained for backward compatibility with the current culling pipeline.

---

## Stage 2: Encounter Segmentation

Two-pass grouping: adjacent-frame segmentation to find boundaries, then neighbor-segment merging to reconnect short pauses. This works better than clustering (e.g., HDBSCAN) because time order is a very strong prior in photographer data.

### 2.1 Pairwise Encounter Similarity

For adjacent photos `i` and `j`, define normalized similarities in [0, 1]:

```
sim_time    = exp(-|t_i - t_j| / τ_enc)                          τ_enc = 40s
sim_subj    = max(0, cosine(s_i, s_j))                           DINOv2 crop
sim_global  = max(0, cosine(g_i, g_j))                           DINOv2 full image
sim_species = Σ sqrt(p_i(s) * p_j(s))  for s in intersection of top-5 species sets
sim_meta    = w_f*exp(-|log(f_i/f_j)| / 0.15) + w_g*exp(-gps_dist / 30m)
```

**`sim_species`** uses the Bhattacharyya coefficient over shared species in both photos' top-5 lists. This handles the "different top-5 sets" case naturally — if the sets don't overlap, similarity is 0. When they share high-confidence species, the geometric mean rewards agreement. Range is naturally [0, 1].

**`sim_meta` GPS fallback:** If both photos have GPS, `w_f = 0.4, w_g = 0.6`. If either lacks GPS, drop the GPS term and use `w_f = 1.0`. This avoids penalizing photos without GPS metadata.

Combined encounter score:

```
S_enc(i,j) = 0.35*sim_time
           + 0.35*sim_subj
           + 0.15*sim_global
           + 0.10*sim_species
           + 0.05*sim_meta
```

**Weight rationale:** Subject crop embedding and time are the anchors. Full-image embedding is secondary context. Species helps when embeddings wobble. Metadata (focal length, GPS) is a tie-breaker.

**Hard rule:** If two photos share a camera burst ID, they are in the same encounter regardless of `S_enc`.

If no subject is detected, renormalize over remaining terms and mark confidence lower.

### 2.2 Pass 1: Cut Timeline into Microsegments

Sort by timestamp. For adjacent pairs `(i, i+1)`, compute `S_enc(i, i+1)`.

Cut rules:
- **Hard cut** if `Δt > 180s`
- **Hard cut** if `S_enc < 0.42`
- **Soft cut** if two of the last three adjacent scores are `< 0.52` (catches gradual scene drift)

All thresholds are initial values subject to empirical calibration.

### 2.3 Pass 2: Merge Neighboring Microsegments

Merge adjacent microsegments `A` and `B` if `S_seg(A, B) > 0.62` and `gap(A, B) < 60s`:

```
S_seg(A,B) = 0.5 * mean(top 3 pairwise S_enc between tail(A) and head(B))
           + 0.2 * cosine(mean_subject_emb(A), mean_subject_emb(B))
           + 0.2 * species_similarity(mean_species(A), mean_species(B))
           + 0.1 * exp(-gap(A,B) / 20s)
```

This handles the common case where a photographer hesitates, re-acquires, or briefly pauses during a single encounter. The boundary comparison (tail of A vs. head of B) is more informative than comparing segment centroids.

All thresholds are initial values subject to empirical calibration.

### 2.4 Encounter-Level Species Label

Aggregate per-photo species classifications into an encounter-level label via confidence-weighted majority vote. Used for display and downstream rarity scoring. No additional model calls needed — this uses the per-photo top-5 predictions from Stage 1.

---

## Stage 3: Burst Clustering

Inside each encounter, group near-duplicates and small pose variants into bursts. Since bursts are almost always contiguous in time, simple sequential cuts are sufficient — no formal clustering algorithm needed.

### 3.1 Burst Boundary Detection

Walk through the encounter in timestamp order. Cut a new burst between adjacent photos `(i, i+1)` if **any** of these fire:

- `Δt > 3s`
- `Hamming(hC_i, hC_j) > 12` (crop pHash — frames are no longer near-identical)
- `cosine(s_i, s_j) < 0.80` (DINOv2 crop — subject changed substantially)

This produces tight, contiguous bursts of rapid-fire frames. All thresholds are initial values subject to empirical calibration.

**Future upgrade:** If edge cases arise (e.g., long continuous sequences where endpoints are very different but each adjacent pair is similar), replace with complete-linkage agglomerative clustering to resist chaining.

---

## Stage 4: Subject-Aware Quality Scoring

The most important stage. A wildlife culler fails if it scores the whole frame instead of the bird.

### 4.1 Subject-Focus Score

Let `T(R)` be multi-scale Tenengrad sharpness on region `R`, computed on the **original-resolution crop** (not the downsampled working image). Normalize within the encounter using **percentile rank** — simpler than sigmoid, naturally maps to [0, 1], no hyperparameters, and robust to outliers by nature.

```
F_i = 0.70*percentile_rank(T(B_i), within=encounter)
    + 0.30*sigmoid(log((T(B_i) + eps) / (T(R_bg) + eps)))
```

Where:
- `B_i` = subject mask region
- `R_bg` = background ring produced by **dilating the mask by 10% of its equivalent diameter**, then subtracting the original mask. This adapts to subject size — a distant bird gets a proportionally similar ring regardless of absolute pixel count. Equivalent diameter = `2 * sqrt(mask_area / π)`.

The background-ratio term is the most valuable signal — it catches the "sharp branches, blurry bird" misfocus failure mode.

**AF metadata bonus** (if available):

```
AF_i = 1.0 if AF point inside subject mask
       0.0 otherwise

F'_i = 0.85*F_i + 0.15*AF_i
```

AF metadata is only present on some cameras/formats, so this is a bonus when available, not a requirement. If no AF data exists, `F'_i = F_i`.

**Future upgrade:** With a keypoint model, replace the single-branch formula with an eye→head→body fallback hierarchy, weighting eye sharpness highest when the eye is visible. Also check AF point against eye/head specifically.

### 4.2 Exposure

Compute on the **bird mask**, not the whole frame:

```
E_i = exp(-6*clip_high_subject - 3*clip_low_subject)
    * exp(-abs(Y_med - 0.45) / 0.30)
```

Where:
- `clip_high_subject` = fraction of subject mask pixels > 250
- `clip_low_subject` = fraction of subject mask pixels < 5
- `Y_med` = median luminance of subject mask

Highlight clipping is penalized ~2x harder than shadow clipping — blown highlights are unrecoverable in post, while shadows can often be lifted. The `Y_med` term penalizes extreme under/overexposure even without hard clipping.

All constants are initial values subject to calibration. White birds and silhouettes may need different calibration.

### 4.3 Composition

```
C_i = 0.55*crop_complete + 0.45*bg_separation
```

Where:
- `crop_complete` = **continuous value [0, 1]**: fraction of the mask perimeter that does NOT touch the frame edge. A bird fully within frame scores 1.0; half the perimeter clipped scores ~0.5. Continuous is better than binary — a single pixel touching the edge shouldn't score the same as half the bird being cut off. The hard reject threshold at 0.60 (Section 4.5) handles the binary cutoff decision.
- `bg_separation` = background pixel variance as a proxy for cleanliness. Low variance = smooth bokeh = better. Crude but captures the real difference between a bird against clean sky vs. lost in a tangle of branches. Normalize within the encounter.

**Deferred:** Flight room (space in front of the bird's gaze direction). Requires head yaw from keypoint model.

### 4.4 Composite Quality Score

```
Q_i = 0.45*F'_i           # subject focus (dominant)
    + 0.20*E_i            # exposure
    + 0.15*C_i            # composition
    + 0.10*area_score     # subject size in frame (mask pixels / frame pixels)
    + 0.10*noise_score    # noise estimate from background regions, or ISO proxy
```

Deliberately subject-focus-heavy. All weights are initial values subject to calibration.

**Deferred for future versions (with keypoint model):**
- Occlusion scoring (branch/leaf obstruction of head vs. body)
- Pose / behavior scoring (eye visibility, head angle, wing spread)
- Mode-specific weight adjustment (perched vs. flight vs. environmental vs. silhouette)
- Global IQA models (BRISQUE, HyperIQA, NIMA) as weak priors

### 4.5 Hard Reject Rules

Hard reject a frame if **any** of these fire:

1. No bird detected (no usable subject mask)
2. `crop_complete < 0.60` (subject severely clipped by frame edge)
3. `F'_i < 0.35` (subject badly out of focus)
4. Subject highlight clipping exceeds threshold (blown-out bird)
5. `Q_i < 0.40` (composite score floor)

All thresholds are configurable and photographer-style dependent (expect +/-0.05 variation).

---

## Stage 5: Diverse Selection (MMR)

Use **Maximal Marginal Relevance** at both burst and encounter level. MMR balances quality against visual diversity, avoiding N near-identical top-scoring shots.

### 5.1 Diversity Distance

```
D_div(i,j) = 0.60*(1 - cosine(s_i, s_j))       # DINOv2 crop embedding
           + 0.40*(1 - sim_hash_c(i,j))         # crop pHash
```

DINOv2 catches semantic differences (different pose, different angle). pHash catches near-identical frames. Together they cover both coarse and fine-grained similarity.

**Deferred:** Pose distance from keypoint model. Would replace/supplement the embedding distance for more precise diversity.

### 5.2 MMR Selection

```
score_add(i | K) = λ * Q_i + (1 - λ) * min_{j in K} D_div(i,j)
```

| Scope                       | λ    | max_keep |
|-----------------------------|------|----------|
| Within burst                | 0.85 | 1-3      |
| Across encounter survivors  | 0.70 | 3-5      |

Higher λ within bursts (favor quality — frames are already very similar). Lower λ across encounter survivors (favor diversity — want different poses/moments).

---

## Stage 6: Triage

Applied in order:

1. **REJECT** — hard reject rules fired (Stage 4.5)
2. **Species rarity protection** — see below
3. **KEEP** — selected by MMR (burst-level, then encounter-level)
4. **REVIEW** — everything else (human decides)

Hard rejects are removed before MMR runs — no point including a hopelessly blurry frame in the candidate pool.

### 6.1 Species Rarity Protection

**Core rule:** A species must always have at least one non-rejected representative in the workspace. Culling should always be relative — a bad photo is only bad if you have a better one of the same species.

After hard rejects fire but before MMR selection, check each species. If every photo of a species would be rejected, promote the best one (highest `Q_i`) to REVIEW instead. This ensures the photographer never loses their only record of a species to automatic culling.

The protection applies at the **species level across the entire workspace**, not per-encounter. If you have 3 robin encounters and encounter #2 is all terrible photos, reject them freely — robins are represented elsewhere. But if encounter #2 is your only barn owl, the best frame survives as REVIEW regardless of its absolute quality.

This interacts with interactive threshold tuning: as the user drags hard-reject thresholds stricter, the species protection visibly kicks in — photos that would otherwise turn red (REJECT) stay amber (REVIEW) with a "only representative of [species]" badge in the UI.

### 6.2 Triage Buckets

| Bucket     | Criteria                                                                          |
|------------|-----------------------------------------------------------------------------------|
| **KEEP**   | Selected by MMR from non-rejected candidates                                      |
| **REJECT** | Hard reject rules fired, AND species has other non-rejected representatives        |
| **REVIEW** | Everything else, including rarity-protected photos — human decides                 |

The REVIEW bucket is important. The system should be aggressive about clearly bad photos (motion blur, misfocus, blown highlights) and confident about clearly great ones, but there is a middle tier where human judgment matters.

---

## End-to-End Pseudocode

```python
photos = sort_by_timestamp(photos)

# Stage 1: Feature extraction
for p in photos:
    p.preview = render_proxy(p.raw, longest_edge=1536)
    p.det = megadetector_detect(p.preview)          # MegaDetector (existing)
    p.mask = sam2_refine(p.preview, p.det.box)      # SAM2-Small → pixel mask
    save_mask(p.mask, f"~/.vireo/masks/{p.id}.png")
    p.crop = crop_subject(p.preview, p.mask, margin=0.15)
    p.crop_masked = gaussian_blur_background(p.preview, p.mask, radius=51)

    p.s = dinov2_embed(p.crop)                      # primary grouping (ViT-B/14)
    p.g = dinov2_embed(p.preview)                   # secondary scene context
    p.species_top5 = bioclip_classify(p.crop, k=5)  # BioCLIP top-5 + embedding

    p.hF = phash(p.preview)                         # full-frame hash
    p.hC = phash(crop_with_blur(p.crop_masked))     # masked crop hash

    p.quality = compute_quality_features(
        image=p.raw,                                # original res for sharpness
        crop=p.crop, mask=p.mask, metadata=p.metadata
    )

# Stage 2: Encounter segmentation
microsegments = cut_on_adjacent_score(photos, S_enc)
encounters = merge_neighbor_segments(microsegments, S_seg)
for enc in encounters:
    enc.species = aggregate_species(enc.photos)     # confidence-weighted majority vote

# Stage 3: Burst detection
for enc in encounters:
    enc.bursts = cut_bursts(enc.photos)             # sequential cuts on time, pHash, embedding

# Stage 4: Quality scoring + Stage 5: Selection + Stage 6: Triage
for enc in encounters:
    for burst in enc.bursts:
        for p in burst.photos:
            p.Q = quality_score(p, burst)           # composite subject-aware score
            if hard_reject(p):
                p.label = "REJECT"

        candidates = [p for p in burst.photos if p.label != "REJECT"]
        burst.keeps = mmr_select(candidates, lam=0.85, max_keep=3)

    enc_candidates = flatten(b.keeps for b in enc.bursts)
    enc.keeps = mmr_select(enc_candidates, lam=0.70, max_keep=5)

    for p in enc.photos:
        if p.label == "REJECT":
            pass                                    # already labeled
        elif p in enc.keeps:
            p.label = "KEEP"
        else:
            p.label = "REVIEW"

# Stage 6b: Species rarity protection
for species in unique_species(photos):
    species_photos = [p for p in photos if p.species == species]
    non_rejected = [p for p in species_photos if p.label != "REJECT"]
    if len(non_rejected) == 0:
        best = max(species_photos, key=lambda p: p.Q)
        best.label = "REVIEW"                       # protect last representative
        best.rarity_protected = True
```

---

## Model Configuration

All model choices are configurable via `~/.vireo/config.json` under the `pipeline` key:

| Setting | Default | Options |
|---------|---------|---------|
| `pipeline.sam2_variant` | `"sam2-small"` | `sam2-tiny`, `sam2-small`, `sam2-base-plus`, `sam2-large` |
| `pipeline.dinov2_variant` | `"vit-b14"` | `vit-s14` (384-dim), `vit-b14` (768-dim), `vit-l14` (1024-dim) |
| `pipeline.proxy_longest_edge` | `1536` | Any integer; working image resolution |

Per-workspace config overrides (existing mechanism) apply to these settings, allowing different workspaces to use different model sizes.

**v1 model stack summary:**

| Role | Model | Status |
|------|-------|--------|
| Detection | MegaDetector v6 | Existing, retained |
| Segmentation | SAM2-Small | **New** |
| Grouping embeddings | DINOv2 ViT-B/14 | **New** |
| Species + text semantics | BioCLIP | Existing, retained |
| Perceptual hashing | imagehash | Existing, retained |

---

## Open Questions

Ordered by decreasing uncertainty:

1. **DINOv2 similarity thresholds for birds.** All similarity ranges are estimates. Same-species birds have high embedding similarity regardless of individual identity, and similar species (e.g., Empidonax flycatchers) may not separate well. Needs calibration on a labeled dataset of bird photography scenes.

2. **Background variance as a composition metric.** Raw pixel variance is a crude proxy for "clean background." Doesn't account for distance, color harmony, or bokeh quality. A VLM could score this better but is expensive per-photo.

3. **All threshold values** (encounter cut at 0.42, merge at 0.62, burst cuts, hard reject thresholds, quality weights) are initial estimates subject to empirical calibration. They should be configurable.

4. **Temporal constants** (τ_enc = 40s, burst gap = 3s, merge window = 60s). Work for active birding but other styles (macro, landscape) have different temporal patterns. Should be calibrated per session or per photographer.

5. **Weight allocations** across scoring components are reasonable defaults but not empirically optimized. The right approach is learning from the photographer's own keep/reject decisions (see Personalization).

---

## Future Upgrades

In priority order, these additions would most improve scoring quality:

1. **Bird keypoint model** (fine-tuned on CUB / NABirds). Unlocks: eye→head→body sharpness fallback hierarchy, head yaw for flight room / look space, wing spread for pose scoring, mode classification (perched / flight / environmental / silhouette), AF point vs. eye/head check, pose-based diversity distance, part-aware embeddings for grouping.

2. **Occlusion scoring.** Detect branches/leaves crossing the bird mask. Requires the keypoint model (head vs. body distinction) and possibly a scene segmentation approach.

3. **Mode-specific scoring weights.** Different weight profiles for perched close, flight close, flight distant, environmental, silhouette/backlit. Requires keypoint-based mode classification.

4. **Global IQA models** (BRISQUE, HyperIQA, NIMA) as weak priors in the composite score.

5. **Species location/time prior** for more stable grouping (`q'_i(species) ∝ q_i(species) * p(species | lat, lon, date)`).

---

## Personalization

The biggest upgrade is not a bigger model — it is learning the photographer's taste.

Keep the feature pipeline above, then train two custom models from historical culls:

1. **Pairwise encounter model:** Label pairs as same/different encounter. Train a calibrated classifier on the pairwise features used in `S_enc`.

2. **Quality ranker:** Use the photographer's keep/reject or star ratings within each burst. Train a ranking model on `F'`, `E`, `C`, area, noise features.

This will outperform any generic aesthetic model quickly, because one photographer may prefer "eye tack sharp even if composition is plain" while another prefers "wings full spread and dramatic light."

---

## UX: Pipeline Page

### Page Structure

The pipeline page (`/pipeline`) is a dedicated full-page view with three zones:

1. **Sidebar (left)** — Pipeline stage list, config controls, and run actions. Always visible.
2. **Main area (center)** — Stage results, photo grid with triage labels, per-photo inspection.
3. **Bottom panel (global)** — When a pipeline extraction job is running, the existing bottom panel shows live progress on any page (photos, collections, etc.), not just the pipeline page.

### Sidebar

The sidebar shows the 6 stages as a vertical step list. Each step shows its status (not started / running / complete / stale) and summary stats (e.g., "47 encounters, 312 bursts"). Clicking a stage filters the main area to show that stage's output.

Three actions, matching three tiers of compute cost:

- **"Extract Features"** — runs stage 1 (detection, masking, embeddings). This is the heavy compute job with model inference. Shows confirmation with estimated time.
- **"Regroup"** — runs stages 2-3 (encounter segmentation, burst clustering) from cached features. Fast (seconds, no model inference), triggered automatically when grouping thresholds change, or manually via button.
- **Reflow is automatic** — stages 4-6 (scoring, selection, triage) recompute instantly whenever a scoring threshold or weight changes. No button needed. Pure arithmetic on stored features, so reflow is milliseconds.

Below the stage list: config controls grouped by stage (see Threshold Tuning below).

### Main Area

Two modes, toggled by what you click:

**Stage summary mode** (default) — Shows the output of the selected stage:

- **Stage 1 (Feature Extraction):** Photo grid with mask overlays. Each thumbnail shows the detected subject outlined. Photos with no detection are flagged. Hovering shows detection confidence.
- **Stage 2 (Encounters):** Photos grouped into encounter cards. Each card shows its species label, photo count, time range. Encounters are laid out chronologically.
- **Stage 3 (Bursts):** Within a selected encounter, bursts shown as tight horizontal strips of thumbnails. Visual separators between bursts.
- **Stages 4-6 (Scoring / Selection / Triage):** The main working view. Photo grid with triage labels (KEEP green, REVIEW amber, REJECT red) overlaid on thumbnails. Sortable by composite score, filterable by label. Quality score bar chart next to each thumbnail showing the F'/E/C/area/noise breakdown at a glance. Rarity-protected photos show a badge ("only [species]").

**Photo inspection mode** — Click any photo to drill into its full feature breakdown:

- Mask overlay on the full image (toggle on/off)
- Quality score waterfall: composite score broken into its 5 weighted components, each shown as a horizontal bar with the raw value and weighted contribution
- Encounter context: which encounter and burst this photo belongs to, with neighboring photos shown as a filmstrip
- Triage reasoning: which rule triggered REJECT (if rejected), its MMR rank within burst/encounter (if kept), or species rarity protection (if protected)
- Raw features: Tenengrad values (subject and background), clip fractions, crop completeness, embedding nearest neighbors within the encounter

Click the photo's encounter or burst label to jump back to stage summary mode filtered to that group.

### Interactive Threshold Tuning

The sidebar config controls are grouped by stage, with sliders and dropdowns:

**Model settings** (require re-extraction — minutes):

- DINOv2 variant dropdown (`vit-s14` / `vit-b14` / `vit-l14`)
- SAM2 variant dropdown (`sam2-tiny` / `sam2-small` / `sam2-base-plus` / `sam2-large`)
- Proxy resolution slider (1024–2048, default 1536)

Changing any model setting marks stage 1 as **stale** (visual indicator on the stage list). Results stay visible but dimmed until the user clicks "Extract Features" again. Stages 2-6 are also stale since they depend on stage 1 outputs.

**Grouping thresholds** (regroup — seconds):

- Encounter cut threshold (default 0.42)
- Encounter merge threshold (default 0.62)
- Burst time gap (default 3s)
- Burst pHash threshold (default 12)
- Burst embedding threshold (default 0.80)

When you adjust a grouping threshold, stages 2-3 automatically re-run from cached features (no model inference, just math on embeddings and timestamps). This takes seconds, not minutes. The encounter/burst structure updates, then stages 4-6 reflow on top.

**Scoring thresholds** (instant reflow — milliseconds):

- Hard reject: crop completeness floor (default 0.60)
- Hard reject: focus floor (default 0.35)
- Hard reject: composite floor (default 0.40)
- Quality weights: 5 sliders for F'/E/C/area/noise constrained to sum to 1.0 (dragging one auto-adjusts the others proportionally)
- MMR λ for burst (default 0.85) and encounter (default 0.70)
- MMR max_keep for burst (default 3) and encounter (default 5)

When you drag any scoring slider, stages 4-6 recompute instantly and the photo grid updates — photos visibly move between KEEP/REVIEW/REJECT in real time. The stage summary stats update too (e.g., "KEEP: 47 → 52, REJECT: 89 → 84"). Species rarity protection kicks in visibly as thresholds get stricter.

A **"Reset to defaults"** button restores all thresholds to their initial values. An **"Undo"** stack lets you step back through recent threshold changes.

All threshold changes save to workspace config overrides, so different workspaces can have different pipeline tuning.

### Bottom Panel Progress

When "Extract Features" is running, the existing bottom panel shows:

- Current stage and substage (e.g., "Stage 1: SAM2 masking — 142/500")
- Per-stage progress bars
- Estimated time remaining
- Expandable log stream (reuses existing SSE infrastructure)

This is visible from any page, so the user can browse photos while extraction runs in the background.
