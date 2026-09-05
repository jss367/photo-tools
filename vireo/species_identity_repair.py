"""Audited, idempotent repair of known legacy common-name enrichment errors."""

import json

from species_identity import COMMON_NAME_CORRECTIONS, resolution_identity


def plan_repairs(conn):
    """Only repair custom BioCLIP labels whose identity has verified evidence.

    Raw labels, confidences, prediction IDs, keywords and all review state are
    preserved. Model-native scientific labels and hybrids are outside this
    repair: a similar common name is not evidence to rewrite their identity.
    """
    changes = []
    for name, evidence in COMMON_NAME_CORRECTIONS.items():
        rows = conn.execute(
            "SELECT id, species, detection_id, classifier_model, labels_fingerprint, scientific_name, "
            "source_taxon_id FROM predictions WHERE species = ? COLLATE NOCASE "
            "AND classifier_model LIKE 'BioCLIP%' AND labels_fingerprint NOT IN ('tol', 'legacy') "
            "AND source_taxon_id IS NULL AND scientific_name IS NOT ?",
            (name, evidence["scientific_name"]),
        ).fetchall()
        for row in rows:
            changes.append({
                **dict(row), "new_scientific_name": evidence["scientific_name"],
                "new_source_taxon_id": evidence["taxon_id"],
                "reason": "verified-common-name-correction:" + name,
            })
    return changes


def apply_repairs(conn, changes):
    """Apply the plan atomically, retaining an in-database before/after audit.

    Optimistic row checks reject a plan if predictions changed after preview.
    The caller owns the transaction; no partial commit can escape here.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS species_identity_repairs (
        id INTEGER PRIMARY KEY, prediction_id INTEGER NOT NULL,
        resolution_identity TEXT NOT NULL, before_json TEXT NOT NULL,
        after_json TEXT NOT NULL, reason TEXT NOT NULL,
        repaired_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    count = 0
    for change in changes:
        after = {
            "scientific_name": change["new_scientific_name"],
            "source_taxon_id": change["new_source_taxon_id"],
        }
        updated = conn.execute(
            "UPDATE predictions SET scientific_name = ?, source_taxon_id = ? "
            "WHERE id = ? AND scientific_name IS ? AND source_taxon_id IS ? "
            "AND species IS ? AND classifier_model IS ? AND labels_fingerprint IS ? AND detection_id IS ?",
            (*after.values(), change["id"], change["scientific_name"], change["source_taxon_id"],
             change["species"], change["classifier_model"], change["labels_fingerprint"], change["detection_id"]),
        )
        if updated.rowcount != 1:
            raise ValueError(f"Prediction {change['id']} changed after the repair was planned")
        conn.execute(
            "INSERT INTO species_identity_repairs "
            "(prediction_id, resolution_identity, before_json, after_json, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (change["id"], resolution_identity(), json.dumps({
                "scientific_name": change["scientific_name"],
                "source_taxon_id": change["source_taxon_id"],
            }), json.dumps(after), change["reason"]),
        )
        # Do not export corrected rows under an old artifact fingerprint.
        conn.execute(
            "UPDATE classifier_runs SET runtime_fingerprint = 'legacy' "
            "WHERE detection_id = ? AND classifier_model = ? AND labels_fingerprint = ?",
            (change["detection_id"], change["classifier_model"], change["labels_fingerprint"]),
        )
        count += 1
    if count:
        conn.execute("UPDATE workspaces SET last_group_fingerprint = NULL")
    return count


def repair_on_upgrade(db):
    marker = "species_identity_repair:" + resolution_identity()
    if db.get_meta(marker) == "1":
        return 0
    with db.conn:
        count = apply_repairs(db.conn, plan_repairs(db.conn))
        db.set_meta(marker, "1", _commit=False)
    return count
