"""Keep production Classifier construction behind the shared model cache."""

import ast
from pathlib import Path

from classifier_cache import (
    acquire_cached_classifier,
    classifier_cache_key,
    model_files_fingerprint,
)
from model_cache import reset_default_cache_for_tests


def _callee_name(node):
    """Return the called name for ``f(...)`` and ``mod.f(...)`` alike."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _factory_arguments(tree):
    """Collect every AST node passed as ``factory=`` to the cache acquirer.

    Only calls whose callee is ``acquire_cached_classifier`` count. A
    ``factory=`` keyword on some unrelated call does not satisfy the
    contract, so an inline ``Classifier(...)`` cannot be laundered through a
    lookalike helper.
    """
    factories = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node) != "acquire_cached_classifier":
            continue
        for keyword in node.keywords:
            if keyword.arg == "factory":
                factories.append(keyword.value)
    return factories


def test_production_classifier_calls_are_cache_factories():
    vireo_dir = Path(__file__).resolve().parents[1]
    violations = []

    for path in vireo_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        factories = _factory_arguments(tree)
        # Lambdas are matched by identity; named functions by the name the
        # acquirer was actually handed.
        factory_lambdas = {
            id(value) for value in factories if isinstance(value, ast.Lambda)
        }
        factory_names = {
            value.id for value in factories if isinstance(value, ast.Name)
        }

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Classifier"
            ):
                continue
            current = parents.get(node)
            routed = False
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # A def only counts when this module hands that exact name
                    # to acquire_cached_classifier as its factory.
                    routed = current.name in factory_names
                    break
                if isinstance(current, ast.Lambda):
                    routed = id(current) in factory_lambdas
                    break
                current = parents.get(current)
            if not routed:
                violations.append(f"{path.name}:{node.lineno}")

    assert not violations, (
        "Classifier construction must be a factory passed to "
        f"acquire_cached_classifier: {', '.join(violations)}"
    )


def test_contract_rejects_a_factory_keyword_on_an_unrelated_call():
    """The scan must not accept any function merely named
    ``_construct_classifier``, nor any ``factory=`` keyword on some other
    call. Both were previously enough to pass."""
    source = """
def _construct_classifier():
    return Classifier(labels=[])

some_other_helper(factory=lambda: Classifier(labels=[]))
"""
    tree = ast.parse(source)
    assert _factory_arguments(tree) == []


def test_contract_accepts_named_and_lambda_factories_on_the_acquirer():
    source = """
def _construct_classifier():
    return Classifier(labels=[])

acquire_cached_classifier(model_str="m", factory=_construct_classifier)
acquire_cached_classifier(model_str="m", factory=lambda: Classifier(labels=[]))
"""
    tree = ast.parse(source)
    factories = _factory_arguments(tree)
    assert len(factories) == 2
    assert isinstance(factories[0], ast.Name)
    assert factories[0].id == "_construct_classifier"
    assert isinstance(factories[1], ast.Lambda)


def test_optional_files_flip_fingerprint_when_they_appear_on_disk(tmp_path):
    """Regression: a Repair that downloads a declared-optional artifact
    (e.g. timm's ``label_descriptions.json``, or bioclip-2.5's ToL files)
    must invalidate any pre-repair cache entry. Without folding the
    optional artifacts into ``files=`` — the two production sites pass
    only the *required* manifest — the fingerprint would not change and
    the pre-repair classifier (constructed without the artifact and
    therefore emitting different labels) would keep being reused."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "text_encoder.onnx").write_bytes(b"weights")
    (model_dir / "config.json").write_bytes(b"config")

    required = ["text_encoder.onnx", "config.json"]
    optional = ["label_descriptions.json"]

    before = model_files_fingerprint(
        str(model_dir), files=required, optional_files=optional,
    )
    key_before = classifier_cache_key(
        model_type="timm",
        model_str="timm-a",
        weights_path=str(model_dir),
        labels=["bird"],
        files=required,
        optional_files=optional,
    )

    # Repair lands the optional artifact.
    (model_dir / "label_descriptions.json").write_bytes(b"descriptions")

    after = model_files_fingerprint(
        str(model_dir), files=required, optional_files=optional,
    )
    key_after = classifier_cache_key(
        model_type="timm",
        model_str="timm-a",
        weights_path=str(model_dir),
        labels=["bird"],
        files=required,
        optional_files=optional,
    )

    assert before != after, (
        "optional artifact appearing on disk must flip the fingerprint"
    )
    assert key_before != key_after, (
        "optional artifact appearing on disk must flip the cache key so a "
        "post-Repair caller does not reuse the pre-Repair classifier"
    )
    assert any(name == "label_descriptions.json" for name, *_ in after)


def test_absent_optional_files_do_not_bloat_the_fingerprint(tmp_path):
    """An optional artifact never present on disk must not appear in the
    fingerprint at all — otherwise every fresh install would produce a
    fingerprint distinguishable only by a phantom entry, and existing
    tests that build a fingerprint from just the required manifest would
    stop matching production."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "text_encoder.onnx").write_bytes(b"weights")
    (model_dir / "config.json").write_bytes(b"config")

    required = ["text_encoder.onnx", "config.json"]

    baseline = model_files_fingerprint(str(model_dir), files=required)
    with_optional = model_files_fingerprint(
        str(model_dir),
        files=required,
        optional_files=["label_descriptions.json"],
    )

    assert baseline == with_optional


def test_shared_classifier_cache_preserves_authoritative_label_order(tmp_path):
    reset_default_cache_for_tests()
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "weights").write_bytes(b"weights")
    calls = []

    def acquire(labels):
        return acquire_cached_classifier(
            model_type="bioclip",
            model_str="model-a",
            weights_path=str(model_dir),
            labels=labels,
            factory=lambda: calls.append(tuple(labels)) or object(),
        )

    first = acquire(["bird", "cat"])
    same = acquire([" bird ", "cat"])
    reordered = acquire(["cat", "bird"])
    try:
        assert first.__enter__() is same.__enter__()
        assert reordered.__enter__() is not first.__enter__()
        assert calls == [("bird", "cat"), ("cat", "bird")]
    finally:
        first.release()
        same.release()
        reordered.release()
        reset_default_cache_for_tests()
