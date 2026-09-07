"""Capture-time grouping and its workspace-scoped review API."""

import pytest
from location_review import time_review_groups


def photo(index, timestamp, lat=None, lng=None):
    return {
        "id": index,
        "filename": f"photo-{index}.jpg",
        "companion_path": None,
        "timestamp": timestamp,
        "latitude": lat,
        "longitude": lng,
    }


def ids(groups):
    return [group["photo_ids"] for group in groups]


def test_outings_split_at_gaps_days_and_maximum_span():
    photos = [
        photo(i + 1, timestamp)
        for i, timestamp in enumerate(
            [
                "2026-08-01T08:00:00",
                "2026-08-01T09:00:00",
                "2026-08-01T10:00:00",
                "2026-08-01T11:00:00",
                "2026-08-01T12:00:00",
                "2026-08-01T12:01:00",
                "2026-08-01T13:02:00",
                "2026-08-01T23:59:00",
                "2026-08-02T00:01:00",
            ]
        )
    ]
    groups = time_review_groups(list(reversed(photos)))
    assert ids(groups) == [[1, 2, 3, 4, 5], [6], [7], [8], [9]]
    assert all(group["center"] is None for group in groups)
    assert groups[0]["captured_from"] == photos[0]["timestamp"]
    assert groups[0]["captured_to"] == photos[4]["timestamp"]


def test_undated_and_invalid_dates_are_individual_and_gps_is_excluded():
    groups = time_review_groups(
        [
            photo(1, None),
            photo(2, "bad date"),
            photo(3, "2026-08-01"),
            photo(4, "2026-08-01T12:00:00", 0, 0),
            photo(5, "2026-08-01T12:00:00", 91, 0),
            photo(6, "2026-08-01T12:01:00", 0, None),
            photo(7, "2026-08-01T12:02:00", float("nan"), 0),
        ]
    )
    assert ids(groups) == [[5, 6, 7], [1], [2], [3]]
    assert all(group["captured_from"] is None for group in groups[1:])


def test_offsets_are_normalized_but_never_guessed_for_naive_times():
    groups = time_review_groups(
        [
            photo(1, "2026-08-01T12:00:00+02:00"),
            photo(2, "2026-08-01T10:05:00Z"),
            photo(3, "2026-08-01T10:01:00"),
            photo(4, "2026-08-01T12:00:00Z"),
        ]
    )
    assert ids(groups) == [[3], [1, 2], [4]]


def test_gap_can_be_tightened_and_equal_times_have_stable_order():
    photos = [photo(3, "2026-08-01T10:20:00"), photo(2, "2026-08-01T10:00:00"), photo(1, "2026-08-01T10:00:00")]
    assert ids(time_review_groups(photos, 15)) == [[1, 2], [3]]
    assert ids(time_review_groups(photos, 30)) == [[1, 2, 3]]


@pytest.mark.parametrize("timestamps, expected", [
    (["2026-08-01T16:50:00-07:00", "2026-08-01T17:10:00-07:00"], [[1, 2]]),
    (["2026-08-01T23:50:00-07:00", "2026-08-02T00:10:00-07:00"], [[1], [2]]),
    (["2026-08-01T06:50:00+07:00", "2026-08-01T07:10:00+07:00"], [[1, 2]]),
])
def test_day_boundaries_use_camera_local_dates(timestamps, expected):
    photos = [photo(index + 1, timestamp) for index, timestamp in enumerate(timestamps)]
    assert ids(time_review_groups(photos)) == expected


def test_time_preview_skips_existing_locations_and_gps(app_and_db):
    app, db = app_and_db
    p1, p2, p3 = db.get_photo_ids()
    db.conn.execute("UPDATE photos SET latitude = 0, longitude = 0 WHERE id = ?", (p1,))
    location_id = db.get_or_create_text_location("Already assigned")
    db.set_photo_location(p2, location_id)
    db.conn.commit()
    response = app.test_client().post("/api/location-review/preview", json={"scope": "all", "mode": "time"})
    assert response.status_code == 200
    data = response.get_json()
    assert ids(data["groups"]) == [[p3]]
    assert {p["reason"] for p in data["skipped"]} == {"already_has_location", "has_coordinates"}
    assert data["reviewable"] == 1
    assert db.conn.execute("SELECT latitude FROM photos WHERE id = ?", (p3,)).fetchone()[0] is None


def test_all_photo_scope_respects_active_workspace(app_and_db):
    app, db = app_and_db
    db.create_workspace("Empty workspace")
    workspace = db.conn.execute("SELECT id FROM workspaces WHERE name = 'Empty workspace'").fetchone()[0]
    client = app.test_client()
    assert client.post(f"/api/workspaces/{workspace}/activate").status_code == 200
    response = client.post("/api/location-review/preview", json={"scope": "all", "mode": "time"})
    assert response.status_code == 200
    assert response.get_json()["groups"] == []


@pytest.mark.parametrize(
    "options", [{"mode": "invalid"}, {"gap_minutes": True}, {"gap_minutes": 0}, {"gap_minutes": "60"}]
)
def test_time_preview_rejects_invalid_options(app_and_db, options):
    app, _ = app_and_db
    response = app.test_client().post("/api/location-review/preview", json={"scope": "all", **options})
    assert response.status_code == 400


@pytest.mark.parametrize("source", [{"photo_ids": []}, {"photo_ids": [1]}, {"collection_id": 1}])
def test_all_photo_scope_cannot_expand_an_explicit_selection(app_and_db, source):
    app, _ = app_and_db
    response = app.test_client().post(
        "/api/location-review/preview", json={"scope": "all", "mode": "time", **source}
    )
    assert response.status_code == 400
