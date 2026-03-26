import dataclasses
import json
import os

import pytest

from classify_job import ClassifyParams, run_classify_job


def test_classify_params_is_dataclass():
    """ClassifyParams is a dataclass with all required fields."""
    assert dataclasses.is_dataclass(ClassifyParams)
    fields = {f.name for f in dataclasses.fields(ClassifyParams)}
    assert fields == {
        "collection_id",
        "labels_file",
        "labels_files",
        "model_id",
        "model_name",
        "grouping_window",
        "similarity_threshold",
        "reclassify",
    }


def test_run_classify_job_is_callable():
    """run_classify_job exists and is callable."""
    assert callable(run_classify_job)


# ── Task 2: _load_taxonomy and _load_labels tests ──────────────────────────


class FakeRunner:
    """Minimal runner that records push_event calls."""

    def __init__(self):
        self.events = []

    def push_event(self, job_id, event_type, data):
        self.events.append((job_id, event_type, data))


def _make_job(job_id="classify-test"):
    return {
        "id": job_id,
        "progress": {"current": 0, "total": 0, "current_file": "", "rate": 0},
        "errors": [],
    }


def test_taxonomy_loads_when_file_exists(tmp_path):
    """Phase 1: taxonomy.json is loaded when present."""
    tax_data = {
        "last_updated": "2024-01-01",
        "taxa_by_common": {
            "northern cardinal": {
                "taxon_id": 9083,
                "scientific_name": "Cardinalis cardinalis",
                "common_name": "Northern Cardinal",
                "rank": "species",
                "lineage_names": [
                    "Animalia", "Chordata", "Aves",
                    "Passeriformes", "Cardinalidae", "Cardinalis",
                    "Cardinalis cardinalis",
                ],
                "lineage_ranks": [
                    "kingdom", "phylum", "class",
                    "order", "family", "genus", "species",
                ],
            }
        },
        "taxa_by_scientific": {},
    }
    tax_path = tmp_path / "taxonomy.json"
    tax_path.write_text(json.dumps(tax_data))

    from classify_job import _load_taxonomy

    tax = _load_taxonomy(str(tax_path))
    assert tax is not None
    assert tax.taxa_count >= 1


def test_taxonomy_returns_none_when_missing(tmp_path):
    """Phase 1: returns None when taxonomy.json doesn't exist."""
    from classify_job import _load_taxonomy

    tax = _load_taxonomy(str(tmp_path / "nonexistent.json"))
    assert tax is None


def test_load_labels_from_file(tmp_path):
    """Phase 2: labels loaded from a single file path."""
    labels_file = tmp_path / "labels.txt"
    labels_file.write_text("Northern Cardinal\nBlue Jay\nAmerican Robin\n")

    from classify_job import _load_labels

    labels, use_tol = _load_labels(
        model_type="bioclip",
        model_str="hf-hub:imageomics/bioclip",
        labels_file=str(labels_file),
        labels_files=None,
    )
    assert labels == ["Northern Cardinal", "Blue Jay", "American Robin"]
    assert use_tol is False


def test_load_labels_tol_fallback():
    """Phase 2: Tree of Life mode when no labels and model supports it."""
    from unittest.mock import patch

    from classify_job import _load_labels

    # Mock get_active_labels to return empty so we fall through to ToL
    with patch("classify_job.get_active_labels", return_value=[]):
        labels, use_tol = _load_labels(
            model_type="bioclip",
            model_str="hf-hub:imageomics/bioclip",
            labels_file=None,
            labels_files=None,
        )
    assert labels is None
    assert use_tol is True


def test_load_labels_timm_skips():
    """Phase 2: timm models skip label loading entirely."""
    from classify_job import _load_labels

    labels, use_tol = _load_labels(
        model_type="timm",
        model_str="hf-hub:timm/some_model",
        labels_file=None,
        labels_files=None,
    )
    assert labels is None
    assert use_tol is False


def test_load_labels_raises_when_no_labels_unsupported_model():
    """Phase 2: raises RuntimeError when no labels and model doesn't support ToL."""
    from unittest.mock import patch

    from classify_job import _load_labels

    # Mock get_active_labels to return empty
    with patch("classify_job.get_active_labels", return_value=[]):
        with pytest.raises(RuntimeError, match="No labels available"):
            _load_labels(
                model_type="bioclip",
                model_str="hf-hub:some/unsupported-model",
                labels_file=None,
                labels_files=None,
            )
