import json
import os
import sys
import time

import pytest


def test_api_darktable_status(app_and_db):
    """GET /api/darktable/status returns availability info."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.get('/api/darktable/status')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'available' in data
    assert isinstance(data['available'], bool)
    assert 'bin' in data


def test_api_job_develop_requires_photo_ids(app_and_db):
    """POST /api/jobs/develop returns 400 without photo_ids."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.post('/api/jobs/develop',
                       data=json.dumps({}),
                       content_type='application/json')
    assert resp.status_code == 400


def test_api_config_saves_darktable_settings(app_and_db):
    """POST /api/config saves darktable settings."""
    app, _ = app_and_db
    client = app.test_client()
    resp = client.post('/api/config',
                       data=json.dumps({
                           "darktable_bin": "/usr/local/bin/darktable-cli",
                           "darktable_style": "Wildlife",
                           "darktable_output_format": "tiff",
                           "darktable_output_dir": "/output",
                           "darktable_auto_convert_dng": True,
                           "dng_converter_bin": "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter",
                       }),
                       content_type='application/json')
    assert resp.status_code == 200

    resp2 = client.get('/api/config')
    cfg = resp2.get_json()
    assert cfg["darktable_bin"] == "/usr/local/bin/darktable-cli"
    assert cfg["darktable_style"] == "Wildlife"
    assert cfg["darktable_output_format"] == "tiff"
    assert cfg["darktable_output_dir"] == "/output"
    assert cfg["darktable_auto_convert_dng"] is True
    assert cfg["dng_converter_bin"] == "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter"


def _poll_job(client, job_id, timeout_iters=50):
    """Poll /api/jobs/<id> until it reaches a terminal state or times out."""
    data = None
    for _ in range(timeout_iters):
        resp = client.get(f'/api/jobs/{job_id}')
        data = resp.get_json()
        if data['status'] in ('completed', 'failed', 'cancelled'):
            return data
        time.sleep(0.05)
    return data


