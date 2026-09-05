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


@pytest.mark.parametrize("value", [True, -1, "18976", [], {}, 1 << 63])
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


@pytest.mark.parametrize("species_list", [["Red-crowned Amazon"], []])
def test_cached_refresh_preserves_authoritative_unconfirmed_override(taxonomy, species_list):
    override = {"species": "Red-crowned Amazon", "species_list": species_list, "confirmed": False}
    data = {"photos": [{"id": 1, "species_top5": [["Red-crowned Amazon", .9, "BioCLIP"]]}],
            "encounters": [{"photo_ids": [1], "bursts": [{"photo_ids": [1], "species_override": override}]}]}
    before = dict(override)
    normalize_cached_species(data, SpeciesResolver(taxonomy=taxonomy))
    assert override == before
    assert data["photos"][0]["species_top5"][0][0] == "Red-crowned Parrot"


@pytest.mark.parametrize("field", ["species", "classifier_model", "labels_fingerprint", "detection_id"])
def test_repair_rejects_changed_eligibility_and_run_key(db, tmp_path, field):
    _, det = _photo(db, tmp_path)
    _, other_det = _photo(db, tmp_path, "other.jpg")
    db.add_prediction(det, "Red-crowned Amazon", .9, "BioCLIP-2.5", labels_fingerprint="custom",
                      taxonomy={"scientific_name": BROWED["scientific_name"]})
    plan = plan_repairs(db.conn)
    value = other_det if field == "detection_id" else "changed"
    db.conn.execute(f"UPDATE predictions SET {field} = ?", (value,))
    db.conn.commit()
    with pytest.raises(ValueError, match="changed after"), db.conn:
        apply_repairs(db.conn, plan)
    assert db.conn.execute("SELECT scientific_name FROM predictions").fetchone()[0] == BROWED["scientific_name"]
    assert db.conn.execute("SELECT count(*) FROM species_identity_repairs").fetchone()[0] == 0


def test_id_only_taxonomy_controls_matching_and_enrichment(db, tmp_path, taxonomy):
    from classify_job import _store_grouped_predictions

    pid, det = _photo(db, tmp_path)
    raw = [{"photo": {"id": pid, "filename": "photo.jpg"}, "folder_path": str(tmp_path),
            "detection_id": det, "prediction": "Red-crowned Amazon", "confidence": .9,
            "alternatives": [], "taxonomy": {"taxon_id": 18997}, "timestamp": None}]
    with patch("classify_job._categorize_detection_prediction", return_value="new") as category, \
         patch("classify_job._can_auto_accept_detection_prediction", return_value=False) as accept:
        _store_grouped_predictions(raw, "test-job", "BioCLIP", 10, .99, taxonomy, db, "custom")
    assert category.call_args.args[0] == BROWED["scientific_name"]
    assert accept.call_args.args[0] == BROWED["scientific_name"]
    stored = db.conn.execute("SELECT scientific_name, source_taxon_id FROM predictions").fetchone()
    assert tuple(stored) == (BROWED["scientific_name"], 18997)


@pytest.mark.parametrize("complete", [True, False])
def test_api_common_names_verify_only_complete_refresh(db, complete):
    from taxonomy import fetch_common_names

    db.set_meta("common_name_identity_version", "")
    db.conn.execute("INSERT INTO taxa_common_names (taxon_id, name, locale) "
                    "SELECT id, 'Stale alias', 'en' FROM taxa WHERE inat_id = ?", (RED["taxon_id"],))
    entries = [RED, BROWED, LILAC] if complete else [RED]
    results = [{"id": e["taxon_id"], "preferred_common_name": e["common_name"],
                "names": [{"name": "Shared alias", "locale": "en"}]} for e in entries]
    with patch("taxonomy.requests.get") as request:
        request.return_value.status_code = 200
        request.return_value.json.return_value = {"results": results}
        fetch_common_names(db)
    assert db.get_meta("common_name_identity_version") == ("1" if complete else "")
    assert SpeciesResolver(db=db).resolve("Shared alias").taxon_id is None
    assert not db.conn.execute("SELECT 1 FROM taxa_common_names WHERE name = 'Stale alias'").fetchone()
    if complete:
        assert SpeciesResolver(db=db).resolve("Lilac-crowned Parrot").taxon_id == LILAC["taxon_id"]


def test_api_lookup_cannot_reintroduce_ambiguous_names(tmp_path):
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps({"taxa_by_common": {}, "ambiguous_common_names": ["parrot"],
                                "taxa_by_scientific": {RED["scientific_name"].lower(): RED}}))
    tax = Taxonomy(path)
    with patch("urllib.request.urlopen") as request:
        assert tax.api_lookup("Parrot") is None
    request.assert_not_called()
    assert tax.lookup("Parrot") is None


def test_stored_id_only_prediction_retains_source_identity(db, tmp_path, taxonomy):
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .9, "BioCLIP", labels_fingerprint="custom",
                      taxonomy={"taxon_id": BROWED["taxon_id"]})
    row = db.conn.execute("SELECT * FROM predictions").fetchone()
    for resolver in (SpeciesResolver(db=db), SpeciesResolver(taxonomy=taxonomy), SpeciesResolver()):
        assert resolver.prediction(row).key == "taxon:18997"
    assert load_photo_features(db)[0]["species_top5"][0][0] == BROWED["common_name"]


