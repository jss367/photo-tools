import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from classifier import Classifier
from classifier_cache import _ordered_labels_identity
from computation_cache import CacheFormatError, _validate_candidate_taxonomy, classifier_runtime_fingerprint
from db import Database
from embedding_cache import canonicalize_labels
from labels import SpeciesLabels, fetch_species_list, load_merged_labels, read_label_file, save_labels
from labels_fingerprint import compute_full_fingerprint
from pipeline import load_photo_features, normalize_cached_species
from species_identity import SpeciesResolver
from species_identity_repair import apply_repairs, plan_repairs
from taxonomy import Taxonomy

RED = {"taxon_id": 18976, "scientific_name": "Amazona viridigenalis",
       "common_name": "Red-crowned Parrot", "rank": "species"}
BROWED = {"taxon_id": 18997, "scientific_name": "Amazona rhodocorytha",
          "common_name": "Red-browed Parrot", "rank": "species"}
LILAC = {"taxon_id": 18993, "scientific_name": "Amazona finschi",
         "common_name": "Lilac-crowned Parrot", "rank": "species"}


@pytest.fixture
def taxonomy(tmp_path):
    entries = []
    for source in (RED, BROWED, LILAC):
        entries.append({**source, "lineage_names": ["Animalia", "Aves", "Amazona", source["scientific_name"]],
                        "lineage_ranks": ["kingdom", "class", "genus", "species"]})
    by_sci = {e["scientific_name"].lower(): e for e in entries}
    by_common = {e["common_name"].lower(): e for e in entries}
    by_common["red-crowned amazon"] = entries[1]  # Actual broken DWCA mapping.
    by_common["lilac-crowned amazon"] = entries[2]
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps({"taxa_by_common": by_common, "taxa_by_scientific": by_sci}))
    return Taxonomy(path)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "photos.db"))
    for entry in (RED, BROWED, LILAC):
        database.conn.execute(
            "INSERT INTO taxa (inat_id, name, common_name, rank) VALUES (?, ?, ?, ?)",
            (entry["taxon_id"], entry["scientific_name"], entry["common_name"], entry["rank"]),
        )
    database.set_meta("common_name_identity_version", "1")
    database.conn.commit()
    yield database
    database.close()


def test_corrects_bad_alias_without_merging_red_browed_species(taxonomy):
    resolver = SpeciesResolver(taxonomy=taxonomy)
    assert resolver.resolve("Red-crowned Amazon").key == resolver.resolve("Red-crowned Parrot").key
    assert taxonomy.relationship("Red-crowned Amazon", "Red-crowned Parrot") == "same"
    assert resolver.resolve("Red-browed Parrot").key != resolver.resolve("Red-crowned Parrot").key
    assert resolver.resolve("Red-crowned Amazon", scientific_name=BROWED["scientific_name"]).taxon_id == 18997
    assert resolver.resolve("Lilac-crowned Amazon").key == resolver.resolve("Lilac-crowned Parrot").key
    assert resolver.resolve("Lilac-crowned × Red-crowned Amazon").taxon_id is None


def test_source_identity_survives_fetch_save_merge_and_classifier(tmp_path, monkeypatch, taxonomy, db):
    monkeypatch.setattr("labels.LABELS_DIR", str(tmp_path / "labels"))
    payload = {"total_results": 1, "results": [{"taxon": {
        "id": RED["taxon_id"], "name": RED["scientific_name"],
        "preferred_common_name": "Red-crowned Amazon", "rank": "species",
    }}]}
    with patch("labels.urllib.request.urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        fetched = fetch_species_list(14, ["birds"])
    path = save_labels("California birds", 14, "California", ["birds"], fetched)
    labels = load_merged_labels([{"labels_file": path}])
    assert labels == ["Red-crowned Amazon"]
    assert labels.identities[labels[0]]["taxon_id"] == 18976
    classifier = Classifier.__new__(Classifier)
    classifier._classes = canonicalize_labels(labels)
    classifier._label_identities = labels.identities
    prediction = classifier._build_custom_results(np.array([0.9]), 0.1)[0]
    assert prediction["species"] == "Red-crowned Amazon"
    assert prediction["taxonomy"]["scientific_name"] == RED["scientific_name"]
    from classify_job import _prediction_taxonomy
    enriched = _prediction_taxonomy(taxonomy, prediction["species"], prediction["taxonomy"])
    assert enriched["taxon_id"] == 18976
    assert enriched["class"] == "Aves"
    assert enriched["scientific_name"] == RED["scientific_name"]
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, prediction["species"], .9, "BioCLIP-2.5",
                      labels_fingerprint=compute_full_fingerprint(labels)[:12], taxonomy=enriched)
    stored = db.get_predictions_for_detection(det, min_classifier_conf=0)[0]
    from classify_job import _cached_prediction_taxonomy
    assert _cached_prediction_taxonomy(stored)["taxon_id"] == 18976
    assert SpeciesResolver(db=db).prediction(stored).key == "taxon:18976"


