from __future__ import annotations

import io
import tarfile

import pytest

from archive_backup.updates import UpdateManager


def test_update_status_reads_release_marker(tmp_path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / ".smsi-release").write_text("abcdef1234567890\n", encoding="ascii")
    manager = UpdateManager(tmp_path / "state", app_root, helper_socket=str(tmp_path / "missing.sock"))
    status = manager.status()
    assert status["current_revision"] == "abcdef1234567890"
    assert status["update_available"] is False
    assert status["helper_available"] is False


class ArchiveResponse:
    def __init__(self, payload: bytes) -> None:
        self.stream = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


def update_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, content in {
            "repo-version/app.py": b"print('ok')\n",
            "repo-version/archive_backup/__init__.py": b"",
            "repo-version/static/app.js": b"",
            "repo-version/templates/index.html": b"",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_update_download_reports_progress_and_ready_state(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    manager = UpdateManager(
        tmp_path / "state", app_root, helper_socket=str(tmp_path / "missing.sock")
    )
    revision = "a" * 40
    payload = update_archive()
    monkeypatch.setattr(manager, "_request_json", lambda _url: {"sha": revision})
    monkeypatch.setattr(
        "archive_backup.updates.urllib.request.urlopen",
        lambda *_args, **_kwargs: ArchiveResponse(payload),
    )

    result = manager.download(revision)

    operation = manager.status()["operation"]
    assert result["bytes"] == len(payload)
    assert operation["active"] is False
    assert operation["phase"] == "ready"
    assert operation["percent"] == 100
    assert operation["bytes_done"] == len(payload)


def test_failed_update_releases_operation_lock(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    manager = UpdateManager(
        tmp_path / "state", app_root, helper_socket=str(tmp_path / "missing.sock")
    )
    monkeypatch.setattr(
        manager, "_request_json", lambda _url: (_ for _ in ()).throw(RuntimeError("offline"))
    )

    with pytest.raises(RuntimeError, match="offline"):
        manager.check()

    assert manager.status()["operation"]["phase"] == "failed"
    monkeypatch.setattr(
        manager,
        "_request_json",
        lambda _url: {"sha": "b" * 40, "commit": {}},
    )
    assert manager.check()["operation"]["active"] is False


def test_restart_without_staged_update_restarts_current_version(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    manager = UpdateManager(tmp_path / "state", app_root, helper_socket=str(tmp_path / "helper.sock"))
    requests = []
    monkeypatch.setattr(manager, "_helper_request", lambda payload: requests.append(payload) or {"ok": True})

    assert manager.restart() == {"ok": True}
    assert requests == [{"action": "restart", "revision": ""}]


def test_restart_activates_only_staged_revision(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = "a" * 40
    staged = "b" * 40
    (app_root / ".smsi-release").write_text(current, encoding="ascii")
    manager = UpdateManager(tmp_path / "state", app_root, helper_socket=str(tmp_path / "helper.sock"))
    manager.update_root.mkdir(parents=True)
    manager.metadata_path.write_text(
        '{"revision":"' + staged + '"}', encoding="utf-8"
    )
    (manager.update_root / staged).mkdir()
    requests = []
    monkeypatch.setattr(manager, "_helper_request", lambda payload: requests.append(payload) or {"ok": True})

    manager.restart()

    assert requests == [{"action": "restart", "revision": staged}]
