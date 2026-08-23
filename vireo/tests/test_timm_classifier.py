# vireo/tests/test_timm_classifier.py
"""Tests for TimmClassifier -- uses mocked ONNX session to avoid downloading models."""

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image


def _make_test_image():
    """Create a temporary test image."""
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    path = f.name
    f.close()
    img = Image.new("RGB", (336, 336), color="green")
    img.save(path)
    return path


def _make_model_dir(tmp_path, label_descriptions=None):
    """Create a fake model directory with JSON config files.

    Returns the model directory path.
    """
    model_dir = tmp_path / "timm-inat21-eva02-l"
    model_dir.mkdir(parents=True, exist_ok=True)

    class_names = [
        "Sturnus vulgaris",
        "Turdus migratorius",
        "Corvus brachyrhynchos",
    ]

    if label_descriptions is None:
        label_descriptions = {
            "Sturnus vulgaris": "European Starling, Bird",
            "Turdus migratorius": "American Robin, Bird",
            "Corvus brachyrhynchos": "American Crow, Bird",
        }

    config = {
        "input_size": [3, 336, 336],
        "mean": [0.485, 0.456, 0.406],
        "std": [0.229, 0.224, 0.225],
    }

    with open(model_dir / "class_names.json", "w") as f:
        json.dump(class_names, f)
    with open(model_dir / "label_descriptions.json", "w") as f:
        json.dump(label_descriptions, f)
    with open(model_dir / "config.json", "w") as f:
        json.dump(config, f)
    # Create a dummy model.onnx file (just needs to exist for file checks)
    (model_dir / "model.onnx").write_text("dummy")

    return model_dir


def _make_fake_session(num_classes=3):
    """Create a mock ONNX InferenceSession that returns fixed logits."""
    session = MagicMock()

    # Mock get_inputs to return an input with a name
    mock_input = MagicMock()
    mock_input.name = "input"
    session.get_inputs.return_value = [mock_input]

    # Mock run to return logits: class 0 highest
    def fake_run(output_names, input_dict):
        batch_size = list(input_dict.values())[0].shape[0]
        logits = np.array([[5.0, 2.0, 0.5]] * batch_size, dtype=np.float32)
        return [logits]

    session.run = MagicMock(side_effect=fake_run)
    return session


def _make_fake_classifier(tmp_path, label_descriptions=None):
    """Build a TimmClassifier with fake ONNX session -- no model download needed."""
    from timm_classifier import TimmClassifier

    model_dir = _make_model_dir(tmp_path, label_descriptions)
    fake_session = _make_fake_session()

    # Patch the models root and create_session to avoid real ONNX loading
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
        )

    return clf


def test_json_config_loading(tmp_path):
    """Verify that JSON config files are loaded correctly during __init__."""
    from timm_classifier import TimmClassifier

    model_dir = _make_model_dir(tmp_path)
    fake_session = _make_fake_session()

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
        )

    assert len(clf._class_names) == 3
    assert clf._class_names[0] == "Sturnus vulgaris"
    assert clf._input_size == (336, 336)
    assert clf._mean == [0.485, 0.456, 0.406]
    assert clf._std == [0.229, 0.224, 0.225]
    assert "sturnus vulgaris" in clf._common_names
    assert clf._common_names["sturnus vulgaris"] == "European Starling"


def test_missing_model_dir(tmp_path):
    """__init__ raises FileNotFoundError when model directory is missing."""
    from timm_classifier import TimmClassifier

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)):
        with pytest.raises(FileNotFoundError, match="ONNX model not found"):
            TimmClassifier(
                "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
            )


def test_unknown_model_str():
    """__init__ raises ValueError for unknown model_str."""
    from timm_classifier import TimmClassifier

    with pytest.raises(ValueError, match="Unknown timm model"):
        TimmClassifier("some-unknown-model")