def test_api_job_develop_all_failures_marks_job_failed(app_and_db, tmp_path, monkeypatch):
    """If every develop_photo call fails, the rollup status must be 'failed',
    not 'completed' (rollups with any failed item report failure)."""
    app, db = app_and_db
    client = app.test_client()

    # develop requires a configured/findable binary or we short-circuit with
    # a 400 before the job even starts. Monkeypatch find_darktable in the
    # develop module so the endpoint proceeds into the job.
    import develop as develop_mod
    fake_bin = str(tmp_path / "darktable-cli")
    with open(fake_bin, "w") as f:
        f.write("")
    os.chmod(fake_bin, 0o755)
    monkeypatch.setattr(develop_mod, "find_darktable", lambda _p: fake_bin)

    # Make every develop_photo call fail deterministically.
    monkeypatch.setattr(
        develop_mod,
        "develop_photo",
        lambda **kwargs: {
            "success": False,
            "output_path": kwargs.get("output_path", ""),
            "error": "fake failure",
        },
    )

    # Pick one photo from the fixture.
    photos = db.get_photos(per_page=1)
    assert photos, "fixture should provide at least one photo"
    pid = photos[0]['id']

    resp = client.post(
        '/api/jobs/develop',
        data=json.dumps({"photo_ids": [pid]}),
        content_type='application/json',
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job_id = resp.get_json()['job_id']

    data = _poll_job(client, job_id)
    assert data is not None
    # Bug being fixed: used to be 'completed' with 0/1 developed.
    assert data['status'] == 'failed', f"expected failed, got {data['status']}: {data}"
    # Result counts must still be present so the UI can show them.
    result = data.get('result') or {}
    assert result.get('developed') == 0
    assert result.get('errors') == 1
    assert result.get('total') == 1
    # And the primary per-photo error should be surfaced in job['errors'].
    errs = data.get('errors') or []
    assert any('fake failure' in e for e in errs), f"expected fake failure in errors: {errs}"
    # Regression: the rollup failure raise must not synthesize a second,
    # non-matching error string that _run_job then appends on top of the
    # real per-photo failure (would inflate error_count to 2 for 1 photo).
    assert len(errs) == 1, f"expected exactly one error entry, got {len(errs)}: {errs}"


def test_api_job_develop_mixed_outcomes_marks_job_failed(app_and_db, tmp_path, monkeypatch):
    """If some photos succeed and some fail, the rollup status is still
    'failed' (any failure => failed, per the rollup rule)."""
    app, db = app_and_db
    client = app.test_client()

    import develop as develop_mod
    fake_bin = str(tmp_path / "darktable-cli")
    with open(fake_bin, "w") as f:
        f.write("")
    os.chmod(fake_bin, 0o755)
    monkeypatch.setattr(develop_mod, "find_darktable", lambda _p: fake_bin)

    # Alternate success/failure based on input filename.
    def flaky_develop(**kwargs):
        in_path = kwargs.get("input_path", "")
        if "bird1" in in_path:
            return {"success": True, "output_path": kwargs["output_path"], "error": None}
        return {
            "success": False,
            "output_path": kwargs["output_path"],
            "error": "fake failure on second photo",
        }

    monkeypatch.setattr(develop_mod, "develop_photo", flaky_develop)

    photos = db.get_photos(per_page=2)
    assert len(photos) >= 2, "fixture should provide at least two photos"
    pids = [photos[0]['id'], photos[1]['id']]

    resp = client.post(
        '/api/jobs/develop',
        data=json.dumps({"photo_ids": pids}),
        content_type='application/json',
    )
    assert resp.status_code == 200
    job_id = resp.get_json()['job_id']

    data = _poll_job(client, job_id)
    assert data is not None
    assert data['status'] == 'failed', f"expected failed, got {data['status']}: {data}"
    result = data.get('result') or {}
    assert result.get('developed') == 1
    assert result.get('errors') == 1
    assert result.get('total') == 2
    # Regression: only the actual per-photo failure should appear in the
    # errors list — no synthetic summary string tacked on by _run_job.
    errs = data.get('errors') or []
    assert len(errs) == 1, f"expected exactly one error entry, got {len(errs)}: {errs}"
    assert any('fake failure on second photo' in e for e in errs), errs


def test_api_darktable_status_reports_checked_paths(app_and_db):
    """The status route says where it looked, so a bare 'not found' can explain itself."""
    app, _ = app_and_db
    client = app.test_client()
    data = client.get('/api/darktable/status').get_json()

    assert 'checked_paths' in data
    assert isinstance(data['checked_paths'], list)
    assert all(isinstance(p, str) for p in data['checked_paths'])


def test_api_darktable_status_checked_paths_mentions_path_probe(app_and_db):
    """$PATH is the probe most likely to explain a miss, so it must be listed.

    find_darktable tries shutil.which() before any filesystem candidate;
    omitting it would make "we checked here" untrue by omission.
    """
    app, _ = app_and_db
    data = app.test_client().get('/api/darktable/status').get_json()

    assert any('PATH' in p for p in data['checked_paths'])


def test_api_darktable_status_checked_paths_never_empty(app_and_db, monkeypatch):
    """Never render an empty 'we checked here' list.

    darktable_search_paths() returns [] on a Linux box with no AppImage
    installed — precisely the user this feature targets.
    """
    import develop
    app, _ = app_and_db
    monkeypatch.setattr(develop, "darktable_search_paths", lambda: [])

    data = app.test_client().get('/api/darktable/status').get_json()

    assert data['checked_paths'], "checked_paths must never be empty"


def test_api_darktable_status_checked_paths_includes_detector_candidates(app_and_db, monkeypatch):
    """The real detector candidates reach the response, in priority order.

    Asserting the whole list rather than membership also pins ordering, which
    users read as "we tried these, in this sequence". Pinned to macOS so the
    tools dir is not appended and the expected list is exact on any host.
    """
    import develop
    app, _ = app_and_db
    monkeypatch.setattr(develop, "darktable_search_paths", lambda: ["/sentinel/darktable-cli"])
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")

    data = app.test_client().get('/api/darktable/status').get_json()

    assert data['checked_paths'] == ["$PATH (darktable-cli)", "/sentinel/darktable-cli"]


def test_api_darktable_status_checked_paths_names_linux_tools_dir(app_and_db, monkeypatch):
    """On Linux the list names the AppImage directory even when it's empty.

    darktable_search_paths() only reports AppImages that already exist, so a
    fresh Linux box contributes nothing; the route must still name the
    directory find_darktable looks in, since that is where a download lands.
    """
    import develop
    app, _ = app_and_db
    monkeypatch.setattr(develop, "darktable_search_paths", lambda: [])
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")

    data = app.test_client().get('/api/darktable/status').get_json()

    tools_dir = os.path.expanduser("~/.vireo/tools/darktable")
    assert tools_dir in data['checked_paths']


def test_api_darktable_status_checked_paths_omits_linux_tools_dir_on_macos(app_and_db, monkeypatch):
    """macOS never probes the Linux AppImage directory, so don't claim we did."""
    import develop
    app, _ = app_and_db
    monkeypatch.setattr(develop, "darktable_search_paths", lambda: [])
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")

    data = app.test_client().get('/api/darktable/status').get_json()

    tools_dir = os.path.expanduser("~/.vireo/tools/darktable")
    assert tools_dir not in data['checked_paths']
    assert data['checked_paths'], "checked_paths must never be empty"


@pytest.fixture(autouse=True)
def _clear_release_cache():
    """The release cache is module-level state that would leak between tests.

    Without this, a test that stubs a failure would see the previous test's
    cached success and pass for the wrong reason.
    """
    import darktable_install
    darktable_install._release_cache.update(at=0.0, value=None)
    yield
    darktable_install._release_cache.update(at=0.0, value=None)


def _fake_release(version="5.6.0"):
    return {
        "version": version,
        "name": f"darktable-{version}-arm64.dmg",
        "size": 87094261,
        "url": "https://github.com/darktable-org/darktable/releases/download/x/d.dmg",
        "digest": "sha256:" + "a" * 64,
    }


def test_api_darktable_install_available_shape(app_and_db, monkeypatch):
    """The confirmation needs version, name, size, url and digest."""
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release", lambda: (_fake_release(), None))

    data = app.test_client().get('/api/darktable/install/available').get_json()
    assert data['available'] is True
    assert data['version'] == "5.6.0"
    assert data['name'] == "darktable-5.6.0-arm64.dmg"
    assert data['size'] == 87094261
    assert data['url'].startswith("https://github.com/darktable-org/darktable/releases/download/")
    assert data['digest'].startswith("sha256:")


def test_api_darktable_install_available_reports_network_failure_honestly(app_and_db, monkeypatch):
    """A user behind a proxy must not be told their platform is unsupported."""
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_UNREACHABLE))

    resp = app.test_client().get('/api/darktable/install/available')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['available'] is False
    assert data['reason'] == darktable_install.REASON_UNREACHABLE
    assert 'platform' not in data['reason'].lower()


