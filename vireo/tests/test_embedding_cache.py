"""Concurrency and correctness tests for the label embedding cache."""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from embedding_cache import (
    EmbeddingCache,
    EmbeddingWaitCancelled,
    build_embedding_identity,
    canonicalize_labels,
    identity_digest,
)


def _identity(labels=("bird", "cat"), suffix="a"):
    return {
        "embedding_schema": 2,
        "model_runtime": {
            "family": "test",
            "model_str": f"model-{suffix}",
        },
        "labels": list(labels),
    }


def _payload(label_count=2, value=1):
    return np.full((4, label_count), value, dtype=np.float32)


def test_canonical_labels_match_the_values_sent_to_encoder():
    assert canonicalize_labels([" bird ", "Cat"]) == ["bird", "Cat"]
    with pytest.raises(ValueError, match="empty"):
        canonicalize_labels(["bird", "  "])


def test_complete_identity_changes_with_each_text_side_input(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "text_encoder.onnx").write_bytes(b"weights-a")
    (model_dir / "text_encoder.onnx.data").write_bytes(b"external-a")
    (model_dir / "tokenizer.json").write_bytes(b"tokenizer-a")

    def build(**overrides):
        args = {
            "labels": [" bird ", "Cat"],
            "model_str": "model-a",
            "model_dir": str(model_dir),
            "prompt_template_identity": "prompts-a",
            "tokenizer_context_length": 77,
        }
        args.update(overrides)
        return build_embedding_identity(**args)

    baseline = identity_digest(build())
    assert baseline == identity_digest(build(labels=["bird", "Cat"]))
    assert baseline != identity_digest(build(labels=["Cat", "bird"]))
    assert baseline != identity_digest(build(prompt_template_identity="prompts-b"))
    assert baseline != identity_digest(build(tokenizer_context_length=76))

    (model_dir / "tokenizer.json").write_bytes(b"tokenizer-b")
    assert baseline != identity_digest(build())
    (model_dir / "tokenizer.json").write_bytes(b"tokenizer-a")
    (model_dir / "text_encoder.onnx.data").write_bytes(b"external-b")
    assert baseline != identity_digest(build())


def test_pinned_install_does_not_rehash_large_encoder_files(
    tmp_path, monkeypatch,
):
    import embedding_cache

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / ".hf_revision").write_text("immutable-commit")
    (model_dir / "text_encoder.onnx").write_bytes(b"onnx")
    (model_dir / "text_encoder.onnx.data").write_bytes(b"external")
    (model_dir / "tokenizer.json").write_bytes(b"tokenizer")
    hashed = []
    original = embedding_cache._sha256_file

    def record(path):
        hashed.append(os.path.basename(path))
        return original(path)

    monkeypatch.setattr(embedding_cache, "_sha256_file", record)
    build_embedding_identity(
        ["bird"],
        "model-a",
        str(model_dir),
        prompt_template_identity="prompts-a",
        tokenizer_context_length=77,
    )

    assert hashed == ["tokenizer.json"]


def test_equal_key_callers_compute_once_and_waiter_rereads_disk(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    producer_started = threading.Event()
    release_producer = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def compute():
        nonlocal calls
        with calls_lock:
            calls += 1
        producer_started.set()
        assert release_producer.wait(2)
        return _payload()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get_or_compute, identity, compute)
        assert producer_started.wait(2)
        second = pool.submit(cache.get_or_compute, identity, compute)
        release_producer.set()
        first_value, _ = first.result(timeout=2)
        second_value, _ = second.result(timeout=2)

    assert calls == 1
    assert np.array_equal(first_value, second_value)
    assert first_value is not second_value