def test_classify_returns_predictions(tmp_path):
    """classify() returns a list of dicts with species, score, auto_tag."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        results = clf.classify(path)
        assert isinstance(results, list)
        assert len(results) > 0
        top = results[0]
        assert "species" in top
        assert "score" in top
        assert "auto_tag" in top
        assert top["auto_tag"].startswith("auto:")
        assert 0 <= top["score"] <= 1
    finally:
        os.unlink(path)


def test_classify_maps_scientific_to_common(tmp_path):
    """Top prediction maps scientific name to common name."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        results = clf.classify(path)
        top = results[0]
        # Sturnus vulgaris should map to European Starling
        assert top["species"] == "European Starling"
    finally:
        os.unlink(path)


def test_classify_includes_taxonomy(tmp_path):
    """Each prediction includes taxonomy with scientific_name."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        results = clf.classify(path)
        top = results[0]
        assert "taxonomy" in top
        assert "scientific_name" in top["taxonomy"]
        assert top["taxonomy"]["scientific_name"] == "Sturnus vulgaris"
    finally:
        os.unlink(path)


def test_classify_threshold_filters(tmp_path):
    """classify() filters results below threshold."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        # High threshold should filter most results
        results = clf.classify(path, threshold=0.9)
        for r in results:
            assert r["score"] >= 0.9
    finally:
        os.unlink(path)