def test_identity_changes_invalidate_predictions_but_not_text_embeddings():
    first = SpeciesLabels(["Parrot"], {"Parrot": RED})
    second = SpeciesLabels(["Parrot"], {"Parrot": BROWED})
    assert canonicalize_labels(first) == canonicalize_labels(second)
    assert compute_full_fingerprint(first) != compute_full_fingerprint(second)
    assert _ordered_labels_identity(first) != _ordered_labels_identity(second)
    assert compute_full_fingerprint(SpeciesLabels(["Parrot"])) == compute_full_fingerprint(["Parrot"])


def test_edited_prompt_does_not_reuse_stale_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("labels.LABELS_DIR", str(tmp_path))
    path = save_labels("Birds", 14, "CA", ["birds"], SpeciesLabels(["Parrot"], {"Parrot": RED}))
    with open(path, "w") as f:
        f.write("Other parrot\n")
    assert read_label_file(path).identities == {}


def test_conflicting_label_sources_are_not_silently_merged(tmp_path, monkeypatch):
    monkeypatch.setattr("labels.LABELS_DIR", str(tmp_path))
    a = save_labels("A", 14, "CA", ["birds"], SpeciesLabels(["Parrot"], {"Parrot": RED}))
    b = save_labels("B", 14, "CA", ["birds"], SpeciesLabels(["Parrot"], {"Parrot": BROWED}))
    labels = load_merged_labels([{"labels_file": a}, {"labels_file": b}])
    assert labels.identities["Parrot"] == {"ambiguous": True}
    with pytest.raises(ValueError, match="multiple taxa"):
        Classifier(labels)


def test_merged_source_synonyms_do_not_split_softmax_probability(tmp_path, monkeypatch):
    monkeypatch.setattr("labels.LABELS_DIR", str(tmp_path))
    a = save_labels("A", 14, "CA", ["birds"], SpeciesLabels(["Red-crowned Amazon"], {"Red-crowned Amazon": RED}))
    b = save_labels("B", 14, "CA", ["birds"], SpeciesLabels(["Red-crowned Parrot"], {"Red-crowned Parrot": RED}))
    first = load_merged_labels([{"labels_file": a}, {"labels_file": b}])
    second = load_merged_labels([{"labels_file": b}, {"labels_file": a}])
    assert first == second == ["Red-crowned Amazon"]
    assert compute_full_fingerprint(first) == compute_full_fingerprint(second)


def test_source_taxon_id_survives_name_changes(taxonomy):
    identity = SpeciesResolver(taxonomy=taxonomy).resolve(
        "Old name", source={"taxon_id": 18976, "scientific_name": "Oldgenus viridigenalis"},
    )
    assert identity.scientific_name == RED["scientific_name"]
    assert identity.display_name == "Red-crowned Parrot"


def _photo(db, tmp_path, name="photo.jpg"):
    folder = db.add_folder(str(tmp_path), name="Photos")
    pid = db.add_photo(folder, name, ".jpg", 1000, 1.0, timestamp="2026-09-01T10:00:00")
    det = db.save_detections(pid, [{"box": {"x": .1, "y": .1, "w": .5, "h": .5},
                                    "confidence": .99}], detector_model="megadetector-v6")[0]
    return pid, det


def test_repair_preserves_review_state_and_is_idempotent(db, tmp_path):
    pid, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .91, "BioCLIP-2.5", status="accepted",
                      labels_fingerprint="abcdef123456", taxonomy={"scientific_name": BROWED["scientific_name"]})
    db.add_prediction(det, "Red-browed Parrot", .8, "iNat21", labels_fingerprint="tol",
                      taxonomy={"scientific_name": BROWED["scientific_name"]})
    before = [tuple(r) for r in db.conn.execute("SELECT * FROM prediction_review")]
    plan = plan_repairs(db.conn)
    assert len(plan) == 1
    with db.conn:
        assert apply_repairs(db.conn, plan) == 1
    row = db.conn.execute("SELECT * FROM predictions WHERE id = ?", (plan[0]["id"],)).fetchone()
    assert row["species"] == "Red-crowned Amazon"
    assert row["confidence"] == .91
    assert row["scientific_name"] == RED["scientific_name"]
    assert row["source_taxon_id"] == 18976
    assert [tuple(r) for r in db.conn.execute("SELECT * FROM prediction_review")] == before
    assert plan_repairs(db.conn) == []
    audit = db.conn.execute("SELECT * FROM species_identity_repairs").fetchone()
    assert json.loads(audit["before_json"])["scientific_name"] == BROWED["scientific_name"]


