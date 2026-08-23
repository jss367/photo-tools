"""timm-based classifier for species classification (iNaturalist 2021).

Uses ONNX Runtime for inference with pre-exported EVA-02 models.
Model files are stored in ~/.vireo/models/timm-inat21-eva02-l/.
"""

import json
import logging
import os
import threading

import numpy as np
import onnx_runtime

log = logging.getLogger(__name__)

# Map model_str identifiers to local model directory names
_MODEL_DIR_MAP = {
    "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21": "timm-inat21-eva02-l",
}

_MODELS_ROOT = os.path.expanduser("~/.vireo/models")

# Bounded retry for the background label_descriptions.json heal: at most
# one heal attempt per (model_str) per process. A previous attempt that
# succeeded leaves the file on disk (so future TimmClassifier inits skip
# spawning entirely); a previous attempt that failed sets state="failed"
# and later inits skip too — HF was unreachable once, so repeat network
# probes on every classify job would just re-block for the same reason.
_HEAL_LOCK = threading.Lock()
_HEAL_STATE: dict = {}  # model_str -> "in_flight" | "done" | "failed"
_HEAL_THREADS: dict = {}  # model_str -> threading.Thread (kept for tests)


def _spawn_label_desc_heal(model_dir, model_str):
    """Best-effort async heal of a missing label_descriptions.json.

    Runs the network-touching self-heal in a daemon thread so
    TimmClassifier construction stays offline-only after the initial
    model download — matching the docs' promise (help.json: "Only for
    downloading AI models on first use"). Existing classifier instances
    use the taxonomy fallback until the file is on disk; the next
    TimmClassifier construction picks it up.

    Bounded to one attempt per process per model_str (see _HEAL_STATE).
    Returns the spawned Thread, or None if a prior attempt already ran.
    """
    with _HEAL_LOCK:
        if _HEAL_STATE.get(model_str) is not None:
            return None
        _HEAL_STATE[model_str] = "in_flight"

    def _worker():
        try:
            import models as _models_mod
            ok = _models_mod.ensure_timm_label_descriptions(
                model_dir, model_str,
            )
        except Exception as e:
            log.info(
                "label_descriptions heal failed for %s: %s", model_str, e,
            )
            ok = False
        with _HEAL_LOCK:
            _HEAL_STATE[model_str] = "done" if ok else "failed"

    thread = threading.Thread(
        target=_worker,
        name=f"label-desc-heal:{model_str}",
        daemon=True,
    )
    with _HEAL_LOCK:
        _HEAL_THREADS[model_str] = thread
    thread.start()
    return thread