def test_classify_confidence_tag(tmp_path):
    """Each result includes a confidence tag."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        results = clf.classify(path, threshold=0.0)
        for r in results:
            assert "confidence_tag" in r
            assert r["confidence_tag"].startswith("auto:confidence:")
    finally:
        os.unlink(path)


def test_classify_fallback_to_scientific_name(tmp_path):
    """If no common name mapping, use scientific name as-is."""
    clf = _make_fake_classifier(tmp_path, label_descriptions={})

    path = _make_test_image()
    try:
        results = clf.classify(path)
        top = results[0]
        assert top["species"] == "Sturnus vulgaris"
    finally:
        os.unlink(path)


def test_classify_all_results_sorted_by_score(tmp_path):
    """Results are sorted by descending score."""
    clf = _make_fake_classifier(tmp_path)

    path = _make_test_image()
    try:
        results = clf.classify(path, threshold=0.0)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
    finally:
        os.unlink(path)


def test_classify_batch(tmp_path):
    """classify_batch() returns one result list per image."""
    clf = _make_fake_classifier(tmp_path)

    img1 = Image.new("RGB", (336, 336), color="red")
    img2 = Image.new("RGB", (336, 336), color="blue")

    results = clf.classify_batch([img1, img2], threshold=0.0)
    assert len(results) == 2
    assert isinstance(results[0], list)
    assert isinstance(results[1], list)
    # Each image should produce results for all 3 classes
    assert len(results[0]) == 3
    assert len(results[1]) == 3


def test_classify_batch_runs_single_onnx_batch(tmp_path):
    """classify_batch() sends the whole image batch through ONNX at once."""
    clf = _make_fake_classifier(tmp_path)

    img1 = Image.new("RGB", (336, 336), color="red")
    img2 = Image.new("RGB", (336, 336), color="blue")

    clf.classify_batch([img1, img2], threshold=0.0)

    assert clf._session.run.call_count == 1
    _output_names, input_dict = clf._session.run.call_args.args
    input_arr = input_dict[clf._input_name]
    assert input_arr.shape[0] == 2


def test_classify_with_pil_image(tmp_path):
    """classify() accepts PIL Image directly."""
    clf = _make_fake_classifier(tmp_path)

    img = Image.new("RGB", (500, 400), color="green")
    results = clf.classify(img)
    assert isinstance(results, list)
    assert len(results) > 0


def test_known_models_have_model_type():
    """All entries in KNOWN_MODELS have a model_type field."""
    from models import KNOWN_MODELS

    for m in KNOWN_MODELS:
        assert "model_type" in m, f"Model {m['id']} missing model_type"
        assert m["model_type"] in ("bioclip", "timm"), (
            f"Model {m['id']} has unexpected model_type: {m['model_type']}"
        )


def test_timm_model_in_known_models():
    """The timm iNat21 model is in KNOWN_MODELS."""
    from models import KNOWN_MODELS

    timm_models = [m for m in KNOWN_MODELS if m["model_type"] == "timm"]
    assert len(timm_models) >= 1
    inat = timm_models[0]
    assert inat["id"] == "timm-inat21-eva02-l"
    assert "iNat21" in inat["name"]
    assert inat["model_str"].startswith("hf-hub:timm/")


def test_get_models_includes_model_type():
    """get_models() returns model_type for each model."""
    from models import get_models

    models = get_models()
    for m in models:
        assert "model_type" in m, f"Model {m['id']} missing model_type from get_models()"


# ── GPU lock scope ────────────────────────────────────────────────────────


def test_classify_holds_gpu_lock_around_session_run_only(tmp_path):
    """``TimmClassifier.classify`` must hold the GPU lock around
    ``session.run`` only — not around preprocessing or softmax.

    Regression for Codex P2 on PR #899: the lock has been pushed down
    out of ``classify_job._flush_batch`` and into the classifier
    implementations so concurrent pipelines aren't blocked on CPU work.
    """
    import pipeline_locks

    clf = _make_fake_classifier(tmp_path)
    # Declare a GPU provider so the conditional lock engages — this is
    # the on-GPU code path; the CPU-only skip is covered in its own test
    # below.
    clf._session.get_providers.return_value = [
        "CUDAExecutionProvider", "CPUExecutionProvider",
    ]

    snapshots = {}
    original_run = clf._session.run.side_effect  # underlying fake_run

    def record_during_run(output_names, input_dict):
        snapshots["during_run"] = pipeline_locks._GPU_SEMAPHORE._value
        return original_run(output_names, input_dict)

    clf._session.run.side_effect = record_during_run

    baseline = pipeline_locks._GPU_SEMAPHORE._value
    img = Image.new("RGB", (500, 400), color="green")
    clf.classify(img)

    assert pipeline_locks._GPU_SEMAPHORE._value == baseline, (
        "semaphore must be released on the way out"
    )
    assert snapshots["during_run"] == baseline - 1, (
        "GPU lock must be held during _session.run"
    )


def test_classify_batch_holds_gpu_lock_around_session_run_only(tmp_path):
    """``TimmClassifier.classify_batch`` must hold the GPU lock around
    the single batched ``session.run`` call — not around per-image
    preprocessing or the result-building loop.
    """
    import pipeline_locks

    clf = _make_fake_classifier(tmp_path)
    clf._session.get_providers.return_value = [
        "CUDAExecutionProvider", "CPUExecutionProvider",
    ]

    snapshots = {}
    original_run = clf._session.run.side_effect

    def record_during_run(output_names, input_dict):
        snapshots["during_run"] = pipeline_locks._GPU_SEMAPHORE._value
        return original_run(output_names, input_dict)

    clf._session.run.side_effect = record_during_run

    baseline = pipeline_locks._GPU_SEMAPHORE._value
    images = [Image.new("RGB", (500, 400), color="green") for _ in range(4)]
    results = clf.classify_batch(images)

    assert len(results) == 4
    assert pipeline_locks._GPU_SEMAPHORE._value == baseline, (
        "semaphore must be released on the way out"
    )
    assert snapshots["during_run"] == baseline - 1, (
        "GPU lock must be held during _session.run"
    )


def test_classify_skips_gpu_lock_for_cpu_only_session(tmp_path):
    """When the timm session runs on CPU (Apple Silicon excludes CoreML
    for external-data models; CPU-only installs likewise), classify()
    must not take the process-wide GPU semaphore. Codex P2 on PR #899:
    blocking real GPU stages in other pipelines for CPU-only work
    defeats the concurrency this design enables.
    """
    import pipeline_locks

    clf = _make_fake_classifier(tmp_path)
    clf._session.get_providers.return_value = ["CPUExecutionProvider"]

    snapshots = {}
    original_run = clf._session.run.side_effect

    def record_during_run(output_names, input_dict):
        snapshots["during_run"] = pipeline_locks._GPU_SEMAPHORE._value
        return original_run(output_names, input_dict)

    clf._session.run.side_effect = record_during_run

    baseline = pipeline_locks._GPU_SEMAPHORE._value
    img = Image.new("RGB", (500, 400), color="green")
    clf.classify(img)

    assert snapshots["during_run"] == baseline, (
        "CPU-only session must not take the GPU semaphore"
    )


def test_classify_batch_skips_gpu_lock_for_cpu_only_session(tmp_path):
    """Same CPU-only skip as classify(), but for the batched path."""
    import pipeline_locks

    clf = _make_fake_classifier(tmp_path)
    clf._session.get_providers.return_value = ["CPUExecutionProvider"]

    snapshots = {}
    original_run = clf._session.run.side_effect

    def record_during_run(output_names, input_dict):
        snapshots["during_run"] = pipeline_locks._GPU_SEMAPHORE._value
        return original_run(output_names, input_dict)

    clf._session.run.side_effect = record_during_run

    baseline = pipeline_locks._GPU_SEMAPHORE._value
    images = [Image.new("RGB", (500, 400), color="green") for _ in range(3)]
    clf.classify_batch(images)

    assert snapshots["during_run"] == baseline, (
        "CPU-only session must not take the GPU semaphore in the batched path"
    )


def _reset_heal_state():
    """Clear the module-level bounded-retry state between tests so each
    test that exercises the heal path gets a fresh attempt slot."""
    import timm_classifier as tc
    with tc._HEAL_LOCK:
        tc._HEAL_STATE.clear()
        tc._HEAL_ATTEMPTS.clear()
        tc._HEAL_THREADS.clear()


def test_missing_label_descriptions_spawns_async_heal(tmp_path):
    """A model dir without label_descriptions.json spawns the heal on a
    background thread — startup never waits on the network probe. The
    healed file lands on disk once the thread completes."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    heal_gate = __import__("threading").Event()
    def _fake_ensure(dir_arg, model_str, progress_callback=None):
        heal_gate.wait(timeout=5)
        assert dir_arg == str(model_dir)
        with open(os.path.join(dir_arg, "label_descriptions.json"), "w") as f:
            json.dump({"Sturnus vulgaris": "European Starling, Bird"}, f)
        return True

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", side_effect=_fake_ensure) as ensure:
        clf = TimmClassifier(model_str)
        # Startup returned before the (blocked) heal call, proving the
        # network probe is off the critical path.
        assert not (model_dir / "label_descriptions.json").exists()
        assert clf._common_names == {}
        heal_gate.set()
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.called
    assert (model_dir / "label_descriptions.json").exists()
    # A new classifier instance sees the healed file.
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session):
        clf2 = TimmClassifier(model_str)
    assert clf2._common_names["sturnus vulgaris"] == "European Starling"


