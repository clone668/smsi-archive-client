from __future__ import annotations

import atexit
import hmac
import secrets
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from . import __version__
from .config import CONFIG_VERSION, ClientConfig, ConfigStore
from .database import StateDatabase
from .manager import ArchiveManager
from .service import ArchiveService
from .updates import UpdateManager


def create_app(store: ConfigStore | None = None) -> Flask:
    config_store = store or ConfigStore()
    config = config_store.load()
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    app.secret_key = config.session_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=12 * 60 * 60,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    database = StateDatabase(config_store.root / "state.sqlite3")
    service = ArchiveService(config_store, database)
    service.start()
    atexit.register(service.stop)
    updater = UpdateManager(config_store.root, root)
    app.extensions["smsi_config_store"] = config_store
    app.extensions["smsi_database"] = database
    app.extensions["smsi_archive_service"] = service
    app.extensions["smsi_update_manager"] = updater

    attempts: dict[str, list[float]] = {}
    attempt_lock = threading.Lock()

    def logged_in() -> bool:
        return session.get("authenticated") is True

    def ensure_csrf() -> str:
        token = str(session.get("csrf") or "")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    def require_csrf() -> None:
        expected = str(session.get("csrf") or "")
        observed = str(request.headers.get("X-CSRF-Token") or "")
        if not expected or not hmac.compare_digest(expected, observed):
            raise PermissionError("请求校验失败，请刷新页面后重试")

    @app.before_request
    def protect_routes():
        endpoint = request.endpoint or ""
        if endpoint in {"login", "static", "health"}:
            return None
        if not logged_in():
            if endpoint.startswith("api_"):
                return jsonify({"ok": False, "error": "登录已失效"}), 401
            return redirect(url_for("login"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            require_csrf()
        return None

    @app.errorhandler(PermissionError)
    def permission_error(exc: PermissionError):
        return jsonify({"ok": False, "error": str(exc)}), 403

    @app.errorhandler(ValueError)
    def value_error(exc: ValueError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(RuntimeError)
    def runtime_error(exc: RuntimeError):
        return jsonify({"ok": False, "error": str(exc)}), 409

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if logged_in():
                return redirect(url_for("index"))
            return render_template("login.html", error="")
        ip = request.remote_addr or "unknown"
        now = time.monotonic()
        with attempt_lock:
            recent = [item for item in attempts.get(ip, []) if now - item < 300]
            if len(recent) >= 8:
                return render_template("login.html", error="尝试次数过多，请稍后再试"), 429
        password = str(request.form.get("password") or "")
        active = config_store.load()
        if not check_password_hash(active.password_hash, password):
            with attempt_lock:
                attempts[ip] = [*recent, now]
            return render_template("login.html", error="密码不正确"), 401
        with attempt_lock:
            attempts.pop(ip, None)
        session.clear()
        session["authenticated"] = True
        session.permanent = True
        ensure_csrf()
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/")
    def index():
        return render_template("index.html", client_version=__version__)

    @app.get("/api/bootstrap")
    def api_bootstrap():
        current = config_store.load()
        return jsonify({
            "ok": True,
            "csrf": ensure_csrf(),
            "config": current.public_dict(),
            "runtime": service.status(),
            "updates": updater.status(),
            "days": database.days(120),
            "jobs": database.jobs(50),
            "comparisons": database.comparisons(30),
            "events": database.events(50),
            "initial_password_pending": config_store.initial_password_path.exists(),
        })

    @app.get("/api/status")
    def api_status():
        return jsonify({
            "ok": True,
            "runtime": service.status(),
            "updates": updater.status(),
        })

    @app.get("/api/archive-days")
    def api_archive_days():
        limit = request.args.get("limit", default=120, type=int) or 120
        return jsonify({"ok": True, "days": database.days(limit)})

    @app.get("/api/comparisons")
    def api_comparisons():
        limit = request.args.get("limit", default=30, type=int) or 30
        return jsonify({
            "ok": True,
            "comparisons": database.comparisons(limit),
        })

    @app.get("/api/events")
    def api_events():
        limit = request.args.get("limit", default=50, type=int) or 50
        return jsonify({"ok": True, "events": database.events(limit)})

    @app.get("/api/day-detail")
    def api_day_detail():
        profile_id = str(request.args.get("profile_id") or "").strip()
        archive_date = str(request.args.get("archive_date") or "").strip()
        if not profile_id or not archive_date:
            raise ValueError("缺少配置或归档日期")
        current = config_store.load()
        database_manager = ArchiveManager(current, database)
        return jsonify({
            "ok": True,
            "detail": database_manager.day_detail(profile_id, archive_date),
        })

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify({"ok": True, "jobs": database.jobs(200)})

    @app.get("/api/jobs/<int:job_id>/items")
    def api_job_items(job_id: int):
        job = database.job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "items": database.job_items(job_id, 10000),
        })

    @app.get("/api/files/dates")
    def api_file_dates():
        profile_id = str(request.args.get("profile_id") or "").strip()
        scope = str(request.args.get("scope") or "").strip()
        if not profile_id or not scope:
            raise ValueError("缺少文件浏览参数")
        manager = ArchiveManager(config_store.load(), database)
        return jsonify({
            "ok": True,
            "result": manager.browse_dates(profile_id, scope=scope),
        })

    @app.get("/api/files/list")
    def api_file_list():
        profile_id = str(request.args.get("profile_id") or "").strip()
        archive_date = str(request.args.get("archive_date") or "").strip()
        scope = str(request.args.get("scope") or "").strip()
        path = str(request.args.get("path") or "").strip()
        if not profile_id or not archive_date or not scope:
            raise ValueError("缺少文件浏览参数")
        manager = ArchiveManager(config_store.load(), database)
        return jsonify({
            "ok": True,
            "result": manager.browse_files(
                profile_id, archive_date, scope=scope, path=path
            ),
        })

    @app.get("/api/update/check")
    def api_update_check():
        return jsonify({"ok": True, "updates": updater.check()})

    @app.get("/api/update/status")
    def api_update_status():
        return jsonify({
            "ok": True,
            "updates": updater.status(),
            "archive_running": bool(service.status().get("running")),
        })

    @app.post("/api/update/download")
    def api_update_download():
        payload = request.get_json(silent=True) or {}
        revision = str(payload.get("revision") or "").strip()
        if not revision:
            raise ValueError("缺少目标版本")
        result = updater.download(revision)
        database.event("info", "更新包已下载", detail=revision)
        return jsonify({"ok": True, "result": result, "updates": updater.status()})

    @app.post("/api/update/restart")
    def api_update_restart():
        payload = request.get_json(silent=True) or {}
        # A maintenance restart must not install a staged release as a side effect.
        activate = payload.get("activate")
        activate = True if activate is None else bool(activate)
        if activate:
            # Refuse before stopping anything: a task paused for an operation
            # the helper will reject has lost a pass for nothing.
            updater.ensure_switchable()
        archive_was_running = bool(service.status().get("running"))
        if not service.stop(timeout=30):
            service.resume_when_stopped()
            raise RuntimeError("当前任务未能在 30 秒内安全暂停，客户端没有重启")
        try:
            result = updater.restart(activate=activate)
        except Exception:
            # The process stays alive when the privileged helper rejects the
            # request, so resume archive work instead of leaving it stopped.
            service.start()
            raise
        if archive_was_running:
            database.event(
                "info",
                "客户端重启前已安全暂停归档任务",
                detail="已完成对象保留，客户端启动后将继续任务",
            )
        return jsonify({"ok": True, "result": result})

    @app.put("/api/config")
    def api_config():
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            raise ValueError("配置内容无效")
        allowed = {
            "local_root", "rclone_binary", "poll_minutes", "history_days",
            "download_workers", "bandwidth_limit", "minimum_free_bytes",
            "auto_download", "web_host", "web_port", "profiles",
            "alert_enabled", "alert_bot_token", "alert_chat_id",
            "alert_min_level", "alert_stale_hours",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("配置包含不支持的字段")
        previous = config_store.load()
        changed = {
            key for key, value in payload.items()
            if previous.public_dict().get(key) != value
        }
        if (
            "local_root" in changed
            and database.days(1)
        ):
            raise RuntimeError(
                "本地归档目录已有状态记录，不能在普通设置中修改；请使用专用迁移流程"
            )
        if "profiles" in payload:
            proposed = {
                str(item.get("profile_id") or ""): item
                for item in payload.get("profiles") or []
                if isinstance(item, Mapping)
            }
            for profile in previous.profiles:
                if not database.profile_day_count(profile.profile_id):
                    continue
                replacement = proposed.get(profile.profile_id)
                if replacement is None:
                    raise RuntimeError(
                        f"{profile.display_name} 已有归档状态，不能删除或修改配置 ID"
                    )
                if str(replacement.get("collector_id") or "") != profile.collector_id:
                    raise RuntimeError(
                        f"{profile.display_name} 已有归档状态，不能修改 Collector ID"
                    )
        current = config_store.update_public(payload)
        rescan_fields = {"local_root", "history_days", "profiles", "auto_download"}
        restart_fields = {"web_host", "web_port"}
        next_task_fields = {
            "download_workers", "bandwidth_limit", "minimum_free_bytes",
            "rclone_binary",
        }
        rescan_required = bool(changed & rescan_fields)
        if rescan_required:
            try:
                service.request_scan(
                    download=current.auto_download, requested_by="config"
                )
            except RuntimeError:
                service.wake()
        elif "poll_minutes" in changed or any(
            key.startswith("alert_") for key in payload
        ):
            # An alert setting must take effect now, not in fifteen minutes.
            service.wake()
        activation = {
            "rescan_started": rescan_required,
            "restart_required": bool(changed & restart_fields),
            "next_task": bool(changed & next_task_fields),
            "changed_fields": sorted(changed),
        }
        database.event(
            "info",
            "客户端配置已更新",
            detail=(
                "Web 服务重启后生效"
                if activation["restart_required"]
                else "设置已生效"
            ),
        )
        return jsonify({
            "ok": True,
            "config": current.public_dict(),
            "activation": activation,
        })

    @app.post("/api/actions/scan")
    def api_scan():
        payload = request.get_json(silent=True) or {}
        job = service.request_scan(download=bool(payload.get("download", True)))
        return jsonify({"ok": True, "job": job})

    @app.post("/api/actions/download")
    def api_download():
        payload = request.get_json(silent=True) or {}
        profile_id = str(payload.get("profile_id") or "").strip()
        archive_date = str(payload.get("archive_date") or "").strip()
        if not profile_id or not archive_date:
            raise ValueError("缺少采集服务器或归档日期")
        try:
            date.fromisoformat(archive_date)
        except ValueError as exc:
            raise ValueError("归档日期无效") from exc
        job = service.request_download(profile_id, archive_date)
        return jsonify({"ok": True, "job": job})

    @app.post("/api/actions/verify")
    def api_verify():
        payload = request.get_json(silent=True) or {}
        profile_id = str(payload.get("profile_id") or "")
        archive_date = str(payload.get("archive_date") or "")
        if not profile_id or not archive_date:
            raise ValueError("缺少配置或归档日期")
        try:
            date.fromisoformat(archive_date)
        except ValueError as exc:
            raise ValueError("归档日期无效") from exc
        job = service.request_verify(profile_id, archive_date)
        return jsonify({"ok": True, "job": job})

    @app.post("/api/actions/cancel")
    def api_cancel():
        service.request_cancel()
        return jsonify({"ok": True})

    @app.post("/api/actions/alert-test")
    def api_alert_test():
        # Deliberately independent of the alert cursor: this answers "does the
        # channel work right now", which is the only question worth a button.
        service.notifier.send_test()
        database.event("info", "已发送告警测试消息")
        return jsonify({"ok": True, "alert": service.notifier.state()})

    @app.put("/api/password")
    def api_password():
        payload = request.get_json(silent=True) or {}
        old_password = str(payload.get("current_password") or "")
        new_password = str(payload.get("new_password") or "")
        current = config_store.load()
        if not check_password_hash(current.password_hash, old_password):
            raise PermissionError("当前密码不正确")
        config_store.change_password(new_password)
        database.event("info", "Web 登录密码已修改")
        return jsonify({"ok": True})

    return app