class TimmClassifier:
    """Wraps an ONNX model for species classification.

    Uses a supervised model with a fixed class set (10K iNat21 species).
    No label files or text embeddings needed.

    Args:
        model_str: timm model identifier (e.g. "hf-hub:timm/eva02_large_patch14_clip_336.merged2b_ft_inat21")
        taxonomy: optional Taxonomy instance for enriching predictions with hierarchy
    """

    def __init__(self, model_str, taxonomy=None):
        # Resolve model directory from model_str
        dir_name = _MODEL_DIR_MAP.get(model_str)
        if dir_name is None:
            raise ValueError(
                f"Unknown timm model: {model_str}. "
                f"Known models: {list(_MODEL_DIR_MAP.keys())}"
            )

        model_dir = os.path.join(_MODELS_ROOT, dir_name)
        model_path = os.path.join(model_dir, "model.onnx")
        class_names_path = os.path.join(model_dir, "class_names.json")
        label_desc_path = os.path.join(model_dir, "label_descriptions.json")
        config_path = os.path.join(model_dir, "config.json")

        # Validate required files exist
        for path, desc in [
            (model_path, "ONNX model"),
            (class_names_path, "class names"),
            (config_path, "preprocessing config"),
        ]:
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"{desc} not found at {path}. "
                    "Download the model from the Models page in Settings."
                )

        # Installs that predate the label_descriptions.json export have no
        # scientific→common mapping and leak raw binomials ("Bubulcus
        # ibis") into predictions whenever the taxonomy fallback misses.
        # Spawn the heal off the startup path — the network probes can
        # take a full HF timeout when offline, and classifier construction
        # runs per classify job. This instance proceeds with the taxonomy
        # fallback; the next TimmClassifier picks up the healed file.
        if not os.path.isfile(label_desc_path):
            _spawn_label_desc_heal(model_dir, model_str)

        import models as _models_mod

        # Load ONNX session with self-heal on corruption: if the bytes
        # are truncated / partial, delete them and re-download once
        # rather than bricking the classify pipeline.

        redownload = _models_mod.build_self_heal_redownloader(model_dir)
        log.info("Loading timm ONNX model: %s", model_path)
        self._session = onnx_runtime.create_session_with_self_heal(
            model_path, redownload=redownload,
        )
        self._input_name = self._session.get_inputs()[0].name

        # Load class names (list of scientific names, index = class id)
        with open(class_names_path) as f:
            self._class_names = json.load(f)

        # Load preprocessing config (input_size, mean, std)
        with open(config_path) as f:
            preproc = json.load(f)
        self._input_size = tuple(preproc["input_size"][-2:])  # (H, W) from [C, H, W]
        self._mean = preproc["mean"]
        self._std = preproc["std"]

        # Build scientific -> common name mapping from label_descriptions
        # Format: {"Sturnus vulgaris": "European Starling, Bird"}
        self._common_names = {}
        if os.path.isfile(label_desc_path):
            with open(label_desc_path) as f:
                descs = json.load(f)
            if isinstance(descs, dict):
                for sci_name, desc in descs.items():
                    # desc format: "Common Name, Category" -- take part before last comma
                    parts = desc.rsplit(", ", 1)
                    common = parts[0] if len(parts) > 1 else desc
                    # If common name equals scientific name, it has no common name
                    if common.lower() != sci_name.lower():
                        self._common_names[sci_name.lower()] = common

        # Also use taxonomy.json for any names not in label_descriptions
        self._taxonomy = taxonomy

        log.info(
            "TimmClassifier ready: %d classes, %d common name mappings",
            len(self._class_names),
            len(self._common_names),
        )

    def _resolve_common_name(self, scientific_name):
        """Map a scientific name to a common name.

        Priority: label_descriptions from model > taxonomy.json > scientific name as-is.
        """
        key = scientific_name.lower()
        if key in self._common_names:
            return self._common_names[key]

        if self._taxonomy:
            taxon = self._taxonomy.lookup(scientific_name)
            if taxon and taxon.get("common_name"):
                return taxon["common_name"]

        return scientific_name

    def _build_results(self, probs, threshold):
        """Build sorted prediction dicts from a probability array."""
        indexed = sorted(enumerate(probs), key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in indexed:
            score = float(score)
            if score < threshold:
                break  # sorted, so all remaining are below threshold

            scientific_name = self._class_names[idx]
            common_name = self._resolve_common_name(scientific_name)

            # Build taxonomy hierarchy
            taxonomy = {"scientific_name": scientific_name}
            if self._taxonomy:
                hierarchy = self._taxonomy.get_hierarchy(scientific_name)
                if hierarchy:
                    taxonomy = hierarchy
                elif common_name != scientific_name:
                    hierarchy = self._taxonomy.get_hierarchy(common_name)
                    if hierarchy:
                        taxonomy = hierarchy
                    else:
                        taxonomy["scientific_name"] = scientific_name

            results.append(
                {
                    "species": common_name,
                    "score": score,
                    "auto_tag": f"auto:{common_name}",
                    "confidence_tag": f"auto:confidence:{score:.2f}",
                    "taxonomy": taxonomy,
                }
            )
        return results

    def _preprocess(self, image):
        """Preprocess a PIL Image for ONNX inference.

        Args:
            image: PIL Image (will be converted to RGB)

        Returns:
            numpy float32 array of shape (1, 3, H, W)
        """
        return onnx_runtime.preprocess_image(
            image,
            size=self._input_size,
            mean=self._mean,
            std=self._std,
        )

    def classify(self, image, threshold=0.1):
        """Classify an image and return predictions above threshold.

        Args:
            image: file path (str) or PIL Image
            threshold: minimum confidence to include (default 0.1 -- lower than
                BioCLIP's 0.4 since probability is spread across 10K classes)

        Returns:
            list of dicts with species, score, auto_tag, confidence_tag, taxonomy
        """
        from PIL import Image as PILImage
        from pipeline_locks import acquire_inference_resources

        if isinstance(image, str | os.PathLike):
            with PILImage.open(image) as img:
                input_arr = self._preprocess(img)
        else:
            input_arr = self._preprocess(image)

        # Provider-aware coordination is scoped tightly to the forward pass.
        # Preprocessing above runs without a lease; CPU sessions take their
        # configured permits and cpu_ml, while accelerator sessions take the
        # per-batch GPU semaphore.
        with acquire_inference_resources(self._session):
            output = self._session.run(None, {self._input_name: input_arr})
        logits = output[0]  # shape: (1, num_classes)
        probs = onnx_runtime.softmax(logits, axis=-1).flatten()

        return self._build_results(probs, threshold)

    def classify_batch(self, images, threshold=0.1):
        """Classify multiple PIL images.

        Args:
            images: list of PIL Images
            threshold: minimum confidence to include

        Returns:
            list of prediction lists (one per image)
        """
        if not images:
            return []

        from pipeline_locks import acquire_inference_resources

        input_arr = np.concatenate(
            [self._preprocess(img) for img in images],
            axis=0,
        )
        # Same provider-aware inference lease as classify(), scoped tightly
        # around the forward pass. Preprocessing and result building remain
        # outside it.
        with acquire_inference_resources(self._session):
            output = self._session.run(None, {self._input_name: input_arr})
        logits = output[0]
        probs_batch = onnx_runtime.softmax(logits, axis=-1)

        results = []
        for probs in probs_batch:
            results.append(self._build_results(probs, threshold))
        return results
