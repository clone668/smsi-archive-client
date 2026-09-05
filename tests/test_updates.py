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


def staged_manager(tmp_path, *, live: str | None, staged_requirements: str | None):
    """A manager with one staged release, optionally with requirements files.

    Both files are written as raw bytes: text mode on Windows would rewrite the
    line endings, which is exactly what the CRLF case needs to control.
    """
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = "a" * 40
    staged = "b" * 40
    (app_root / ".smsi-release").write_text(current, encoding="ascii")
    if live is not None:
        (app_root / "requirements.txt").write_bytes(live.encode("utf-8"))
    manager = UpdateManager(
        tmp_path / "state", app_root, helper_socket=str(tmp_path / "helper.sock")
    )
    manager.update_root.mkdir(parents=True)
    manager.metadata_path.write_text('{"revision":"' + staged + '"}', encoding="utf-8")
    (manager.update_root / staged).mkdir()
    if staged_requirements is not None:
        (manager.update_root / staged / "requirements.txt").write_bytes(
            staged_requirements.encode("utf-8")
        )
    return manager, staged


def test_status_reports_dependency_change_when_staged_requirements_differ(tmp_path) -> None:
    manager, staged = staged_manager(
        tmp_path, live="flask==3.0.3\n", staged_requirements="flask==3.0.3\npyarrow==17.0.0\n"
    )

    status = manager.status()

    assert status["staged_revision"] == staged
    assert status["dependency_change"] is True
    assert status["install_command"] == "sudo bash deploy/install_ubuntu.sh"


def test_status_ignores_line_ending_differences_in_requirements(tmp_path) -> None:
    # The privileged helper compares with splitlines(), so a CRLF working copy
    # must not look like a dependency change to the page either.
    manager, _staged = staged_manager(
        tmp_path, live="flask==3.0.3\nwaitress==3.0.0\n",
        staged_requirements="flask==3.0.3\r\nwaitress==3.0.0\r\n",
    )

    assert manager.status()["dependency_change"] is False


def test_status_reports_no_dependency_change_without_live_requirements(tmp_path) -> None:
    # The helper skips its own comparison in this case, so nothing is refused.
    manager, _staged = staged_manager(
        tmp_path, live=None, staged_requirements="flask==3.0.3\n"
    )

    assert manager.status()["dependency_change"] is False


def test_status_reports_no_dependency_change_without_staged_release(tmp_path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / ".smsi-release").write_text("a" * 40, encoding="ascii")
    (app_root / "requirements.txt").write_text("flask==3.0.3\n", encoding="utf-8")
    manager = UpdateManager(
        tmp_path / "state", app_root, helper_socket=str(tmp_path / "helper.sock")
    )

    status = manager.status()

    assert status["staged_revision"] == ""
    assert status["dependency_change"] is False


def test_ensure_switchable_refuses_release_that_changes_dependencies(tmp_path) -> None:
    manager, _staged = staged_manager(
        tmp_path, live="flask==3.0.3\n", staged_requirements="flask==3.0.3\npyarrow==17.0.0\n"
    )

    with pytest.raises(RuntimeError, match="install_ubuntu.sh"):
        manager.ensure_switchable()


def test_ensure_switchable_allows_release_with_identical_dependencies(tmp_path) -> None:
    manager, _staged = staged_manager(
        tmp_path, live="flask==3.0.3\n", staged_requirements="flask==3.0.3\n"
    )

    manager.ensure_switchable()


def test_maintenance_restart_never_activates_a_staged_release(tmp_path, monkeypatch) -> None:
    manager, _staged = staged_manager(
        tmp_path, live="flask==3.0.3\n", staged_requirements="flask==3.0.3\n"
    )
    requests = []
    monkeypatch.setattr(
        manager, "_helper_request", lambda payload: requests.append(payload) or {"ok": True}
    )

    manager.restart(activate=False)

    assert requests == [{"action": "restart", "revision": ""}]
