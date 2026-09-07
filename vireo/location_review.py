"""Capture-time suggestions for reviewing photos without usable GPS."""

import math
from datetime import UTC, datetime, timedelta


def has_usable_coordinates(photo):
    try:
        lat, lng = float(photo["latitude"]), float(photo["longitude"])
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def capture_time(value):
    """Keep camera wall time separate from timestamps with known offsets.

    A date alone is insufficient evidence for grouping. Unknown offsets must
    not silently be interpreted in the server's timezone.
    """
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo else result
    except (ValueError, OverflowError):
        return None


def time_review_groups(photos, gap_minutes=60):
    """Suggest bounded outings; never infer coordinates from capture time.

    Split at a gap, calendar-day boundary, or four hours from the first photo.
    Missing/invalid times stay as singletons. Offset-aware and naive times
    form separate queues because their relative order is unknown.
    """
    dated = []
    undated = []
    for photo in photos:
        if has_usable_coordinates(photo):
            continue
        timestamp = capture_time(photo["timestamp"])
        if timestamp is None:
            undated.append([photo])
        else:
            dated.append((timestamp.tzinfo is not None, timestamp, photo))
    dated.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    batches = []
    first = previous = None
    previous_aware = None
    for aware, timestamp, photo in dated:
        if (
            first is None
            or aware != previous_aware
            or timestamp.date() != previous.date()
            or timestamp - previous > timedelta(minutes=gap_minutes)
            or timestamp - first > timedelta(hours=4)
        ):
            batches.append([])
            first = timestamp
        batches[-1].append(photo)
        previous, previous_aware = timestamp, aware
    batches.extend(sorted(undated, key=lambda batch: batch[0]["id"]))
    groups = []
    for index, batch in enumerate(batches, start=1):
        data = [
            {
                key: photo[key]
                for key in (
                    "id",
                    "filename",
                    "companion_path",
                    "timestamp",
                    "latitude",
                    "longitude",
                )
            }
            for photo in batch
        ]
        for item in data:
            # Invalid/partial coordinates are not map evidence and may include
            # non-finite values which browsers cannot parse as JSON.
            item["latitude"] = item["longitude"] = None
        groups.append(
            {
                "id": f"time-{index}",
                "grouping": "time",
                "count": len(data),
                "photo_ids": [photo["id"] for photo in data],
                "photos": data,
                "center": None,
                "bounds": None,
                "spread_m": None,
                "captured_from": data[0]["timestamp"] if capture_time(data[0]["timestamp"]) else None,
                "captured_to": data[-1]["timestamp"] if capture_time(data[-1]["timestamp"]) else None,
            }
        )
    return groups
