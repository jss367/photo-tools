"""Grouping edits share persistent history with ordinary photo edits."""

import contextlib
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


def test_stale_grouping_entry_is_retired_and_preserves_newer_work(app_and_db):
    """A stale grouping entry never blocks older undoable edits.

    When reflow, regroup-live, or a later pipeline run replaces the cached
    encounters after a detach, the entry's ``after`` snapshot no longer
    matches the cache and can never be restored without erasing that newer
    work. Undo must retire the stale row (deleting only the row, not the
    cache) so the search resumes with the next candidate — otherwise the
    row would remain the newest undoable grouping entry forever and every
    older undoable edit would sit permanently unreachable behind it.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    # Older undoable edit that must remain reachable through undo.
    client.post('/api/batch/flag', json={'photo_ids': ids, 'flag': 'flagged'})
    _detach(client)
    updated = _load(db)
    # Structural change (a burst split) mirrors what reflow/regroup-live
    # would land: the cache no longer matches the detach entry's snapshot.
    updated['encounters'][0]['bursts'].append({'photo_ids': [ids[0]]})
    save_results_raw(updated, os.path.dirname(db._db_path), db._ws_id())

    # Undo retires the stale detach and then undoes the older flag edit.
    response = client.post('/api/undo')
    assert response.status_code == 200, response.get_json()
    assert 'flag' in response.get_json()['undone'].lower()
    assert _load(db)['encounters'] == updated['encounters']
    assert all(db.get_photo(pid)['flag'] == 'none' for pid in ids)
    assert client.get('/api/undo/status').json['count'] == 0
    # Redo replays the flag edit; the retired detach never resurfaces.
    assert client.post('/api/redo').status_code == 200
    assert all(db.get_photo(pid)['flag'] == 'flagged' for pid in ids)
    assert client.get('/api/redo/status').json['available'] is False
    assert _load(db)['encounters'] == updated['encounters']


def test_grouping_undo_preserves_cleared_burst_override(app_and_db):
    """A ``clearBurstOverride`` clear survives the next grouping undo.

    ``clearBurstOverride`` posts ``species_override = None`` through
    ``/api/pipeline/save-cache`` and records no history entry. The next
    grouping undo therefore sees the cache as structurally unchanged
    (``_grouping_signature`` ignores ``species_override``) and would
    otherwise silently resurrect the pre-clear override from the
    detach's ``before`` snapshot. The clear must carry through instead.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, original = _seed(db)
    # Seed a burst-level override the detach's ``before`` snapshot
    # captures, so restoring it without a preservation pass would
    # resurrect the old value.
    original_state = _load(db)
    original_state['encounters'][0]['bursts'][0]['species_override'] = {
        'species': 'Old override', 'confirmed': True,
    }
    save_results_raw(original_state, os.path.dirname(db._db_path), db._ws_id())
    original = _load(db)

    _detach(client)
    detached = _load(db)
    # The detached burst is the new encounter appended by the detach
    # handler — locate it by photo composition so this test survives any
    # future reordering.
    detached_photo_ids = ids[:2]
    detached_enc_idx = next(
        i for i, enc in enumerate(detached['encounters'])
        if enc.get('photo_ids') == detached_photo_ids
    )
    updated = copy.deepcopy(detached)
    updated['encounters'][detached_enc_idx]['bursts'][0]['species_override'] = None
    save_results_raw(updated, os.path.dirname(db._db_path), db._ws_id())

    assert client.post('/api/undo').status_code == 200
    restored = _load(db)
    # Structure returns to the pre-detach shape, but the user's clear on
    # that burst is preserved rather than silently overwritten by the
    # detach's stale before-snapshot value.
    assert len(restored['encounters']) == len(original['encounters'])
    matching_burst = next(
        b for b in restored['encounters'][0]['bursts']
        if b.get('photo_ids') == detached_photo_ids
    )
    assert matching_burst.get('species_override') is None