def test_heal_bounded_to_one_attempt_per_process(tmp_path):
    """Second TimmClassifier construction with the file still missing
    does not re-spawn the heal — an already-failed attempt would just
    re-block on the same unreachable HF."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", side_effect=OSError("offline")) as ensure:
        TimmClassifier(model_str)
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)
        first_calls = ensure.call_count
        # File is still missing, but a second init must not re-attempt.
        TimmClassifier(model_str)

    assert first_calls == 1
    assert ensure.call_count == 1


def test_reinstalling_the_model_clears_a_failed_heal_verdict(tmp_path):
    """Removing the model in Settings and downloading it again must get
    a fresh heal attempt.

    The bounded-retry state used to be keyed by model_str alone and
    lived for the life of the process, so a heal that failed once (HF
    unreachable) would keep suppressing the repair for a *brand new*
    installation whose download could now succeed. The user's only
    recourse was restarting Vireo — Vireo repairs its own model state.
    """
    import timm_classifier as tc
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions",
               side_effect=OSError("offline")) as ensure:
        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)
        assert ensure.call_count == 1
        # Same installation, still missing: no re-probe.
        TimmClassifier(model_str)
        assert ensure.call_count == 1

        # User removes the model and downloads it again. The new
        # installation writes a new model.onnx, and its optional-file
        # fetch still did not supply label_descriptions.json.
        shutil.rmtree(model_dir)
        model_dir = _make_model_dir(tmp_path)
        (model_dir / "label_descriptions.json").unlink()
        (model_dir / "model.onnx").write_text("dummy-reinstalled")

        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.call_count == 2, (
        "a fresh installation must not inherit the previous one's "
        "failed heal verdict"
    )


def test_reinstall_that_lands_identical_size_and_mtime_still_reheals(tmp_path):
    """A remove+re-download that reproduces the previous model.onnx's
    ``(size, mtime_ns)`` must still get a fresh heal attempt.

    ``download_model`` publishes weights via ``shutil.copy2``, which
    preserves the source file's mtime. If HF hands back the same cached
    blob on the re-download (unchanged in the repo since the last
    fetch), the copied destination lands with an identical
    ``(size, mtime_ns)`` tuple. A generation marker derived from only
    those two fields would keep the previous installation's ``failed``
    verdict — reintroducing the "user must restart Vireo" bug this
    keying is supposed to prevent.
    """
    import timm_classifier as tc
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    onnx_path = model_dir / "model.onnx"
    weight_bytes = onnx_path.read_bytes()
    frozen_mtime_ns = onnx_path.stat().st_mtime_ns

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions",
               side_effect=OSError("offline")) as ensure:
        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)
        assert ensure.call_count == 1

        # Simulate a remove+re-download that lands the exact same
        # bytes at the exact same mtime — the copy2-preserves-mtime
        # scenario Codex flagged. Size and mtime_ns will both match
        # the previous installation's.
        shutil.rmtree(model_dir)
        model_dir = _make_model_dir(tmp_path)
        (model_dir / "label_descriptions.json").unlink()
        onnx_path = model_dir / "model.onnx"
        onnx_path.write_bytes(weight_bytes)
        os.utime(onnx_path, ns=(frozen_mtime_ns, frozen_mtime_ns))
        assert onnx_path.stat().st_size == len(weight_bytes)
        assert onnx_path.stat().st_mtime_ns == frozen_mtime_ns

        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.call_count == 2, (
        "a re-download that reproduces size+mtime must not inherit "
        "the previous installation's failed heal verdict"
    )


def test_deleting_the_healed_file_reopens_the_heal(tmp_path):
    """A completed heal must not suppress the repair once the file it
    produced is gone again.

    "done" recorded that a heal succeeded, but reaching the spawn at all
    means the file read back as missing/unusable. Trusting the stale
    "done" leaked raw scientific names until the app restarted.
    """
    import timm_classifier as tc
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    target = model_dir / "label_descriptions.json"
    target.unlink()
    fake_session = _make_fake_session()

    def _fake_ensure(dir_arg, model_str, progress_callback=None):
        with open(os.path.join(dir_arg, "label_descriptions.json"), "w") as f:
            json.dump({"Sturnus vulgaris": "European Starling, Bird"}, f)
        return True

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions",
               side_effect=_fake_ensure) as ensure:
        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)
        assert ensure.call_count == 1
        assert target.exists()

        # Something removes the healed file out from under us.
        target.unlink()
        TimmClassifier(model_str)
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.call_count == 2
    assert target.exists()


def test_reheal_after_done_is_bounded(tmp_path):
    """The "done was contradicted, try again" path is capped so a heal
    that keeps reporting success without producing a usable file cannot
    fire an HF probe on every classify job."""
    import timm_classifier as tc
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    # Pathological: claims success, never writes the file.
    def _lying_ensure(dir_arg, model_str, progress_callback=None):
        return True

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions",
               side_effect=_lying_ensure) as ensure:
        for _ in range(tc._HEAL_MAX_ATTEMPTS + 3):
            TimmClassifier(model_str)
            tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.call_count == tc._HEAL_MAX_ATTEMPTS


def test_self_heal_failure_keeps_classifier_working(tmp_path):
    """When the self-heal can't produce the file (offline), the classifier
    still constructs and falls back to taxonomy/scientific names."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", side_effect=OSError("offline")):
        clf = TimmClassifier(model_str)
        # Even before the background heal thread finishes, the classifier
        # is usable via the taxonomy fallback.
        assert clf._common_names == {}
        assert clf._resolve_common_name("Sturnus vulgaris") == "Sturnus vulgaris"
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)


