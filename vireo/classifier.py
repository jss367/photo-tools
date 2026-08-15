"""BioCLIP classifier wrapper for species-level classification.

Uses ONNX Runtime for inference with separate image encoder and text encoder
sessions. Model files are stored in ~/.vireo/models/{model-id}/.
"""

import json
import logging
import os

import numpy as np
import onnx_runtime

log = logging.getLogger(__name__)


class ClassificationCancelled(RuntimeError):
    """Raised when caller-local cancellation interrupts classifier setup."""


CACHE_DIR = os.path.expanduser("~/.vireo/embedding_cache")
_MANIFEST_PATH = os.path.join(CACHE_DIR, "manifest.json")

# Map model_str identifiers to local model directory names
_MODEL_DIR_MAP = {
    "ViT-B-16": "bioclip-vit-b-16",
    "hf-hub:imageomics/bioclip-2": "bioclip-2",
    "hf-hub:imageomics/bioclip-2.5-vith14": "bioclip-2.5-vith14",
}

_MODELS_ROOT = os.path.expanduser("~/.vireo/models")

# Context length for CLIP-style tokenizers (pad/truncate to this length)
_CONTEXT_LENGTH = 77

# Full set of 80 OpenAI ImageNet templates for zero-shot classification.
# Source: openai/CLIP and open_clip. Averaging across all 80 templates
# produces robust text embeddings that match the original BioCLIP pipeline.
OPENAI_IMAGENET_TEMPLATE = [
    lambda c: f"a bad photo of a {c}.",
    lambda c: f"a photo of many {c}.",
    lambda c: f"a sculpture of a {c}.",
    lambda c: f"a photo of the hard to see {c}.",
    lambda c: f"a low resolution photo of the {c}.",
    lambda c: f"a rendering of a {c}.",
    lambda c: f"graffiti of a {c}.",
    lambda c: f"a bad photo of the {c}.",
    lambda c: f"a cropped photo of the {c}.",
    lambda c: f"a tattoo of a {c}.",
    lambda c: f"the embroidered {c}.",
    lambda c: f"a photo of a hard to see {c}.",
    lambda c: f"a bright photo of a {c}.",
    lambda c: f"a photo of a clean {c}.",
    lambda c: f"a photo of a dirty {c}.",
    lambda c: f"a dark photo of the {c}.",
    lambda c: f"a drawing of a {c}.",
    lambda c: f"a photo of my {c}.",
    lambda c: f"the plastic {c}.",
    lambda c: f"a photo of the cool {c}.",
    lambda c: f"a close-up photo of a {c}.",
    lambda c: f"a black and white photo of the {c}.",
    lambda c: f"a painting of the {c}.",
    lambda c: f"a painting of a {c}.",
    lambda c: f"a pixelated photo of the {c}.",
    lambda c: f"a sculpture of the {c}.",
    lambda c: f"a bright photo of the {c}.",
    lambda c: f"a cropped photo of a {c}.",
    lambda c: f"a plastic {c}.",
    lambda c: f"a photo of the dirty {c}.",
    lambda c: f"a jpeg corrupted photo of a {c}.",
    lambda c: f"a blurry photo of the {c}.",
    lambda c: f"a photo of the {c}.",
    lambda c: f"a good photo of the {c}.",
    lambda c: f"a rendering of the {c}.",
    lambda c: f"a {c} in a video game.",
    lambda c: f"a photo of one {c}.",
    lambda c: f"a doodle of a {c}.",
    lambda c: f"a close-up photo of the {c}.",
    lambda c: f"a photo of a {c}.",
    lambda c: f"the origami {c}.",
    lambda c: f"the {c} in a video game.",
    lambda c: f"a sketch of a {c}.",
    lambda c: f"a doodle of the {c}.",
    lambda c: f"a origami {c}.",
    lambda c: f"a low resolution photo of a {c}.",
    lambda c: f"the toy {c}.",
    lambda c: f"a rendition of the {c}.",
    lambda c: f"a photo of the clean {c}.",
    lambda c: f"a photo of a large {c}.",
    lambda c: f"a rendition of a {c}.",
    lambda c: f"a photo of a nice {c}.",
    lambda c: f"a photo of a weird {c}.",
    lambda c: f"a blurry photo of a {c}.",
    lambda c: f"a cartoon {c}.",
    lambda c: f"art of a {c}.",
    lambda c: f"a sketch of the {c}.",
    lambda c: f"a embroidered {c}.",
    lambda c: f"a pixelated photo of a {c}.",
    lambda c: f"itap of the {c}.",
    lambda c: f"a jpeg corrupted photo of the {c}.",
    lambda c: f"a good photo of a {c}.",
    lambda c: f"a plushie {c}.",
    lambda c: f"a photo of the nice {c}.",
    lambda c: f"a photo of the small {c}.",
    lambda c: f"a photo of the weird {c}.",
    lambda c: f"the cartoon {c}.",
    lambda c: f"art of the {c}.",
    lambda c: f"a drawing of the {c}.",
    lambda c: f"a photo of the large {c}.",
    lambda c: f"a black and white photo of a {c}.",
    lambda c: f"the plushie {c}.",
    lambda c: f"a dark photo of a {c}.",
    lambda c: f"itap of a {c}.",
    lambda c: f"graffiti of the {c}.",
    lambda c: f"a toy {c}.",
    lambda c: f"itap of my {c}.",
    lambda c: f"a photo of a cool {c}.",
    lambda c: f"a photo of a small {c}.",
    lambda c: f"a tattoo of the {c}.",
]


