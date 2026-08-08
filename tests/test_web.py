from __future__ import annotations

from archive_backup.config import ConfigStore
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
