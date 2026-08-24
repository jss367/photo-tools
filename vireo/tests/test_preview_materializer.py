import os
import sys
import threading

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


def test_render_preview_retries_original_when_working_copy_is_evicted(
    tmp_path, monkeypatch,
):
    """A quota unlink between selection and decode falls back cleanly."""
    import image_loader
    from PIL import Image

    folder = tmp_path / "photos"
    folder.mkdir()
    original = folder / "source.jpg"
    Image.new("RGB", (640, 480), color=(0, 180, 0)).save(original, "JPEG")
    vireo_dir = tmp_path / "vireo"
    working_dir = vireo_dir / "working"
    working_dir.mkdir(parents=True)
    working = working_dir / "7.jpg"
    Image.new("RGB", (640, 480), color=(180, 0, 0)).save(working, "JPEG")
    photo = {
        "id": 7,
        "folder_id": 3,
        "filename": original.name,
        "working_copy_path": "working/7.jpg",
        "width": 640,
        "height": 480,
        "companion_path": None,
    }

    real_load_image = image_loader.load_image
    loaded_paths = []

    def evicting_load(path, *args, **kwargs):
        loaded_paths.append(os.path.abspath(path))
        if os.path.abspath(path) == os.path.abspath(working):
            working.unlink()
            return None
        return real_load_image(path, *args, **kwargs)

    monkeypatch.setattr(image_loader, "load_image", evicting_load)

    rendered = render_preview_bytes(
        None,
        photo,
        str(folder),
        size=320,
        vireo_dir=str(vireo_dir),
        preview_quality=90,
    )

    assert rendered
    assert loaded_paths == [
        os.path.abspath(working), os.path.abspath(original),
    ]


def test_render_preview_pins_working_copy_when_original_is_offline(
    tmp_path, monkeypatch,
):
    """Offline preview decode holds the eviction lock for its only source."""
    import image_loader
    import working_copy_cache
    from PIL import Image

    folder = tmp_path / "offline-photos"
    folder.mkdir()
    vireo_dir = tmp_path / "vireo"
    working_dir = vireo_dir / "working"
    working_dir.mkdir(parents=True)
    working = working_dir / "7.jpg"
    Image.new("RGB", (640, 480), color=(180, 0, 0)).save(working, "JPEG")
    photo = {
        "id": 7,
        "folder_id": 3,
        "filename": "offline.NEF",
        "working_copy_path": "working/7.jpg",
        "width": 640,
        "height": 480,
        "companion_path": None,
    }

    real_load_image = image_loader.load_image
    held_during_decode = False

    def probing_load_image(path, *args, **kwargs):
        nonlocal held_during_decode
        if os.path.abspath(path) == os.path.abspath(working):
            probe = {"acquired": None}

            def try_evict_lock():
                acquired = working_copy_cache._eviction_lock.acquire(
                    blocking=False,
                )
                probe["acquired"] = acquired
                if acquired:
                    working_copy_cache._eviction_lock.release()

            thread = threading.Thread(target=try_evict_lock)
            thread.start()
            thread.join()
            held_during_decode = probe["acquired"] is False
        return real_load_image(path, *args, **kwargs)

    monkeypatch.setattr(image_loader, "load_image", probing_load_image)

    rendered = render_preview_bytes(
        None,
        photo,
        str(folder),
        size=320,
        vireo_dir=str(vireo_dir),
        preview_quality=90,
    )

    assert rendered
    assert held_during_decode, (
        "offline preview must prevent quota eviction until the working copy "
        "has been decoded"
    )