def _prompt_template_identity():
    """Content identity for the prompts that produce cached embeddings."""
    from embedding_cache import identity_digest

    sentinel = "__VIREO_LABEL__"
    return {
        "name": "openai-imagenet-80",
        "sha256": identity_digest({
            "prompts": [template(sentinel) for template in OPENAI_IMAGENET_TEMPLATE]
        }),
    }


def _embedding_identity(labels, model_str, model_dir, *, allow_missing=False):
    from embedding_cache import build_embedding_identity

    return build_embedding_identity(
        labels,
        model_str,
        model_dir,
        prompt_template_identity=_prompt_template_identity(),
        tokenizer_context_length=_CONTEXT_LENGTH,
        allow_missing=allow_missing,
    )


def _embedding_cache_service():
    from embedding_cache import EmbeddingCache

    manifest_path = _MANIFEST_PATH
    if os.path.abspath(os.path.dirname(manifest_path)) != os.path.abspath(CACHE_DIR):
        # Tests and embedded callers sometimes redirect CACHE_DIR. Keep every
        # cache artifact together even if the legacy manifest constant was not
        # patched separately.
        manifest_path = os.path.join(CACHE_DIR, "manifest.json")
    return EmbeddingCache(CACHE_DIR, manifest_path)


def _resolve_model_dir(model_str, pretrained_str=None):
    """Resolve the model directory for a given model_str and optional pretrained_str.

    Mirrors the logic in ``Classifier.__init__`` so callers outside of
    ``Classifier`` (e.g. app.py cache-status checks) can compute a model
    directory that exactly matches the one ``Classifier`` would use, ensuring
    that ``_embedding_cache_path`` produces the same key in both places.

    Args:
        model_str: model identifier (e.g. ``"ViT-B-16"``).
        pretrained_str: optional configured ``weights_path`` from the model
            registry.  When it points to an existing directory it is used as-is,
            just like ``Classifier.__init__`` does.

    Returns:
        Resolved absolute path to the model directory, or ``None`` if
        ``model_str`` is not recognised and ``pretrained_str`` is not a valid
        directory.
    """
    if pretrained_str and os.path.isdir(pretrained_str):
        return pretrained_str
    dir_name = _MODEL_DIR_MAP.get(model_str)
    if dir_name is None:
        return None
    return os.path.join(_MODELS_ROOT, dir_name)


def _embedding_cache_path(labels, model_str, model_dir=None):
    """Return the complete-identity cache path used by ``EmbeddingCache``."""
    model_dir = model_dir or _resolve_model_dir(model_str)
    if model_dir is None:
        # Preserve a deterministic diagnostic path for unknown models while
        # keeping real cache access strict about resolving exact files.
        model_dir = os.path.join(_MODELS_ROOT, f"unknown-{model_str}")
    identity = _embedding_identity(
        labels, model_str, model_dir, allow_missing=True
    )
    return _embedding_cache_service().path_for(identity)


