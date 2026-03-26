import dataclasses

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
