"""Unit tests for the shared streaming source traversal."""

import json
import threading
import time
from pathlib import Path

import source_discovery
from image_loader import ScanCancelled


def _parse(frames):
    out = []
    for chunk in frames:
        if chunk.startswith("data: "):
            out.append(json.loads(chunk[len("data: "):].strip()))
    return out


def test_unique_root_names_disambiguates_same_leaf():
    names = source_discovery.unique_root_names(
        ["/mnt/cardA/DCIM", "/mnt/cardB/DCIM"])
    assert names["/mnt/cardA/DCIM"] == "cardA/DCIM"
    assert names["/mnt/cardB/DCIM"] == "cardB/DCIM"


def test_unique_root_names_single_source_is_empty():
    assert source_discovery.unique_root_names(["/photos/trip"]) == {}


def _serial_network_policy(paths):
    return [
        {
            "path": path,
            "volume_key": "nas",
            "storage": "network",
            "max_parallel": 1,
        }
        for path in paths
    ]


def test_closing_the_stream_cancels_running_walkers(monkeypatch):
    """Client disconnect must stop the disk walk, not orphan it.

    This is the property the whole streaming design exists for: an aborted
    fetch used to leave the server walking the folder to completion.
    """
    walker_started = threading.Event()
    walker_exited = threading.Event()

    def fake_discover(folder, file_types="both", recursive=True, onerror=None,
                      cancel_check=None, progress_callback=None):
        walker_started.set()
        while not cancel_check():
            time.sleep(0.01)
        walker_exited.set()
        raise ScanCancelled("cancelled")

    monkeypatch.setattr(
        source_discovery, "discover_source_files", fake_discover)
    gen = source_discovery.stream_folder_preview(
        ["/slow/nas"], classify=_serial_network_policy)
    # policy frame, folder_started frame, then one heartbeat/ping while the
    # walker grinds — the generator is suspended mid-walk at that point.
    frames = [next(gen), next(gen), next(gen)]
    parsed = _parse(frames)
    assert parsed[0]["type"] == "policy"
    assert parsed[1]["type"] == "folder_started"
    assert walker_started.wait(timeout=5)

    gen.close()
    assert walker_exited.wait(timeout=5), "walker kept running after close"


def test_stream_survives_a_crashing_walker(monkeypatch):
    """A walker that dies unexpectedly yields an error row, not a hang."""

    def exploding_discover(folder, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        source_discovery, "discover_source_files", exploding_discover)
    frames = _parse(list(source_discovery.stream_folder_preview(
        ["/photos/a"], classify=_serial_network_policy)))
    done = [f for f in frames if f["type"] == "folder_done"]
    assert done == [{
        "type": "folder_done", "path": "/photos/a", "count": 0, "error": True,
    }]
    assert frames[-1]["type"] == "done"
    assert frames[-1]["total_count"] == 0
    assert frames[-1]["source_counts"] == {"/photos/a": 0}


def test_metadata_progress_is_throttled_by_time_not_file_count(
        tmp_path, monkeypatch):
    root = tmp_path / "card"
    root.mkdir()
    files = []
    for index in range(3):
        photo = root / f"photo-{index}.jpg"
        photo.write_bytes(b"x")
        files.append(photo)

    monkeypatch.setattr(
        source_discovery, "discover_source_files", lambda *_args, **_kwargs: files,
    )
    ticks = iter([0.0, 0.1, 0.4])
    monkeypatch.setattr(source_discovery.time, "monotonic", ticks.__next__)
    events = []

    result = source_discovery._walk_folder(
        str(root), Path(root).name, False, "both", True,
        threading.Event(), events.append,
    )

    assert result["count"] == 3
    assert [event for event in events if event.get("stage") == "metadata"] == [{
        "type": "folder_progress",
        "path": str(root),
        "stage": "metadata",
        "checked": 3,
        "found": 3,
    }]
