"""Endpoint tests for /api/remote-setup/* (NAS setup wizard backend).

The endpoints stay thin — remote_setup functions are monkeypatched so no
test ever spawns a real ssh."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_mounts_endpoint_returns_parsed_mounts(app_and_db, monkeypatch):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "list_network_mounts", lambda **kw: [
        {"fs_type": "smbfs", "host": "1.2.3.4", "friendly_host": "nas.ts.net",
         "display_name": "nas", "share": "Photos",
         "mount_point": "/Volumes/Photos", "user": "admin"}])
    res = app.test_client().get("/api/remote-setup/mounts")
    assert res.status_code == 200
    assert res.get_json()["mounts"][0]["share"] == "Photos"


def test_mounts_endpoint_rejects_non_loopback(app_and_db):
    app, _db = app_and_db
    res = app.test_client().get(
        "/api/remote-setup/mounts",
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert res.status_code == 403


def test_ssh_check_reports_port_auth_and_pubkey(app_and_db, monkeypatch, tmp_path):
    # ssh-check ensures the Vireo key exists (idempotent, local-only) and
    # returns its public line — the Terminal-fallback expander needs it
    # WITHOUT ever calling install-key (no password submitted).
    app, _db = app_and_db
    import remote_setup
    pub = tmp_path / "vireo_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAA vireo\n")
    monkeypatch.setattr(remote_setup, "port_reachable",
                        lambda host, port, timeout=5: True)
    monkeypatch.setattr(remote_setup, "ensure_vireo_key",
                        lambda **kw: (str(tmp_path / "vireo_ed25519"), str(pub)))
    monkeypatch.setattr(remote_setup, "key_auth_works", lambda **kw: False)
    res = app.test_client().post("/api/remote-setup/ssh-check",
                                 json={"host": "nas", "port": 22, "user": "admin"})
    body = res.get_json()
    assert body["port_open"] is True and body["key_auth_ok"] is False
    assert body["pub_key_line"] == "ssh-ed25519 AAAA vireo"


def test_ssh_check_validates_input(app_and_db):
    app, _db = app_and_db
    c = app.test_client()
    assert c.post("/api/remote-setup/ssh-check",
                  json={"host": "", "user": "a"}).status_code == 400
    assert c.post("/api/remote-setup/ssh-check",
                  json={"host": "nas", "user": "a",
                        "port": 99999}).status_code == 400


def test_install_key_rejects_non_loopback(app_and_db):
    app, _db = app_and_db
    res = app.test_client().post(
        "/api/remote-setup/install-key",
        json={"host": "nas", "user": "a", "port": 22, "password": "x"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert res.status_code == 403


def test_install_key_happy_path_never_echoes_password(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-ed25519 AAAA vireo\n")
    monkeypatch.setattr(remote_setup, "ensure_vireo_key",
                        lambda **kw: (str(tmp_path / "k"), str(pub)))
    monkeypatch.setattr(remote_setup, "build_install_argv",
                        lambda **kw: ["ssh"])
    monkeypatch.setattr(remote_setup, "install_key_with_password",
                        lambda **kw: {"ok": True, "error": None})
    monkeypatch.setattr(remote_setup, "key_auth_works", lambda **kw: True)
    res = app.test_client().post(
        "/api/remote-setup/install-key",
        json={"host": "nas", "user": "a", "port": 22, "password": "hunter2"})
    body = res.get_json()
    assert body["ok"] is True
    assert "hunter2" not in res.get_data(as_text=True)


def test_install_key_wrong_password_maps_cleanly(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-ed25519 AAAA vireo\n")
    monkeypatch.setattr(remote_setup, "ensure_vireo_key",
                        lambda **kw: (str(tmp_path / "k"), str(pub)))
    monkeypatch.setattr(remote_setup, "build_install_argv",
                        lambda **kw: ["ssh"])
    monkeypatch.setattr(remote_setup, "install_key_with_password",
                        lambda **kw: {"ok": False, "error": "wrong_password"})
    res = app.test_client().post(
        "/api/remote-setup/install-key",
        json={"host": "nas", "user": "a", "port": 22, "password": "nope"})
    body = res.get_json()
    assert body["ok"] is False and body["error"] == "wrong_password"


def test_locate_share_endpoint(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "locate_share",
                        lambda **kw: "/volume1/Photos")
    res = app.test_client().post(
        "/api/remote-setup/locate-share",
        json={"mount_path": str(tmp_path), "share": "Photos",
              "host": "nas", "user": "a", "port": 22})
    assert res.get_json()["remote_path"] == "/volume1/Photos"


def test_locate_share_readonly_mount_is_clean_error(app_and_db, monkeypatch, tmp_path):
    app, _db = app_and_db
    import remote_setup

    def boom(**kw):
        raise remote_setup.MountNotWritable("read-only")

    monkeypatch.setattr(remote_setup, "locate_share", boom)
    res = app.test_client().post(
        "/api/remote-setup/locate-share",
        json={"mount_path": str(tmp_path), "share": "P", "host": "n",
              "user": "a", "port": 22})
    body = res.get_json()
    assert res.status_code == 400 and "writable" in body["error"].lower()


def test_locate_share_validates_mount_path(app_and_db):
    app, _db = app_and_db
    res = app.test_client().post(
        "/api/remote-setup/locate-share",
        json={"mount_path": "relative/path", "share": "P", "host": "n",
              "user": "a", "port": 22})
    assert res.status_code == 400


def test_list_remote_dirs_endpoint(app_and_db, monkeypatch):
    app, _db = app_and_db
    import remote_setup
    monkeypatch.setattr(remote_setup, "list_remote_dirs",
                        lambda **kw: ["Exports", "Raw Files"])
    res = app.test_client().post(
        "/api/remote-setup/list-remote-dirs",
        json={"path": "/volume1", "host": "n", "user": "a", "port": 22})
    assert res.get_json()["dirs"] == ["Exports", "Raw Files"]


def test_disk_free_endpoint(app_and_db, tmp_path):
    app, _db = app_and_db
    res = app.test_client().get(
        "/api/remote-setup/disk-free", query_string={"path": str(tmp_path)})
    assert res.status_code == 200
    assert res.get_json()["free_bytes"] > 0
    bad = app.test_client().get(
        "/api/remote-setup/disk-free", query_string={"path": "not/absolute"})
    assert bad.status_code == 400
