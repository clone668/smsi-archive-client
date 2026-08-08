from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Callable

from .config import ClientConfig, ProfileConfig
from .verifier import OperationCancelled


DATE_DIR_RE = re.compile(r"^date=(20\d{2}-\d{2}-\d{2})/?$")
ProgressCallback = Callable[[int], None]


def safe_relative_key(value: str) -> str:
    text = str(value or "").strip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or "\\" in text:
        raise RuntimeError("归档来源路径无效")
    return path.as_posix()


def resolve_rclone_binary(configured: str) -> str:
    value = configured.strip() or "rclone"
    resolved = shutil.which(value)
    if resolved:
        return resolved
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    if os.name == "nt" and candidate.name.lower() in {"rclone", "rclone.exe"}:
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            root = Path(local_app_data) / "Microsoft" / "WinGet"
            candidates = [root / "Links" / "rclone.exe"]
            candidates.extend(sorted((root / "Packages").glob("Rclone.Rclone_*/*/rclone.exe"), reverse=True))
            for item in candidates:
                if item.is_file():
                    return str(item.resolve())
    raise RuntimeError("未找到 rclone，请先安装或在设置中指定路径")


class ArchiveSource(ABC):
    name: str

    @abstractmethod
    def list_dates(self) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def read_small(self, relative_key: str, maximum_bytes: int) -> bytes | None:
        raise NotImplementedError

    @abstractmethod
    def download(self, relative_key: str, destination: Path, cancel: Event | None = None) -> int:
        raise NotImplementedError