def test_grouping_undo_restores_summary_counts(app_and_db):
    """Restored encounter/burst counts stay in sync with the restored groups.

    ``/api/pipeline/page-init`` reads structural counts off ``summary``
    and the review page's ``updateSummaryBar()`` keeps them when the
    normal ``confirmed_count`` fields are present, so a stale summary
    left behind after undo shows the wrong encounter/burst totals.
    """
    app, db = app_and_db
    client = app.test_client()
    _seed(db)
    _detach(client)
    after_detach = _load(db)
    assert after_detach['summary']['encounter_count'] == 2
    assert after_detach['summary']['burst_count'] == 2

    assert client.post('/api/undo').status_code == 200
    restored = _load(db)
    assert restored['summary']['encounter_count'] == 1
    assert restored['summary']['burst_count'] == 2

    assert client.post('/api/redo').status_code == 200
    replayed = _load(db)
    assert replayed['summary']['encounter_count'] == 2
    assert replayed['summary']['burst_count'] == 2


def test_grouping_undo_tolerates_reverted_species_cache_state(app_and_db):
    """A species edit's leftover cache does not block a later grouping undo.

    ``/api/encounters/species`` writes ``confirmed_species`` /
    ``species_override`` into the cache but records its DB revert as an
    ordinary undoable edit. After that species edit is undone, its cache
    state remains — undo is strict LIFO, so a subsequent grouping undo is
    not overwriting active newer work and must succeed.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, original = _seed(db)
    _detach(client)
    detached = _load(db)
    updated = copy.deepcopy(detached)
    updated['encounters'][0]['confirmed_species'] = 'New species'
    updated['encounters'][0]['species_confirmed'] = True
    updated['encounters'][0]['bursts'][0]['species_override'] = {
        'species': 'New species', 'confirmed': True,
    }
    save_results_raw(updated, os.path.dirname(db._db_path), db._ws_id())
    response = client.post('/api/undo')
    assert response.status_code == 200, response.get_json()
    assert _load(db)['encounters'] == original['encounters']


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


def test_cache_only_species_confirm_is_undone_before_older_grouping_edit(app_and_db):
    """A cache-only encounter confirmation is undoable in LIFO with a detach.

    ``/api/encounters/species`` writes ``confirmed_species`` /
    ``species_confirmed`` into the cache regardless of whether any photo's
    keyword tags change. When every submitted photo already carries the
    requested species keyword (``newly_tagged`` is empty), no
    ``keyword_add`` history row would be recorded. Without a dedicated
    entry, a preceding grouping edit stays newest and its undo silently
    discards the still-active confirmation because grouping signatures
    strip these species fields by design.

    A ``species_confirm_cache`` entry restores LIFO order: the first undo
    reverts the cache confirmation, the second undo reverts the detach
    with pristine species state.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, original = _seed(db)
    kid = db.conn.execute(
        "SELECT id FROM keywords WHERE name = ? COLLATE NOCASE", ('Cardinal',),
    ).fetchone()[0]
    # Pre-tag every seeded photo with Cardinal so a subsequent confirmation
    # produces no ``newly_tagged`` and no ``keyword_add`` history row.
    for pid in ids:
        # Already-tagged rows raise; ignore.
        with contextlib.suppress(Exception):
            db.tag_photo(pid, kid, source='manual')
    db.conn.commit()

    _detach(client)
    detached = _load(db)
    # Locate the pre-detach parent encounter that survived (still holds the
    # remaining burst) and confirm species on it. All its photos already
    # carry Cardinal, so this write is cache-only.
    parent_enc = next(
        enc for enc in detached['encounters']
        if len(enc.get('bursts') or []) > 0
        and set(enc.get('photo_ids') or []) != set(ids[:2])
    )
    response = client.post('/api/encounters/species', json={
        'species': 'Cardinal',
        'photo_ids': parent_enc['photo_ids'],
    })
    assert response.status_code == 200, response.get_json()

    confirmed = _load(db)
    matched = next(
        enc for enc in confirmed['encounters']
        if enc.get('photo_ids') == parent_enc['photo_ids']
    )
    assert matched.get('confirmed_species') == 'Cardinal'
    assert matched.get('species_confirmed') is True
    # Both edits are undoable: the detach and the cache-only confirmation.
    assert client.get('/api/undo/status').json['count'] == 2

    # First undo reverts the cache confirmation, not the detach.
    response = client.post('/api/undo')
    assert response.status_code == 200, response.get_json()
    after_first_undo = _load(db)
    reverted = next(
        enc for enc in after_first_undo['encounters']
        if enc.get('photo_ids') == parent_enc['photo_ids']
    )
    assert reverted.get('confirmed_species') is None
    assert not reverted.get('species_confirmed')
    # Structure still shows the detach — its history entry is next in line.
    assert len(after_first_undo['encounters']) == len(detached['encounters'])

    # Second undo reverts the detach itself.
    response = client.post('/api/undo')
    assert response.status_code == 200, response.get_json()
    assert _load(db)['encounters'] == original['encounters']

    # Both redos replay in order.
    assert client.post('/api/redo').status_code == 200
    assert client.post('/api/redo').status_code == 200
    replayed = _load(db)
    replayed_match = next(
        enc for enc in replayed['encounters']
        if enc.get('photo_ids') == parent_enc['photo_ids']
    )
    assert replayed_match.get('confirmed_species') == 'Cardinal'
    assert replayed_match.get('species_confirmed') is True


