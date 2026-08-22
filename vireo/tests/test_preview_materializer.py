import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preview_materializer import (
    PreviewSourceUnavailable,
    materialize_preview,
    render_preview_bytes,
)


def test_render_preview_rejects_missing_source_folder():
    with pytest.raises(PreviewSourceUnavailable, match="source folder"):
        render_preview_bytes(
            None,
            {"id": 7, "folder_id": 3},
            None,
            size=1920,
            vireo_dir="/unused",
            preview_quality=90,
        )


def test_coordinated_best_effort_publication_is_rejected():
    with pytest.raises(ValueError, match="reliable artifact publication"):
        materialize_preview(
            None,
            {"id": 7},
            "/unused",
            size=1920,
            vireo_dir="/unused",
            preview_quality=90,
            cache_path="/unused/7_1920.jpg",
            coordinate=True,
            publish_best_effort=True,
        )
