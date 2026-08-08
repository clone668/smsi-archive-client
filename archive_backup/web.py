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
        return render_template("index.html")

    @app.get("/api/bootstrap")
    def api_bootstrap():
        current = config_store.load()
        return jsonify({
            "ok": True,
            "csrf": ensure_csrf(),
            "config": current.public_dict(),
            "runtime": service.status(),
            "updates": updater.status(),
            "days": database.days(1000),
            "events": database.events(100),
            "initial_password_pending": config_store.initial_password_path.exists(),
        })

    @app.get("/api/status")
    def api_status():
        return jsonify({
            "ok": True,
            "runtime": service.status(),
            "updates": updater.status(),
            "days": database.days(1000),
            "events": database.events(100),
        })

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
        if not profile_id or not archive_date or not scope:
            raise ValueError("缺少文件浏览参数")
        manager = ArchiveManager(config_store.load(), database)
        return jsonify({
            "ok": True,
            "result": manager.browse_files(
                profile_id, archive_date, scope=scope
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
        if service.status().get("running"):
            raise RuntimeError("归档任务正在运行，请完成后再重启客户端")
        result = updater.restart()
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
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("配置包含不支持的字段")
        current = config_store.update_public(payload)
        try:
            service.request_scan(download=current.auto_download)
        except RuntimeError:
            service.wake()
        database.event("info", "客户端配置已更新")
        return jsonify({"ok": True, "config": current.public_dict()})

    @app.post("/api/actions/scan")
    def api_scan():
        payload = request.get_json(silent=True) or {}
        service.request_scan(download=bool(payload.get("download", True)))
        return jsonify({"ok": True})

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
        service.request_verify(profile_id, archive_date)
        return jsonify({"ok": True})

    @app.post("/api/actions/cancel")
    def api_cancel():
        service.request_cancel()
        return jsonify({"ok": True})

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