def test_present_label_descriptions_skips_self_heal(tmp_path):
    """No self-heal attempt when the file is already on disk."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    _make_model_dir(tmp_path)
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions") as ensure:
        TimmClassifier(model_str)

    import timm_classifier as tc
    assert model_str not in tc._HEAL_THREADS
    assert not ensure.called


def test_corrupt_label_descriptions_heals_instead_of_aborting(tmp_path):
    """A truncated label_descriptions.json must not brick the classifier.

    Existence alone used to gate the heal *and* json.load ran unguarded,
    so one torn write (process killed mid-download) would raise out of
    every subsequent TimmClassifier construction and abort the classify
    job forever. Treat unparseable as missing: construct with the
    taxonomy fallback and spawn the repair."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    # Truncated mid-write: os.path.isfile passes, json.load raises.
    (model_dir / "label_descriptions.json").write_text(
        '{"Sturnus vulgaris": "European Star'
    )
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", return_value=False) as ensure:
        clf = TimmClassifier(model_str)
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert ensure.called
    assert clf._common_names == {}
    assert clf._resolve_common_name("Sturnus vulgaris") == "Sturnus vulgaris"


def test_optional_files_snapshot_records_present_label_descriptions(tmp_path):
    """When label_descriptions.json is on disk at construction time,
    the instance records the file's (size, mtime_ns) under
    ``optional_files_snapshot`` so ``acquire_cached_classifier`` keys
    the entry by exactly what this instance consumed."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session):
        clf = TimmClassifier(model_str)

    stat = os.stat(model_dir / "label_descriptions.json")
    assert clf.optional_files_snapshot == {
        "label_descriptions.json": (stat.st_size, int(stat.st_mtime_ns)),
    }


class _RepublishOnClose:
    """File proxy that atomically republishes the file it wrapped, right
    after the reader closes it.

    Staging the republish at close (rather than mid-``json.load``) is
    what makes this test run on every platform: Windows refuses
    ``os.replace`` over a path that still has an open handle, because
    CPython does not open files with FILE_SHARE_DELETE. Closing first is
    also the *narrower* hazard — it is the window a path stat taken
    after the ``with`` block would fall into, which is exactly the bug
    under test.
    """

    def __init__(self, fh, on_close):
        self._fh = fh
        self._on_close = on_close

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc_info):
        result = self._fh.__exit__(*exc_info)
        self._on_close()
        return result


def test_snapshot_ignores_a_republish_that_lands_after_the_read(tmp_path):
    """The snapshot must describe the bytes this instance parsed, not the
    file sitting at the path once construction moves on.

    The snapshot used to be a fresh ``os.stat`` taken after
    ``load_label_descriptions`` returned. A Repair (or the heal spawned
    by a sibling construction) that atomically republishes
    label_descriptions.json in that gap would stamp this instance with
    the *replacement's* identity — so the cache would key a classifier
    holding the old labels under the new file's fingerprint and keep
    serving it. Same "keyed by artifacts it never read" hazard as the
    heal-during-session-load case, one line narrower. Reading the
    signature off the descriptor that produced the mapping closes it.
    """
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    target = model_dir / "label_descriptions.json"
    consumed = target.stat()
    consumed_signature = (consumed.st_size, int(consumed.st_mtime_ns))

    real_open = open
    republished = []

    def _republish():
        if republished:
            return
        replacement = model_dir / "label_descriptions.json.new"
        # Different entry count => different size, so the signatures
        # differ regardless of filesystem mtime granularity.
        replacement.write_text(json.dumps({
            "Sturnus vulgaris": "Common Starling, Bird",
        }))
        os.replace(replacement, target)
        republished.append(True)

    def _open_and_republish_on_close(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if str(file) != str(target):
            return fh
        return _RepublishOnClose(fh, _republish)

    fake_session = _make_fake_session()
    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.open", _open_and_republish_on_close, create=True):
        clf = TimmClassifier(model_str)

    assert republished, "the republish hook never fired"
    now = target.stat()
    assert (now.st_size, int(now.st_mtime_ns)) != consumed_signature

    # It parsed the pre-republish file, so it must be keyed as such.
    assert clf._common_names["sturnus vulgaris"] == "European Starling"
    assert clf.optional_files_snapshot == {
        "label_descriptions.json": consumed_signature,
    }


def test_optional_files_snapshot_records_absent_label_descriptions(tmp_path):
    """When label_descriptions.json is missing at construction time,
    the instance records ``None`` — locking the cache entry to the
    pre-heal fingerprint. If the background heal lands during
    construction, the next acquire's disk-based fingerprint will not
    match and a fresh classifier will be built (which will actually
    read the healed file), instead of the stale pre-heal instance
    getting rekeyed to the healed fingerprint and reused forever."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", side_effect=OSError("offline")):
        clf = TimmClassifier(model_str)
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert clf.optional_files_snapshot == {
        "label_descriptions.json": None,
    }


