"""Grouping edits share persistent history with ordinary photo edits."""

import copy
import os

import pytest
from pipeline import load_results_raw, save_results_raw


def _seed(db):
    ids = sorted(p['id'] for p in db.get_photos())
    cache = {
        'photos': [{'id': pid, 'label': 'REVIEW', 'rating': 0} for pid in ids],
        'encounters': [{
            'photo_ids': ids, 'photo_count': len(ids), 'burst_count': 2,
            'bursts': [{'photo_ids': ids[:2]}, {'photo_ids': ids[2:]}],
            'species': [], 'species_predictions': [],
        }],
        'summary': {},
    }
    save_results_raw(cache, os.path.dirname(db._db_path), db._ws_id())
    return ids, cache


def _load(db):
    return load_results_raw(os.path.dirname(db._db_path), db._ws_id())


def _detach(client, kind='burst', **extra):
    response = client.post('/api/pipeline/detach-' + kind, json={
        'encounter_index': 0, 'burst_index': 0, **extra,
    })
    assert response.status_code == 200, response.get_json()


def test_grouping_and_flags_undo_redo_in_one_history(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    ids, original = _seed(db)
    _detach(client, 'photo', photo_id=ids[0])
    first = _load(db)
    client.post('/api/batch/flag', json={'photo_ids': ids, 'flag': 'rejected'})
    _detach(client)
    last = _load(db)
    assert client.get('/api/undo/status').json['count'] == 3

    # A new client/page uses the same history; grouping never restores photos.
    client = app.test_client()
    assert client.post('/api/undo').status_code == 200
    assert _load(db)['encounters'] == first['encounters']
    assert all(db.get_photo(pid)['flag'] == 'rejected' for pid in ids)
    assert client.post('/api/undo').status_code == 200
    assert all(db.get_photo(pid)['flag'] == 'none' for pid in ids)
    assert client.post('/api/undo').status_code == 200
    assert _load(db)['encounters'] == original['encounters']
    assert client.get('/api/undo/status').json['available'] is False
    for _ in range(3):
        assert client.post('/api/redo').status_code == 200
    assert _load(db)['encounters'] == last['encounters']
    assert all(db.get_photo(pid)['flag'] == 'rejected' for pid in ids)


def test_grouping_restore_preserves_new_photo_metadata(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    ids, original = _seed(db)
    _detach(client)
    updated = _load(db)
    updated['photos'][0]['rating'] = 5
    updated['photos'][0]['quality_composite'] = .95
    updated['miss_computed_at'] = 'new marker'
    save_results_raw(updated, os.path.dirname(db._db_path), db._ws_id())
    assert client.post('/api/undo').status_code == 200
    restored = _load(db)
    assert restored['encounters'] == original['encounters']
    assert restored['photos'] == updated['photos']
    assert restored['miss_computed_at'] == 'new marker'


def test_stale_grouping_undo_retains_history_and_newer_work(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    _seed(db)
    _detach(client)
    updated = _load(db)
    updated['encounters'][0]['confirmed_species'] = 'New species'
    save_results_raw(updated, os.path.dirname(db._db_path), db._ws_id())
    response = client.post('/api/undo')
    assert response.status_code == 409
    assert 'newer work' in response.json['error']
    assert _load(db) == updated
    assert client.get('/api/undo/status').json['count'] == 1
    assert client.get('/api/redo/status').json['available'] is False


@pytest.mark.parametrize('operation', ['undo', 'redo'])
def test_failed_grouping_write_can_be_retried(app_and_db, monkeypatch, operation):
    app, db = app_and_db
    client = app.test_client()
    _seed(db)
    _detach(client)
    if operation == 'redo':
        assert client.post('/api/undo').status_code == 200
    before = copy.deepcopy(_load(db))
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('Disk unavailable')
        patch.setattr('pipeline.save_results_raw', fail)
        assert client.post('/api/' + operation).status_code == 500
    assert _load(db) == before
    assert client.get('/api/' + operation + '/status').json['available'] is True
    assert client.post('/api/' + operation).status_code == 200


def test_new_grouping_edit_clears_redo(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    _detach(client)
    assert client.post('/api/undo').status_code == 200
    _detach(client, 'photo', photo_id=ids[0])
    assert client.get('/api/redo/status').json['available'] is False


def test_processing_lock_does_not_stall_undo_or_consume_history(app_and_db):
    from pipeline_locks import acquire_workspace_regroup

    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    _detach(client)
    client.post(f'/api/photos/{ids[0]}/flag', json={'flag': 'flagged'})
    with acquire_workspace_regroup(db._ws_id()):
        # Ordinary edits can still be undone during processing.
        assert client.post('/api/undo').status_code == 200
        # Grouping undo fails promptly and is retryable when the lock releases.
        assert client.post('/api/undo').status_code == 409
        assert client.get('/api/undo/status').json['count'] == 1
    assert client.post('/api/undo').status_code == 200


def test_grouping_history_is_workspace_scoped(app_and_db):
    app, db = app_and_db
    client = app.test_client()
    _seed(db)
    _detach(client)
    original_workspace = db._ws_id()
    other = db.create_workspace('Another trip')
    # The app's request connections follow the active workspace stored in meta.
    assert client.post(f'/api/workspaces/{other}/activate', json={}).status_code == 200
    assert client.get('/api/undo/status').json['available'] is False
    assert client.post(f'/api/workspaces/{original_workspace}/activate', json={}).status_code == 200
    assert client.get('/api/undo/status').json['available'] is True
    assert client.post('/api/undo').status_code == 200
