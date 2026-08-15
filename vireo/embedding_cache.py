"""Correct, process-wide cache for BioCLIP label embeddings.

The cache key describes the exact text-side inputs.  Cache misses are
single-flight within the Vireo process: one caller computes and atomically
publishes the payload while callers for the same key wait without loading a
second text encoder.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

EMBEDDING_SCHEMA_VERSION = 2

_identity_lock = threading.Lock()
_manifest_lock = threading.Lock()
_flights_lock = threading.Lock()


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    published_digest: str | None = None


_flights: dict[tuple[str, str], _Flight] = {}


class EmbeddingComputationError(RuntimeError):
    """An equal-key producer failed before publishing an embedding."""


class EmbeddingWaitCancelled(RuntimeError):
    """A caller stopped waiting without cancelling the shared producer."""


def canonicalize_labels(labels):
    """Return the exact labels sent to the encoder.

    Order and case are authoritative.  Deduplication belongs to the label-set
    loader, not the cache, because changing either would change classifier
    output semantics.
    """
    canonical = []
    for label in labels:
        if not isinstance(label, str):
            raise TypeError("labels must contain only strings")
        value = label.strip()
        if not value:
            raise ValueError("labels must not contain empty entries")
        canonical.append(value)
    if not canonical:
        raise ValueError("labels list must not be empty")
    return canonical


def _sha256_file(path):
    # computation_cache.sha256_file memoizes by path/size/mtime.  The lock
    # prevents simultaneous first callers from both streaming a multi-GB ONNX
    # external-data file merely to construct the same cache identity.
    from computation_cache import sha256_file

    with _identity_lock:
        return sha256_file(path)


def _file_identity(path, required=True, allow_missing=False):
    if not os.path.isfile(path):
        if not required:
            return None
        if allow_missing:
            return {"name": os.path.basename(path), "missing": True}
        raise FileNotFoundError(path)
    return {
        "name": os.path.basename(path),
        "sha256": _sha256_file(path),
    }


def _pinned_revision(model_dir):
    path = os.path.join(model_dir, ".hf_revision")
    try:
        with open(path, encoding="utf-8") as handle:
            revision = handle.read().strip()
    except OSError:
        return None
    return revision or None


def _revision_file_identity(path, revision, required=True, allow_missing=False):
    if not os.path.isfile(path):
        if not required:
            return None
        if allow_missing:
            return {"name": os.path.basename(path), "missing": True}
        raise FileNotFoundError(path)
    stat = os.stat(path)
    return {
        "name": os.path.basename(path),
        "immutable_upstream_revision": revision,
        "size": stat.st_size,
    }


def build_embedding_identity(
    labels,
    model_str,
    model_dir,
    *,
    prompt_template_identity,
    tokenizer_context_length,
    allow_missing=False,
):
    """Build the complete, path-independent identity for an embedding set."""
    canonical_labels = canonicalize_labels(labels)
    text_encoder = os.path.join(model_dir, "text_encoder.onnx")
    external_data = os.path.join(model_dir, "text_encoder.onnx.data")
    tokenizer = os.path.join(model_dir, "tokenizer.json")
    revision = _pinned_revision(model_dir)
    if revision:
        text_onnx_identity = _revision_file_identity(
            text_encoder, revision, allow_missing=allow_missing
        )
        external_data_identity = _revision_file_identity(
            external_data,
            revision,
            required=False,
            allow_missing=allow_missing,
        )
    else:
        # Legacy and custom installs have no immutable upstream promise, so
        # bind the cache to their exact text-encoder bytes.
        text_onnx_identity = _file_identity(
            text_encoder, allow_missing=allow_missing
        )
        external_data_identity = _file_identity(
            external_data, required=False, allow_missing=allow_missing
        )

    return {
        "embedding_schema": EMBEDDING_SCHEMA_VERSION,
        "model_runtime": {
            "family": "bioclip-onnx-text",
            "model_str": model_str,
        },
        "text_encoder": {
            "onnx": text_onnx_identity,
            "external_data": external_data_identity,
        },
        "tokenizer": {
            "file": _file_identity(tokenizer, allow_missing=allow_missing),
            "context_length": tokenizer_context_length,
            "padding": "right-zero",
            "truncation": "right",
        },
        "prompt_template_set": prompt_template_identity,
        "labels": canonical_labels,
    }


def identity_digest(identity):
    body = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def cache_path(cache_dir, identity):
    return os.path.join(cache_dir, f"{identity_digest(identity)}.npy")


def _payload_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_payload(value, label_count):
    if not isinstance(value, np.ndarray):
        raise ValueError("embedding payload is not a NumPy array")
    if value.ndim != 2 or value.shape[1] != label_count:
        raise ValueError(
            "embedding payload has shape "
            f"{value.shape}; expected (embedding_dim, {label_count})"
        )
    if value.dtype != np.float32:
        raise ValueError(
            f"embedding payload has dtype {value.dtype}; expected float32"
        )
    if not np.isfinite(value).all():
        raise ValueError("embedding payload contains non-finite values")
    return value


class EmbeddingCache:
    """Durable embedding cache with per-key in-process single-flight."""

    def __init__(self, cache_dir, manifest_path=None):
        self.cache_dir = os.path.abspath(os.path.expanduser(os.fspath(cache_dir)))
        if manifest_path:
            self.manifest_path = os.path.abspath(
                os.path.expanduser(os.fspath(manifest_path))
            )
        else:
            self.manifest_path = os.path.join(self.cache_dir, "manifest.json")

    def path_for(self, identity):
        return cache_path(self.cache_dir, identity)

    def is_cached(self, identity, label_count):
        try:
            self._load(identity_digest(identity), label_count)
            return True
        except (EOFError, OSError, ValueError):
            return False

    def get_or_compute(
        self,
        identity,
        compute,
        *,
        identity_after=None,
        cancel_check=None,
    ):
        """Load or compute one embedding payload.

        ``identity_after`` is evaluated after computation because ONNX
        self-healing can replace the model directory while the producer is
        loading the text encoder.  In that case the payload is published under
        the healed identity and waiters follow the producer to that durable
        path.
        """
        label_count = len(identity["labels"])
        initial_digest = identity_digest(identity)

        if cancel_check and cancel_check():
            raise EmbeddingWaitCancelled("classification cancelled")
        try:
            value = self._load(initial_digest, label_count)
            if cancel_check and cancel_check():
                raise EmbeddingWaitCancelled("classification cancelled")
            return value, identity
        except (EOFError, OSError, ValueError):
            pass

        flight_key = (self.cache_dir, initial_digest)
        with _flights_lock:
            flight = _flights.get(flight_key)
            if flight is None:
                flight = _Flight()
                _flights[flight_key] = flight
                producer = True
            else:
                producer = False

        if not producer:
            log.info("EmbeddingCache: joining producer key=%s", initial_digest[:12])
            while not flight.event.wait(0.1):
                if cancel_check and cancel_check():
                    raise EmbeddingWaitCancelled("classification cancelled")
            if cancel_check and cancel_check():
                raise EmbeddingWaitCancelled("classification cancelled")
            if flight.error is not None:
                if flight.error.__class__.__name__ in {
                    "ClassificationCancelled", "EmbeddingWaitCancelled"
                }:
                    # Cancellation belongs to the producer, not the key. A
                    # healthy waiter retries and one of the waiters becomes
                    # the replacement producer.
                    return self.get_or_compute(
                        identity,
                        compute,
                        identity_after=identity_after,
                        cancel_check=cancel_check,
                    )
                raise EmbeddingComputationError(
                    "label embedding producer failed"
                ) from flight.error
            published_digest = flight.published_digest or initial_digest
            # Durable publication is the hand-off contract.  Do not reuse the
            # producer's mutable ndarray in memory.
            value = self._load(published_digest, label_count)
            actual_identity = identity_after() if identity_after else identity
            return value, actual_identity

        try:
            if cancel_check and cancel_check():
                raise EmbeddingWaitCancelled("classification cancelled")
            log.info("EmbeddingCache: producing key=%s", initial_digest[:12])
            value = _validate_payload(compute(), label_count)
            if cancel_check and cancel_check():
                raise EmbeddingWaitCancelled("classification cancelled")
            actual_identity = identity_after() if identity_after else identity
            actual_digest = identity_digest(actual_identity)
            self._publish(actual_digest, value, label_count)
            try:
                self._update_manifest(actual_digest, actual_identity, value)
            except Exception:
                # The manifest is explanatory metadata, never cache validity.
                # Publication has already committed a complete payload.
                log.exception(
                    "EmbeddingCache: manifest update failed key=%s",
                    actual_digest[:12],
                )
            flight.published_digest = actual_digest
            log.info("EmbeddingCache: published key=%s", actual_digest[:12])
            return value, actual_identity
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            # Remove only the registry lookup before waking waiters. Existing
            # waiters retain the _Flight object, while a retry after producer
            # cancellation can immediately claim a fresh flight.
            with _flights_lock:
                if _flights.get(flight_key) is flight:
                    del _flights[flight_key]
            flight.event.set()

    def _load(self, digest, label_count):
        path = os.path.join(self.cache_dir, f"{digest}.npy")
        try:
            value = np.load(path, allow_pickle=False)
            return _validate_payload(value, label_count)
        except (EOFError, ValueError):
            # A truncated or otherwise invalid final file cannot satisfy later
            # callers.  Removal is best-effort; a racing repair can replace it.
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise

    def _publish(self, digest, value, label_count):
        os.makedirs(self.cache_dir, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".npy.tmp", dir=self.cache_dir
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                np.save(handle, value, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            loaded = np.load(temporary, allow_pickle=False)
            _validate_payload(loaded, label_count)
            before = _payload_digest(temporary)
            os.replace(temporary, os.path.join(self.cache_dir, f"{digest}.npy"))
            final_path = os.path.join(self.cache_dir, f"{digest}.npy")
            if _payload_digest(final_path) != before:
                raise ValueError("embedding payload digest changed during publish")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def _update_manifest(self, digest, identity, value):
        identity_metadata = {
            key: item for key, item in identity.items() if key != "labels"
        }
        metadata = {
            "model": identity["model_runtime"]["model_str"],
            "label_count": len(identity["labels"]),
            "labels_sha256": identity_digest({"labels": identity["labels"]}),
            "embedding_dim": value.shape[0],
            "dtype": str(value.dtype),
            "created": datetime.now().isoformat(timespec="seconds"),
            "identity": identity_metadata,
        }
        with _manifest_lock:
            manifest = {}
            try:
                with open(self.manifest_path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, ValueError):
                pass
            manifest[f"{digest}.npy"] = metadata
            os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".manifest.", suffix=".json.tmp",
                dir=os.path.dirname(self.manifest_path),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.manifest_path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