def _embedding_is_cached(labels, model_str, model_dir=None):
    """Return whether a complete and shape-valid embedding payload exists."""
    model_dir = model_dir or _resolve_model_dir(model_str)
    if model_dir is None:
        return False
    try:
        identity = _embedding_identity(labels, model_str, model_dir)
    except (OSError, ValueError):
        return False
    return _embedding_cache_service().is_cached(identity, len(identity["labels"]))


def _load_tokenizer(tokenizer_path):
    """Load a HuggingFace tokenizer from a JSON file.

    Args:
        tokenizer_path: path to tokenizer.json

    Returns:
        tokenizers.Tokenizer instance
    """
    from tokenizers import Tokenizer

    return Tokenizer.from_file(tokenizer_path)


def _tokenize(tokenizer, texts, context_length=_CONTEXT_LENGTH):
    """Tokenize a list of text strings, padding/truncating to context_length.

    Args:
        tokenizer: tokenizers.Tokenizer instance
        texts: list of strings
        context_length: max sequence length

    Returns:
        numpy int64 array of shape (len(texts), context_length)
    """
    encodings = tokenizer.encode_batch(texts)
    result = np.zeros((len(texts), context_length), dtype=np.int64)
    for i, enc in enumerate(encodings):
        ids = enc.ids[:context_length]
        result[i, : len(ids)] = ids
    return result


def _normalize(vec):
    """L2-normalize a vector or batch of vectors along last axis."""
    norm = np.linalg.norm(vec, axis=-1, keepdims=True)
    # Avoid division by zero
    norm = np.maximum(norm, 1e-8)
    return vec / norm


def _looks_like_stale_batched_export(err):
    """Heuristic: does this exception match the BioCLIP stale-export
    Reshape failure?

    Old BioCLIP ONNX exports baked batch=1 into a Reshape node named
    ``gemm_input_reshape``. ONNXRuntime surfaces the failure as
    ``Reshape node Name:'gemm_input_reshape' ... input_shape_size == size
    was false`` whenever batch>1. The node name is the discriminator:
    matching only on "Reshape" would also flag unrelated reshape failures
    in healthy installs and write a sentinel that locks them out of
    inference until a forced re-download.
    """
    msg = str(err)
    return "gemm_input_reshape" in msg


def _run_text_batched(text_session, text_input_name, tokens, model_dir=None):
    """Run the text encoder on a (N, seq_len) token batch.

    Vireo's text encoder ONNX exports are batch-agnostic (see
    ``scripts/export_onnx.py:_TextEncoderWrapper``). If a session rejects a
    batched input with the stale-export Reshape signature, write the
    standard ``.verify_failed`` sentinel into ``model_dir`` so Settings →
    Models flips the install to "incomplete" and surfaces the existing
    Repair button — the same self-heal flow used for hash-mismatch
    corruption.

    The pinned-revision verifier can't catch this on its own: the user's
    on-disk bytes legitimately match the upstream commit they were
    downloaded from, so SHA256 verification passes even though the export
    is broken. Detection has to happen at inference, repair has to happen
    through Settings.

    Other inference failures (memory pressure, provider glitches, mmap
    races) are passed through unchanged so a transient runtime error
    can't permanently flag a healthy install for Repair.
    """
    # Serialise GPU access across concurrent pipelines: when two pipelines
    # load BioCLIP with different label fingerprints, both factories run
    # concurrently under the cache's load_lock-per-key, and each one
    # computes its own label embeddings here. Without this lock, both
    # text encoders run on the GPU at the same time and can OOM.
    # Skipped for CPU-only sessions (same rationale as image_session).
    from pipeline_locks import acquire_gpu_if_session_uses_it
    try:
        with acquire_gpu_if_session_uses_it(text_session):
            return text_session.run(None, {text_input_name: tokens})[0]
    except Exception as e:
        if not _looks_like_stale_batched_export(e):
            raise
        if model_dir:
            import model_verify
            sentinel = os.path.join(
                model_dir, model_verify.VERIFY_FAILED_SENTINEL
            )
            try:
                with open(sentinel, "w") as f:
                    f.write(
                        "stale-export: text encoder rejected batched input "
                        f"(underlying: {type(e).__name__}: {e})\n"
                    )
            except OSError:
                pass
        raise RuntimeError(
            "Text encoder is from a stale export with a hardcoded batch "
            "dimension. Open Settings → Models and click Repair to "
            "re-download the model. "
            f"Underlying error: {type(e).__name__}: {e}"
        ) from e