def test_cache_only_burst_confirm_that_auto_detaches_is_undoable(app_and_db):
    """A cache-only burst confirmation that triggers auto-detach records history.

    When every photo in a multi-burst encounter already carries a species
    different from the encounter's confirmed species, ``/api/encounters/species``
    would restructure the cache via ``auto_detach_burst_for_species`` without
    recording any keyword change. Before this fix the structural change was
    invisible to undo, so pressing Undo popped an unrelated older edit.
    The route now persists the auto-detach as a grouping edit so undo
    reverses the burst move and re-attaches it to its original encounter.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    # Tag every photo with Cardinal so the first burst carries a species
    # that differs from the encounter's confirmed one.
    kid = db.conn.execute(
        "SELECT id FROM keywords WHERE name = ? COLLATE NOCASE", ('Cardinal',),
    ).fetchone()[0]
    for pid in ids:
        with contextlib.suppress(Exception):
            db.tag_photo(pid, kid, source='manual')
    db.conn.commit()
    # Seed the cache so the encounter is confirmed as Sparrow — the confirm
    # below asks for Cardinal on burst 0, which triggers auto-detach.
    seeded = _load(db)
    seeded['encounters'][0]['confirmed_species'] = 'Sparrow'
    seeded['encounters'][0]['species_confirmed'] = True
    save_results_raw(seeded, os.path.dirname(db._db_path), db._ws_id())

    burst_photo_ids = ids[:2]
    response = client.post('/api/encounters/species', json={
        'species': 'Cardinal',
        'photo_ids': burst_photo_ids,
        'burst_index': 0,
    })
    assert response.status_code == 200, response.get_json()
    after_confirm = _load(db)
    # Auto-detach split the burst into its own encounter with Cardinal.
    assert len(after_confirm['encounters']) == 2
    assert client.get('/api/undo/status').json['count'] == 1

    assert client.post('/api/undo').status_code == 200, \
        'auto-detach must be reversible via undo'
    restored = _load(db)
    assert len(restored['encounters']) == 1
    assert sorted(restored['encounters'][0]['photo_ids']) == sorted(ids)
    assert restored['encounters'] == seeded['encounters']

    assert client.post('/api/redo').status_code == 200
    replayed = _load(db)
    assert replayed['encounters'] == after_confirm['encounters']


def test_cache_only_confirm_rolls_back_when_cache_write_fails(
    app_and_db, monkeypatch,
):
    """A failed cache save rolls back the species_confirm_cache history row.

    ``record_species_confirm_cache`` is committed with the rest of the
    request. If ``save_results_raw`` fails afterward, the DB row must roll
    back too — otherwise Undo would report a change that never landed on
    disk, and Redo could re-apply a confirmation the user never saw.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    kid = db.conn.execute(
        "SELECT id FROM keywords WHERE name = ? COLLATE NOCASE", ('Cardinal',),
    ).fetchone()[0]
    for pid in ids:
        with contextlib.suppress(Exception):
            db.tag_photo(pid, kid, source='manual')
    db.conn.commit()

    before_count = client.get('/api/undo/status').json['count']
    before_cache = _load(db)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('Disk full')
        patch.setattr('pipeline.save_results_raw', fail)
        response = client.post('/api/encounters/species', json={
            'species': 'Cardinal',
            'photo_ids': ids,
        })
        assert response.status_code == 500
    # The failed request must not leave a species_confirm_cache entry
    # sitting in undo history for a change that never happened.
    assert client.get('/api/undo/status').json['count'] == before_count
    assert _load(db) == before_cache


