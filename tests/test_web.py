from __future__ import annotations

from archive_backup.config import ConfigStore
from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.web import create_app


def _login(client, store: ConfigStore) -> str:
    password = store.initial_password_path.read_text(encoding="utf-8").strip()
    assert client.post("/login", data={"password": password}).status_code == 302
    return str(client.get("/api/bootstrap").get_json()["csrf"])


def test_overview_is_the_default_workspace(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        password = store.initial_password_path.read_text(encoding="utf-8").strip()
        assert client.post("/login", data={"password": password}).status_code == 302
        page = client.get("/").get_data(as_text=True)

        assert 'class="nav-item active" data-page="overview"' in page
        assert 'id="overview-page" class="page active"' in page
        assert 'id="remote-files-page" class="page file-page"' in page
        assert page.index('data-page="overview"') < page.index('data-page="jobs"')
        assert page.index('data-page="jobs"') < page.index('data-page="files"')
        assert 'id="remote-tree"' in page
        assert 'id="remote-search"' in page
        assert 'id="remote-files-body"' in page
        assert 'id="remote-inspector"' in page
        assert 'id="transfer-dock"' in page
        assert '<span>未完成</span>' in page
        assert '<th>数据对比</th>' in page
        assert '<th>运行报告</th>' in page
    finally:
        app.extensions["smsi_archive_service"].stop()


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
        assert response.get_json()["job"]["status"] == "queued"
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


def test_file_browser_endpoints_require_scope_and_return_inventory(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    store = ConfigStore(tmp_path / "state")
    security = store.load()
    store.save(ClientConfig(
        local_root=str(tmp_path / "local"),
        auto_download=False,
        profiles=[ProfileConfig(
            profile_id="collector-a",
            display_name="A",
            collector_id="collector-a",
            source_type="verified_directory",
            verified_source_root=str(fixture["source_root"]),
        )],
        password_hash=security.password_hash,
        session_secret=security.session_secret,
    ))
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        password = store.initial_password_path.read_text(encoding="utf-8").strip()
        assert client.post("/login", data={"password": password}).status_code == 302
        assert client.get("/api/files/dates?profile_id=collector-a").status_code == 400
        dates = client.get(
            "/api/files/dates?profile_id=collector-a&scope=remote"
        ).get_json()["result"]
        files = client.get(
            f"/api/files/list?profile_id=collector-a&archive_date={fixture['archive_date']}&scope=remote"
        ).get_json()["result"]
        assert dates["scope"] == "remote"
        assert dates["dates"][0]["archive_date"] == fixture["archive_date"]
        assert files["entry_count"] == 2
        assert files["entries"][0]["type"] == "directory"
        assert files["download_eligible"] is True
    finally:
        app.extensions["smsi_archive_service"].stop()


def test_download_endpoint_requires_csrf_and_queues_selected_date(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        password = store.initial_password_path.read_text(encoding="utf-8").strip()
        assert client.post("/login", data={"password": password}).status_code == 302
        bootstrap = client.get("/api/bootstrap").get_json()
        assert client.post(
            "/api/actions/download",
            json={"profile_id": "tencent-paper", "archive_date": "2026-08-09"},
        ).status_code == 403
        service = app.extensions["smsi_archive_service"]
        service.request_download = lambda profile_id, archive_date: {
            "id": 42,
            "profile_id": profile_id,
            "archive_date": archive_date,
            "status": "queued",
        }

        response = client.post(
            "/api/actions/download",
            json={"profile_id": "tencent-paper", "archive_date": "2026-08-09"},
            headers={"X-CSRF-Token": bootstrap["csrf"]},
        )

        assert response.status_code == 200
        assert response.get_json()["job"] == {
            "id": 42,
            "profile_id": "tencent-paper",
            "archive_date": "2026-08-09",
            "status": "queued",
        }
    finally:
        app.extensions["smsi_archive_service"].stop()


def test_restart_safely_stops_archive_service_before_calling_helper(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    updater = app.extensions["smsi_update_manager"]
    original_stop = service.stop
    calls = []
    try:
        csrf = _login(client, store)
        service.status = lambda: {"running": True}
        service.stop = lambda timeout=30: calls.append(("stop", timeout)) or True
        updater.restart = lambda: calls.append(("restart", None)) or {"restarted": True}

        response = client.post(
            "/api/update/restart",
            json={},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 200
        assert calls == [("stop", 30), ("restart", None)]
    finally:
        service.stop = original_stop
        service.stop()


def test_restart_failure_resumes_archive_service(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    updater = app.extensions["smsi_update_manager"]
    original_stop = service.stop
    original_start = service.start
    calls = []
    try:
        csrf = _login(client, store)
        service.status = lambda: {"running": True}
        service.stop = lambda timeout=30: calls.append(("stop", timeout)) or True
        service.start = lambda: calls.append(("start", None))

        def fail_restart():
            calls.append(("restart", None))
            raise RuntimeError("更新助手拒绝操作")

        updater.restart = fail_restart
        response = client.post(
            "/api/update/restart",
            json={},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 409
        assert response.get_json()["error"] == "更新助手拒绝操作"
        assert calls == [("stop", 30), ("restart", None), ("start", None)]
    finally:
        service.stop = original_stop
        service.start = original_start
        service.stop()


def test_restart_pause_timeout_schedules_archive_service_resume(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    updater = app.extensions["smsi_update_manager"]
    original_stop = service.stop
    original_resume = service.resume_when_stopped
    calls = []
    try:
        csrf = _login(client, store)
        service.status = lambda: {"running": True}
        service.stop = lambda timeout=30: calls.append(("stop", timeout)) or False
        service.resume_when_stopped = lambda: calls.append(("resume", None))
        updater.restart = lambda: calls.append(("restart", None))

        response = client.post(
            "/api/update/restart",
            json={},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 409
        assert "没有重启" in response.get_json()["error"]
        assert calls == [("stop", 30), ("resume", None)]
    finally:
        service.stop = original_stop
        service.resume_when_stopped = original_resume
        service.stop()