def _compute_embeddings_with_progress(
    text_session,
    text_input_name,
    tokenizer,
    labels,
    progress_callback=None,
    cancel_check=None,
    model_dir=None,
):
    """Compute text embeddings for labels with progress logging.

    For each label, generates text from all templates, encodes via ONNX
    text encoder, and averages the resulting features.

    Args:
        text_session: ONNX InferenceSession for text encoder
        text_input_name: input tensor name for the text session
        tokenizer: tokenizers.Tokenizer instance
        labels: list of label strings
        progress_callback: optional callable(current, total) for UI progress
        cancel_check: optional callable() -> bool checked between labels

    Returns:
        numpy float32 array of shape (embedding_dim, num_labels) --
        transposed so it can be used directly for matmul with image features
    """
    total = len(labels)
    log.info("Computing label embeddings: 0/%d", total)
    if progress_callback:
        progress_callback(0, total)

    all_features = []
    for i, classname in enumerate(labels):
        if cancel_check and cancel_check():
            raise ClassificationCancelled("classification cancelled")
        txts = [template(classname) for template in OPENAI_IMAGENET_TEMPLATE]
        tokens = _tokenize(tokenizer, txts)
        txt_features = _run_text_batched(
            text_session, text_input_name, tokens, model_dir=model_dir
        )
        txt_features = txt_features.astype(np.float32)
        # Normalize each template's output, then average
        txt_features = _normalize(txt_features)
        mean_feature = txt_features.mean(axis=0)
        # Re-normalize the averaged feature
        mean_feature = _normalize(mean_feature)
        all_features.append(mean_feature)

        done = i + 1
        if progress_callback:
            progress_callback(done, total)
        if cancel_check and cancel_check():
            raise ClassificationCancelled("classification cancelled")
        if done % 50 == 0 or done == total:
            log.info("Computing label embeddings: %d/%d", done, total)

    # Stack into (num_labels, embedding_dim) then transpose to (embedding_dim, num_labels)
    stacked = np.stack(all_features, axis=0)  # (num_labels, embedding_dim)
    return stacked.T  # (embedding_dim, num_labels)


def _load_or_compute_label_embeddings(
    labels,
    model_str,
    model_dir,
    *,
    redownload=None,
    progress_callback=None,
    cancel_check=None,
    embedding_dim=None,
):
    """Resolve custom-label embeddings through the shared cache service.

    ``embedding_dim`` is the image encoder's feature width for this model;
    when supplied, cached payloads with a mismatched first axis are rejected
    so a malformed file cannot slip past to fail at inference time on
    ``img_features @ txt_embeddings``.
    """
    from embedding_cache import (
        EmbeddingWaitCancelled,
        canonicalize_labels,
    )

    classes = canonicalize_labels(labels)
    text_encoder_path = os.path.join(model_dir, "text_encoder.onnx")
    tokenizer_path = os.path.join(model_dir, "tokenizer.json")
    for path, desc in [
        (text_encoder_path, "text encoder ONNX model"),
        (tokenizer_path, "tokenizer"),
    ]:
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"{desc} not found at {path}. "
                "Download the model from the Models page in Settings."
            )

    initial_identity = _embedding_identity(classes, model_str, model_dir)
    cache = _embedding_cache_service()

    def _compute():
        if cancel_check and cancel_check():
            raise ClassificationCancelled("classification cancelled")
        text_session = onnx_runtime.create_session_with_self_heal(
            text_encoder_path, redownload=redownload,
        )
        try:
            text_input_name = text_session.get_inputs()[0].name
            tokenizer = _load_tokenizer(tokenizer_path)
            return _compute_embeddings_with_progress(
                text_session,
                text_input_name,
                tokenizer,
                classes,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                model_dir=model_dir,
            )
        finally:
            del text_session

    try:
        embeddings, actual_identity = cache.get_or_compute(
            initial_identity,
            _compute,
            identity_after=lambda: _embedding_identity(
                classes, model_str, model_dir
            ),
            cancel_check=cancel_check,
            embedding_dim=embedding_dim,
        )
    except EmbeddingWaitCancelled as exc:
        raise ClassificationCancelled("classification cancelled") from exc
    return classes, embeddings, initial_identity, actual_identity


