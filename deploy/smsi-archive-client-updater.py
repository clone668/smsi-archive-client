#!/usr/bin/env python3
"""Privileged, narrowly scoped updater for the SMSI archive client."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
from pathlib import Path


REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")
RUNTIME_PATHS = ("app.py", "archive_backup", "static", "templates", "requirements.txt")
OBSOLETE_RUNTIME_PATHS = ("archive_backup/desktop.py",)
INSTALL_DIR = Path("/data/smsi-archive-client")
STATE_DIR = Path("/data/smsi-archive-client-state")
SERVICE = "smsi-archive-client.service"


def _reply(client: socket.socket, payload: dict[str, object]) -> None:
    client.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _busy() -> bool:
    database = STATE_DIR / "state.sqlite3"
    try:
        with sqlite3.connect(database, timeout=2) as connection:
            row = connection.execute(
                "SELECT 1 FROM archive_days WHERE status IN ('downloading', 'verifying') LIMIT 1"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        # Fail closed: never replace code when state cannot be inspected.
        return True


def _validate_release(revision: str) -> Path:
    if not REVISION_RE.fullmatch(revision):
        raise RuntimeError("版本号无效")
    release = STATE_DIR / ".updates" / revision
    metadata = release / "release.json"
    if not release.is_dir() or not metadata.is_file():
        raise RuntimeError("更新包尚未下载")
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("更新包清单无效") from exc
    if payload.get("revision") != revision:
        raise RuntimeError("更新包版本校验失败")
    for relative in RUNTIME_PATHS:
        if not (release / relative).exists():
            raise RuntimeError(f"更新包缺少运行文件: {relative}")
    return release


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.chmod(temporary_path, 0o644)
    os.replace(temporary_path, destination)


def _release_files(release: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for relative in RUNTIME_PATHS:
        source = release / relative
        if source.is_file():
            files.append((source, INSTALL_DIR / relative))
        elif source.is_dir():
            for item in source.rglob("*"):
                if item.is_file() and "__pycache__" not in item.parts:
                    files.append((item, INSTALL_DIR / item.relative_to(release)))
    return files


def _activate(revision: str) -> None:
    if _busy():
        raise RuntimeError("归档任务正在运行，请完成后再安装更新")
    release = _validate_release(revision)
    requirements = release / "requirements.txt"
    live_requirements = INSTALL_DIR / "requirements.txt"
    if live_requirements.is_file():
        source_lines = requirements.read_text(encoding="utf-8").splitlines()
        live_lines = live_requirements.read_text(encoding="utf-8").splitlines()
        if source_lines != live_lines:
            raise RuntimeError("更新包含新的 Python 依赖，请先重新运行 Ubuntu 安装脚本")
    files = _release_files(release)
    if not files:
        raise RuntimeError("更新包没有可安装文件")
    backup = STATE_DIR / ".updates" / f".backup-{revision}"
    shutil.rmtree(backup, ignore_errors=True)
    try:
        for _source, destination in files:
            if destination.is_file():
                backup_path = backup / destination.relative_to(INSTALL_DIR)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_path)
        for relative in OBSOLETE_RUNTIME_PATHS:
            destination = INSTALL_DIR / relative
            if destination.is_file():
                backup_path = backup / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_path)
        for source, destination in files:
            _copy_file_atomic(source, destination)
        for relative in OBSOLETE_RUNTIME_PATHS:
            (INSTALL_DIR / relative).unlink(missing_ok=True)
        marker = INSTALL_DIR / ".smsi-release"
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary_marker = marker.with_name(f".{marker.name}.tmp")
        temporary_marker.write_text(revision + "\n", encoding="ascii")
        os.chmod(temporary_marker, 0o644)
        os.replace(temporary_marker, marker)
    except Exception:
        for item in backup.rglob("*") if backup.exists() else []:
            if item.is_file():
                _copy_file_atomic(item, INSTALL_DIR / item.relative_to(backup))
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def _handle(payload: dict[str, object]) -> dict[str, object]:
    action = str(payload.get("action") or "")
    if action == "activate":
        _activate(str(payload.get("revision") or ""))
        return {"ok": True, "activated": True}
    if action == "restart":
        if _busy():
            raise RuntimeError("归档任务正在运行，请完成后再重启客户端")
        revision = str(payload.get("revision") or "").strip().lower()
        activated = False
        if revision:
            _activate(revision)
            activated = True
        subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=30)
        return {"ok": True, "restarted": True, "activated": activated}
    raise RuntimeError("不支持的更新操作")


def main() -> None:
    socket_path = Path("/run/smsi-archive-client-updater.sock")
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o660)
    try:
        import grp
        os.chown(socket_path, 0, grp.getgrnam("smsi-archive").gr_gid)
    except (KeyError, PermissionError):
        pass
    server.listen(8)

    def stop(_signum: int, _frame: object) -> None:
        server.close()
        socket_path.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        client, _ = server.accept()
        with client:
            try:
                raw = client.recv(1024 * 1024)
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("请求格式无效")
                _reply(client, _handle(payload))
            except Exception as exc:
                _reply(client, {"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()