@pytest.mark.parametrize("same_taxon", [False, True])
def test_pipeline_serialization_keeps_unresolved_source_keys(db, tmp_path, same_taxon):
    from encounters import _confident_species_conflict, encounter_species_label, sim_species
    from pipeline import _build_species_predictions, attach_species_identities, serialize_results

    for i, tid in enumerate([900001, 900001 if same_taxon else 900002]):
        _, det = _photo(db, tmp_path, f"unknown-{i}.jpg")
        db.add_prediction(det, "Unknown parrot", .99, "BioCLIP", labels_fingerprint="custom",
                          taxonomy={"taxon_id": tid})
    photos = load_photo_features(db)
    entries = [p["species_top5"][0] for p in photos]
    assert (entries[0][3] == entries[1][3]) == same_taxon
    assert (sim_species([entries[0]], [entries[1]]) > 0) == same_taxon
    assert (_confident_species_conflict(*photos) is None) == same_taxon
    predictions = _build_species_predictions(photos)
    assert len(predictions) == (1 if same_taxon else 2)
    assert {p["species_key"] for p in predictions} == {e[3] for e in entries}
    data = serialize_results({"photos": photos, "summary": {}, "encounters": [
        {"photos": photos, "species": encounter_species_label(photos), "bursts": [photos]},
    ]})
    data = json.loads(json.dumps(data))
    resolver = SpeciesResolver(db=db)
    normalize_cached_species(data, resolver)
    attach_species_identities(data, resolver)
    normalize_cached_species(data, resolver)
    for photo, entry in zip(data["photos"], entries, strict=True):
        assert photo["species_top5"][0] == list(entry)
        assert data["species_identities"][entry[0]]["key"] == entry[3]
    assert len(data["encounters"][0]["species_predictions"]) == (1 if same_taxon else 2)
    node = shutil.which("node")
    if node:
        html = (Path(__file__).parents[1] / "templates/pipeline_review.html").read_text()
        source = html[html.index("var SPECIES_CONFLICT_THRESHOLDS"):html.index("function buildSpeciesConflictEvidence")]
        script = ("var pipelineResults = " + json.dumps(data) + ";\n" + source
                  + "\nprocess.stdout.write(JSON.stringify(analyzePhotoSpeciesConflict("
                    "pipelineResults.photos[1], pipelineResults.photos[0].species_top5[0][0])));")
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
        assert json.loads(result.stdout)["severity"] == (None if same_taxon else "strong")


@pytest.mark.parametrize("same_taxon", [False, True])
def test_culling_groups_unresolved_taxa_by_id(db, tmp_path, same_taxon):
    from culling import analyze_for_culling

    for i, tid in enumerate([900001, 900001 if same_taxon else 900002]):
        pid, det = _photo(db, tmp_path, f"cull-{i}.jpg")
        db.add_prediction(det, "Unknown parrot", .99, "BioCLIP", labels_fingerprint="custom",
                          taxonomy={"taxon_id": tid})
        db.conn.execute("UPDATE photos SET phash = '0000000000000000' WHERE id = ?", (pid,))
        db.upsert_photo_embedding(pid, "BioCLIP", np.ones(4, dtype=np.float32).tobytes())
    db.conn.commit()
    result = analyze_for_culling(db)
    assert len(result["species_groups"]) == (1 if same_taxon else 2)
    assert len({g["species_key"] for g in result["species_groups"]}) == (1 if same_taxon else 2)
    if not same_taxon:
        assert result["suggested_rejects"] == 0


@pytest.mark.parametrize("key", ["scientific:unknownus species", "taxon:900001"])
def test_cache_refresh_keeps_original_scientific_spelling_without_local_taxon(db, key):
    entry = ["Unknownus species", .99, "BioCLIP", key]
    data = {"photos": [{"id": 1, "species_top5": [entry]}], "encounters": []}
    for _ in range(2):
        normalize_cached_species(data, SpeciesResolver(db=db))
        assert data["photos"][0]["species_top5"][0] == entry
    assert not data.get("species_names_refreshed")


def test_unknown_id_enrichment_does_not_borrow_common_name_taxonomy(db, tmp_path, taxonomy):
    from classify_job import _prediction_taxonomy

    evidence = _prediction_taxonomy(taxonomy, "Red-crowned Amazon", {"taxon_id": 900001})
    assert evidence == {"taxon_id": 900001}
    _, det = _photo(db, tmp_path)
    db.add_prediction(det, "Red-crowned Amazon", .9, "BioCLIP", labels_fingerprint="custom", taxonomy=evidence)
    row = db.conn.execute("SELECT * FROM predictions").fetchone()
    assert row["source_taxon_id"] == 900001
    assert row["scientific_name"] is None
    assert row["taxonomy_genus"] is None
    assert SpeciesResolver(db=db).prediction(row).key == "taxon:900001"


def test_burst_votes_merge_different_labels_with_same_unknown_id(db, tmp_path):
    from datetime import datetime

    from classify_job import _store_grouped_predictions

    raw = []
    for i, name in enumerate(["Old parrot name", "New parrot name"]):
        pid, det = _photo(db, tmp_path, f"synonym-{i}.jpg")
        raw.append({"photo": {"id": pid, "filename": f"synonym-{i}.jpg"},
                    "folder_path": str(tmp_path), "detection_id": det,
                    "prediction": name, "confidence": .99, "alternatives": [],
                    "taxonomy": {"taxon_id": 900001}, "timestamp": datetime(2026, 9, 1, 10, 0, i)})
    _store_grouped_predictions(raw, "test-job", "BioCLIP", 10, .99, None, db, "custom")
    rows = db.get_predictions()
    assert {r["species"] for r in rows} == {"Old parrot name", "New parrot name"}
    assert rows[0]["group_id"] == rows[1]["group_id"]
    for row in rows:
        assert row["vote_count"] == row["total_votes"] == 2
        assert json.loads(row["individual"]) == {"Old parrot name (taxon 900001)": 2}
