"""Outbound alerting so that unattended failures do not stay invisible.

This client runs headless on Ubuntu, so anything it detects but cannot report is
unknown until somebody opens the web UI.  Two independent triggers are covered
here:

* high-severity events the client already records, forwarded once each;
* *silence* - a client that has stopped checking records no events at all,
  which is the one failure event-driven alerting can never catch by itself.

Nothing here is on the data path: an alert that cannot be delivered is retried
on the next poll and never fails a sync, a download or a verification.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .config import ConfigStore
from .database import StateDatabase


ALERT_STATE_KEY = "alert_notifier"
LAST_SUCCESS_KEY = "last_successful_scan_at"
LEVEL_RANK = {"info": 10, "warning": 20, "error": 30}
LEVEL_MARKS = {"info": "·", "warning": "!", "error": "X"}
LEVEL_LABELS = {"info": "信息", "warning": "警告", "error": "错误"}
MAX_EVENTS_PER_MESSAGE = 8
MAX_MESSAGE_CHARS = 3500  # Telegram rejects anything past 4096.
REQUEST_TIMEOUT_SECONDS = 10
TELEGRAM_ENDPOINT = "https://api.telegram.org/bot{token}/sendMessage"

Sender = Callable[[str, str, str], None]


def level_rank(level: str) -> int:
    return LEVEL_RANK.get(str(level or "").strip().lower(), LEVEL_RANK["info"])


def forwarded_levels(minimum: str) -> list[str]:
    """Event levels at or above the configured minimum."""
    threshold = level_rank(minimum)
    return [name for name, rank in LEVEL_RANK.items() if rank >= threshold]


def _scrub(text: str, secret: str) -> str:
    """Keep the bot token out of anything that can be logged or displayed."""
    cleaned = str(text or "")
    if secret and len(secret) > 8:
        cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    """Telegram explains refusals in the body; that text is what the user needs."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        payload = {}
    description = ""
    if isinstance(payload, Mapping):
        description = str(payload.get("description") or "")
    return description or f"HTTP {exc.code}"


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Deliver one message, or raise RuntimeError with a token-free reason."""
    url = TELEGRAM_ENDPOINT.format(token=token)
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            payload = json.loads(reply.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_scrub(_describe_http_error(exc), token)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(_scrub(str(exc) or exc.__class__.__name__, token)) from exc
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        detail = ""
        if isinstance(payload, Mapping):
            detail = str(payload.get("description") or "")
        raise RuntimeError(_scrub(detail or "Telegram 拒绝了这条消息", token))


def parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _short_time(value: str) -> str:
    moment = parse_utc(value)
    return moment.strftime("%m-%d %H:%M") if moment else "--"


def _describe_hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f}" if hours < 10 else str(int(hours))


class AlertNotifier:
    """Forwards recorded events and prolonged silence to one Telegram chat.

    State lives in ``runtime_state`` so that a restart neither replays old
    alerts nor forgets an alarm that is still active.  ``sender`` is injectable
    so tests never touch the network.
    """

    def __init__(
        self,
        store: ConfigStore,
        database: StateDatabase,
        *,
        sender: Sender | None = None,
        hostname: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.database = database
        self._sender = sender or send_telegram
        self._hostname = hostname or socket.gethostname()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _load_state(self) -> dict[str, Any]:
        value = self.database.get_runtime(ALERT_STATE_KEY, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _stamp(self) -> str:
        """Every timestamp this class writes or compares comes from one clock."""
        return self._now().isoformat().replace("+00:00", "Z")

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.database.set_runtime(ALERT_STATE_KEY, dict(state))

    def state(self) -> dict[str, Any]:
        """Delivery health for the web UI: alerting itself can fail silently."""
        state = self._load_state()
        return {
            "watching_since": str(state.get("watch_started_at") or ""),
            "last_sent_at": str(state.get("last_sent_at") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_error_at": str(state.get("last_error_at") or ""),
            "stale_alerted": bool(state.get("stale_alerted")),
            "pending_events": bool(state.get("pending_events")),
        }

    def record_success(self) -> None:
        """Mark a completed check; this is what the silence alarm measures."""
        self.database.set_runtime(LAST_SUCCESS_KEY, self._stamp())

    def _compose(self, headline: str, lines: Sequence[str]) -> str:
        head = f"【SMSI 归档备份客户端】{self._hostname}"
        text = "\n".join([head, headline, ""] + list(lines))
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
        return text[: MAX_MESSAGE_CHARS - 9] + "\n…（已截断）"

    def _event_lines(self, events: Sequence[Mapping[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in events:
            mark = LEVEL_MARKS.get(str(item.get("level") or ""), "·")
            when = _short_time(str(item.get("created_at") or ""))
            scope = " ".join(
                part
                for part in (
                    str(item.get("profile_id") or ""),
                    str(item.get("archive_date") or ""),
                )
                if part
            )
            lines.append(f"{mark} {when} {item.get('event') or ''}".rstrip())
            if scope:
                lines.append(f"    {scope}")
            detail = str(item.get("detail") or "").strip().replace("\n", " ")
            if detail:
                lines.append(f"    {detail[:200]}")
        return lines

    def _silence_lines(
        self, stale_hours: int, state: dict[str, Any]
    ) -> tuple[list[str], bool | None]:
        """Report crossing into, and out of, "this client stopped checking".

        Returns the lines to send and the new alarm flag, or ``None`` when the
        flag must not change.  A never-yet-successful client is measured from
        the moment alerting was switched on, so a fresh install is not accused
        of being stale.
        """
        reference = parse_utc(
            str(self.database.get_runtime(LAST_SUCCESS_KEY, "") or "")
        ) or parse_utc(str(state.get("watch_started_at") or ""))
        if reference is None:
            return [], None
        idle = self._now() - reference
        alerted = bool(state.get("stale_alerted"))
        if idle >= timedelta(hours=max(1, int(stale_hours))):
            if alerted:
                return [], None
            return [
                f"! 已经 {_describe_hours(idle)} 小时没有成功完成归档检查",
                f"    上次成功：{_short_time(reference.isoformat())}",
                "    请确认 smsi-archive-client 服务是否仍在运行",
            ], True
        if alerted:
            return [f"· 归档检查已恢复，上次成功 {_short_time(reference.isoformat())}"], False
        return [], None

    def poll(self) -> dict[str, Any]:
        """One alerting pass.  Never raises: alerting must not break the loop."""
        try:
            config = self.store.load()
        except Exception as exc:  # noqa: BLE001 - a bad config must not stop work
            return {"ok": False, "sent": 0, "error": str(exc)[:200]}
        if not config.alert_enabled:
            return {"ok": True, "sent": 0, "skipped": "disabled"}
        token = config.alert_bot_token.strip()
        chat_id = config.alert_chat_id.strip()
        if not token or not chat_id:
            return {"ok": True, "sent": 0, "skipped": "unconfigured"}
        state = self._load_state()
        if not state.get("watch_started_at"):
            # Enabling alerts must not replay the whole event history into the
            # chat, so the first pass only marks where "new" begins.
            state["watch_started_at"] = self._stamp()
            state["last_event_id"] = self.database.max_event_id()
            self._save_state(state)
            return {"ok": True, "sent": 0, "skipped": "initialised"}
        return self._deliver(config, token, chat_id, state)

    def _deliver(
        self, config: Any, token: str, chat_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        watermark = int(state.get("last_event_id") or 0)
        events = self.database.events_since(
            watermark,
            forwarded_levels(config.alert_min_level),
            limit=MAX_EVENTS_PER_MESSAGE,
        )
        silence_lines, silence_flag = self._silence_lines(
            int(config.alert_stale_hours), state
        )
        lines = silence_lines + self._event_lines(events)
        if not lines:
            # Keep the cursor moving even when nothing qualified, otherwise the
            # query rescans an ever-growing tail of the event log.
            newest = self.database.max_event_id()
            if newest != watermark or state.get("pending_events"):
                state["last_event_id"] = newest
                state["pending_events"] = False
                self._save_state(state)
            return {"ok": True, "sent": 0, "skipped": "nothing_to_report"}
        counts: dict[str, int] = {}
        for item in events:
            level = str(item.get("level") or "info")
            counts[level] = counts.get(level, 0) + 1
        parts = [
            f"{count} 条{LEVEL_LABELS.get(level, level)}"
            for level, count in counts.items()
        ]
        if silence_lines:
            parts.append("检查中断" if silence_flag else "已恢复")
        headline = "、".join(parts) or "状态更新"
        try:
            self._sender(token, chat_id, self._compose(headline, lines))
        except RuntimeError as exc:
            reason = str(exc)[:200]
            if reason != str(state.get("last_error") or ""):
                # Only the first occurrence is worth an event; alerting about a
                # broken alert channel through that same channel cannot work,
                # so the web UI is the only place this can be seen.
                self.database.event(
                    "info", "告警通知发送失败", detail=f"{reason}（本次告警会自动重试）"
                )
            state["last_error"] = reason
            state["last_error_at"] = self._stamp()
            state["pending_events"] = True
            self._save_state(state)
            return {"ok": False, "sent": 0, "error": reason}
        if events:
            state["last_event_id"] = max(int(item.get("id") or 0) for item in events)
        if silence_flag is not None:
            state["stale_alerted"] = silence_flag
        state["pending_events"] = bool(
            self.database.events_since(
                int(state.get("last_event_id") or 0),
                forwarded_levels(config.alert_min_level),
                limit=1,
            )
        )
        state["last_sent_at"] = self._stamp()
        state["last_error"] = ""
        state["last_error_at"] = ""
        self._save_state(state)
        return {"ok": True, "sent": len(lines), "events": len(events)}

    def send_test(self) -> dict[str, Any]:
        """Prove the channel works now, without touching the alert cursor."""
        config = self.store.load()
        token = config.alert_bot_token.strip()
        chat_id = config.alert_chat_id.strip()
        if not token or not chat_id:
            raise RuntimeError("请先填写 Telegram 机器人令牌与会话 ID")
        text = self._compose(
            "测试消息",
            [
                "· 告警通道连通性测试",
                f"    发送时间 {_short_time(self._stamp())} UTC",
            ],
        )
        self._sender(token, chat_id, text)
        state = self._load_state()
        state["last_test_at"] = self._stamp()
        state["last_error"] = ""
        state["last_error_at"] = ""
        if not state.get("watch_started_at"):
            state["watch_started_at"] = self._stamp()
            state["last_event_id"] = self.database.max_event_id()
        self._save_state(state)
        return {"ok": True}