def test_api_darktable_install_available_handles_unsupported_platform(app_and_db, monkeypatch):
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_NO_PLATFORM_BUILD))

    data = app.test_client().get('/api/darktable/install/available').get_json()
    assert data['available'] is False
    assert data['reason'] == darktable_install.REASON_NO_PLATFORM_BUILD


def test_api_darktable_install_available_distinguishes_no_usable_asset(app_and_db, monkeypatch):
    """"Release had no build we could use" is a third, distinct fact.

    Together with the two tests above this pins that the route relays whatever
    resolve_release said rather than collapsing every miss to one sentence.
    """
    import darktable_install
    app, _ = app_and_db
    monkeypatch.setattr(darktable_install, "resolve_release",
                        lambda: (None, darktable_install.REASON_NO_USABLE_ASSET))

    data = app.test_client().get('/api/darktable/install/available').get_json()
    assert data['available'] is False
    assert data['reason'] == darktable_install.REASON_NO_USABLE_ASSET


def test_api_darktable_install_available_survives_an_unexpected_raise(app_and_db, monkeypatch):
    """Belt and braces: resolve_release should never raise, but if it does the
    route must still 200 with a reason rather than 500 into a dead button."""
    import darktable_install
    app, _ = app_and_db

    def boom():
        raise OSError("network unreachable")
    monkeypatch.setattr(darktable_install, "resolve_release", boom)

    resp = app.test_client().get('/api/darktable/install/available')
    assert resp.status_code == 200
    assert resp.get_json()['available'] is False
    assert resp.get_json()['reason']


def test_api_darktable_install_available_caches_a_successful_lookup(app_and_db, monkeypatch):
    """Repeated Settings loads must not burn GitHub's 60 unauthenticated req/hr.

    Once that budget is gone every user on the IP gets the fallback link, so a
    second call within the TTL must not reach the API at all.
    """
    import darktable_install
    app, _ = app_and_db
    calls = []

    def counting():
        calls.append(1)
        return _fake_release(), None
    monkeypatch.setattr(darktable_install, "resolve_release", counting)

    client = app.test_client()
    first = client.get('/api/darktable/install/available').get_json()
    second = client.get('/api/darktable/install/available').get_json()

    assert first['available'] is True
    assert second == first
    assert len(calls) == 1, f"expected one API lookup, got {len(calls)}"


def test_api_darktable_install_available_does_not_cache_failures(app_and_db, monkeypatch):
    """A transient outage must not pin the fallback link for the whole TTL.

    Caching a failure would also freeze its reason string, so a user who fixed
    their network would keep being told GitHub is unreachable.
    """
    import darktable_install
    app, _ = app_and_db
    outcomes = [
        (None, darktable_install.REASON_UNREACHABLE),
        (_fake_release(), None),
    ]
    monkeypatch.setattr(darktable_install, "resolve_release", lambda: outcomes.pop(0))

    client = app.test_client()
    first = client.get('/api/darktable/install/available').get_json()
    second = client.get('/api/darktable/install/available').get_json()

    assert first['available'] is False
    assert first['reason'] == darktable_install.REASON_UNREACHABLE
    assert second['available'] is True, "a failure must not be cached over a later success"
    assert second['version'] == "5.6.0"
