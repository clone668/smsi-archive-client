from __future__ import annotations

from pathlib import Path

from archive_backup import __version__
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
        # 需要处理是运维第一眼要看的数字，所以排在指标行第一位；磁盘已经常驻侧栏，
        # 不在总览重复一遍。
        assert page.index("<span>需要处理</span>") < page.index("<span>双服务器共同完整</span>")
        assert 'id="metric-disk"' not in page
        # 更新面板只留一句话，之前那一句状态被拆成 7 个元素反复说。
        assert page.count('class="workflow-notice"') == 1
        assert 'id="update-state-text"' not in page
        assert 'id="update-blocked-reason"' not in page
        assert 'id="update-detail"' not in page
        assert 'id="install-hint"' not in page
        assert '<th>数据对比</th>' in page
        assert '<th>归档日数据质量</th>' in page
        assert f"归档中心 · v{__version__}" in page
        script = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")
        assert 'replace("smsi-runtime-health-assessment/", "")' in script
        assert '"归档状态检查"' in script
        # 侧栏圆点和总览条读同一张表，重启期间不会一个说在线、一个说重启中。
        assert 'restarting: ["正在重启", "重启中", "warn"]' in script
        assert "function setLiveness(" in script
        # 总览和任务两页渲染同一个任务，用同一段代码，措辞不会再分叉。
        assert "const taskPanels = {" in script
        # 这两页本身就有完整任务条，浮窗只服务其他页面。
        assert '["jobs", "overview"].includes(state.currentPage)' in script
        assert 'checking ? "停止检查" : "取消任务"' in script
        assert 'const checking = runtime.running &&' in script
        assert 'object_checksum_difference: "校验和"' in script
        assert '? "轻微差异" : baseLabel' in script
        assert "item.data_status || item.status ||" in script
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


def test_config_activation_does_not_scan_for_resource_only_changes(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    config = store.load()
    config.auto_download = False
    store.save(config)
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    calls = []
    original_request_scan = service.request_scan
    service.request_scan = lambda **kwargs: calls.append(kwargs) or {"id": 1}
    try:
        csrf = _login(client, store)
        response = client.put(
            "/api/config",
            json={"download_workers": 3, "bandwidth_limit": "10M"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.get_json()["activation"] == {
            "rescan_started": False,
            "restart_required": False,
            "next_task": True,
            "changed_fields": ["bandwidth_limit", "download_workers"],
        }
        assert calls == []
    finally:
        service.request_scan = original_request_scan
        service.stop()


def test_alert_settings_are_accepted_and_never_publish_the_token(tmp_path) -> None:
    """The token must reach the config file and nothing else."""
    token = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    woke: list[bool] = []
    original_wake = service.wake
    service.wake = lambda: woke.append(True)
    try:
        csrf = _login(client, store)
        response = client.put(
            "/api/config",
            json={
                "alert_bot_token": token,
                "alert_chat_id": "-1001234567890",
                "alert_min_level": "error",
                "alert_stale_hours": 12,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert token not in body
        config = response.get_json()["config"]
        assert "alert_bot_token" not in config
        assert config["alert_bot_token_set"] is True
        assert config["alert_bot_token_hint"] == "…3456"
        assert config["alert_min_level"] == "error"
        assert config["alert_stale_hours"] == 12
        # An alert setting is useless if it waits for the next poll interval.
        assert woke == [True]
        assert store.load().alert_bot_token == token
    finally:
        service.wake = original_wake
        service.stop()


def test_alert_test_endpoint_explains_missing_credentials(tmp_path) -> None:
    """The test button must fail with an instruction, not a stack trace."""
    store = ConfigStore(tmp_path / "state")
    store.load()
    app = create_app(store)
    app.config["TESTING"] = True
    client = app.test_client()
    service = app.extensions["smsi_archive_service"]
    try:
        csrf = _login(client, store)
        response = client.post(
            "/api/actions/alert-test", headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 409
        assert "请先填写" in response.get_json()["error"]
    finally:
        service.stop()


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
        updater.restart = lambda **options: calls.append(("restart", options)) or {"restarted": True}

        response = client.post(
            "/api/update/restart",
            json={},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 200
        assert calls == [("stop", 30), ("restart", {"activate": True})]
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

        def fail_restart(**options):
            calls.append(("restart", options))
            raise RuntimeError("更新助手拒绝操作")

        updater.restart = fail_restart
        response = client.post(
            "/api/update/restart",
            json={},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 409
        assert response.get_json()["error"] == "更新助手拒绝操作"
        assert calls == [("stop", 30), ("restart", {"activate": True}), ("start", None)]
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
        updater.restart = lambda **options: calls.append(("restart", options))

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


def _stage_release_with_new_dependency(updater) -> str:
    """Stage a release whose requirements differ from the running client's."""
    revision = "c" * 40
    release = updater.update_root / revision
    release.mkdir(parents=True)
    live = (updater.app_root / "requirements.txt").read_text(encoding="utf-8")
    (release / "requirements.txt").write_text(
        live + "\npyarrow==17.0.0\n", encoding="utf-8"
    )
    updater.metadata_path.write_text(
        '{"revision":"' + revision + '"}', encoding="utf-8"
    )
    return revision


def test_version_switch_refuses_dependency_change_before_pausing_archive(tmp_path) -> None:
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
        _stage_release_with_new_dependency(updater)
        service.status = lambda: {"running": True}
        service.stop = lambda timeout=30: calls.append(("stop", timeout)) or True
        updater.restart = lambda **options: calls.append(("restart", options))

        response = client.post(
            "/api/update/restart",
            json={"activate": True},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 409
        assert "install_ubuntu.sh" in response.get_json()["error"]
        # Refusing before the stop is the point: a task paused for an operation
        # the helper would reject has lost an archive pass for nothing.
        assert calls == []
    finally:
        service.stop = original_stop
        service.stop()


def test_maintenance_restart_ignores_a_staged_dependency_change(tmp_path) -> None:
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
        _stage_release_with_new_dependency(updater)
        service.status = lambda: {"running": False}
        service.stop = lambda timeout=30: calls.append(("stop", timeout)) or True
        updater.restart = lambda **options: calls.append(("restart", options)) or {"restarted": True}

        response = client.post(
            "/api/update/restart",
            json={"activate": False},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 200
        assert calls == [("stop", 30), ("restart", {"activate": False})]
    finally:
        service.stop = original_stop
        service.stop()