def test_optional_files_snapshot_records_absent_for_unparseable_file(tmp_path):
    """A truncated label_descriptions.json is treated as missing by
    the classifier (it uses the taxonomy fallback), so the snapshot
    must also mark it absent — otherwise the pre-heal instance would
    be keyed as if it had consumed the torn file, and the heal that
    replaces it would not invalidate the cache entry."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").write_text(
        '{"Sturnus vulgaris": "European Star'
    )
    fake_session = _make_fake_session()

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", return_value=fake_session), \
         patch("models.ensure_timm_label_descriptions", return_value=False):
        clf = TimmClassifier(model_str)
        import timm_classifier as tc
        tc._HEAL_THREADS[model_str].join(timeout=5)

    assert clf.optional_files_snapshot == {
        "label_descriptions.json": None,
    }


def test_classifier_never_reads_a_repair_it_just_spawned(tmp_path):
    """The instance that spawns the heal must not read the file the heal
    is publishing underneath it — otherwise a partially written file
    could be json.load-ed mid-write. The read happens once, before the
    thread starts, so a heal that completes during ONNX session init is
    invisible to this instance (and picked up by the next one)."""
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    model_dir = _make_model_dir(tmp_path)
    (model_dir / "label_descriptions.json").unlink()

    heal_done = __import__("threading").Event()

    def _fake_ensure(dir_arg, model_str, progress_callback=None):
        with open(os.path.join(dir_arg, "label_descriptions.json"), "w") as f:
            json.dump({"Sturnus vulgaris": "European Starling, Bird"}, f)
        heal_done.set()
        return True

    fake_session = _make_fake_session()

    def _slow_create_session(*args, **kwargs):
        # Session init is where the real race lives: the heal lands while
        # the classifier is still constructing.
        heal_done.wait(timeout=5)
        return fake_session

    model_str = "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session", side_effect=_slow_create_session), \
         patch("models.ensure_timm_label_descriptions", side_effect=_fake_ensure):
        clf = TimmClassifier(model_str)

    assert heal_done.is_set()
    # File is on disk now, but this instance never re-read it.
    assert (model_dir / "label_descriptions.json").exists()
    assert clf._common_names == {}


class _StubTaxonomy:
    """Minimal Taxonomy substitute for _resolve_common_name tests."""

    def __init__(self, by_scientific):
        # by_scientific: {scientific_lower: {"common_name": ..., ...}}
        self._by_scientific = by_scientific

    def lookup(self, name):
        return self._by_scientific.get(name.lower().strip())

    def get_hierarchy(self, name):
        return {}


def test_taxonomy_wins_over_stale_label_description(tmp_path):
    """When label_descriptions carries an iNat21-vintage common name for
    a class the taxonomy has under a current preferred name, the
    classifier must persist the taxonomy name — otherwise
    /api/predictions/compare cannot canonicalize it and reports a false
    disagreement with a model that emits the current name.
    """
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    _make_model_dir(
        tmp_path,
        label_descriptions={"Bubulcus ibis": "Cattle Egret, Bird"},
    )
    fake_session = _make_fake_session()
    taxonomy = _StubTaxonomy(
        {"bubulcus ibis": {"scientific_name": "Ardea ibis",
                           "common_name": "Western Cattle-Egret"}},
    )

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session",
               return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21",
            taxonomy=taxonomy,
        )

    # Sanity: the stale mapping is still populated from the file so the
    # dict remains a valid fallback for names the taxonomy misses.
    assert clf._common_names.get("bubulcus ibis") == "Cattle Egret"
    # But the resolver must return the taxonomy's current preferred name.
    assert clf._resolve_common_name("Bubulcus ibis") == "Western Cattle-Egret"


def test_label_description_used_when_taxonomy_misses(tmp_path):
    """Taxonomy-first must not lose classes the taxonomy has never
    heard of — the label description remains the fallback so a model
    with entries outside taxonomy.json still emits common names.
    """
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    _make_model_dir(
        tmp_path,
        label_descriptions={"Sturnus vulgaris": "European Starling, Bird"},
    )
    fake_session = _make_fake_session()
    taxonomy = _StubTaxonomy({})  # empty — every lookup misses

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session",
               return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21",
            taxonomy=taxonomy,
        )

    assert clf._resolve_common_name("Sturnus vulgaris") == "European Starling"


def test_synonym_resolution_used_when_taxonomy_lacks_common_name(tmp_path):
    """A taxonomy synonym hit without a common_name must still return
    the resolved current scientific name, not fall through to the
    old-binomial label mapping (or worse, to the input on legacy /
    offline installs lacking label_descriptions.json).

    Otherwise ``_build_results`` persists the obsolete binomial and its
    ``auto:`` tag on every prediction whose taxonomy entry happens to
    lack a preferred common name, so the fix that adds synonym
    resolution silently does nothing for those species.
    """
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    _make_model_dir(
        tmp_path,
        # No entry for the obsolete binomial — mirrors the legacy /
        # offline install scenario where label_descriptions.json is
        # missing or does not cover this class.
        label_descriptions={},
    )
    fake_session = _make_fake_session()
    taxonomy = _StubTaxonomy(
        {"bubulcus ibis": {"scientific_name": "Ardea ibis",
                           "common_name": ""}},
    )

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session",
               return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21",
            taxonomy=taxonomy,
        )

    assert clf._resolve_common_name("Bubulcus ibis") == "Ardea ibis"


def test_taxonomy_without_common_name_falls_back_to_label(tmp_path):
    """A taxonomy hit with a blank common_name must not shadow the
    label description; the resolver should still produce a common name
    when one is available in either source.
    """
    from timm_classifier import TimmClassifier

    _reset_heal_state()
    _make_model_dir(
        tmp_path,
        label_descriptions={"Sturnus vulgaris": "European Starling, Bird"},
    )
    fake_session = _make_fake_session()
    taxonomy = _StubTaxonomy(
        {"sturnus vulgaris": {"scientific_name": "Sturnus vulgaris",
                              "common_name": ""}},
    )

    with patch("timm_classifier._MODELS_ROOT", str(tmp_path)), \
         patch("timm_classifier.onnx_runtime.create_session",
               return_value=fake_session):
        clf = TimmClassifier(
            "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21",
            taxonomy=taxonomy,
        )

    assert clf._resolve_common_name("Sturnus vulgaris") == "European Starling"