def _image_encoder_embedding_dim(image_session):
    """Extract the image encoder's feature width from its ONNX outputs.

    The image session's output is ``(batch, embedding_dim)`` — the second
    axis is what the label embeddings must match to satisfy the matmul in
    ``classify_with_embedding``.  Returns ``None`` when the dimension is
    symbolic (rare for exported CLIP heads) so the caller can skip the
    stricter check rather than reject valid caches.
    """
    try:
        shape = image_session.get_outputs()[0].shape
    except Exception:
        return None
    if not shape:
        return None
    dim = shape[-1]
    if isinstance(dim, int) and dim > 0:
        return dim
    return None


def precompute_label_embeddings(
    labels,
    model_str="ViT-B-16",
    pretrained_str=None,
    *,
    progress_callback=None,
    cancel_check=None,
):
    """Populate custom-label embeddings without loading the image encoder."""
    model_dir = _resolve_model_dir(model_str, pretrained_str)
    if model_dir is None:
        raise ValueError(
            f"Unknown BioCLIP model: {model_str}. "
            f"Known models: {list(_MODEL_DIR_MAP.keys())}"
        )
    import models as _models_mod

    redownload = _models_mod.build_self_heal_redownloader(model_dir)
    classes, _embeddings, _before, _after = _load_or_compute_label_embeddings(
        labels,
        model_str,
        model_dir,
        redownload=redownload,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return len(classes)


class Classifier:
    """Wraps BioCLIP ONNX models for species classification.

    Args:
        labels: list of species/label strings for custom labels mode.
                If None, uses Tree of Life mode with pre-computed embeddings.
        model_str: model identifier (e.g. "ViT-B-16", "hf-hub:imageomics/bioclip-2")
        pretrained_str: optional path to the model directory. When provided and
                        pointing to an existing directory it takes precedence over
                        the default ``~/.vireo/models/<mapped-id>`` location, so
                        models registered at a custom ``weights_path`` are loaded
                        from the correct place.
        embedding_progress_callback: optional callable(current, total) for
                                     embedding computation progress
        cancel_check: optional callable() -> bool checked during slow setup
    """

    def __init__(
        self,
        labels=None,
        model_str="ViT-B-16",
        pretrained_str=None,
        embedding_progress_callback=None,
        cancel_check=None,
    ):
        # Resolve model directory.
        # pretrained_str may be a configured weights_path (e.g. from a custom
        # model registration).  Use it directly when it points to an existing
        # directory so that non-default install locations are respected.
        if pretrained_str and os.path.isdir(pretrained_str):
            self._model_dir = pretrained_str
        else:
            if pretrained_str:
                log.warning(
                    "pretrained_str %r is not a directory; falling back to "
                    "default model directory for model_str=%r",
                    pretrained_str,
                    model_str,
                )
            dir_name = _MODEL_DIR_MAP.get(model_str)
            if dir_name is None:
                raise ValueError(
                    f"Unknown BioCLIP model: {model_str}. "
                    f"Known models: {list(_MODEL_DIR_MAP.keys())}"
                )
            self._model_dir = os.path.join(_MODELS_ROOT, dir_name)
        image_encoder_path = os.path.join(self._model_dir, "image_encoder.onnx")
        config_path = os.path.join(self._model_dir, "config.json")

        # Validate required files
        for path, desc in [
            (image_encoder_path, "image encoder ONNX model"),
            (config_path, "preprocessing config"),
        ]:
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"{desc} not found at {path}. "
                    "Download the model from the Models page in Settings."
                )

        # Load image encoder ONNX session. When this model lives in the
        # known-models directory we wrap the load in a self-heal retry so
        # a corrupt / truncated file triggers a single delete+redownload
        # attempt before surfacing the error to the user. Custom models
        # fall back to the plain loader (no redownloader available).
        import models as _models_mod

        redownload = _models_mod.build_self_heal_redownloader(self._model_dir)

        log.info("Loading BioCLIP image encoder: %s", image_encoder_path)
        self._image_session = onnx_runtime.create_session_with_self_heal(
            image_encoder_path, redownload=redownload,
        )
        self._image_input_name = self._image_session.get_inputs()[0].name

        # Load preprocessing config AFTER the session loads: a self-heal
        # redownload may have replaced config.json alongside the ONNX
        # bytes, and reading it before would leave us with stale
        # input_size/mean/std causing silent mis-preprocessing.
        with open(config_path) as f:
            preproc = json.load(f)
        self._input_size = tuple(preproc["input_size"][-2:])  # (H, W)
        self._mean = preproc["mean"]
        self._std = preproc["std"]

        if labels is not None:
            text_heal_state = {"triggered": False}

            if redownload is not None:
                def _tracked_redownload():
                    text_heal_state["triggered"] = True
                    redownload()

                text_redownload = _tracked_redownload
            else:
                text_redownload = None

            expected_embedding_dim = _image_encoder_embedding_dim(
                self._image_session
            )
            (
                self._classes,
                self._txt_embeddings,
                identity_before,
                identity_after,
            ) = _load_or_compute_label_embeddings(
                labels,
                model_str,
                self._model_dir,
                redownload=text_redownload,
                progress_callback=embedding_progress_callback,
                cancel_check=cancel_check,
                embedding_dim=expected_embedding_dim,
            )

            # A producer shared with this caller may self-heal the entire
            # model directory while we wait. Rebuild the already-loaded image
            # side whenever the durable embedding identity shows that change.
            if text_heal_state["triggered"] or identity_before != identity_after:
                log.info(
                    "Text-encoder self-heal refreshed model dir; rebuilding "
                    "image encoder and preprocessing from the healed snapshot."
                )
                self._image_session = onnx_runtime.create_session(
                    image_encoder_path,
                )
                self._image_input_name = self._image_session.get_inputs()[0].name
                with open(config_path) as f:
                    preproc = json.load(f)
                self._input_size = tuple(preproc["input_size"][-2:])
                self._mean = preproc["mean"]
                self._std = preproc["std"]

            self._mode = "custom"
        else:
            # Tree of Life mode: load pre-computed embeddings
            log.info("Loading Tree of Life classifier...")
            tol_embeddings_path = os.path.join(
                self._model_dir, "tol_embeddings.npy"
            )
            tol_classes_path = os.path.join(self._model_dir, "tol_classes.json")

            for path, desc in [
                (tol_embeddings_path, "Tree of Life embeddings"),
                (tol_classes_path, "Tree of Life classes"),
            ]:
                if not os.path.isfile(path):
                    raise FileNotFoundError(
                        f"{desc} not found at {path}. "
                        "Download the model from the Models page in Settings."
                    )

            self._txt_embeddings = np.load(tol_embeddings_path)
            with open(tol_classes_path) as f:
                self._tol_classes = json.load(f)
            log.info(
                "Tree of Life classifier ready: %d species",
                len(self._tol_classes),
            )
            self._mode = "tol"

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
            center_crop=True,
        )

    def _get_image_embedding(self, image):
        """Compute a normalized image embedding from a PIL Image or file path.

        Args:
            image: file path (str) or PIL Image

        Returns:
            numpy float32 array of shape (1, embedding_dim) -- normalized
        """
        from PIL import Image as PILImage
        from pipeline_locks import acquire_gpu_if_session_uses_it

        if isinstance(image, str | os.PathLike):
            with PILImage.open(image) as img:
                input_arr = self._preprocess(img)
        else:
            input_arr = self._preprocess(image)

        # GPU serialisation across concurrent pipelines, scoped tightly to
        # the forward pass. Preprocessing above (load/decode/resize) and the
        # normalisation below run without the lock so concurrent pipelines
        # can use the GPU while this one does CPU work. Skipped entirely
        # for CPU-only sessions — Apple Silicon excludes CoreML when an
        # external-data ONNX is present, and CPU-only installs likewise
        # report no GPU provider; taking the semaphore there would block
        # real GPU stages in other pipelines for work that never touches
        # the GPU.
        with acquire_gpu_if_session_uses_it(self._image_session):
            features = self._image_session.run(
                None, {self._image_input_name: input_arr}
            )[0]
        features = features.astype(np.float32)
        return _normalize(features)

    def _build_custom_results(self, probs, threshold):
        """Build sorted prediction dicts from a probability array (custom labels mode)."""
        ranked = sorted(
            zip(self._classes, probs), key=lambda x: x[1], reverse=True
        )
        results = []
        for species, score in ranked:
            score = float(score)
            if score < threshold:
                continue
            results.append(
                {
                    "species": species,
                    "score": score,
                    "auto_tag": f"auto:{species}",
                    "confidence_tag": f"auto:confidence:{score:.2f}",
                }
            )
        return results

    def _build_tol_results(self, probs, threshold):
        """Build sorted prediction dicts from a probability array (Tree of Life mode).

        Each entry in tol_classes is a dict with taxonomy fields.
        """
        indexed = sorted(enumerate(probs), key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in indexed:
            score = float(score)
            if score < threshold:
                break  # sorted, so remaining are below threshold

            entry = self._tol_classes[idx]
            species = entry.get("common_name") or entry.get("species", "")
            result = {
                "species": species,
                "score": score,
                "auto_tag": f"auto:{species}",
                "confidence_tag": f"auto:confidence:{score:.2f}",
            }
            taxonomy = {}
            for rank in (
                "kingdom",
                "phylum",
                "class",
                "order",
                "family",
                "genus",
            ):
                if rank in entry and entry[rank]:
                    taxonomy[rank] = entry[rank]
            if entry.get("species"):
                taxonomy["scientific_name"] = entry["species"]
            if taxonomy:
                result["taxonomy"] = taxonomy
            results.append(result)
        return results

    def classify(self, image, threshold=0.4):
        """Classify an image and return predictions above threshold.

        Args:
            image: file path (str) or PIL Image

        Returns:
            list of dicts with species, score, auto_tag, confidence_tag
        """
        preds, _ = self.classify_with_embedding(image, threshold)
        return preds

    def classify_with_embedding(self, image, threshold=0.4):
        """Classify an image and return both predictions and the image embedding.

        Single forward pass -- computes the image embedding once, uses it for
        classification, and returns it for downstream use (e.g. similarity grouping).

        Args:
            image: file path (str) or PIL Image

        Returns:
            (predictions, embedding) where:
                predictions: list of dicts with species, score, auto_tag, confidence_tag
                embedding: numpy float32 array (the normalized image embedding vector)
        """
        img_features = self._get_image_embedding(image)  # (1, embedding_dim)
        embedding = img_features.flatten()

        # Cosine similarity: img_features @ txt_embeddings
        # img_features: (1, D), txt_embeddings: (D, num_labels)
        logits = 100.0 * (img_features @ self._txt_embeddings)  # (1, num_labels)
        probs = onnx_runtime.softmax(logits, axis=-1).flatten()

        if self._mode == "custom":
            return self._build_custom_results(probs, threshold), embedding
        else:
            return self._build_tol_results(probs, threshold), embedding

    def classify_batch_with_embedding(self, images, threshold=0.4):
        """Classify multiple PIL images.

        Processes each image individually through the ONNX image encoder.
        TODO: batch ONNX inference (stack preprocessed arrays along batch dim)
        would improve throughput for large classification jobs.

        Args:
            images: list of PIL Images
            threshold: minimum confidence to include

        Returns:
            list of (predictions, embedding) tuples
        """
        results = []
        for img in images:
            img_features = self._get_image_embedding(img)  # (1, D)
            embedding = img_features.flatten()

            logits = 100.0 * (img_features @ self._txt_embeddings)
            probs = onnx_runtime.softmax(logits, axis=-1).flatten()

            if self._mode == "custom":
                preds = self._build_custom_results(probs, threshold)
            else:
                preds = self._build_tol_results(probs, threshold)
            results.append((preds, embedding))
        return results
