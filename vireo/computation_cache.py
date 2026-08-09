"""Portable, content-addressed detector and classifier result cache.

The format is deliberately data-only.  It never carries SQLite ids, paths,
workspace state, or executable content.  Bundles are validated in full before
objects are published to the local store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ARTIFACT_SCHEMA = 1
BUNDLE_FORMAT = 1
DEFAULT_CACHE_DIR = "~/.vireo/computation-cache"

MAX_OBJECTS = 10_000
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

_HEX64 = frozenset("0123456789abcdef")
_ARTIFACT_TYPES = frozenset({"detection", "classification"})
_FILE_DIGEST_CACHE = {}


class CacheFormatError(ValueError):
    """Raised when an artifact or bundle is invalid or unsupported."""


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX64 for char in value)
    )


def _normalize_json(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CacheFormatError("NaN and infinity are not valid cache data")
        return 0.0 if value == 0 else value
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CacheFormatError("cache object keys must be strings")
        return {key: _normalize_json(item) for key, item in value.items()}
    raise CacheFormatError(f"unsupported cache value: {type(value).__name__}")


def canonical_bytes(value):
    """Return the exact canonical JSON bytes used for all cache digests."""
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def artifact_digest(artifact):
    return fingerprint(artifact)


def runtime_fingerprint(descriptor):
    """Hash a caller-supplied immutable model/runtime descriptor."""
    return fingerprint({
        "artifact_schema": ARTIFACT_SCHEMA,
        "runtime": descriptor,
    })


def sha256_file(path):
    """Hash a model file once per unchanged (path, size, mtime) process state."""
    stat_result = os.stat(path)
    key = (os.path.abspath(path), stat_result.st_size, stat_result.st_mtime_ns)
    cached = _FILE_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _FILE_DIGEST_CACHE.clear()
    _FILE_DIGEST_CACHE[key] = value
    return value


def megadetector_runtime_fingerprint(weights_path=None):
    """Resolve the exact installed MegaDetector runtime without loading ONNX."""
    from detector import (
        CLASS_NAMES,
        INPUT_SIZE,
        MEGADETECTOR_ONNX_PATH,
        RAW_CONF_FLOOR,
    )

    weights_path = weights_path or MEGADETECTOR_ONNX_PATH
    if not os.path.isfile(weights_path):
        return None
    return runtime_fingerprint({
        "type": "detection",
        "model": "megadetector-v6",
        "weights_sha256": sha256_file(weights_path),
        "input_recipe": "vireo-detector-source-v1",
        "preprocess": {
            "version": 1,
            "input_size": INPUT_SIZE,
            "resize": "Pillow.BILINEAR-letterbox",
            "padding": 114,
        },
        "postprocess": {
            "version": 1,
            "nms_iou": 0.45,
            "raw_confidence_floor": RAW_CONF_FLOOR,
            "categories": [[key, CLASS_NAMES[key]] for key in sorted(CLASS_NAMES)],
        },
        "comparison_policy": "provider-tolerance-experimental-v1",
    })


def full_image_runtime_fingerprint():
    """Identity for Vireo's synthetic whole-photo classifier subject."""
    return runtime_fingerprint({
        "type": "detection",
        "model": "full-image",
        "input_recipe": "vireo-detector-source-v1",
        "pipeline": "synthetic-full-image-v1",
        "comparison_policy": "exact-v1",
    })


def classifier_model_identity(active_model):
    """Resolve immutable classifier files without including local paths."""
    if not isinstance(active_model, dict):
        return None
    weights_path = active_model.get("weights_path")
    if not weights_path:
        return None
    declared = list(active_model.get("files") or [])
    revision_path = os.path.join(weights_path, ".hf_revision")
    revision = None
    try:
        with open(revision_path, encoding="utf-8") as handle:
            revision = handle.read().strip()
    except OSError:
        pass
    identity = {
        "id": active_model.get("id"),
        "model_str": active_model.get("model_str"),
        "model_type": active_model.get("model_type", "bioclip"),
        "declared_files": sorted(declared),
    }
    if revision:
        identity["immutable_upstream_revision"] = revision
        return identity

    # Known Hugging Face installs without a pinned revision pre-date portable
    # identity. Hashing multi-gigabyte external-data files on startup would
    # make classification appear hung; keep those runs local until Repair or
    # a fresh download writes .hf_revision. Custom models have no upstream
    # revision, so exact file hashes are their intended identity.
    if declared and active_model.get("source") != "custom":
        return None

    paths = []
    if declared and os.path.isdir(weights_path):
        paths = [(name, os.path.join(weights_path, name)) for name in sorted(declared)]
    elif os.path.isdir(weights_path):
        paths = [
            (name, os.path.join(weights_path, name))
            for name in sorted(os.listdir(weights_path))
            if not name.startswith(".")
            and os.path.isfile(os.path.join(weights_path, name))
        ]
    elif os.path.isfile(weights_path):
        paths = [("weights", weights_path)]
    if not paths or any(not os.path.isfile(path) for _name, path in paths):
        return None
    identity["file_sha256"] = [
        [name, sha256_file(path)] for name, path in paths
    ]
    return identity


def classifier_runtime_fingerprint(
    model_identity, labels_fingerprint_full, detector_runtime,
):
    if (
        not isinstance(model_identity, dict)
        or not _is_sha256(labels_fingerprint_full)
        or not _is_sha256(detector_runtime)
    ):
        return None
    return runtime_fingerprint({
        "type": "classification",
        "model": model_identity,
        "labels_fingerprint": labels_fingerprint_full,
        "detector_runtime_fingerprint": detector_runtime,
        "input_recipe": "vireo-classifier-crops-v1",
        "preprocess": "model-config-owned-v1",
        "comparison_policy": "provider-tolerance-experimental-v1",
    })