def test_producer_cancellation_wakes_waiter_and_waiter_takes_over(tmp_path):
    class ClassificationCancelled(RuntimeError):
        pass

    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    producer_started = threading.Event()
    cancel_producer = threading.Event()
    waiter_computed = threading.Event()

    def cancelled_compute():
        producer_started.set()
        assert cancel_producer.wait(2)
        raise ClassificationCancelled("cancelled")

    def healthy_compute():
        waiter_computed.set()
        return _payload(value=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(
            cache.get_or_compute, identity, cancelled_compute
        )
        assert producer_started.wait(2)
        waiter = pool.submit(cache.get_or_compute, identity, healthy_compute)
        cancel_producer.set()
        with pytest.raises(ClassificationCancelled):
            producer.result(timeout=2)
        value, _ = waiter.result(timeout=2)

    assert waiter_computed.is_set()
    assert np.array_equal(value, _payload(value=2))


def test_cancelled_waiter_does_not_cancel_shared_producer(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    producer_started = threading.Event()
    release_producer = threading.Event()
    cancel_waiter = threading.Event()

    def compute():
        producer_started.set()
        assert release_producer.wait(2)
        return _payload()

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(cache.get_or_compute, identity, compute)
        assert producer_started.wait(2)
        waiter = pool.submit(
            cache.get_or_compute,
            identity,
            lambda: pytest.fail("waiter must not compute"),
            cancel_check=cancel_waiter.is_set,
        )
        cancel_waiter.set()
        with pytest.raises(EmbeddingWaitCancelled):
            waiter.result(timeout=2)
        release_producer.set()
        value, _ = producer.result(timeout=2)

    assert np.array_equal(value, _payload())


def test_mismatched_embedding_dim_rejects_cache_hit(tmp_path):
    """Regression: a malformed cache file whose feature axis does not match
    the model's expected embedding dim used to pass rank + label-axis
    validation and load, then blow up at inference on
    ``img_features @ txt_embeddings``. Passing ``embedding_dim`` must
    reject that file so the caller falls back to recomputing."""
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()

    # Publish a malformed payload — shape (1, 2) instead of (512, 2).
    malformed = np.ones((1, 2), dtype=np.float32)
    cache.get_or_compute(identity, lambda: malformed)

    # A cache-hit check that knows the expected dim must reject it.
    assert not cache.is_cached(identity, 2, embedding_dim=512)
    # And a fresh get_or_compute with the right dim must recompute rather
    # than serve the malformed file.
    computed = np.ones((512, 2), dtype=np.float32)
    value, _ = cache.get_or_compute(
        identity, lambda: computed, embedding_dim=512,
    )
    assert value.shape == (512, 2)
    assert cache.is_cached(identity, 2, embedding_dim=512)


def test_cancel_triggered_retry_still_enforces_embedding_dim(tmp_path):
    """Regression: the recursive retry a waiter runs after an equal-key
    producer is cancelled must carry ``embedding_dim`` forward. Dropping
    it lets the replacement computation publish a mismatched payload that
    would only fail at inference on ``img_features @ txt_embeddings``."""
    class ClassificationCancelled(RuntimeError):
        pass

    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    producer_started = threading.Event()
    cancel_producer = threading.Event()

    def cancelled_compute():
        producer_started.set()
        assert cancel_producer.wait(2)
        raise ClassificationCancelled("cancelled")

    def wrong_dim_compute():
        # A 512-dim model receiving a 256-dim payload — the exact scenario
        # the fresh-computation validator catches on the primary path.
        return np.ones((256, 2), dtype=np.float32)

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(
            cache.get_or_compute, identity, cancelled_compute,
        )
        assert producer_started.wait(2)
        waiter = pool.submit(
            cache.get_or_compute,
            identity,
            wrong_dim_compute,
            embedding_dim=512,
        )
        cancel_producer.set()
        with pytest.raises(ClassificationCancelled):
            producer.result(timeout=2)
        with pytest.raises(ValueError, match=r"expected \(512, 2\)"):
            waiter.result(timeout=2)

    assert not os.path.exists(cache.path_for(identity))


def test_mismatched_embedding_dim_rejects_freshly_computed_payload(tmp_path):
    """A producer whose factory returns a wrong-dim ndarray must fail up
    front rather than publish a payload that later callers must eventually
    reject."""
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()

    with pytest.raises(ValueError, match=r"expected \(512, 2\)"):
        cache.get_or_compute(
            identity,
            lambda: np.ones((256, 2), dtype=np.float32),
            embedding_dim=512,
        )

    assert not os.path.exists(cache.path_for(identity))


def test_invalid_payload_never_replaces_final_path(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()

    with pytest.raises(ValueError, match="dtype"):
        cache.get_or_compute(
            identity,
            lambda: np.ones((4, 2), dtype=np.float64),
        )

    assert not os.path.exists(cache.path_for(identity))
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_competing_publisher_does_not_fail_our_publish(tmp_path, monkeypatch):
    """Regression: publish used to digest the temp file, os.replace, then
    re-read the *final* path and compare. Single-flight is in-process only,
    so a second process publishing the same identity can land its replace in
    that window. Its payload is equally valid but not bit-identical (the
    encoder is not bitwise reproducible), so we raised a hard
    ValueError over a perfectly good cache entry. Integrity must be checked
    on the temp file before the rename instead."""
    import embedding_cache as ec

    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    competitor = _payload(value=7)
    real_replace = os.replace

    def replace_then_race(src, dst):
        real_replace(src, dst)
        if str(dst).endswith(".npy"):
            # Stand in for another process winning the same rename just after
            # us. Leave the manifest's own os.replace alone.
            np.save(dst, competitor, allow_pickle=False)

    monkeypatch.setattr(ec.os, "replace", replace_then_race)

    value, _ = cache.get_or_compute(identity, _payload)

    # Our own call returns without raising, and the durable entry is the
    # competitor's valid payload rather than a deleted/corrupt file.
    assert np.array_equal(value, _payload())
    assert np.array_equal(np.load(cache.path_for(identity)), competitor)


def test_publish_rejects_bytes_that_do_not_round_trip(tmp_path, monkeypatch):
    """The pre-rename check must still catch a write that did not persist
    what we serialized — and must not leave a partial file behind."""
    import embedding_cache as ec

    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()

    real_load = np.load

    def corrupt_load(path, *args, **kwargs):
        if str(path).endswith(".npy.tmp"):
            # Serialized fine, came back as something else.
            return _payload(value=99)
        return real_load(path, *args, **kwargs)

    monkeypatch.setattr(ec.np, "load", corrupt_load)

    with pytest.raises(ValueError, match="changed during write"):
        cache.get_or_compute(identity, _payload)

    assert not os.path.exists(cache.path_for(identity))
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_manifest_failure_does_not_invalidate_published_payload(
    tmp_path, monkeypatch,
):
    cache = EmbeddingCache(tmp_path / "cache")
    identity = _identity()
    monkeypatch.setattr(
        cache,
        "_update_manifest",
        lambda *_args: (_ for _ in ()).throw(OSError("read-only manifest")),
    )

    value, _ = cache.get_or_compute(identity, _payload)

    assert np.array_equal(value, _payload())
    assert cache.is_cached(identity, 2)


def test_concurrent_manifest_updates_do_not_lose_entries(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache")
    identities = [_identity(suffix=str(i)) for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(cache.get_or_compute, identity, _payload)
            for identity in identities
        ]
        for future in futures:
            future.result(timeout=3)

    with open(tmp_path / "cache" / "manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert set(manifest) == {
        f"{identity_digest(identity)}.npy" for identity in identities
    }