class RcloneDriveSource(ArchiveSource):
    name = "google_drive"

    def __init__(self, config: ClientConfig, profile: ProfileConfig) -> None:
        self.config = config
        self.profile = profile
        self.binary = resolve_rclone_binary(config.rclone_binary)

    @staticmethod
    def _creation_flags() -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _remote_path(self, relative_key: str = "") -> str:
        if not relative_key:
            return self.profile.drive_root.rstrip("/")
        return f"{self.profile.drive_root.rstrip('/')}/{safe_relative_key(relative_key)}"

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: int,
        binary_output: bool = False,
        missing_ok: bool = False,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.binary, *arguments],
                capture_output=True,
                text=not binary_output,
                encoding=None if binary_output else "utf-8",
                errors=None if binary_output else "replace",
                timeout=timeout,
                check=True,
                creationflags=self._creation_flags(),
            )
        except subprocess.CalledProcessError as exc:
            raw = exc.stderr or exc.stdout or b"rclone failed"
            detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            lowered = detail.casefold()
            if missing_ok and any(marker in lowered for marker in ("not found", "directory not found", "object not found")):
                raise FileNotFoundError(detail.strip()) from exc
            raise RuntimeError(f"rclone 执行失败: {detail.strip()[:2000]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("rclone 请求超时") from exc

    def list_dates(self) -> set[str]:
        result = self._run(
            ["lsf", self._remote_path(), "--dirs-only", "--max-depth", "1"],
            timeout=90,
        )
        dates: set[str] = set()
        for line in result.stdout.splitlines():
            match = DATE_DIR_RE.fullmatch(line.strip())
            if match:
                dates.add(match.group(1))
        return dates

    def read_small(self, relative_key: str, maximum_bytes: int) -> bytes | None:
        try:
            result = self._run(
                ["cat", self._remote_path(relative_key), "--count", str(maximum_bytes + 1)],
                timeout=120,
                binary_output=True,
                missing_ok=True,
            )
        except FileNotFoundError:
            return None
        payload = bytes(result.stdout)
        if len(payload) > maximum_bytes:
            raise RuntimeError(f"远端文件超过安全大小: {relative_key}")
        return payload

    def download(self, relative_key: str, destination: Path, cancel: Event | None = None) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        arguments = [
            "copyto",
            self._remote_path(relative_key),
            str(destination),
            "--no-traverse",
            "--transfers",
            "1",
            "--checkers",
            "2",
            "--retries",
            "3",
            "--low-level-retries",
            "5",
            "--contimeout",
            "15s",
            "--timeout",
            "2m",
        ]
        limit = self.config.bandwidth_limit.strip()
        if limit:
            arguments.extend(["--bwlimit", limit])
        process = subprocess.Popen(
            [self.binary, *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=self._creation_flags(),
        )
        deadline = time.monotonic() + 24 * 60 * 60
        while True:
            if cancel is not None and cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                raise OperationCancelled("下载已取消，未完成结果不会发布")
            if time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                raise RuntimeError("rclone 下载超时")
            try:
                _stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            raise RuntimeError(f"rclone 下载失败: {(stderr or 'unknown error').strip()[:2000]}")
        if not destination.is_file():
            raise RuntimeError(f"rclone 未生成目标文件: {relative_key}")
        return destination.stat().st_size


class VerifiedDirectorySource(ArchiveSource):
    name = "verified_directory"

    def __init__(self, profile: ProfileConfig) -> None:
        self.profile = profile
        self.root = Path(profile.verified_source_root).expanduser().resolve()

    def _path(self, relative_key: str) -> Path:
        key = safe_relative_key(relative_key)
        candidate = (self.root / Path(*PurePosixPath(key).parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise RuntimeError("已验证目录路径越界") from exc
        return candidate

    def _day_is_verified(self, archive_date: str) -> bool:
        receipt = self._path(f"date={archive_date}/.smsi-verified.json")
        manifest = self._path(f"date={archive_date}/manifest.json")
        if not receipt.is_file() or not manifest.is_file():
            return False
        if receipt.stat().st_size > 32 * 1024 * 1024 or manifest.stat().st_size > 32 * 1024 * 1024:
            return False
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
        return (
            isinstance(payload, dict)
            and payload.get("contract_version") == "smsi-local-archive-verification/v1"
            and payload.get("status") == "verified"
            and payload.get("archive_date") == archive_date
            and payload.get("manifest_sha256") == manifest_sha
        )

    def list_dates(self) -> set[str]:
        if not self.root.is_dir():
            raise RuntimeError(f"已验证目录不可访问: {self.root}")
        dates: set[str] = set()
        for item in self.root.iterdir():
            match = DATE_DIR_RE.fullmatch(item.name)
            if item.is_dir() and match and self._day_is_verified(match.group(1)):
                dates.add(match.group(1))
        return dates

    def read_small(self, relative_key: str, maximum_bytes: int) -> bytes | None:
        key = safe_relative_key(relative_key)
        date_match = DATE_DIR_RE.fullmatch(PurePosixPath(key).parts[0])
        if not date_match or not self._day_is_verified(date_match.group(1)):
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        if path.stat().st_size > maximum_bytes:
            raise RuntimeError(f"来源文件超过安全大小: {relative_key}")
        return path.read_bytes()

    def download(self, relative_key: str, destination: Path, cancel: Event | None = None) -> int:
        key = safe_relative_key(relative_key)
        date_match = DATE_DIR_RE.fullmatch(PurePosixPath(key).parts[0])
        if not date_match or not self._day_is_verified(date_match.group(1)):
            raise RuntimeError("来源日期尚未完成本地验证")
        source = self._path(key)
        if not source.is_file():
            raise RuntimeError(f"来源对象不存在: {relative_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_handle, destination.open("wb") as output_handle:
            for chunk in iter(lambda: input_handle.read(8 * 1024 * 1024), b""):
                if cancel is not None and cancel.is_set():
                    raise OperationCancelled("复制已取消，未完成结果不会发布")
                output_handle.write(chunk)
        return destination.stat().st_size


def build_source(config: ClientConfig, profile: ProfileConfig) -> ArchiveSource:
    if profile.source_type == "google_drive":
        return RcloneDriveSource(config, profile)
    if profile.source_type == "verified_directory":
        return VerifiedDirectorySource(profile)
    raise RuntimeError(f"不支持的来源类型: {profile.source_type}")