def test_burst_confirm_undo_preserves_cleared_override(app_and_db):
    """Undoing a cache-only burst confirmation preserves a later clear.

    After a cache-only burst confirmation, ``clearBurstOverride`` can
    write ``species_override = None`` through ``/api/pipeline/save-cache``
    without a history entry. Undoing the still-latest confirmation must
    not blindly restore the recorded previous override — that would
    silently resurrect a state the user cleared.
    """
    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    kid = db.conn.execute(
        "SELECT id FROM keywords WHERE name = ? COLLATE NOCASE", ('Cardinal',),
    ).fetchone()[0]
    for pid in ids:
        with contextlib.suppress(Exception):
            db.tag_photo(pid, kid, source='manual')
    db.conn.commit()
    # Seed a burst-level previous override so undo of the confirmation
    # would (before the fix) restore it and overwrite the user's clear.
    seeded = _load(db)
    seeded['encounters'][0]['bursts'][0]['species_override'] = {
        'species': 'Old', 'confirmed': True,
    }
    save_results_raw(seeded, os.path.dirname(db._db_path), db._ws_id())

    burst_photo_ids = ids[:2]
    response = client.post('/api/encounters/species', json={
        'species': 'Cardinal',
        'photo_ids': burst_photo_ids,
        'burst_index': 0,
    })
    assert response.status_code == 200, response.get_json()
    # Simulate ``clearBurstOverride``: user picks "Use encounter label",
    # save-cache writes ``species_override = None`` with no history.
    cleared = _load(db)
    cleared['encounters'][0]['bursts'][0]['species_override'] = None
    response = client.post('/api/pipeline/save-cache', json=cleared)
    assert response.status_code == 200

    assert client.post('/api/undo').status_code == 200
    restored = _load(db)
    assert restored['encounters'][0]['bursts'][0].get('species_override') is None


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


@pytest.mark.parametrize('cache_only', [False, True])
@pytest.mark.parametrize('auto_detach', [False, True])
def test_species_confirmation_restores_cache_on_commit_failure(
    app_and_db, monkeypatch, cache_only, auto_detach,
):
    """A failed DB commit restores the complete pre-confirmation cache."""
    from db import Database

    app, db = app_and_db
    client = app.test_client()
    ids, _ = _seed(db)
    if cache_only:
        kid = db.add_keyword('Cardinal', is_species=True)
        for pid in ids:
            db.tag_photo(pid, kid)
    before = _load(db)
    if auto_detach:
        before['encounters'][0]['confirmed_species'] = 'Sparrow'
        before['encounters'][0]['species_confirmed'] = True
    save_results_raw(before, os.path.dirname(db._db_path), db._ws_id())
    keywords_before = {pid: db.get_photo_keywords(pid) for pid in ids}
    history_before = db.get_edit_history()
    pending_before = [dict(row) for row in db.conn.execute('SELECT * FROM pending_changes')]
    original_init = Database.__init__

    class FailCommit:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            raise OSError('Commit failed')

    with monkeypatch.context() as patch:
        def fail_commit_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.conn = FailCommit(self.conn)

        patch.setattr(Database, '__init__', fail_commit_init)
        payload = {'species': 'Cardinal', 'photo_ids': ids[:2] if auto_detach else ids}
        if auto_detach:
            payload['burst_index'] = 0
        response = client.post('/api/encounters/species', json=payload)
        assert response.status_code == 500
    assert _load(db) == before
    assert db.get_edit_history() == history_before
    assert {pid: db.get_photo_keywords(pid) for pid in ids} == keywords_before
    assert [dict(row) for row in db.conn.execute('SELECT * FROM pending_changes')] == pending_before
    assert client.post('/api/encounters/species', json=payload).status_code == 200
