from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_helper():
    path = Path(__file__).parents[1] / "deploy" / "smsi-archive-client-updater.py"
    spec = importlib.util.spec_from_file_location("smsi_archive_client_updater", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restart_current_version_does_not_activate_stale_update(monkeypatch) -> None:
    helper = load_helper()
    activations = []
    restarts = []
    monkeypatch.setattr(helper, "_busy", lambda: False)
    monkeypatch.setattr(helper, "_activate", activations.append)
    monkeypatch.setattr(helper.subprocess, "run", lambda command, **options: restarts.append((command, options)))

    result = helper._handle({"action": "restart", "revision": ""})

    assert result == {"ok": True, "restarted": True, "activated": False}
    assert activations == []
    assert restarts[0][0] == ["systemctl", "restart", helper.SERVICE]


def test_restart_activates_only_requested_staged_revision(monkeypatch) -> None:
    helper = load_helper()
    revision = "b" * 40
    activations = []
    monkeypatch.setattr(helper, "_busy", lambda: False)
    monkeypatch.setattr(helper, "_activate", activations.append)
    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_options: None)

    result = helper._handle({"action": "restart", "revision": revision})

    assert result == {"ok": True, "restarted": True, "activated": True}
    assert activations == [revision]


def test_restart_fails_closed_while_archive_is_running(monkeypatch) -> None:
    helper = load_helper()
    monkeypatch.setattr(helper, "_busy", lambda: True)

    with pytest.raises(RuntimeError, match="归档任务正在运行"):
        helper._handle({"action": "restart", "revision": ""})
