"""Attach derived per-photo fields to API photo dicts, in place.

These enrichers are shared by Browse, Highlights, Misses, Predictions and
the Process Review payloads. Each takes the request ``Database`` and a list
of photo-like dicts (``id`` or ``photo_id``) and batches its lookups so a
page of results costs a handful of queries, not one per photo.
"""


def attach_species(db, photo_dicts):
    """Attach species keyword names to a list of photo dicts (in-place)."""
    if not photo_dicts:
        return photo_dicts
    ids = [p["id"] for p in photo_dicts]
    species_map = db.get_species_keywords_for_photos(ids)
    for p in photo_dicts:
        p["species"] = species_map.get(p["id"], [])
    return photo_dicts

def attach_location_statuses(db, photo_dicts):
    """Attach the effective coordinate source used by Browse and Map UI."""
    if not photo_dicts:
        return photo_dicts
    ids = [p["id"] for p in photo_dicts if isinstance(p.get("id"), int)]
    statuses = db.get_photo_location_statuses(ids)
    for photo in photo_dicts:
        photo["location_status"] = statuses.get(photo.get("id"), "none")
    return photo_dicts

def attach_species_representatives(db, photo_dicts):
    """Attach species representative state to photo dicts (in-place)."""
    if not photo_dicts:
        return photo_dicts
    ids = []
    for p in photo_dicts:
        pid = p.get("photo_id", p.get("id"))
        if isinstance(pid, int) and not isinstance(pid, bool):
            ids.append(pid)
    species_map = db.get_species_keywords_for_photos(ids)
    # Gate representatives on current DB eligibility so a stale preference
    # row (photo later rejected, folder removed from workspace, or species
    # keyword untagged) doesn't light up a Representative badge on views
    # whose photo dicts lack the `flag` column (notably /api/predictions,
    # whose SELECT only pulls filename/timestamp from photos). The
    # in-loop `p.get("flag") == "rejected"` shortcut still short-circuits
    # rejected photos for views that DO include flag, so this only shifts
    # behavior for the payloads that were previously reading raw prefs.
    representatives = db.get_species_representatives(eligible_only=True)
    for p in photo_dicts:
        pid = p.get("photo_id", p.get("id"))
        if p.get("flag") == "rejected":
            species = []
        else:
            species = species_map.get(pid, [])
        entries = [
            {
                "species": s,
                "is_current_photo": representatives.get(s) == pid,
                "is_species_representative": representatives.get(s) == pid,
            }
            for s in species
        ]
        p["life_list"] = entries
        p["species_representatives"] = entries
        p["is_species_representative"] = any(
            entry["is_species_representative"] for entry in entries
        )
    return photo_dicts

def attach_detections(db, photo_dicts):
    """Attach detection bounding boxes to a list of photo dicts (in-place).

    Each photo gets a `detections` list of {x, y, w, h, confidence,
    category} dicts, ordered by confidence DESC. Photos with no
    detections get an empty list.
    """
    if not photo_dicts:
        return photo_dicts
    ids = [p["id"] for p in photo_dicts]
    det_map = db.get_detections_for_photos(ids)
    for p in photo_dicts:
        p["detections"] = det_map.get(p["id"], [])
    return photo_dicts

def attach_edit_recipes(db, photo_dicts):
    """Attach non-destructive edit recipes to photo dicts (in-place)."""
    if not photo_dicts:
        return photo_dicts
    ids = [p["id"] for p in photo_dicts]
    recipe_map = db.get_photo_edit_recipes(ids)
    for p in photo_dicts:
        p["edit_recipe"] = recipe_map.get(p["id"])
    return photo_dicts

def attach_nested_edit_recipes(db, payload):
    """Attach edit recipes to nested photo-like dicts in an API payload."""
    refs = []

    def visit(value):
        if isinstance(value, dict):
            pid = value.get("photo_id", value.get("id"))
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and "filename" in value
            ):
                refs.append((value, pid))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if not refs:
        return payload
    recipe_map = db.get_photo_edit_recipes(sorted({pid for _, pid in refs}))
    for photo, pid in refs:
        photo["edit_recipe"] = recipe_map.get(pid)
    attach_species_representatives(db, [photo for photo, _pid in refs])
    return payload
