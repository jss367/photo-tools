"""Edit cached Process Review results (encounters / bursts) in place.

The review page lets the user detach a burst or photo and confirm species
without re-running the pipeline; these helpers keep the cached result
structure (photo ids, counts, time ranges, species labels) consistent
after such edits. Pure functions over the results dict.
"""


def compute_time_range(photos_by_id, photo_ids):
    """Return [min_ts, max_ts] ISO strings for photo_ids, or [None, None]."""
    timestamps = [
        photos_by_id[pid]["timestamp"]
        for pid in photo_ids
        if pid in photos_by_id and photos_by_id[pid].get("timestamp")
    ]
    if not timestamps:
        return [None, None]
    return [min(timestamps), max(timestamps)]


def rebuild_encounter_species_label(results, photo_ids, fallback=None):
    """Return the encounter-level species label for exactly photo_ids."""
    from encounters import encounter_species_label

    id_set = set(photo_ids or [])
    photos = [p for p in results.get("photos", []) if p.get("id") in id_set]
    name, confidence = encounter_species_label(photos)
    if name is None and fallback is not None:
        return list(fallback)
    return [name, confidence]


def candidate_species_override(species_label):
    """Convert a derived species label into an unconfirmed burst candidate."""
    species = species_label[0] if species_label else None
    if not species:
        return None
    return {"species": species, "confirmed": False}


def find_merge_target(encounters, detached_range, target_species):
    """Find an encounter index whose confirmed species matches target_species and
    whose time range is adjacent to detached_range (no other encounter sits in
    the gap between them). Returns None if none found.
    """
    d_min, d_max = detached_range
    if d_min is None or d_max is None:
        return None

    other_ranges = []
    for i, e in enumerate(encounters):
        tr = e.get("time_range") or [None, None]
        if tr[0] is not None and tr[1] is not None:
            other_ranges.append((i, tr[0], tr[1]))

    for i, e in enumerate(encounters):
        if not e.get("species_confirmed"):
            continue
        if e.get("confirmed_species") != target_species:
            continue
        tr = e.get("time_range") or [None, None]
        if tr[0] is None or tr[1] is None:
            continue
        c_min, c_max = tr[0], tr[1]
        if c_max < d_min:
            gap_start, gap_end = c_max, d_min
        elif d_max < c_min:
            gap_start, gap_end = d_max, c_min
        else:
            return i  # overlapping — treat as adjacent
        intervening = False
        for j, o_min, o_max in other_ranges:
            if j == i:
                continue
            if o_max > gap_start and o_min < gap_end:
                intervening = True
                break
        if not intervening:
            return i
    return None


def auto_detach_burst_for_species(results, enc_idx, burst_idx, new_species):
    """Detach the burst at (enc_idx, burst_idx) from its encounter. If an adjacent
    encounter already has new_species as its confirmed species, merge the burst
    into that encounter; otherwise create a new single-burst encounter with
    new_species confirmed. Mutates results in place.
    """
    from pipeline import rebuild_species_predictions

    encounters = results["encounters"]
    enc = encounters[enc_idx]
    bursts = enc["bursts"]
    detached = bursts.pop(burst_idx)
    detached_ids = detached["photo_ids"]

    photos_by_id = {p["id"]: p for p in results.get("photos", [])}
    detached_range = compute_time_range(photos_by_id, detached_ids)

    if len(bursts) == 0:
        encounters.pop(enc_idx)
    else:
        remaining = [pid for pid in enc["photo_ids"] if pid not in set(detached_ids)]
        enc["photo_ids"] = remaining
        enc["photo_count"] = len(remaining)
        enc["burst_count"] = len(bursts)
        enc["species_predictions"] = rebuild_species_predictions(results, remaining)
        enc["species"] = rebuild_encounter_species_label(
            results, remaining, fallback=enc.get("species")
        )
        for b in bursts:
            b["species_predictions"] = rebuild_species_predictions(results, b["photo_ids"])
        enc["time_range"] = compute_time_range(photos_by_id, remaining)
        # Pair indices in trace reference the original composition; drop it
        # so the algorithm-trace panel renders an honest "needs recompute"
        # state instead of stale rows.
        enc.pop("trace", None)

    detached["species_predictions"] = rebuild_species_predictions(results, detached_ids)

    merge_idx = find_merge_target(encounters, detached_range, new_species)
    if merge_idx is not None:
        target = encounters[merge_idx]
        target["bursts"].append(detached)
        target["photo_ids"] = list(target["photo_ids"]) + list(detached_ids)
        target["photo_count"] = len(target["photo_ids"])
        target["burst_count"] = len(target["bursts"])
        target["species_predictions"] = rebuild_species_predictions(
            results, target["photo_ids"]
        )
        target["species"] = rebuild_encounter_species_label(
            results, target["photo_ids"], fallback=target.get("species")
        )
        # Same reason as above — target's trace no longer matches its photo set.
        target.pop("trace", None)
        t_min, t_max = target.get("time_range") or [None, None]
        d_min, d_max = detached_range
        mins = [x for x in (t_min, d_min) if x is not None]
        maxs = [x for x in (t_max, d_max) if x is not None]
        target["time_range"] = [
            min(mins) if mins else None,
            max(maxs) if maxs else None,
        ]
    else:
        detached_species = rebuild_encounter_species_label(
            results, detached_ids, fallback=enc.get("species")
        )
        encounters.append({
            "species": detached_species,
            "confirmed_species": new_species,
            "species_predictions": detached["species_predictions"],
            "species_confirmed": True,
            "photo_count": len(detached_ids),
            "burst_count": 1,
            "time_range": detached_range,
            "photo_ids": list(detached_ids),
            "bursts": [detached],
        })

    summary = results.setdefault("summary", {})
    summary["encounter_count"] = len(encounters)
    summary["burst_count"] = sum(e.get("burst_count", 0) for e in encounters)
