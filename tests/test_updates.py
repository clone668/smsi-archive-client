from __future__ import annotations

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
