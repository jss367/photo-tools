"""Shared construction path for process-wide classifier session reuse."""

import hashlib
import json
import os

from embedding_cache import canonicalize_labels
from model_cache import get_default_cache


def _ordered_labels_identity(labels):
    if labels is None:
        return "__tol__"
    canonical = canonicalize_labels(labels)
    body = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def model_files_fingerprint(weights_path, files=None):
    """Cheap identity for in-process invalidation after model replacement."""
    if not weights_path:
        return None
    if files:
        names = sorted(files)
    elif os.path.isdir(weights_path):
        names = sorted(
            name for name in os.listdir(weights_path)
            if os.path.isfile(os.path.join(weights_path, name))
        )
    elif os.path.isfile(weights_path):
        names = ["__file__"]
    else:
        return None

    parts = []
    for name in names:
        path = weights_path if name == "__file__" else os.path.join(
            weights_path, name
        )
        try:
            stat = os.stat(path)
            parts.append((name, stat.st_size, int(stat.st_mtime_ns)))
        except OSError:
            parts.append((name, None, None))
    return tuple(parts)


def classifier_cache_key(
    *,
    model_type,
    model_str,
    weights_path,
    labels,
    files=None,
    taxonomy_fingerprint=None,
):
    return (
        "classifier-v2",
        model_type,
        model_str,
        os.path.abspath(weights_path) if weights_path else None,
        _ordered_labels_identity(labels),
        taxonomy_fingerprint,
        model_files_fingerprint(weights_path, files),
    )


def acquire_cached_classifier(
    *,
    model_type,
    model_str,
    weights_path,
    labels,
    factory,
    files=None,
    taxonomy_fingerprint=None,
    cancel_check=None,
):
    """Acquire a refcounted classifier, rekeying after in-place self-heal.

    ``cancel_check`` propagates the caller's cancellation signal down to the
    shared-cache waiter loop so a job blocked here while another caller's
    factory is loading a text encoder or computing label embeddings can
    still cancel promptly.
    """

    def _key():
        return classifier_cache_key(
            model_type=model_type,
            model_str=model_str,
            weights_path=weights_path,
            labels=labels,
            files=files,
            taxonomy_fingerprint=taxonomy_fingerprint,
        )

    return get_default_cache().acquire(
        _key(),
        factory,
        post_load_key=lambda _value: _key(),
        cancel_check=cancel_check,
    )
