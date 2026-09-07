"""``/api/predictions/compare``: one page of rows, and honest counts.

The endpoint derives the whole comparison once, keeps it as a snapshot and
hands the browser a token. These tests cover what the page depends on: that
a page is a page, that the counts describe the collection rather than the
page, and that a decision moves both together.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def compare_collection(app_and_db):
    """An app whose collection holds three photos with predictions."""
    app, db = app_and_db
    photo_ids = [
        row["id"] for row in
        db.conn.execute("SELECT id FROM photos ORDER BY id").fetchall()
    ]
    cardinal = db.add_keyword("Cardinal", is_species=True)
    db.tag_photo(photo_ids[0], cardinal)
    for index, photo_id in enumerate(photo_ids):
        det_ids = db.save_detections(photo_id, [{
            "box": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
            "confidence": 0.9,
            "category": "animal",
        }], detector_model="MDV6")
        db.add_prediction(det_ids[0], "Cardinal" if index == 0 else "Blue Jay",
                          0.9 - index / 10, "model-a")
        db.add_prediction(det_ids[0], "Cardinal" if index == 0 else "Sparrow",
                          0.8 - index / 10, "model-b")
    cid = db.add_collection(
        "Everything", json.dumps([{"field": "photo_ids", "value": photo_ids}]),
    )
    return app, db, cid, photo_ids


def _get(app, cid, **params):
    query = "&".join(
        f"{key}={value}" for key, value in [("collection_id", cid)] + list(params.items())
    )
    response = app.test_client().get(f"/api/predictions/compare?{query}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_compare_returns_one_page_and_collection_wide_counts(compare_collection):
    app, _db, cid, photo_ids = compare_collection

    payload = _get(app, cid, filter="all", per_page=2)

    assert len(payload["photos"]) == 2
    assert payload["total"] == len(photo_ids)
    assert payload["summary"]["photos"] == len(photo_ids)
    assert payload["page"] == 1
    assert payload["per_page"] == 2
    assert payload["token"]
    assert set(payload["models"]) == {"model-a", "model-b"}


def test_compare_pages_do_not_repeat_or_drop_rows(compare_collection):
    app, _db, cid, photo_ids = compare_collection

    first = _get(app, cid, filter="all", sort="filename", per_page=2)
    second = _get(
        app, cid, filter="all", sort="filename", per_page=2, page=2,
        token=first["token"],
    )

    seen = [photo["photo_id"] for photo in first["photos"] + second["photos"]]
    assert sorted(seen) == sorted(photo_ids)


def test_compare_reuses_the_snapshot_behind_its_token(compare_collection):
    app, _db, cid, _photo_ids = compare_collection

    first = _get(app, cid, filter="all")
    second = _get(app, cid, filter="conflict", token=first["token"])

    assert second["token"] == first["token"]


def test_changing_the_threshold_rederives_the_comparison(compare_collection):
    """The conflict threshold changes what every row means, so the snapshot
    it was derived under cannot answer for the new one."""
    app, _db, cid, _photo_ids = compare_collection

    first = _get(app, cid, filter="all")
    second = _get(app, cid, filter="all", token=first["token"], min_confidence=0.9)

    assert second["token"] != first["token"]


def test_narrowing_to_one_model_rederives_the_comparison(compare_collection):
    app, _db, cid, _photo_ids = compare_collection

    first = _get(app, cid, filter="all")
    second = _get(app, cid, filter="all", token=first["token"], model="model-a")

    assert second["token"] != first["token"]
    assert second["visible_models"] == ["model-a"]
    assert set(second["models"]) == {"model-a", "model-b"}


def test_filter_counts_cover_every_filter_the_page_offers(compare_collection):
    app, _db, cid, photo_ids = compare_collection

    payload = _get(app, cid, filter="all")

    assert {item["id"] for item in payload["filters"]} == set(payload["filter_counts"])
    assert payload["filter_counts"]["all"] == len(photo_ids)
    assert {item["id"] for item in payload["excludes"]} == set(payload["exclusion_counts"])


def test_rows_carry_the_status_and_signal_the_page_renders(compare_collection):
    app, _db, cid, _photo_ids = compare_collection

    payload = _get(app, cid, filter="all", per_page=200)
    row = next(
        photo for photo in payload["photos"]
        if photo["signal"]["model_disagreement_score"] > 0
    )

    assert row["status"]["category"] == "models_disagree"
    assert len(row["subject_statuses"]) == len(row["subjects"])
    assert row["subjects"][0]["status"]["category"] == "models_disagree"
    assert "vs" in row["signal"]["model_disagreement_label"]


def test_a_decision_updates_the_counts_through_refresh_photo_id(compare_collection):
    """A refreshed photo is rebuilt inside the snapshot, so the chip counts
    follow the work instead of reporting the state before the click."""
    app, db, cid, photo_ids = compare_collection
    client = app.test_client()

    first = _get(app, cid, filter="needs_review")
    assert first["summary"]["needs_review"] == len(photo_ids)

    for row in db.get_predictions(photo_ids=[photo_ids[0]]):
        db.update_prediction_status(row["id"], "reviewed", _commit=False)
    db.conn.commit()

    refreshed = client.get(
        f"/api/predictions/compare?collection_id={cid}&filter=needs_review"
        f"&token={first['token']}&refresh_photo_id={photo_ids[0]}"
    ).get_json()

    assert refreshed["token"] == first["token"]
    assert refreshed["summary"]["needs_review"] == len(photo_ids) - 1
    assert photo_ids[0] not in [
        photo["photo_id"] for photo in refreshed["photos"]
    ]


def test_refresh_photo_id_picks_up_a_sibling_that_just_joined(app_and_db):
    """A grouped decision can hand ``refresh_photo_id`` a sibling that was
    outside the snapshot but now satisfies a keyword-based collection rule.
    The snapshot must rebuild the sibling and append it to the records — it
    cannot silently discard IDs it did not already index."""
    app, db = app_and_db
    photo_ids = [
        row["id"] for row in
        db.conn.execute("SELECT id FROM photos ORDER BY id").fetchall()
    ]
    bird = db.add_keyword("Bird", is_species=True)
    db.tag_photo(photo_ids[0], bird)
    db.tag_photo(photo_ids[1], bird)
    # Only photos with the "Bird" species keyword satisfy the rule, so the
    # third photo starts outside the collection until it is tagged too.
    for photo_id in photo_ids:
        det_ids = db.save_detections(photo_id, [{
            "box": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
            "confidence": 0.9,
            "category": "animal",
        }], detector_model="MDV6")
        db.add_prediction(det_ids[0], "Bird", 0.9, "model-a")
    cid = db.add_collection(
        "Birds",
        json.dumps([{"field": "keyword", "op": "equals", "value": "Bird"}]),
    )

    first = _get(app, cid, filter="all")
    assert first["total"] == 2

    db.tag_photo(photo_ids[2], bird)

    refresh_url = (
        f"/api/predictions/compare?collection_id={cid}&filter=all"
        f"&token={first['token']}"
        f"&refresh_photo_id={photo_ids[0]}"
        f"&refresh_photo_id={photo_ids[1]}"
        f"&refresh_photo_id={photo_ids[2]}"
    )
    refreshed = app.test_client().get(refresh_url).get_json()

    assert refreshed["token"] == first["token"]
    assert refreshed["total"] == 3
    assert set(photo["photo_id"] for photo in refreshed["photos"]) == set(photo_ids)


def test_targeted_photo_ids_still_return_full_rows(compare_collection):
    """The decision path asks for named photos and gets the same row shape,
    assessment included."""
    app, _db, cid, photo_ids = compare_collection

    response = app.test_client().get(
        f"/api/predictions/compare?collection_id={cid}&photo_id={photo_ids[1]}"
    )

    payload = response.get_json()
    assert [photo["photo_id"] for photo in payload["photos"]] == [photo_ids[1]]
    assert payload["photos"][0]["status"]["category"]
    assert "token" not in payload


def test_per_page_is_capped(compare_collection):
    app, _db, cid, _photo_ids = compare_collection

    payload = _get(app, cid, filter="all", per_page=100000)

    assert payload["per_page"] == 200


def test_page_past_the_end_is_clamped_to_the_last_page(compare_collection):
    """A decision that shrinks the queue can leave the caller on a page that
    no longer exists. The endpoint clamps the page rather than echoing an
    impossible one like "page 3 of 2" with an empty row list."""
    app, _db, cid, photo_ids = compare_collection

    payload = _get(app, cid, filter="all", per_page=2, page=99)

    expected_last = (len(photo_ids) + 1) // 2
    assert payload["page"] == expected_last
    assert payload["total"] == len(photo_ids)
    assert payload["photos"]


def test_search_narrows_the_listed_rows(compare_collection):
    app, db, cid, photo_ids = compare_collection

    payload = _get(app, cid, filter="all", q="bird2")

    assert [photo["photo_id"] for photo in payload["photos"]] == [photo_ids[1]]
    assert payload["total"] == 1


def test_excludes_are_applied_and_counted(compare_collection):
    app, db, cid, photo_ids = compare_collection
    db.update_photo_flag(photo_ids[0], "rejected")

    payload = _get(app, cid, filter="all", exclude="rejected")

    assert payload["total"] == len(photo_ids) - 1
    assert payload["exclusion_counts"]["rejected"] == 1


def test_unknown_filter_and_sort_fall_back_instead_of_failing(compare_collection):
    app, _db, cid, photo_ids = compare_collection

    payload = _get(app, cid, filter="not-a-filter", sort="not-a-sort")

    assert payload["total"] == len(photo_ids)