def classifier_runtime_for_detection(
    db, detection_id, model_identity, labels_fingerprint_full,
):
    row = db.conn.execute(
        "SELECT runtime_fingerprint FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    if row is None:
        return None
    return classifier_runtime_fingerprint(
        model_identity,
        labels_fingerprint_full,
        row["runtime_fingerprint"],
    )


def publish_detection_artifact(
    photo_sha256,
    detector_model,
    runtime,
    detections,
    store=None,
):
    """Publish one newly committed detector result when identity is complete."""
    if not _is_sha256(photo_sha256) or not _is_sha256(runtime):
        return None
    input_block, input_fp = source_input(
        photo_sha256, "vireo-detector-source-v1",
    )
    subjects = [{
        "key": f"d{index}",
        "kind": "box",
        "box": detection["box"],
        "confidence": detection["confidence"],
        "category": detection.get("category", "animal"),
    } for index, detection in enumerate(detections)]
    artifact = {
        "artifact_schema": ARTIFACT_SCHEMA,
        "type": "detection",
        "detector_model": detector_model,
        "photo_sha256": photo_sha256,
        "runtime_fingerprint": runtime,
        "input_fingerprint": input_fp,
        "input": input_block,
        "completed": True,
        "subjects": subjects,
    }
    digest, _created = (store or ArtifactStore()).put(artifact)
    return digest


def promote_and_publish_classifier_run(
    db,
    detection_id,
    classifier_model,
    labels_fingerprint,
    labels_fingerprint_full,
    model_identity,
    store=None,
):
    """Attach portable identity to one fresh run and publish its raw output."""
    if not _is_sha256(labels_fingerprint_full):
        return None
    row = db.conn.execute(
        """SELECT d.photo_id, d.detector_model, d.runtime_fingerprint,
                  d.box_x, d.box_y, d.box_w, d.box_h, d.category,
                  p.file_hash, p.companion_path, p.working_copy_path
           FROM detections d
           JOIN photos p ON p.id = d.photo_id
           WHERE d.id = ?""",
        (detection_id,),
    ).fetchone()
    # Working-copy-backed photos also stay local-only: ``_prepare_image``
    # feeds the extracted working-copy JPEG (a specific rendition/recipe
    # of the original) to the classifier when ``vireo_dir`` is set, but
    # the v1 input identity carries only ``p.file_hash``. Publishing the
    # result would advertise it as computed from the original bytes, so
    # a destination that decodes the original — or that has a different
    # working copy — would materialize predictions made on foreign pixels.
    if (
        row is None
        or row["companion_path"]
        or row["working_copy_path"]
        or not _is_sha256(row["file_hash"])
        or not _is_sha256(row["runtime_fingerprint"])
    ):
        return None
    classifier_runtime = classifier_runtime_fingerprint(
        model_identity, labels_fingerprint_full, row["runtime_fingerprint"],
    )
    if classifier_runtime is None:
        return None
    kind = "full_image" if row["detector_model"] == "full-image" else "box"
    subject = {"key": "d0", "kind": kind}
    if kind == "box":
        subject["box"] = {
            "x": row["box_x"], "y": row["box_y"],
            "w": row["box_w"], "h": row["box_h"],
        }
        subject["category"] = row["category"] or "animal"
    input_block, input_fp = classification_input(
        row["file_hash"], row["runtime_fingerprint"], [subject],
    )
    predictions = db.conn.execute(
        """SELECT species, confidence, scientific_name,
                  taxonomy_kingdom, taxonomy_phylum, taxonomy_class,
                  taxonomy_order, taxonomy_family, taxonomy_genus
           FROM predictions
           WHERE detection_id = ? AND classifier_model = ?
             AND labels_fingerprint = ?
           ORDER BY confidence DESC, species COLLATE NOCASE""",
        (detection_id, classifier_model, labels_fingerprint),
    ).fetchall()
    if not predictions:
        return None
    candidates = []
    for prediction in predictions:
        candidate = {
            "species": prediction["species"],
            "confidence": prediction["confidence"],
        }
        taxonomy = {
            "scientific_name": prediction["scientific_name"],
            "kingdom": prediction["taxonomy_kingdom"],
            "phylum": prediction["taxonomy_phylum"],
            "class": prediction["taxonomy_class"],
            "order": prediction["taxonomy_order"],
            "family": prediction["taxonomy_family"],
            "genus": prediction["taxonomy_genus"],
        }
        taxonomy = {key: value for key, value in taxonomy.items() if value}
        if taxonomy:
            candidate["taxonomy"] = taxonomy
        candidates.append(candidate)
    subject["candidates"] = candidates
    label_meta = db.conn.execute(
        """SELECT display_name, label_count FROM labels_fingerprints
           WHERE fingerprint = ?""",
        (labels_fingerprint,),
    ).fetchone()
    artifact = {
        "artifact_schema": ARTIFACT_SCHEMA,
        "type": "classification",
        "classifier_model": classifier_model,
        "detector_model": row["detector_model"],
        "photo_sha256": row["file_hash"],
        "runtime_fingerprint": classifier_runtime,
        "detector_runtime_fingerprint": row["runtime_fingerprint"],
        "input_fingerprint": input_fp,
        "input": input_block,
        "labels": {
            "fingerprint": labels_fingerprint_full,
            "short_fingerprint": labels_fingerprint,
            "display_name": label_meta["display_name"] if label_meta else None,
            "count": label_meta["label_count"] if label_meta else None,
        },
        "completed": True,
        "subjects": [subject],
    }
    try:
        db.conn.execute(
            """UPDATE classifier_runs
               SET labels_fingerprint_full = ?, runtime_fingerprint = ?,
                   input_fingerprint = ?
               WHERE detection_id = ? AND classifier_model = ?
                 AND labels_fingerprint = ?""",
            (
                labels_fingerprint_full, classifier_runtime, input_fp,
                detection_id, classifier_model, labels_fingerprint,
            ),
        )
        db.conn.execute(
            """UPDATE predictions SET labels_fingerprint_full = ?
               WHERE detection_id = ? AND classifier_model = ?
                 AND labels_fingerprint = ?""",
            (
                labels_fingerprint_full, detection_id,
                classifier_model, labels_fingerprint,
            ),
        )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    digest, _created = (store or ArtifactStore()).put(artifact)
    return digest


def source_input(photo_sha256, recipe):
    """Build the versioned source identity used by the experimental v1 cache.

    The model runtime identity owns decode/preprocess behavior.  The input
    identity owns the exact source bytes and named source-selection recipe.
    """
    if not _is_sha256(photo_sha256):
        raise CacheFormatError("photo_sha256 must be a lowercase SHA-256")
    block = {
        "recipe": recipe,
        "sources": [{"role": "original", "sha256": photo_sha256}],
    }
    return block, fingerprint(block)


def classification_input(
    photo_sha256, detector_runtime_fingerprint, subjects,
    recipe="vireo-classifier-crops-v1",
):
    if not _is_sha256(photo_sha256):
        raise CacheFormatError("photo_sha256 must be a lowercase SHA-256")
    if not _is_sha256(detector_runtime_fingerprint):
        raise CacheFormatError("detector runtime must be a SHA-256")
    subject_inputs = []
    for subject in subjects:
        entry = {"key": subject["key"], "kind": subject["kind"]}
        if subject["kind"] == "box":
            entry["box"] = subject["box"]
        subject_inputs.append(entry)
    block = {
        "recipe": recipe,
        "source_sha256": photo_sha256,
        "detector_runtime_fingerprint": detector_runtime_fingerprint,
        "subjects": subject_inputs,
    }
    return block, fingerprint(block)


def _validate_box(box):
    if not isinstance(box, dict) or set(box) != {"x", "y", "w", "h"}:
        raise CacheFormatError("box must contain exactly x, y, w, and h")
    for key in ("x", "y", "w", "h"):
        value = box[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CacheFormatError(f"box.{key} must be numeric")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise CacheFormatError(f"box.{key} must be finite and in [0, 1]")
    if box["w"] <= 0 or box["h"] <= 0:
        raise CacheFormatError("box width and height must be positive")
    if box["x"] + box["w"] > 1.000001 or box["y"] + box["h"] > 1.000001:
        raise CacheFormatError("box must fit inside normalized image bounds")


def _validate_confidence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CacheFormatError("confidence must be numeric")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise CacheFormatError("confidence must be finite and in [0, 1]")


_TAXONOMY_SCALAR_FIELDS = frozenset({
    "scientific_name", "kingdom", "phylum", "class", "order", "family", "genus",
})


def _validate_candidate_taxonomy(taxonomy):
    # A candidate may omit taxonomy entirely.  When present it must be a JSON
    # object whose recognized fields are optional strings — materialization
    # binds these directly to SQLite text columns, so a list or nested object
    # here would surface as an uncaught ``TypeError`` mid-write and leave the
    # bundle half-applied.  Reject the whole artifact up front instead.
    if taxonomy is None:
        return
    if not isinstance(taxonomy, dict):
        raise CacheFormatError("candidate taxonomy must be an object")
    for field in _TAXONOMY_SCALAR_FIELDS:
        if field not in taxonomy:
            continue
        value = taxonomy[field]
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 500:
            raise CacheFormatError(
                f"candidate taxonomy field {field!r} must be a string"
            )


def validate_artifact(artifact):
    """Validate one artifact and return its normalized data-only form."""
    artifact = _normalize_json(artifact)
    if not isinstance(artifact, dict):
        raise CacheFormatError("artifact must be an object")
    required = {
        "artifact_schema", "type", "photo_sha256", "runtime_fingerprint",
        "input_fingerprint", "input", "completed", "subjects",
    }
    missing = required - set(artifact)
    if missing:
        raise CacheFormatError(f"artifact missing fields: {', '.join(sorted(missing))}")
    if artifact["artifact_schema"] != ARTIFACT_SCHEMA:
        raise CacheFormatError(
            f"unsupported artifact schema {artifact['artifact_schema']!r}"
        )
    if artifact["type"] not in _ARTIFACT_TYPES:
        raise CacheFormatError(f"unsupported artifact type {artifact['type']!r}")
    for field in ("photo_sha256", "runtime_fingerprint", "input_fingerprint"):
        if not _is_sha256(artifact[field]):
            raise CacheFormatError(f"{field} must be a lowercase SHA-256")
    if artifact["completed"] is not True:
        raise CacheFormatError("only completed computation may be cached")
    if not isinstance(artifact["input"], dict):
        raise CacheFormatError("input must be an object")
    if fingerprint(artifact["input"]) != artifact["input_fingerprint"]:
        raise CacheFormatError("input_fingerprint does not match input block")
    if not isinstance(artifact["subjects"], list):
        raise CacheFormatError("subjects must be a list")

    seen_keys = set()
    for subject in artifact["subjects"]:
        if not isinstance(subject, dict):
            raise CacheFormatError("subject must be an object")
        key = subject.get("key")
        kind = subject.get("kind")
        if not isinstance(key, str) or not key or len(key) > 80:
            raise CacheFormatError("subject key must be a short non-empty string")
        if key in seen_keys:
            raise CacheFormatError(f"duplicate subject key {key!r}")
        seen_keys.add(key)
        if kind not in {"box", "full_image"}:
            raise CacheFormatError(f"unsupported subject kind {kind!r}")
        if kind == "box":
            _validate_box(subject.get("box"))
            # Category is optional on classification box subjects
            # (materialize falls back to "animal") but MUST be a bounded
            # string when supplied.  ``compute_detection_id`` feeds it to
            # ``positive_int_hash``, which calls ``len()`` on each part —
            # a bool/int/None would raise ``TypeError`` mid-materialization
            # after the bundle object has already been published, leaving
            # an unmaterialized entry behind and returning 500 to the
            # import route.  Detection artifacts still require the field
            # to be present (checked further below).
            if "category" in subject:
                category = subject["category"]
                if (
                    not isinstance(category, str)
                    or not category
                    or len(category) > 80
                ):
                    raise CacheFormatError(
                        "subject category must be a non-empty string"
                    )
        elif "box" in subject:
            raise CacheFormatError("full_image subject must not contain a box")

        if artifact["type"] == "detection":
            if kind != "box":
                raise CacheFormatError("detection artifacts contain only boxes")
            _validate_confidence(subject.get("confidence"))
            if not isinstance(subject.get("category"), str):
                raise CacheFormatError("detection category must be a string")
        else:
            candidates = subject.get("candidates")
            # Reject empty candidate lists — a completed classification
            # subject with no predictions still writes a classifier_runs
            # marker on materialize, which _all_photos_cache_satisfied
            # counts as covered.  That would let a bundle containing
            # empty subjects short-circuit an entire Classify job and
            # leave the photo permanently unclassified until a forced
            # reclassify.
            if not isinstance(candidates, list) or not candidates:
                raise CacheFormatError(
                    "classification subject needs at least one candidate"
                )
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise CacheFormatError("classification candidate must be an object")
                species = candidate.get("species")
                if not isinstance(species, str) or not species or len(species) > 500:
                    raise CacheFormatError("candidate species must be a non-empty string")
                _validate_confidence(candidate.get("confidence"))
                _validate_candidate_taxonomy(candidate.get("taxonomy"))

    if artifact["type"] == "classification":
        if not isinstance(artifact.get("classifier_model"), str):
            raise CacheFormatError("classification artifact needs classifier_model")
        if not isinstance(artifact.get("detector_model"), str):
            raise CacheFormatError("classification artifact needs detector_model")
        labels = artifact.get("labels")
        if not isinstance(labels, dict):
            raise CacheFormatError("classification artifact needs labels metadata")
        full_fp = labels.get("fingerprint")
        if not _is_sha256(full_fp):
            raise CacheFormatError("labels.fingerprint must be a full SHA-256")
        short = labels.get("short_fingerprint")
        if not isinstance(short, str) or not short or len(short) > 64:
            raise CacheFormatError("labels.short_fingerprint is invalid")
        # short_fingerprint is a cache key for classifier_runs — normal jobs
        # derive it from the first 12 characters of the full digest.  An
        # arbitrary short key would let a crafted bundle claim an unrelated
        # historical row (e.g. a legacy row with full_fingerprint IS NULL)
        # and replace its unreviewed predictions under a foreign label set.
        #
        # Tree-of-Life mode is the documented exception: compute_fingerprint([])
        # returns the "tol" sentinel as the classifier_runs short key, while
        # classify_job synthesizes a portable 64-char labels.fingerprint from
        # the classifier identity.  The sentinel is a well-known constant, not
        # attacker-controlled, and cannot impersonate a legacy row (whose short
        # key is "legacy"), so exempt it from the digest-prefix check.
        from labels_fingerprint import TOL_SENTINEL

        if short != TOL_SENTINEL and short != full_fp[:12]:
            raise CacheFormatError(
                "labels.short_fingerprint must be the first 12 chars of labels.fingerprint"
            )
        # Optional scalar metadata is bound directly to SQLite text/int
        # columns by materialize_artifacts.  A list or dict here would
        # surface as an uncaught binding error mid-write and leave the
        # supposedly-validated object in the local store, so validate
        # its shape up-front alongside the fingerprints.
        display_name = labels.get("display_name")
        if display_name is not None and (
            not isinstance(display_name, str) or len(display_name) > 500
        ):
            raise CacheFormatError(
                "labels.display_name must be a string of at most 500 chars"
            )
        label_count = labels.get("count")
        # SQLite integer columns are signed 64-bit; a bundle carrying an
        # arbitrarily large JSON int (e.g. 10**100) would pass a naive
        # non-negativity check and only fail with OverflowError when
        # materialize_artifacts binds the value.  By that point the bundle
        # object has already been published, leaving an unmaterialized
        # entry behind.  Reject out-of-range values up-front instead.
        _SQLITE_INT64_MAX = (1 << 63) - 1
        if label_count is not None and (
            isinstance(label_count, bool)
            or not isinstance(label_count, int)
            or label_count < 0
            or label_count > _SQLITE_INT64_MAX
        ):
            raise CacheFormatError(
                "labels.count must be a non-negative integer within SQLite's 64-bit range"
            )
        detector_runtime = artifact.get("detector_runtime_fingerprint")
        if not _is_sha256(detector_runtime):
            raise CacheFormatError(
                "classification artifact needs detector_runtime_fingerprint"
            )
        if artifact["input"].get("source_sha256") != artifact["photo_sha256"]:
            raise CacheFormatError("classification input source does not match photo")
        if (
            artifact["input"].get("detector_runtime_fingerprint")
            != detector_runtime
        ):
            raise CacheFormatError("classification input detector runtime mismatch")
        expected_input, _expected_fp = classification_input(
            artifact["photo_sha256"], detector_runtime, artifact["subjects"],
        )
        if artifact["input"] != expected_input:
            raise CacheFormatError("classification input does not match subjects")
    elif not isinstance(artifact.get("detector_model"), str):
        raise CacheFormatError("detection artifact needs detector_model")
    else:
        sources = artifact["input"].get("sources")
        if not isinstance(sources, list) or not any(
            isinstance(source, dict)
            and source.get("role") == "original"
            and source.get("sha256") == artifact["photo_sha256"]
            for source in sources
        ):
            raise CacheFormatError("detection input source does not match photo")
        expected_input, _expected_fp = source_input(
            artifact["photo_sha256"], "vireo-detector-source-v1",
        )
        if artifact["input"] != expected_input:
            raise CacheFormatError("unsupported detection input recipe")
    return artifact


class ArtifactStore:
    def __init__(self, root=None):
        self.root = Path(os.path.expanduser(os.fspath(root or DEFAULT_CACHE_DIR)))

    def object_path(self, artifact, digest=None):
        artifact = validate_artifact(artifact)
        digest = digest or artifact_digest(artifact)
        if not _is_sha256(digest):
            raise CacheFormatError("artifact digest must be a SHA-256")
        return (
            self.root / "objects" / artifact["photo_sha256"][:2]
            / artifact["photo_sha256"] / artifact["type"]
            / artifact["runtime_fingerprint"] / artifact["input_fingerprint"]
            / f"{digest}.json"
        )

    def put(self, artifact):
        artifact = validate_artifact(artifact)
        body = canonical_bytes(artifact)
        if len(body) > MAX_OBJECT_BYTES:
            raise CacheFormatError("artifact exceeds maximum object size")
        digest = hashlib.sha256(body).hexdigest()
        destination = self.object_path(artifact, digest)
        if destination.exists():
            if destination.read_bytes() != body:
                raise CacheFormatError("content-addressed object collision")
            return digest, False
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".incoming-", suffix=".json", dir=destination.parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_name, destination)
                created = True
            except FileExistsError:
                created = False
            except OSError:
                # os.link raises EXDEV, EPERM, or ENOSYS on filesystems
                # without hard-link support (exFAT, network mounts, some
                # Windows volumes). Objects are content-addressed, so
                # replacing an existing file with identical bytes is
                # safe; fall back to os.replace to keep bundle import
                # and result publication working on those volumes.
                os.replace(temp_name, destination)
                created = True
            if not created and destination.read_bytes() != body:
                raise CacheFormatError("content-addressed object collision")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)
        return digest, created

    def iter_artifacts(self):
        objects = self.root / "objects"
        if not objects.exists():
            return
        for path in sorted(objects.rglob("*.json")):
            try:
                body = path.read_bytes()
                artifact = validate_artifact(json.loads(body))
                digest = artifact_digest(artifact)
                if path.name == f"{digest}.json":
                    yield digest, artifact
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def stats(self):
        count = 0
        total = 0
        objects = self.root / "objects"
        if not objects.exists():
            return {"object_count": 0, "total_bytes": 0}
        for path in objects.rglob("*.json"):
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
        return {"object_count": count, "total_bytes": total}

    @property
    def trust_path(self):
        return self.root / "trust.json"

    def trusted_runtimes(self):
        """Return (detector, classifier) runtime fingerprints trusted by this store.

        Populated by ``record_trusted_runtimes`` when the user imports a
        bundle. Later ``materialize_local_store`` calls read this file so
        bundles imported before their matching photos landed still plant
        once the photos are cataloged — the import-time whitelist was
        one-shot per HTTP request and could not reach the later reapply.
        """
        path = self.trust_path
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            return set(), set()
        except OSError:
            return set(), set()
        try:
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return set(), set()
        if not isinstance(data, dict):
            return set(), set()
        detector = {
            value for value in (data.get("detector_runtimes") or [])
            if _is_sha256(value)
        }
        classifier = {
            value for value in (data.get("classifier_runtimes") or [])
            if _is_sha256(value)
        }
        return detector, classifier

    def record_trusted_runtimes(
        self, detector_runtimes=None, classifier_runtimes=None,
    ):
        """Merge runtime fingerprints into the persisted trust set.

        An explicit bundle import is a trust action for the runtimes the
        user chose to accept. Persisting them lets subsequent
        ``materialize_local_store`` calls plant matching artifacts whose
        photos hadn't been cataloged at import time.
        """
        detector = {
            value for value in (detector_runtimes or ())
            if _is_sha256(value)
        }
        classifier = {
            value for value in (classifier_runtimes or ())
            if _is_sha256(value)
        }
        if not detector and not classifier:
            return
        existing_det, existing_clf = self.trusted_runtimes()
        merged_det = sorted(existing_det | detector)
        merged_clf = sorted(existing_clf | classifier)
        if (
            set(merged_det) == existing_det
            and set(merged_clf) == existing_clf
        ):
            return
        payload = {
            "detector_runtimes": merged_det,
            "classifier_runtimes": merged_clf,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        body = canonical_bytes(payload)
        fd, temp_name = tempfile.mkstemp(
            prefix=".trust-", suffix=".json", dir=self.root,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.trust_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)


def write_bundle(destination, artifacts, device_label=None):
    """Atomically write a validated `.vireo-cache` ZIP bundle."""
    destination = Path(destination)
    objects = {}
    for artifact in artifacts:
        normalized = validate_artifact(artifact)
        body = canonical_bytes(normalized)
        if len(body) > MAX_OBJECT_BYTES:
            raise CacheFormatError("artifact exceeds maximum object size")
        objects.setdefault(hashlib.sha256(body).hexdigest(), body)
    if len(objects) > MAX_OBJECTS:
        raise CacheFormatError("bundle has too many objects")
    total = sum(map(len, objects.values()))
    if total > MAX_BUNDLE_BYTES:
        raise CacheFormatError("bundle exceeds maximum uncompressed size")
    manifest = {
        "format": "vireo-computation-cache",
        "format_version": BUNDLE_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "device_label": device_label if device_label else None,
        "object_count": len(objects),
        "uncompressed_bytes": total,
        "objects": [
            {"digest": digest, "size": len(body)}
            for digest, body in sorted(objects.items())
        ],
    }
    manifest_body = canonical_bytes(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(
            temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
        ) as archive:
            archive.writestr("manifest.json", manifest_body)
            for digest, body in sorted(objects.items()):
                archive.writestr(f"objects/{digest}.json", body)
        os.replace(temp_name, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
    return manifest


def _safe_zip_members(archive):
    infos = archive.infolist()
    if len(infos) > MAX_OBJECTS + 1:
        raise CacheFormatError("bundle has too many ZIP members")
    total = 0
    names = set()
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            not info.filename
            or info.filename in names
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in info.filename
        ):
            raise CacheFormatError("bundle contains an unsafe ZIP path")
        names.add(info.filename)
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise CacheFormatError("bundle may not contain symbolic links")
        if info.is_dir():
            raise CacheFormatError("bundle may not contain directory entries")
        if info.file_size > MAX_OBJECT_BYTES and info.filename != "manifest.json":
            raise CacheFormatError("bundle object exceeds maximum size")
        if info.filename == "manifest.json" and info.file_size > MAX_MANIFEST_BYTES:
            raise CacheFormatError("bundle manifest exceeds maximum size")
        if info.compress_size == 0 and info.file_size:
            raise CacheFormatError("bundle member has an invalid compression ratio")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise CacheFormatError("bundle member exceeds compression-ratio limit")
        total += info.file_size
        if total > MAX_BUNDLE_BYTES + MAX_MANIFEST_BYTES:
            raise CacheFormatError("bundle exceeds maximum uncompressed size")
    return {info.filename: info for info in infos}


def read_bundle(source):
    """Validate an entire bundle without extracting it."""
    try:
        archive = zipfile.ZipFile(source, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise CacheFormatError(f"invalid cache bundle: {exc}") from exc
    with archive:
        members = _safe_zip_members(archive)
        if "manifest.json" not in members:
            raise CacheFormatError("bundle has no manifest.json")
        manifest_info = members["manifest.json"]
        manifest_cap = MAX_MANIFEST_BYTES + 1
        try:
            with archive.open(manifest_info, "r") as handle:
                manifest_bytes = handle.read(manifest_cap)
        except (OSError, zipfile.BadZipFile) as exc:
            raise CacheFormatError("bundle manifest is not readable") from exc
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CacheFormatError("bundle manifest exceeds maximum size")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheFormatError("bundle manifest is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise CacheFormatError("bundle manifest must be an object")
        if (
            manifest.get("format") != "vireo-computation-cache"
            or manifest.get("format_version") != BUNDLE_FORMAT
        ):
            raise CacheFormatError("unsupported cache bundle format")
        declarations = manifest.get("objects")
        if not isinstance(declarations, list):
            raise CacheFormatError("bundle manifest objects must be a list")
        if manifest.get("object_count") != len(declarations):
            raise CacheFormatError("bundle object count does not match manifest")

        expected_names = {"manifest.json"}
        artifacts = []
        declared_total = 0
        seen = set()
        for declaration in declarations:
            if not isinstance(declaration, dict):
                raise CacheFormatError("invalid object declaration")
            digest = declaration.get("digest")
            size = declaration.get("size")
            if not _is_sha256(digest) or digest in seen:
                raise CacheFormatError("invalid or duplicate object digest")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise CacheFormatError("invalid object size")
            seen.add(digest)
            name = f"objects/{digest}.json"
            expected_names.add(name)
            info = members.get(name)
            if info is None or info.file_size != size:
                raise CacheFormatError("bundle object size does not match manifest")
            # Stream the member with a hard byte cap instead of
            # ``archive.read(info)``: the ZIP central-directory
            # ``file_size`` is attacker-controlled, so a member declaring
            # ``file_size = size`` could still expand into gigabytes and
            # exhaust memory before the size/CRC checks fire. Reading
            # one byte past the cap lets us reject the member without
            # ever allocating the full uncompressed stream.
            cap = max(size, 0) + 1
            with archive.open(info, "r") as handle:
                body = handle.read(cap)
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise CacheFormatError("bundle object digest mismatch")
            try:
                artifact = validate_artifact(json.loads(body))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CacheFormatError("bundle object is not valid JSON") from exc
            if canonical_bytes(artifact) != body:
                raise CacheFormatError("bundle object JSON is not canonical")
            artifacts.append(artifact)
            declared_total += size
        if set(members) != expected_names:
            raise CacheFormatError("bundle contains undeclared members")
        if manifest.get("uncompressed_bytes") != declared_total:
            raise CacheFormatError("bundle byte total does not match manifest")
        return manifest, artifacts


def import_bundle(source, store):
    """Validate the whole bundle, then immutably publish its objects."""
    manifest, artifacts = read_bundle(source)
    added = 0
    existing = 0
    for artifact in artifacts:
        _digest, created = store.put(artifact)
        if created:
            added += 1
        else:
            existing += 1
    return {
        "manifest": manifest,
        "artifacts": artifacts,
        "added": added,
        "already_present": existing,
    }


def exportable_artifacts(db, artifact_types=None):
    """Synthesize portable artifacts from fully identified database runs.

    Legacy rows and rows whose stored input identity cannot be reconstructed
    are intentionally skipped.  This lets the store be introduced without
    pretending pre-migration inference had a stronger identity than it did.
    """
    artifact_types = set(artifact_types or _ARTIFACT_TYPES)
    unknown = artifact_types - _ARTIFACT_TYPES
    if unknown:
        raise CacheFormatError(f"unknown artifact types: {sorted(unknown)!r}")
    # Classification subjects are keyed to detector-owned boxes. Keep the
    # export dependency-closed so a destination can materialize the imported
    # classifier runs without first reproducing detection locally.
    if "classification" in artifact_types:
        artifact_types.add("detection")
    artifacts = []
    summary = {
        "detector_runs": 0,
        "classifier_runs": 0,
        "skipped_legacy": 0,
        "skipped_missing_hash": 0,
        "skipped_incompatible_input": 0,
    }

    detection_rows = db.conn.execute(
        """SELECT p.id AS photo_id, p.file_hash, dr.detector_model,
                  dr.runtime_fingerprint, dr.input_fingerprint
           FROM detector_runs dr
           JOIN photos p ON p.id = dr.photo_id
           ORDER BY p.id, dr.detector_model"""
    ).fetchall()
    if "detection" in artifact_types:
        for row in detection_rows:
            if not _is_sha256(row["file_hash"]):
                summary["skipped_missing_hash"] += 1
                continue
            if not _is_sha256(row["runtime_fingerprint"]):
                summary["skipped_legacy"] += 1
                continue
            input_block, expected_input = source_input(
                row["file_hash"], "vireo-detector-source-v1",
            )
            if row["input_fingerprint"] != expected_input:
                summary["skipped_incompatible_input"] += 1
                continue
            detections = db.conn.execute(
                """SELECT box_x, box_y, box_w, box_h,
                          detector_confidence, category
                   FROM detections
                   WHERE photo_id = ? AND detector_model = ?
                     AND runtime_fingerprint = ?
                   ORDER BY detector_confidence DESC, id ASC""",
                (row["photo_id"], row["detector_model"],
                 row["runtime_fingerprint"]),
            ).fetchall()
            subjects = [{
                "key": f"d{index}",
                "kind": "box",
                "box": {
                    "x": det["box_x"], "y": det["box_y"],
                    "w": det["box_w"], "h": det["box_h"],
                },
                "confidence": det["detector_confidence"],
                "category": det["category"] or "animal",
            } for index, det in enumerate(detections)]
            artifacts.append({
                "artifact_schema": ARTIFACT_SCHEMA,
                "type": "detection",
                "detector_model": row["detector_model"],
                "photo_sha256": row["file_hash"],
                "runtime_fingerprint": row["runtime_fingerprint"],
                "input_fingerprint": expected_input,
                "input": input_block,
                "completed": True,
                "subjects": subjects,
            })
            summary["detector_runs"] += 1

    if "classification" in artifact_types:
        classifier_rows = db.conn.execute(
            """SELECT DISTINCT
                      p.id AS photo_id, p.file_hash,
                      d.detector_model, d.runtime_fingerprint AS detector_runtime,
                      cr.classifier_model, cr.labels_fingerprint,
                      cr.labels_fingerprint_full, cr.runtime_fingerprint,
                      cr.input_fingerprint
               FROM classifier_runs cr
               JOIN detections d ON d.id = cr.detection_id
               JOIN photos p ON p.id = d.photo_id
               ORDER BY p.id, cr.classifier_model, cr.labels_fingerprint"""
        ).fetchall()
        for row in classifier_rows:
            if not _is_sha256(row["file_hash"]):
                summary["skipped_missing_hash"] += 1
                continue
            if (
                not _is_sha256(row["runtime_fingerprint"])
                or not _is_sha256(row["detector_runtime"])
                or not _is_sha256(row["labels_fingerprint_full"])
            ):
                summary["skipped_legacy"] += 1
                continue
            classified = db.conn.execute(
                """SELECT d.id, d.box_x, d.box_y, d.box_w, d.box_h,
                          d.detector_model, d.category
                   FROM detections d
                   JOIN classifier_runs cr ON cr.detection_id = d.id
                   WHERE d.photo_id = ? AND d.detector_model = ?
                     AND d.runtime_fingerprint = ?
                     AND cr.classifier_model = ?
                     AND cr.labels_fingerprint = ?
                     AND cr.labels_fingerprint_full = ?
                     AND cr.runtime_fingerprint = ?
                     AND cr.input_fingerprint = ?
                   ORDER BY d.detector_confidence DESC, d.id ASC""",
                (
                    row["photo_id"], row["detector_model"],
                    row["detector_runtime"], row["classifier_model"],
                    row["labels_fingerprint"],
                    row["labels_fingerprint_full"],
                    row["runtime_fingerprint"], row["input_fingerprint"],
                ),
            ).fetchall()
            subjects = []
            for index, detection in enumerate(classified):
                kind = (
                    "full_image"
                    if detection["detector_model"] == "full-image" else "box"
                )
                subject = {"key": f"d{index}", "kind": kind}
                if kind == "box":
                    subject["box"] = {
                        "x": detection["box_x"], "y": detection["box_y"],
                        "w": detection["box_w"], "h": detection["box_h"],
                    }
                    subject["category"] = detection["category"] or "animal"
                predictions = db.conn.execute(
                    """SELECT species, confidence, scientific_name,
                              taxonomy_kingdom, taxonomy_phylum,
                              taxonomy_class, taxonomy_order,
                              taxonomy_family, taxonomy_genus
                       FROM predictions
                       WHERE detection_id = ? AND classifier_model = ?
                         AND labels_fingerprint = ?
                       ORDER BY confidence DESC, species COLLATE NOCASE""",
                    (detection["id"], row["classifier_model"],
                     row["labels_fingerprint"]),
                ).fetchall()
                candidates = []
                for prediction in predictions:
                    candidate = {
                        "species": prediction["species"],
                        "confidence": prediction["confidence"],
                    }
                    taxonomy = {
                        "scientific_name": prediction["scientific_name"],
                        "kingdom": prediction["taxonomy_kingdom"],
                        "phylum": prediction["taxonomy_phylum"],
                        "class": prediction["taxonomy_class"],
                        "order": prediction["taxonomy_order"],
                        "family": prediction["taxonomy_family"],
                        "genus": prediction["taxonomy_genus"],
                    }
                    taxonomy = {key: value for key, value in taxonomy.items() if value}
                    if taxonomy:
                        candidate["taxonomy"] = taxonomy
                    candidates.append(candidate)
                subject["candidates"] = candidates
                subjects.append(subject)
            input_block, expected_input = classification_input(
                row["file_hash"], row["detector_runtime"], subjects,
            )
            if row["input_fingerprint"] != expected_input:
                summary["skipped_incompatible_input"] += 1
                continue
            label_meta = db.conn.execute(
                """SELECT display_name, label_count
                   FROM labels_fingerprints WHERE fingerprint = ?""",
                (row["labels_fingerprint"],),
            ).fetchone()
            artifacts.append({
                "artifact_schema": ARTIFACT_SCHEMA,
                "type": "classification",
                "classifier_model": row["classifier_model"],
                "detector_model": row["detector_model"],
                "photo_sha256": row["file_hash"],
                "runtime_fingerprint": row["runtime_fingerprint"],
                "detector_runtime_fingerprint": row["detector_runtime"],
                "input_fingerprint": expected_input,
                "input": input_block,
                "labels": {
                    "fingerprint": row["labels_fingerprint_full"],
                    "short_fingerprint": row["labels_fingerprint"],
                    "display_name": label_meta["display_name"] if label_meta else None,
                    "count": label_meta["label_count"] if label_meta else None,
                },
                "completed": True,
                "subjects": subjects,
            })
            summary["classifier_runs"] += 1
    return artifacts, summary


def exportable_run_counts(db):
    """Cheap Settings-page counts without synthesizing every payload."""
    detector = db.conn.execute(
        """SELECT
             SUM(CASE WHEN length(p.file_hash) = 64
                           AND length(dr.runtime_fingerprint) = 64
                           AND length(dr.input_fingerprint) = 64
                      THEN 1 ELSE 0 END) AS portable,
             SUM(CASE WHEN p.file_hash IS NULL OR length(p.file_hash) != 64
                      THEN 1 ELSE 0 END) AS missing_hash,
             SUM(CASE WHEN dr.runtime_fingerprint = 'legacy'
                      THEN 1 ELSE 0 END) AS legacy
           FROM detector_runs dr JOIN photos p ON p.id = dr.photo_id"""
    ).fetchone()
    classifier = db.conn.execute(
        """SELECT
             SUM(CASE WHEN length(p.file_hash) = 64
                           AND length(cr.runtime_fingerprint) = 64
                           AND length(cr.input_fingerprint) = 64
                           AND length(cr.labels_fingerprint_full) = 64
                           AND length(d.runtime_fingerprint) = 64
                      THEN 1 ELSE 0 END) AS portable,
             SUM(CASE WHEN p.file_hash IS NULL OR length(p.file_hash) != 64
                      THEN 1 ELSE 0 END) AS missing_hash,
             SUM(CASE WHEN cr.runtime_fingerprint = 'legacy'
                           OR cr.labels_fingerprint_full IS NULL
                      THEN 1 ELSE 0 END) AS legacy
           FROM classifier_runs cr
           JOIN detections d ON d.id = cr.detection_id
           JOIN photos p ON p.id = d.photo_id"""
    ).fetchone()
    return {
        "detector_runs": detector["portable"] or 0,
        "classifier_runs": classifier["portable"] or 0,
        "skipped_legacy": (detector["legacy"] or 0) + (classifier["legacy"] or 0),
        "skipped_missing_hash": (
            (detector["missing_hash"] or 0)
            + (classifier["missing_hash"] or 0)
        ),
        "skipped_incompatible_input": 0,
    }


def _manual_review_exists(conn, detection_id, classifier_model, labels_short):
    row = conn.execute(
        """SELECT 1
           FROM predictions p
           JOIN prediction_review pr ON pr.prediction_id = p.id
           WHERE p.detection_id = ? AND p.classifier_model = ?
             AND p.labels_fingerprint = ?
             AND pr.status IN ('accepted', 'rejected')
             AND COALESCE(pr.individual, '') != '__vireo_auto_match__'
           LIMIT 1""",
        (detection_id, classifier_model, labels_short),
    ).fetchone()
    return row is not None


def _is_recognized_detector_runtime(detector_model, runtime, extra=None):
    """True when this Vireo install can identify the artifact's runtime.

    A bundle produced by a newer or foreign runtime carries a well-formed
    fingerprint that this catalog cannot describe or reproduce.  Installing
    such rows would let matching classification artifacts surface too, so
    materialization skips them until the local install recognizes the
    runtime (e.g. matching MegaDetector weights are downloaded).  The
    artifact itself remains in the object store and a later
    ``materialize_local_store`` picks it up once the runtime is known.

    ``extra`` may hold additional trusted runtime fingerprints — callers
    that already validated a runtime through some other channel (tests
    seeding fixtures, an install-time weight registry) pass them here so
    the built-in local check does not have to grow special cases.
    """
    if not _is_sha256(runtime):
        return False
    if extra and runtime in extra:
        return True
    if detector_model == "full-image":
        return runtime == full_image_runtime_fingerprint()
    if detector_model == "megadetector-v6":
        try:
            local = megadetector_runtime_fingerprint()
        except (ImportError, OSError, ValueError):
            # ImportError also fires when the `detector` module is not
            # installed on this instance — quarantine, don't propagate.
            return False
        return local is not None and runtime == local
    return False


def _local_classifier_runtimes(classifier_model, cache):
    """Yield locally-derivable classifier runtimes with matching model name.

    A classifier runtime encodes ``(model_identity, labels_fingerprint,
    detector_runtime)``.  Bundle artifacts carry the derived hash rather
    than the ingredients, so recognition asks: for this ``classifier_model``
    name, does any installed classifier produce the same hash for the
    same labels + detector runtime?  This helper returns just the model
    identities so the caller can plug in the artifact's own labels /
    detector runtime before comparing.

    ``cache`` is a mutable dict scoped to one materialize call — we hash
    each classifier only once regardless of how many artifacts reference
    it.
    """
    if classifier_model in cache:
        return cache[classifier_model]
    identities = []
    try:
        from models import get_models
    except ImportError:
        cache[classifier_model] = identities
        return identities
    try:
        installed = get_models()
    except Exception:
        cache[classifier_model] = identities
        return identities
    for model in installed:
        if not model.get("downloaded"):
            continue
        if model.get("name") != classifier_model:
            continue
        try:
            identity = classifier_model_identity(model)
        except (OSError, ValueError):
            identity = None
        if identity is not None:
            identities.append(identity)
    cache[classifier_model] = identities
    return identities


def _is_recognized_classifier_runtime(
    classifier_model,
    labels_fingerprint_full,
    detector_runtime,
    classifier_runtime,
    identity_cache,
    extra=None,
):
    """True when this install can reproduce the artifact's classifier runtime.

    A classification artifact carries only the derived
    ``runtime_fingerprint``.  To trust it, this install must be able to
    recreate that hash from its own installed classifier for the same
    labels + detector runtime.  A newer, foreign, or renamed classifier
    fails this test and its predictions are quarantined in the object
    store until a matching classifier lands.  Callers that already
    validated a runtime through some other channel (tests seeding
    fixtures, an active classify job that just resolved its own runtime)
    pass it via ``extra`` so the built-in check does not have to grow
    special cases.
    """
    if not _is_sha256(classifier_runtime):
        return False
    if extra and classifier_runtime in extra:
        return True
    if (
        not _is_sha256(labels_fingerprint_full)
        or not _is_sha256(detector_runtime)
    ):
        return False
    for identity in _local_classifier_runtimes(
        classifier_model, identity_cache,
    ):
        expected = classifier_runtime_fingerprint(
            identity, labels_fingerprint_full, detector_runtime,
        )
        if expected == classifier_runtime:
            return True
    return False


def materialize_artifacts(
    db, artifacts, known_runtimes=None, known_classifier_runtimes=None,
):
    """Apply portable output to every matching non-rejected catalog row.

    Review rows are never inserted.  Existing matching materializations are
    left alone, so a later artifact cannot churn already-surfaced results.

    ``known_runtimes`` extends the built-in whitelist of DETECTOR runtimes
    this install recognizes.  ``known_classifier_runtimes`` does the same
    for classifier runtimes — callers with additional trust context (a
    classify job that just resolved its own runtime) pass them here so
    the built-in local check does not have to grow special cases.
    """
    normalized = [validate_artifact(artifact) for artifact in artifacts]
    # Trusted detection artifacts for the same (photo, detector_model)
    # can carry multiple runtime_fingerprints -- e.g. the local store
    # was populated by successive imports from different weights.  Every
    # ``materialize_local_store`` call would otherwise write each of
    # them in turn, and each runtime change deletes the currently
    # selected detections and cascades their unreviewed predictions.
    # The result is churn on every reapply plus a settled winner of
    # whichever runtime happens to sort last.  Collapse to one artifact
    # per logical (photo, detector_model) run before the loop, preferring
    # whichever runtime the catalog already has installed so routine
    # cache reapplication is a no-op; ties fall back to a deterministic
    # runtime_fingerprint sort so repeated calls stay stable.
    detection_by_key = {}
    other_artifacts = []
    for artifact in normalized:
        if artifact["type"] != "detection":
            other_artifacts.append(artifact)
            continue
        key = (artifact["photo_sha256"], artifact["detector_model"])
        detection_by_key.setdefault(key, []).append(artifact)
    chosen_detections = []
    for (photo_sha256, detector_model), candidates in detection_by_key.items():
        if len(candidates) == 1:
            chosen_detections.append(candidates[0])
            continue
        existing = db.conn.execute(
            """SELECT dr.runtime_fingerprint
               FROM detector_runs dr
               JOIN photos p ON p.id = dr.photo_id
               WHERE p.file_hash = ? AND dr.detector_model = ?
                 AND p.companion_path IS NULL
                 AND p.working_copy_path IS NULL
                 AND (p.flag IS NULL OR p.flag != 'rejected')
               LIMIT 1""",
            (photo_sha256, detector_model),
        ).fetchone()
        existing_runtime = (
            existing["runtime_fingerprint"] if existing else None
        )
        match = next(
            (c for c in candidates
             if c["runtime_fingerprint"] == existing_runtime),
            None,
        )
        chosen_detections.append(
            match if match is not None
            else min(candidates, key=lambda a: a["runtime_fingerprint"])
        )
    normalized = chosen_detections + other_artifacts
    normalized.sort(key=lambda item: item["type"] != "detection")
    identity_cache = {}
    result = {
        "matched_photos": 0,
        "detector_runs_applied": 0,
        "classifier_runs_applied": 0,
        "already_materialized": 0,
        "stored_unmatched": 0,
        "pinned_older_runtime": 0,
        "label_collisions": 0,
        "unknown_runtime": 0,
        "unknown_classifier_runtime": 0,
        "classifier_deferred_pending_detection": 0,
    }
    matched_photo_ids = set()

    for artifact in normalized:
        # v1 artifacts carry only the original ``photo_sha256`` in their
        # input identity. When a destination photo has a
        # ``companion_path`` (RAW+JPEG), Vireo processes the companion
        # rendition, whose pixels can differ from the original even though
        # both photos share the RAW's ``file_hash``. Materializing the
        # original-only artifact onto that row would install detections
        # and classifications for the wrong rendition, silently replacing
        # locally correct results. Until an artifact variant declares
        # companion or working-copy identity, skip catalog rows backed by
        # either alternate rendition.
        photos = db.conn.execute(
            """SELECT id FROM photos
               WHERE file_hash = ? AND companion_path IS NULL
                 AND working_copy_path IS NULL
                 AND (flag IS NULL OR flag != 'rejected')
               ORDER BY id""",
            (artifact["photo_sha256"],),
        ).fetchall()
        if not photos:
            result["stored_unmatched"] += 1
            continue
        matched_photo_ids.update(row["id"] for row in photos)

        if artifact["type"] == "detection":
            if not _is_recognized_detector_runtime(
                artifact["detector_model"], artifact["runtime_fingerprint"],
                extra=known_runtimes,
            ):
                # Quarantine: keep the object in the store but don't plant a
                # detector_runs row this install cannot describe.  A future
                # materialize call after weights install will recognize it.
                result["unknown_runtime"] += 1
                continue
            detections = [{
                "box": subject["box"],
                "confidence": subject["confidence"],
                "category": subject["category"],
            } for subject in artifact["subjects"]]
            for photo in photos:
                photo_id = photo["id"]
                existing = db.conn.execute(
                    """SELECT runtime_fingerprint, input_fingerprint
                       FROM detector_runs
                       WHERE photo_id = ? AND detector_model = ?""",
                    (photo_id, artifact["detector_model"]),
                ).fetchone()
                if existing is not None and (
                    existing["runtime_fingerprint"] == artifact["runtime_fingerprint"]
                    and existing["input_fingerprint"] == artifact["input_fingerprint"]
                ):
                    result["already_materialized"] += 1
                    continue
                db.write_detection_batch(
                    photo_id,
                    artifact["detector_model"],
                    detections,
                    runtime_fingerprint=artifact["runtime_fingerprint"],
                    input_fingerprint=artifact["input_fingerprint"],
                )
                current = db.conn.execute(
                    """SELECT runtime_fingerprint, input_fingerprint
                       FROM detector_runs
                       WHERE photo_id = ? AND detector_model = ?""",
                    (photo_id, artifact["detector_model"]),
                ).fetchone()
                if (
                    current is None
                    or current["runtime_fingerprint"] != artifact["runtime_fingerprint"]
                    or current["input_fingerprint"] != artifact["input_fingerprint"]
                ):
                    result["pinned_older_runtime"] += 1
                else:
                    result["detector_runs_applied"] += 1
            continue

        labels = artifact["labels"]
        collision = db.conn.execute(
            """SELECT 1 FROM labels_fingerprints
               WHERE fingerprint = ? AND full_fingerprint IS NOT NULL
                 AND full_fingerprint != ?""",
            (labels["short_fingerprint"], labels["fingerprint"]),
        ).fetchone()
        if collision:
            result["label_collisions"] += 1
            continue

        # Quarantine classification artifacts whose classifier runtime
        # this install cannot reproduce.  The detector-runtime gate above
        # only proves this catalog can describe the source detections;
        # the classifier itself may still be a newer, foreign, or
        # renamed model whose predictions would silently surface as
        # authoritative results.  A future materialize call after the
        # matching classifier is installed will pick these up.
        if not _is_recognized_classifier_runtime(
            artifact["classifier_model"],
            labels["fingerprint"],
            artifact["detector_runtime_fingerprint"],
            artifact["runtime_fingerprint"],
            identity_cache,
            extra=known_classifier_runtimes,
        ):
            result["unknown_classifier_runtime"] += 1
            continue

        for photo in photos:
            photo_id = photo["id"]
            detector_run = db.conn.execute(
                """SELECT runtime_fingerprint FROM detector_runs
                   WHERE photo_id = ? AND detector_model = ?""",
                (photo_id, artifact["detector_model"]),
            ).fetchone()
            if (
                detector_run is None
                or detector_run["runtime_fingerprint"]
                != artifact["detector_runtime_fingerprint"]
            ):
                # Detector dependency not yet available on this install.
                # A follow-up materialize call after detection runs (or
                # after the detector's own artifacts are recognized) will
                # pick these up.
                result["classifier_deferred_pending_detection"] += 1
                continue
            from detection_id import detection_id as compute_detection_id
            from keyword_normalization import normalize_keyword_display

            try:
                db.conn.execute(
                    """INSERT INTO labels_fingerprints
                         (fingerprint, full_fingerprint, display_name,
                          sources_json, label_count)
                       VALUES (?, ?, ?, '[]', ?)
                       ON CONFLICT(fingerprint) DO UPDATE SET
                         full_fingerprint = COALESCE(
                             excluded.full_fingerprint,
                             labels_fingerprints.full_fingerprint
                         ),
                         display_name = COALESCE(excluded.display_name, display_name),
                         label_count = COALESCE(excluded.label_count, label_count)""",
                    (
                        labels["short_fingerprint"], labels["fingerprint"],
                        labels.get("display_name"), labels.get("count"),
                    ),
                )
                applied_subjects = 0
                for subject in artifact["subjects"]:
                    if subject["kind"] == "full_image":
                        detection = db.conn.execute(
                            """SELECT id FROM detections
                               WHERE photo_id = ? AND detector_model = 'full-image'
                               ORDER BY id LIMIT 1""",
                            (photo_id,),
                        ).fetchone()
                        detection_id = detection["id"] if detection else None
                    else:
                        box = subject["box"]
                        detection_id = compute_detection_id(
                            photo_id,
                            artifact["detector_model"],
                            (box["x"], box["y"], box["w"], box["h"]),
                            subject.get("category", "animal"),
                        )
                        detection = db.conn.execute(
                            "SELECT 1 FROM detections WHERE id = ?",
                            (detection_id,),
                        ).fetchone()
                        if detection is None:
                            detection_id = None
                    if detection_id is None:
                        continue
                    prior = db.conn.execute(
                        """SELECT runtime_fingerprint, input_fingerprint
                           FROM classifier_runs
                           WHERE detection_id = ? AND classifier_model = ?
                             AND labels_fingerprint = ?""",
                        (
                            detection_id, artifact["classifier_model"],
                            labels["short_fingerprint"],
                        ),
                    ).fetchone()
                    if prior is not None and (
                        prior["runtime_fingerprint"] == artifact["runtime_fingerprint"]
                        and prior["input_fingerprint"] == artifact["input_fingerprint"]
                    ):
                        result["already_materialized"] += 1
                        continue
                    if prior is not None and _manual_review_exists(
                        db.conn, detection_id, artifact["classifier_model"],
                        labels["short_fingerprint"],
                    ):
                        result["pinned_older_runtime"] += 1
                        continue
                    if prior is not None:
                        db.conn.execute(
                            """DELETE FROM predictions
                               WHERE detection_id = ? AND classifier_model = ?
                                 AND labels_fingerprint = ?""",
                            (
                                detection_id, artifact["classifier_model"],
                                labels["short_fingerprint"],
                            ),
                        )
                    for candidate in subject["candidates"]:
                        taxonomy = candidate.get("taxonomy") or {}
                        species = normalize_keyword_display(candidate["species"])
                        db.conn.execute(
                            """INSERT OR IGNORE INTO predictions
                                 (detection_id, classifier_model,
                                  labels_fingerprint, labels_fingerprint_full,
                                  species, confidence, category, scientific_name,
                                  taxonomy_kingdom, taxonomy_phylum,
                                  taxonomy_class, taxonomy_order,
                                  taxonomy_family, taxonomy_genus)
                               VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                detection_id, artifact["classifier_model"],
                                labels["short_fingerprint"], labels["fingerprint"],
                                species, candidate["confidence"],
                                taxonomy.get("scientific_name"),
                                taxonomy.get("kingdom"), taxonomy.get("phylum"),
                                taxonomy.get("class"), taxonomy.get("order"),
                                taxonomy.get("family"), taxonomy.get("genus"),
                            ),
                        )
                    db.conn.execute(
                        """INSERT INTO classifier_runs
                             (detection_id, classifier_model, labels_fingerprint,
                              labels_fingerprint_full, runtime_fingerprint,
                              input_fingerprint, prediction_count)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(detection_id, classifier_model,
                                       labels_fingerprint)
                           DO UPDATE SET
                             labels_fingerprint_full = excluded.labels_fingerprint_full,
                             runtime_fingerprint = excluded.runtime_fingerprint,
                             input_fingerprint = excluded.input_fingerprint,
                             prediction_count = excluded.prediction_count,
                             run_at = datetime('now')""",
                        (
                            detection_id, artifact["classifier_model"],
                            labels["short_fingerprint"], labels["fingerprint"],
                            artifact["runtime_fingerprint"],
                            artifact["input_fingerprint"],
                            len(subject["candidates"]),
                        ),
                    )
                    applied_subjects += 1
                db.conn.commit()
                result["classifier_runs_applied"] += applied_subjects
            except Exception:
                db.conn.rollback()
                raise
    result["matched_photos"] = len(matched_photo_ids)
    return result


def materialize_local_store(
    db, store=None, known_runtimes=None, known_classifier_runtimes=None,
):
    """Apply stored objects that match the catalog's current photo hashes.

    Runtime fingerprints previously recorded via
    ``ArtifactStore.record_trusted_runtimes`` are unioned into the
    ``known_runtimes`` / ``known_classifier_runtimes`` sets so that
    artifacts whose photos were cataloged after their import bundle
    landed still plant on the next call — the import-time whitelist was
    scoped to one HTTP request and could not reach the later reapply.
    """
    store = store or ArtifactStore()
    persisted_det, persisted_clf = store.trusted_runtimes()
    if persisted_det:
        known_runtimes = set(known_runtimes or ()) | persisted_det
    if persisted_clf:
        known_classifier_runtimes = (
            set(known_classifier_runtimes or ()) | persisted_clf
        )
    artifacts = [artifact for _digest, artifact in (store.iter_artifacts() or ())]
    if not artifacts:
        return {
            "matched_photos": 0,
            "detector_runs_applied": 0,
            "classifier_runs_applied": 0,
        }
    return materialize_artifacts(
        db, artifacts,
        known_runtimes=known_runtimes,
        known_classifier_runtimes=known_classifier_runtimes,
    )
