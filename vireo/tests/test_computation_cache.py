import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computation_cache import (  # noqa: E402
    ArtifactStore,
    CacheFormatError,
    artifact_digest,
    canonical_bytes,
    classification_input,
    classifier_model_identity,
    exportable_artifacts,
    import_bundle,
    materialize_artifacts,
    promote_and_publish_classifier_run,
    read_bundle,
    runtime_fingerprint,
    source_input,
    validate_artifact,
    write_bundle,
)


PHOTO_HASH = "1" * 64
RUNTIME = runtime_fingerprint({
    "type": "detection",
    "model": "megadetector-v6",
    "weights_sha256": "2" * 64,
    "pipeline": "detector-v1",
})


def detection_artifact(subjects=None):
    input_block, input_fingerprint = source_input(
        PHOTO_HASH, "vireo-detector-source-v1",
    )
    return {
        "artifact_schema": 1,
        "type": "detection",
        "detector_model": "megadetector-v6",
        "photo_sha256": PHOTO_HASH,
        "runtime_fingerprint": RUNTIME,
        "input_fingerprint": input_fingerprint,
        "input": input_block,
        "completed": True,
        "subjects": subjects if subjects is not None else [{
            "key": "d0",
            "kind": "box",
            "box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            "confidence": 0.9123456789,
            "category": "animal",
        }],
    }


def test_canonical_json_normalizes_negative_zero_and_rejects_nan():
    assert canonical_bytes({"z": -0.0, "a": 1}) == b'{"a":1,"z":0.0}'
    with pytest.raises(CacheFormatError, match="NaN"):
        canonical_bytes({"confidence": float("nan")})


def test_empty_detection_result_is_valid_completed_computation():
    artifact = detection_artifact(subjects=[])
    assert validate_artifact(artifact)["subjects"] == []


def test_input_fingerprint_and_numeric_bounds_are_verified():
    artifact = detection_artifact()
    artifact["input_fingerprint"] = "9" * 64
    with pytest.raises(CacheFormatError, match="does not match"):
        validate_artifact(artifact)

    artifact = detection_artifact()
    artifact["subjects"][0]["box"]["x"] = -0.1
    with pytest.raises(CacheFormatError, match=r"in \[0, 1\]"):
        validate_artifact(artifact)

    artifact = detection_artifact()
    artifact["photo_sha256"] = "8" * 64
    with pytest.raises(CacheFormatError, match="source does not match photo"):
        validate_artifact(artifact)


def test_artifact_store_is_content_addressed_atomic_and_idempotent(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    artifact = detection_artifact()
    digest, created = store.put(artifact)
    assert created is True
    assert digest == artifact_digest(artifact)
    expected = store.object_path(artifact, digest)
    assert expected.read_bytes() == canonical_bytes(artifact)
    assert not list(expected.parent.glob(".incoming-*"))

    same_digest, created = store.put(artifact)
    assert same_digest == digest
    assert created is False
    assert store.stats() == {
        "object_count": 1,
        "total_bytes": len(canonical_bytes(artifact)),
    }


def test_bundle_round_trip_validates_before_publishing(tmp_path):
    artifact = detection_artifact()
    bundle = tmp_path / "results.vireo-cache"
    manifest = write_bundle(bundle, [artifact, artifact], device_label="Studio Mac")
    assert manifest["object_count"] == 1

    read_manifest, artifacts = read_bundle(bundle)
    assert read_manifest["device_label"] == "Studio Mac"
    assert artifacts == [artifact]

    store = ArtifactStore(tmp_path / "store")
    first = import_bundle(bundle, store)
    second = import_bundle(bundle, store)
    assert (first["added"], first["already_present"]) == (1, 0)
    assert (second["added"], second["already_present"]) == (0, 1)


def _rewrite_bundle(source, destination, transform):
    with zipfile.ZipFile(source) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    transform(members)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)


def test_corrupt_bundle_publishes_nothing(tmp_path):
    good = tmp_path / "good.vireo-cache"
    bad = tmp_path / "bad.vireo-cache"
    write_bundle(good, [detection_artifact()])

    def corrupt(members):
        object_name = next(name for name in members if name.startswith("objects/"))
        members[object_name] = members[object_name] + b" "

    _rewrite_bundle(good, bad, corrupt)
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(CacheFormatError, match="size|digest"):
        import_bundle(bad, store)
    assert not (tmp_path / "store").exists()