def test_stale_repair_plan_rolls_back(db, tmp_path):
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .9, "BioCLIP-2.5", labels_fingerprint="custom",
                      taxonomy={"scientific_name": BROWED["scientific_name"]})
    plan = plan_repairs(db.conn)
    db.conn.execute("UPDATE predictions SET scientific_name = 'changed'")
    db.conn.commit()
    with pytest.raises(ValueError, match="changed after"), db.conn:
        apply_repairs(db.conn, plan)
    assert db.conn.execute("SELECT scientific_name FROM predictions").fetchone()[0] == "changed"


def test_upgrade_repairs_existing_database_once(db, tmp_path):
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .9, "BioCLIP-2.5", status="rejected",
                      labels_fingerprint="custom", taxonomy={"scientific_name": BROWED["scientific_name"]})
    from species_identity import resolution_identity
    db.set_meta("species_identity_repair:" + resolution_identity(), "0")
    path = db._db_path
    db.close()
    for _ in range(2):
        upgraded = Database(path)
        try:
            assert upgraded.conn.execute("SELECT scientific_name FROM predictions").fetchone()[0] == RED["scientific_name"]
            assert upgraded.conn.execute("SELECT status FROM prediction_review").fetchone()[0] == "rejected"
            assert upgraded.conn.execute("SELECT count(*) FROM species_identity_repairs").fetchone()[0] == 1
        finally:
            upgraded.close()


@pytest.mark.parametrize("same_species", [True, False])
def test_burst_grouping_uses_identity_and_keeps_raw_labels(db, tmp_path, taxonomy, same_species):
    from datetime import datetime

    from classify_job import _store_grouped_predictions
    raw = []
    for i, source in enumerate([RED, RED if same_species else BROWED]):
        pid, det = _photo(db, tmp_path, f"photo-{i}.jpg")
        name = ["Red-crowned Amazon", "Red-crowned Parrot"][i] if same_species else "Parrot"
        raw.append({"photo": {"id": pid, "filename": f"photo-{i}.jpg"},
                    "folder_path": str(tmp_path), "detection_id": det,
                    "prediction": name, "confidence": .9, "alternatives": [],
                    "taxonomy": {"taxon_id": source["taxon_id"], "scientific_name": source["scientific_name"]},
                    "timestamp": datetime(2026, 9, 1, 10, 0, i)})
    _store_grouped_predictions(raw, "test-job", "BioCLIP-2.5", 10, .99, taxonomy, db, "custom")
    rows = db.get_predictions()
    assert len(rows) == 2
    assert {r["species"] for r in rows} == {r["prediction"] for r in raw}
    if same_species:
        assert rows[0]["group_id"] and rows[0]["group_id"] == rows[1]["group_id"]
        assert all(r["vote_count"] == 2 for r in rows)
    else:
        assert all(r["group_id"] is None for r in rows)


def test_pipeline_agrees_across_models_and_preserves_native_identity(db, tmp_path):
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .92, "BioCLIP-2.5", labels_fingerprint="custom",
                      taxonomy={"scientific_name": RED["scientific_name"], "taxon_id": 18976})
    db.add_prediction(det, "Red-crowned Parrot", .9, "iNat21", labels_fingerprint="tol",
                      taxonomy={"scientific_name": RED["scientific_name"]})
    photos = load_photo_features(db)
    assert len(photos) == 1
    assert {p[0] for p in photos[0]["species_top5"]} == {"Red-crowned Parrot"}
    assert db._lookup_taxon_id_for_keyword("Red-crowned Amazon", species_only=True) is not None


def test_cached_names_refresh_without_rewriting_confirmations(taxonomy):
    data = {"photos": [{"id": 1, "species_top5": [["Red-crowned Amazon", .9, "BioCLIP"]],
                        "confirmed_species": "My chosen keyword"}],
            "encounters": [{"photo_ids": [1], "species": ["Red-crowned Amazon", .9],
                            "confirmed_species": "My chosen keyword", "bursts": []}]}
    normalize_cached_species(data, SpeciesResolver(taxonomy=taxonomy))
    assert data["encounters"][0]["species"][0] == "Red-crowned Parrot"
    assert data["photos"][0]["confirmed_species"] == "My chosen keyword"
    assert data["encounters"][0]["confirmed_species"] == "My chosen keyword"


