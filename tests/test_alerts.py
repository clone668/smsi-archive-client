from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from archive_backup.alerts import AlertNotifier, _scrub
from archive_backup.config import ConfigStore
from archive_backup.database import StateDatabase


TOKEN = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
CHAT_ID = "-1001234567890"


class FakeSender:
    """Records what would have been delivered, and can refuse on demand."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failure = ""

    def __call__(self, token: str, chat_id: str, text: str) -> None:
        assert token == TOKEN
        assert chat_id == CHAT_ID
        if self.failure:
            raise RuntimeError(self.failure)
        self.messages.append(text)


def _build(tmp_path, *, enabled: bool = True, minimum: str = "warning", stale_hours: int = 6):
    store = ConfigStore(tmp_path / "state")
    store.load()
    store.update_public({
        "alert_enabled": enabled,
        "alert_bot_token": TOKEN,
        "alert_chat_id": CHAT_ID,
        "alert_min_level": minimum,
        "alert_stale_hours": stale_hours,
    })
    database = StateDatabase(tmp_path / "state" / "state.sqlite3")
    clock = {"now": datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)}
    sender = FakeSender()
    notifier = AlertNotifier(
        store,
        database,
        sender=sender,
        hostname="archive-host",
        now=lambda: clock["now"],
    )
    return store, database, notifier, sender, clock


def test_enabling_alerts_does_not_replay_the_event_history(tmp_path) -> None:
    """Switching alerting on must not dump weeks of old events into the chat."""
    store, database, notifier, sender, _ = _build(tmp_path)
    for index in range(5):
        database.event("error", f"历史错误 {index}")

    assert notifier.poll()["skipped"] == "initialised"
    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert sender.messages == []

    database.event("error", "新的错误", detail="rclone 复制失败")
    result = notifier.poll()

    assert result["sent"]
    assert len(sender.messages) == 1
    assert "新的错误" in sender.messages[0]
    assert "历史错误" not in sender.messages[0]
    assert "archive-host" in sender.messages[0]


def test_each_event_is_forwarded_once_and_info_is_filtered(tmp_path) -> None:
    store, database, notifier, sender, _ = _build(tmp_path)
    notifier.poll()
    database.event("info", "例行信息")
    database.event("warning", "后台任务已取消")
    database.event("error", "后台任务失败", detail="磁盘空间不足")

    assert notifier.poll()["events"] == 2
    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert "后台任务已取消" in message
    assert "后台任务失败" in message
    assert "例行信息" not in message


def test_a_failed_delivery_is_retried_and_not_lost(tmp_path) -> None:
    """An unreachable chat must not silently swallow the only warning."""
    store, database, notifier, sender, _ = _build(tmp_path)
    notifier.poll()
    database.event("error", "校验失败", detail="sha256 不一致")
    sender.failure = "Bad Request: chat not found"

    failed = notifier.poll()
    assert failed["ok"] is False
    assert notifier.state()["last_error"] == "Bad Request: chat not found"
    assert notifier.state()["pending_events"] is True

    sender.failure = ""
    assert notifier.poll()["events"] == 1
    assert "校验失败" in sender.messages[0]
    assert notifier.state()["last_error"] == ""


def test_silence_is_alerted_once_and_recovery_is_reported_once(tmp_path) -> None:
    """A client that stopped checking emits no events, so time is the signal."""
    store, database, notifier, sender, clock = _build(tmp_path, stale_hours=6)
    notifier.record_success()
    notifier.poll()

    clock["now"] += timedelta(hours=7)
    assert notifier.poll()["sent"]
    assert "没有成功完成归档检查" in sender.messages[0]
    assert notifier.state()["stale_alerted"] is True

    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1

    notifier.record_success()
    assert notifier.poll()["sent"]
    assert "已恢复" in sender.messages[1]
    assert notifier.state()["stale_alerted"] is False
    assert notifier.poll()["skipped"] == "nothing_to_report"


def test_a_fresh_client_is_not_accused_of_being_stale(tmp_path) -> None:
    """With no success on record, silence is measured from when alerts began."""
    store, database, notifier, sender, clock = _build(tmp_path, stale_hours=6)
    notifier.poll()

    clock["now"] += timedelta(hours=3)
    assert notifier.poll()["skipped"] == "nothing_to_report"

    clock["now"] += timedelta(hours=4)
    assert notifier.poll()["sent"]
    assert "没有成功完成归档检查" in sender.messages[0]


def test_disabled_alerting_sends_nothing(tmp_path) -> None:
    store, database, notifier, sender, _ = _build(tmp_path, enabled=False)
    database.event("error", "后台任务失败")

    assert notifier.poll()["skipped"] == "disabled"
    assert sender.messages == []


def test_bot_token_is_never_published_and_never_wiped_by_a_save(tmp_path) -> None:
    """The token is a credential: readable by nobody, erasable by no accident."""
    store, _database, _notifier, _sender, _clock = _build(tmp_path)

    public = store.load().public_dict()
    assert "alert_bot_token" not in public
    assert public["alert_bot_token_set"] is True
    assert public["alert_bot_token_hint"] == "…3456"
    assert TOKEN not in str(public)

    store.update_public({"poll_minutes": 30})
    assert store.load().alert_bot_token == TOKEN


def test_a_delivery_error_cannot_leak_the_token() -> None:
    assert _scrub(f"HTTP error for https://api.telegram.org/bot{TOKEN}/x", TOKEN) == (
        "HTTP error for https://api.telegram.org/bot***/x"
    )


def test_enabling_alerts_without_credentials_is_refused(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    with pytest.raises(ValueError, match="必须填写机器人令牌与会话 ID"):
        store.update_public({"alert_enabled": True})


def test_a_malformed_token_or_chat_id_is_refused(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    with pytest.raises(ValueError, match="机器人令牌格式无效"):
        store.update_public({"alert_bot_token": "not-a-token"})
    with pytest.raises(ValueError, match="会话 ID 无效"):
        store.update_public({"alert_bot_token": TOKEN, "alert_chat_id": "群聊"})
