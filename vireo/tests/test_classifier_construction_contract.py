"""Keep production Classifier construction behind the shared model cache."""

import ast
from pathlib import Path

from classifier_cache import acquire_cached_classifier
from model_cache import reset_default_cache_for_tests


def test_production_classifier_calls_are_cache_factories():
    vireo_dir = Path(__file__).resolve().parents[1]
    violations = []

    for path in vireo_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

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
                if (
                    isinstance(current, ast.FunctionDef)
                    and current.name == "_construct_classifier"
                ):
                    routed = True
                    break
                if isinstance(current, ast.Lambda):
                    parent = parents.get(current)
                    routed = (
                        isinstance(parent, ast.keyword)
                        and parent.arg == "factory"
                    )
                    break
                current = parents.get(current)
            if not routed:
                violations.append(f"{path.name}:{node.lineno}")

    assert not violations, (
        "Classifier construction must be a factory passed to "
        f"acquire_cached_classifier: {', '.join(violations)}"
    )


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