def test_process_review_does_not_flag_confirmed_synonym_as_conflict(taxonomy):
    from pipeline import attach_species_identities

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the review comparison")
    data = {"photos": [{"id": 1, "confirmed_species": "Red-crowned Amazon",
                        "species_top5": [["Red-crowned Parrot", .99, "BioCLIP"]]}]}
    attach_species_identities(data, SpeciesResolver(taxonomy=taxonomy))
    html = (Path(__file__).parents[1] / "templates/pipeline_review.html").read_text()
    start = html.index("var SPECIES_CONFLICT_THRESHOLDS")
    end = html.index("function buildSpeciesConflictEvidence", start)
    source = ("var pipelineResults = " + json.dumps(data) + ";\n" + html[start:end]
              + "\nprocess.stdout.write(JSON.stringify(analyzePhotoSpeciesConflict("
                "pipelineResults.photos[0], 'Red-crowned Amazon')));")
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, check=True, timeout=15)
    evidence = json.loads(result.stdout)
    assert evidence["severity"] is None
    assert evidence["expectedSupport"] == .99


def test_resolution_policy_is_part_of_portable_runtime(monkeypatch):
    args = ({"model": "test"}, "a" * 64, "b" * 64)
    before = classifier_runtime_fingerprint(*args)
    monkeypatch.setattr("species_identity.RESOLUTION_VERSION", "next-policy")
    assert classifier_runtime_fingerprint(*args) != before


@pytest.mark.parametrize("value", [True, -1, "18976", [], {}])
def test_portable_source_id_validation(value):
    with pytest.raises(CacheFormatError):
        _validate_candidate_taxonomy({"taxon_id": value})


def test_taxonomy_ambiguous_common_name_stays_unresolved(tmp_path):
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps({"taxa_by_common": {"parrot": RED},
                                "taxa_by_scientific": {RED["scientific_name"].lower(): RED},
                                "ambiguous_common_names": ["parrot"]}))
    tax = Taxonomy(path)
    assert tax.lookup("Parrot") is None
    assert tax.lookup(RED["scientific_name"])["taxon_id"] == 18976


def test_legacy_dwca_requires_refresh_before_common_name_inference(tmp_path):
    path = tmp_path / "taxonomy.json"
    payload = {"source": "iNaturalist DWCA", "taxa_by_common": {"parrot": LILAC},
               "taxa_by_scientific": {LILAC["scientific_name"].lower(): LILAC}}
    path.write_text(json.dumps(payload))
    legacy = Taxonomy(path)
    assert legacy.lookup("parrot") is None
    assert legacy.lookup(LILAC["scientific_name"])["taxon_id"] == LILAC["taxon_id"]
    assert legacy.lookup_id(LILAC["taxon_id"])["scientific_name"] == LILAC["scientific_name"]
    # Saving an API miss must not make an old index look verified.
    legacy._dirty = True
    legacy.save()
    assert Taxonomy(path).lookup("parrot") is None
    assert "parrot" in json.loads(path.read_text())["ambiguous_common_names"]
    payload.update(common_name_identity_version=1, ambiguous_common_names=[])
    path.write_text(json.dumps(payload))
    assert Taxonomy(path).lookup("parrot")["taxon_id"] == LILAC["taxon_id"]


def test_unversioned_database_does_not_trust_discarded_alias_collisions(db):
    db.conn.execute("INSERT INTO taxa_common_names (taxon_id, name, locale) "
                    "SELECT id, 'Parrot', 'en' FROM taxa WHERE inat_id = ?", (LILAC["taxon_id"],))
    db.set_meta("common_name_identity_version", "")
    resolver = SpeciesResolver(db=db)
    assert resolver.resolve("Parrot").key == "name:parrot"
    assert resolver.resolve(LILAC["scientific_name"]).taxon_id == LILAC["taxon_id"]
    assert resolver.resolve("Red-crowned Amazon").taxon_id == 18976


def test_import_preserves_ambiguity_even_when_only_one_preferred_name_matches(db, tmp_path):
    from taxonomy import populate_taxa_db_from_json

    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps({"common_name_identity_version": 1, "ambiguous_common_names": ["lilac-crowned parrot"],
                                "taxa_by_common": {}, "taxa_by_scientific": {LILAC["scientific_name"].lower(): LILAC}}))
    populate_taxa_db_from_json(db, path)
    assert SpeciesResolver(db=db).resolve("Lilac-crowned Parrot").taxon_id is None
    assert SpeciesResolver(db=db).resolve(LILAC["scientific_name"]).taxon_id == LILAC["taxon_id"]
