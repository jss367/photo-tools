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


def model_files_fingerprint(weights_path, files=None, optional_files=None):
    """Cheap identity for in-process invalidation after model replacement.

    ``optional_files`` — filenames declared as ``optional_files`` in the
    model's KNOWN_MODELS entry (best-effort downloads, e.g. timm's
    ``label_descriptions.json`` or bioclip-2.5's ToL artifacts). Only
    those optional files present on disk right now are folded into the
    fingerprint. That way a Repair that lands a previously absent
    optional file flips the fingerprint and prevents the pre-repair
    cached classifier — which was constructed without that artifact and
    therefore emits different labels — from being reused.
    """
    if not weights_path:
        return None
    if files:
        names = set(files)
        if optional_files and os.path.isdir(weights_path):
            for name in optional_files:
                if name in names:
                    continue
                if os.path.isfile(os.path.join(weights_path, name)):
                    names.add(name)
        names = sorted(names)
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
    optional_files=None,
    taxonomy_fingerprint=None,
):
    return (
        "classifier-v2",
        model_type,
        model_str,
        os.path.abspath(weights_path) if weights_path else None,
        _ordered_labels_identity(labels),
        taxonomy_fingerprint,
        model_files_fingerprint(weights_path, files, optional_files),
    )


def acquire_cached_classifier(
    *,
    model_type,
    model_str,
    weights_path,
    labels,
    factory,
    files=None,
    optional_files=None,
    taxonomy_fingerprint=None,
    cancel_check=None,
):
    """Acquire a refcounted classifier, rekeying after in-place self-heal.

    ``cancel_check`` propagates the caller's cancellation signal down to the
    shared-cache waiter loop so a job blocked here while another caller's
    factory is loading a text encoder or computing label embeddings can
    still cancel promptly.

    ``optional_files`` — best-effort declared artifacts (see
    ``model_files_fingerprint``). Callers that pass an explicit ``files=``
    list (which typically covers only the required manifest) should also
    pass ``optional_files=`` so a Repair that fills in a previously absent
    optional artifact invalidates the pre-repair classifier entry.
    """

    def _key():
        return classifier_cache_key(
            model_type=model_type,
            model_str=model_str,
            weights_path=weights_path,
            labels=labels,
            files=files,
            optional_files=optional_files,
            taxonomy_fingerprint=taxonomy_fingerprint,
        )

    return get_default_cache().acquire(
        _key(),
        factory,
        post_load_key=lambda _value: _key(),
        cancel_check=cancel_check,
    )
