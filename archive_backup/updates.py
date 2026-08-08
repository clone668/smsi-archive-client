from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY = "clone668/smsi-archive-client"
GITHUB_API = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
GITHUB_ARCHIVE = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{{revision}}"
REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
HELPER_SOCKET = "/run/smsi-archive-client-updater.sock"
RUNTIME_PATHS = (
    "app.py",
    "archive_backup",
    "static",
    "templates",
    "requirements.txt",
)


def _revision(value: str) -> str:
    value = str(value or "").strip().lower()
    if not REVISION_RE.fullmatch(value):
        raise ValueError("版本号无效")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_member_name(name: str) -> str:
    path = PurePosixPath(str(name).lstrip("/"))
    parts = path.parts
    if not parts or ".." in parts or any("\\" in part for part in parts):
        raise RuntimeError("更新包包含不安全路径")
    # GitHub archives contain one top-level directory named repo-revision.
    relative = PurePosixPath(*parts[1:])
    if not relative or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("更新包目录结构无效")
    return relative.as_posix()


class UpdateManager:
    """Check and stage a pinned GitHub source archive without changing live code."""

    def __init__(self, state_root: Path, app_root: Path, *, helper_socket: str = HELPER_SOCKET) -> None:
        self.state_root = state_root.resolve()
        self.app_root = app_root.resolve()
        self.update_root = self.state_root / ".updates"
        self.metadata_path = self.update_root / "latest.json"
        self.helper_socket = helper_socket

    def current_revision(self) -> str:
        marker = self.app_root / ".smsi-release"
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip().lower()
            if REVISION_RE.fullmatch(value):
                return value
        return "unknown"

    def _request_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "SMSI-Archive-Client"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"无法读取更新信息: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("更新信息格式无效")
        return payload

    def check(self) -> dict[str, Any]:
        payload = self._request_json(GITHUB_API)
        remote = _revision(str(payload.get("sha") or ""))
        commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
        message = str(commit.get("message") or "").splitlines()[0][:200]
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        latest = {"revision": remote, "message": message, "published_at": str(author.get("date") or "")}
        self.update_root.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
        staged = self.update_root / remote
        return {
            "current_revision": self.current_revision(),
            "latest": latest,
            "staged_revision": remote if staged.is_dir() and self.current_revision() != remote else "",
            "update_available": self.current_revision() != remote,
            "helper_available": Path(self.helper_socket).exists(),
        }

    def status(self) -> dict[str, Any]:
        current = self.current_revision()
        latest: dict[str, Any] = {}
        if self.metadata_path.is_file():
            try:
                value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    latest = value
            except (OSError, ValueError):
                latest = {}
        remote = str(latest.get("revision") or "")
        staged = remote if remote and (self.update_root / remote).is_dir() and current != remote else ""
        return {
            "current_revision": current,
            "latest": latest,
            "staged_revision": staged,
            "update_available": bool(remote and current != remote),
            "helper_available": Path(self.helper_socket).exists(),
        }

    def download(self, revision: str) -> dict[str, Any]:
        revision = _revision(revision)
        latest = self._request_json(GITHUB_API)
        remote = _revision(str(latest.get("sha") or ""))
        if remote != revision:
            raise RuntimeError("远端版本已变化，请重新检查更新")
        self.update_root.mkdir(parents=True, exist_ok=True)
        target = self.update_root / revision
        if target.is_dir():
            return {"revision": revision, "staged": True, "bytes": 0}
        archive = self.update_root / f"{revision}.tar.gz.partial"
        temporary = self.update_root / f".{revision}.download"
        total = 0
        digest = hashlib.sha256()
        request = urllib.request.Request(
            GITHUB_ARCHIVE.format(revision=revision),
            headers={"User-Agent": "SMSI-Archive-Client"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("更新包超过允许大小")
                    digest.update(chunk)
                    output.write(chunk)
            temporary.replace(archive)
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                if not members:
                    raise RuntimeError("更新包为空")
                extracted = Path(tempfile.mkdtemp(prefix=f"smsi-update-{revision}-", dir=self.update_root))
                try:
                    for member in members:
                        relative = _safe_member_name(member.name)
                        if not relative:
                            continue
                        destination = extracted / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if member.isdir():
                            destination.mkdir(exist_ok=True)
                        elif member.isfile():
                            with bundle.extractfile(member) as source, destination.open("wb") as output:
                                shutil.copyfileobj(source, output)
                    missing = [path for path in ("app.py", "archive_backup", "static", "templates") if not (extracted / path).exists()]
                    if missing:
                        raise RuntimeError("更新包缺少运行文件: " + ", ".join(missing))
                    extracted.replace(target)
                except Exception:
                    shutil.rmtree(extracted, ignore_errors=True)
                    raise
            (target / "release.json").write_text(
                json.dumps({"revision": revision, "sha256": digest.hexdigest(), "downloaded_at": _utc_now()}, ensure_ascii=False),
                encoding="utf-8",
            )
            return {"revision": revision, "staged": True, "bytes": total}
        finally:
            temporary.unlink(missing_ok=True)

    def activate(self, revision: str) -> dict[str, Any]:
        revision = _revision(revision)
        request = {"action": "activate", "revision": revision}
        return self._helper_request(request)

    def restart(self) -> dict[str, Any]:
        return self._helper_request({"action": "restart"})

    def _helper_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not Path(self.helper_socket).exists():
            raise RuntimeError("更新助手未安装，请先运行 Ubuntu 安装脚本")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(8)
                channel.connect(self.helper_socket)
                channel.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                raw = channel.makefile("rb").readline(1024 * 1024)
        except OSError as exc:
            raise RuntimeError(f"无法连接更新助手: {exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("更新助手返回无效响应") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "更新助手拒绝操作"))
        return result