def test_bundle_rejects_traversal_and_undeclared_members(tmp_path):
    bundle = tmp_path / "unsafe.vireo-cache"
    manifest = {
        "format": "vireo-computation-cache",
        "format_version": 1,
        "created_at": "2026-08-08T00:00:00+00:00",
        "device_label": None,
        "object_count": 0,
        "uncompressed_bytes": 0,
        "objects": [],
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", canonical_bytes(manifest))
        archive.writestr("../outside", b"bad")
    with pytest.raises(CacheFormatError, match="unsafe ZIP path"):
        read_bundle(bundle)
    assert not (tmp_path / "outside").exists()


def test_bundle_rejects_symlinks(tmp_path):
    bundle = tmp_path / "symlink.vireo-cache"
    manifest = {
        "format": "vireo-computation-cache",
        "format_version": 1,
        "created_at": "2026-08-08T00:00:00+00:00",
        "device_label": None,
        "object_count": 0,
        "uncompressed_bytes": 0,
        "objects": [],
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", canonical_bytes(manifest))
        info = zipfile.ZipInfo("objects/link.json")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, b"target")
    with pytest.raises(CacheFormatError, match="symbolic links"):
        read_bundle(bundle)


def test_bundle_does_not_leak_source_paths(tmp_path):
    artifact = detection_artifact()
    bundle = tmp_path / "portable.vireo-cache"
    write_bundle(bundle, [artifact])
    body = bundle.read_bytes()
    assert os.fsencode("/Users/alice/Pictures") not in body
    assert b"IMG_0001.CR3" not in body


def _database_with_photo(path, filename, file_hash=PHOTO_HASH):
    from db import Database

    db = Database(str(path))
    folder_id = db.add_folder("/tmp/photos")
    workspace_id = db.create_workspace("Photos")
    db._active_workspace_id = workspace_id
    db.add_workspace_folder(workspace_id, folder_id)
    photo_id = db.add_photo(
        folder_id=folder_id,
        filename=filename,
        extension=".jpg",
        file_size=100,
        file_mtime=1.0,
        file_hash=file_hash,
    )
    return db, folder_id, photo_id


def test_database_export_bundle_import_and_duplicate_fanout(tmp_path):
    source, _folder_id, source_photo = _database_with_photo(
        tmp_path / "source.db", "source.jpg",
    )
    box = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    detector_input, detector_input_fp = source_input(
        PHOTO_HASH, "vireo-detector-source-v1",
    )
    detection_id = source.write_detection_batch(
        source_photo,
        "megadetector-v6",
        [{"box": box, "confidence": 0.91, "category": "animal"}],
        runtime_fingerprint=RUNTIME,
        input_fingerprint=detector_input_fp,
    )[0]

    labels_full = "3" * 64
    labels_short = labels_full[:12]
    classifier_runtime = runtime_fingerprint({
        "type": "classification",
        "model": "bioclip-2.5",
        "weights_sha256": "4" * 64,
        "labels_fingerprint": labels_full,
        "detector_runtime_fingerprint": RUNTIME,
    })
    classifier_subjects = [{
        "key": "d0", "kind": "box", "box": box, "category": "animal",
    }]
    _classifier_input, classifier_input_fp = classification_input(
        PHOTO_HASH, RUNTIME, classifier_subjects,
    )
    source.add_prediction(
        detection_id,
        "European Robin",
        0.87,
        "bioclip-2.5",
        labels_fingerprint=labels_short,
        labels_fingerprint_full=labels_full,
    )
    source.record_classifier_run(
        detection_id,
        "bioclip-2.5",
        labels_short,
        prediction_count=1,
        labels_fingerprint_full=labels_full,
        runtime_fingerprint=classifier_runtime,
        input_fingerprint=classifier_input_fp,
    )
    source.upsert_labels_fingerprint(
        labels_short,
        "European birds",
        ["europe.txt"],
        1,
        full_fingerprint=labels_full,
    )

    artifacts, summary = exportable_artifacts(source)
    assert summary["detector_runs"] == 1
    assert summary["classifier_runs"] == 1
    assert {artifact["type"] for artifact in artifacts} == {
        "detection", "classification",
    }

    bundle = tmp_path / "shared.vireo-cache"
    write_bundle(bundle, artifacts)
    store = ArtifactStore(tmp_path / "destination-store")
    imported = import_bundle(bundle, store)

    destination, folder_id, first_photo = _database_with_photo(
        tmp_path / "destination.db", "renamed.jpg",
    )
    second_photo = destination.add_photo(
        folder_id=folder_id,
        filename="copy.jpg",
        extension=".jpg",
        file_size=100,
        file_mtime=1.0,
    )
    # Exact duplicates are legitimate fan-out targets.  Set this directly so
    # the normal import-time duplicate resolver does not reject either fixture.
    destination.conn.execute(
        "UPDATE photos SET file_hash = ? WHERE id = ?",
        (PHOTO_HASH, second_photo),
    )
    destination.conn.commit()

    applied = materialize_artifacts(destination, imported["artifacts"])
    assert applied["matched_photos"] == 2
    assert applied["detector_runs_applied"] == 2
    assert applied["classifier_runs_applied"] == 2
    for photo_id in (first_photo, second_photo):
        prediction = destination.conn.execute(
            """SELECT p.species, p.labels_fingerprint_full
               FROM predictions p
               JOIN detections d ON d.id = p.detection_id
               WHERE d.photo_id = ?""",
            (photo_id,),
        ).fetchone()
        assert dict(prediction) == {
            "species": "European Robin",
            "labels_fingerprint_full": labels_full,
        }
    assert destination.conn.execute(
        "SELECT COUNT(*) AS c FROM prediction_review",
    ).fetchone()["c"] == 0, "portable output must not transfer review state"

    repeat = materialize_artifacts(destination, imported["artifacts"])
    assert repeat["detector_runs_applied"] == 0
    assert repeat["classifier_runs_applied"] == 0
    assert repeat["already_materialized"] == 4


def test_computation_cache_http_export_and_import(app_and_db, tmp_path):
    app, db = app_and_db
    app.config["COMPUTATION_CACHE_DIR"] = str(tmp_path / "http-store")
    photo_id = db.conn.execute("SELECT id FROM photos ORDER BY id LIMIT 1").fetchone()["id"]
    db.conn.execute(
        "UPDATE photos SET file_hash = ? WHERE id = ?", (PHOTO_HASH, photo_id),
    )
    db.conn.commit()
    _input, input_fp = source_input(PHOTO_HASH, "vireo-detector-source-v1")
    box = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    db.write_detection_batch(
        photo_id,
        "megadetector-v6",
        [{"box": box, "confidence": 0.91, "category": "animal"}],
        runtime_fingerprint=RUNTIME,
        input_fingerprint=input_fp,
    )

    client = app.test_client()
    status = client.get("/api/computation-cache")
    assert status.status_code == 200
    assert status.get_json()["exportable"]["detector_runs"] == 1

    exported = client.get("/api/computation-cache/export?types=detection")
    assert exported.status_code == 200
    assert exported.headers["Content-Disposition"].endswith('.vireo-cache"')
    manifest, artifacts = read_bundle(io.BytesIO(exported.data))
    assert manifest["object_count"] == 1
    assert artifacts[0]["photo_sha256"] == PHOTO_HASH

    db.conn.execute("DELETE FROM detections WHERE photo_id = ?", (photo_id,))
    db.conn.execute("DELETE FROM detector_runs WHERE photo_id = ?", (photo_id,))
    db.conn.commit()
    imported = client.post(
        "/api/computation-cache/import",
        data={"file": (io.BytesIO(exported.data), "results.vireo-cache")},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    body = imported.get_json()
    assert body["added"] == 1
    assert body["detector_runs_applied"] == 1
    assert db.conn.execute(
        "SELECT runtime_fingerprint FROM detector_runs WHERE photo_id = ?",
        (photo_id,),
    ).fetchone()["runtime_fingerprint"] == RUNTIME


def test_computation_cache_http_rejects_invalid_bundle(app_and_db, tmp_path):
    app, _db = app_and_db
    store_root = tmp_path / "http-store"
    app.config["COMPUTATION_CACHE_DIR"] = str(store_root)
    response = app.test_client().post(
        "/api/computation-cache/import",
        data={"file": (io.BytesIO(b"not a zip"), "bad.vireo-cache")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert not store_root.exists()


def test_fresh_classifier_run_is_promoted_and_published(tmp_path):
    source, _folder_id, photo_id = _database_with_photo(
        tmp_path / "source.db", "source.jpg",
    )
    _input, detector_input_fp = source_input(
        PHOTO_HASH, "vireo-detector-source-v1",
    )
    box = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    detection_id = source.write_detection_batch(
        photo_id,
        "megadetector-v6",
        [{"box": box, "confidence": 0.91, "category": "animal"}],
        runtime_fingerprint=RUNTIME,
        input_fingerprint=detector_input_fp,
    )[0]
    labels_full = "5" * 64
    labels_short = labels_full[:12]
    source.upsert_labels_fingerprint(
        labels_short, "Test birds", [], 1, full_fingerprint=labels_full,
    )
    source.add_prediction(
        detection_id, "Robin", 0.92, "BioCLIP",
        labels_fingerprint=labels_short,
    )
    source.record_classifier_run(
        detection_id, "BioCLIP", labels_short, prediction_count=1,
    )

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "image_encoder.onnx").write_bytes(b"exact model bytes")
    identity = classifier_model_identity({
        "id": "bioclip-test",
        "model_str": "ViT-test",
        "model_type": "bioclip",
        "weights_path": str(model_dir),
        "files": ["image_encoder.onnx"],
        "source": "custom",
    })
    store = ArtifactStore(tmp_path / "store")
    digest = promote_and_publish_classifier_run(
        source,
        detection_id,
        "BioCLIP",
        labels_short,
        labels_full,
        identity,
        store=store,
    )
    assert len(digest) == 64
    run = source.conn.execute(
        """SELECT labels_fingerprint_full, runtime_fingerprint,
                  input_fingerprint
           FROM classifier_runs WHERE detection_id = ?""",
        (detection_id,),
    ).fetchone()
    assert run["labels_fingerprint_full"] == labels_full
    assert len(run["runtime_fingerprint"]) == 64
    assert len(run["input_fingerprint"]) == 64
    stored = list(store.iter_artifacts())
    assert len(stored) == 1
    assert stored[0][1]["type"] == "classification"
    assert stored[0][1]["subjects"][0]["candidates"][0]["species"] == "Robin"

    artifacts, summary = exportable_artifacts(source)
    assert summary["classifier_runs"] == 1
    assert any(item["type"] == "classification" for item in artifacts)
