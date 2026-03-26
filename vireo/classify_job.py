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
