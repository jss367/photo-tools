"""Classification job logic extracted from app.py.

This module contains the background work function for the /api/jobs/classify
endpoint. The route handler in app.py parses the request and delegates here.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from labels import get_active_labels, get_saved_labels, load_merged_labels

log = logging.getLogger(__name__)


@dataclass
class ClassifyParams:
    """Parameters for a classification job, parsed from the request body."""

    collection_id: str
    labels_file: Optional[str]
    labels_files: Optional[list]
    model_id: Optional[str]
    model_name: Optional[str]
    grouping_window: int
    similarity_threshold: float
    reclassify: bool


def _load_taxonomy(taxonomy_path):
    """Load taxonomy from JSON file. Returns Taxonomy instance or None."""
    if not os.path.exists(taxonomy_path):
        return None
    try:
        from taxonomy import Taxonomy

        return Taxonomy(taxonomy_path)
    except Exception as e:
        log.warning(
            "Could not load taxonomy: %s — continuing without taxonomy enrichment", e
        )
        return None


def _load_labels(model_type, model_str, labels_file, labels_files):
    """Resolve labels for classification.

    Returns:
        (labels, use_tol) where labels is a list of species strings or None,
        and use_tol is True if Tree of Life mode should be used.
    """
    if model_type == "timm":
        log.info("Classification config: model=%s (timm) — no labels needed", model_str)
        return None, False

    labels = None

    if labels_files and isinstance(labels_files, list):
        saved = get_saved_labels()
        saved_by_file = {s["labels_file"]: s for s in saved}
        active_sets = []
        for p in labels_files:
            meta = saved_by_file.get(p, {"labels_file": p})
            active_sets.append(meta)
        labels = load_merged_labels(active_sets)
        log.info("Using %d merged labels from %d sets", len(labels), len(active_sets))
    elif labels_file and os.path.exists(labels_file):
        with open(labels_file) as f:
            labels = [line.strip() for line in f if line.strip()]
        log.info("Using %d labels from file: %s", len(labels), labels_file)
    else:
        active_sets = get_active_labels()
        if active_sets:
            labels = load_merged_labels(active_sets)
            names = [s.get("name", "?") for s in active_sets]
            log.info(
                "Using %d merged labels from active sets: %s",
                len(labels),
                ", ".join(names),
            )

    if labels:
        log.info(
            "Classification config: model=%s, labels=%d from %s",
            model_str,
            len(labels),
            labels_file or "active labels",
        )
    else:
        log.info("Classification config: model=%s, no labels selected", model_str)

    tol_supported_models = {
        "hf-hub:imageomics/bioclip",
        "hf-hub:imageomics/bioclip-2",
    }
    use_tol = False
    if not labels:
        if model_str in tol_supported_models:
            log.info(
                "No regional labels available — using Tree of Life classifier (all species)"
            )
            use_tol = True
        else:
            raise RuntimeError(
                f"No labels available and Tree of Life mode is not supported "
                f"for {model_str}. Go to Settings > Labels and download "
                f"a species list for your region."
            )

    return labels, use_tol


def run_classify_job(job, runner, db_path, workspace_id, params):
    """Execute classification job. Called by JobRunner in a background thread.

    Args:
        job: job dict from JobRunner (has id, progress, errors, etc.)
        runner: JobRunner instance for push_event()
        db_path: path to SQLite database
        workspace_id: active workspace ID
        params: ClassifyParams with request parameters
    """
    raise NotImplementedError("TODO: move work() body here")
