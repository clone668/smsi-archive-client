from __future__ import annotations

from archive_backup.config import ConfigStore
from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.web import create_app


def test_login_and_csrf_protection(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    initial = store.initial_password_path.read_text(encoding="utf-8").strip()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        assert client.get("/api/bootstrap").status_code == 401
        response = client.post("/login", data={"password": initial})
        assert response.status_code == 302
        bootstrap = client.get("/api/bootstrap").get_json()
        assert bootstrap["ok"] is True
        assert "password_hash" not in bootstrap["config"]
        assert client.post("/api/actions/scan", json={}).status_code == 403
        response = client.post(
            "/api/actions/scan",
            json={"download": False},
            headers={"X-CSRF-Token": bootstrap["csrf"]},
        )
        assert response.status_code == 200
    finally:
        app.extensions["smsi_archive_service"].stop()


def test_day_detail_returns_remote_manifest_and_local_inventory(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    store = ConfigStore(tmp_path / "state")
    store.load()
    config = ClientConfig(
        local_root=str(tmp_path / "local"),
        auto_download=False,
        profiles=[ProfileConfig(
            profile_id="collector-a",
            display_name="A",
            collector_id="collector-a",
            source_type="verified_directory",
            verified_source_root=str(fixture["source_root"]),
        )],
        password_hash=store.load().password_hash,
        session_secret=store.load().session_secret,
    )
    store.save(config)
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        password = ""
        # The generated initial password remains in the store for this test.
        password = store.initial_password_path.read_text(encoding="utf-8").strip()
        assert client.post("/login", data={"password": password}).status_code == 302
        response = client.get(
            f"/api/day-detail?profile_id=collector-a&archive_date={fixture['archive_date']}"
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["detail"]["remote"]["state"] == "ready"
        assert len(payload["detail"]["objects"]) == fixture["manifest"]["object_count"]
        assert payload["detail"]["local"]["object_count"] == 0
    finally:
        app.extensions["smsi_archive_service"].stop()


def test_update_status_is_exposed_without_network(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        password = store.initial_password_path.read_text(encoding="utf-8").strip()
        assert client.post("/login", data={"password": password}).status_code == 302
        bootstrap = client.get("/api/bootstrap").get_json()
        assert bootstrap["updates"]["current_revision"] == "unknown"
        manager = app.extensions["smsi_update_manager"]
        manager.check = lambda: {
            "current_revision": "unknown",
            "latest": {"revision": "abcdef1234567890", "message": "test"},
            "staged_revision": "",
            "update_available": True,
            "helper_available": False,
        }
        response = client.get("/api/update/check")
        assert response.status_code == 200
        assert response.get_json()["updates"]["update_available"] is True
    finally:
        app.extensions["smsi_archive_service"].stop()
