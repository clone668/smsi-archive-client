from __future__ import annotations

import json
import ipaddress
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from werkzeug.security import generate_password_hash


APP_NAME = "SMSIArchiveBackupClient"
CONFIG_VERSION = 3
MIN_PASSWORD_LENGTH = 6
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:$")


def app_data_dir() -> Path:
    override = os.getenv("SMSI_ARCHIVE_CLIENT_DATA", "").strip()
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local")
        root /= APP_NAME
    else:
        root = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state")
        root /= "smsi-archive-backup-client"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def default_archive_root() -> Path:
    override = os.getenv("SMSI_ARCHIVE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        drive = Path("D:/")
        return (drive if drive.exists() else Path.home()) / "SMSI-Archive"
    return Path.home() / "SMSI-Archive"


def default_web_host() -> str:
    return os.getenv("SMSI_ARCHIVE_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"


def default_web_port() -> int:
    try:
        return int(os.getenv("SMSI_ARCHIVE_WEB_PORT", "8788"))
    except ValueError:
        return 8788


def _valid_prefix(value: str) -> bool:
    text = str(value or "").strip("/")
    path = PurePosixPath(text)
    return bool(text) and not path.is_absolute() and ".." not in path.parts and "\\" not in text


def _set_private_mode(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@dataclass(frozen=True)
class ProfileConfig:
    profile_id: str
    display_name: str
    collector_id: str
    enabled: bool = True
    source_type: str = "google_drive"
    drive_remote: str = "gdrive:"
    drive_prefix: str = "smsi/v3"
    verified_source_root: str = ""

    @property
    def drive_root(self) -> str:
        return (
            f"{self.drive_remote}{self.drive_prefix.strip('/')}"
            f"/collector={self.collector_id}"
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not IDENTITY_RE.fullmatch(self.profile_id):
            errors.append("配置 ID 无效")
        if not self.display_name.strip():
            errors.append("配置名称不能为空")
        if not IDENTITY_RE.fullmatch(self.collector_id):
            errors.append("采集服务器 ID 无效")
        if self.source_type not in {"google_drive", "verified_directory"}:
            errors.append("归档来源类型无效")
        elif self.source_type == "google_drive":
            if not REMOTE_RE.fullmatch(self.drive_remote):
                errors.append("Google Drive remote 应类似 gdrive:")
            if not _valid_prefix(self.drive_prefix):
                errors.append("Google Drive 前缀无效")
        elif self.source_type == "verified_directory":
            if not self.verified_source_root.strip():
                errors.append("已验证目录来源不能为空")
        return errors

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProfileConfig":
        return cls(
            profile_id=str(value.get("profile_id") or "").strip(),
            display_name=str(value.get("display_name") or "").strip(),
            collector_id=str(value.get("collector_id") or "").strip(),
            enabled=bool(value.get("enabled", True)),
            source_type=str(value.get("source_type") or "google_drive").strip(),
            drive_remote=str(value.get("drive_remote") or "gdrive:").strip(),
            drive_prefix=str(value.get("drive_prefix") or "smsi/v3").strip("/"),
            verified_source_root=str(value.get("verified_source_root") or "").strip(),
        )


def ubuntu_default_profiles() -> list[ProfileConfig]:
    return [
        ProfileConfig(
            profile_id="tencent-paper",
            display_name="tencent-paper",
            collector_id="tencent-paper",
        ),
        ProfileConfig(
            profile_id="tencent-report",
            display_name="tencent-report",
            collector_id="tencent-report",
        ),
    ]


def _migrate_config_payload(
    value: Mapping[str, Any], *, ubuntu: bool | None = None
) -> tuple[dict[str, Any], bool]:
    payload = dict(value)
    try:
        version = int(payload.get("config_version") or 1)
    except (TypeError, ValueError):
        version = 1
    if version > CONFIG_VERSION:
        return payload, False
    changed = version < CONFIG_VERSION
    if version < 3:
        profiles = [
            dict(item)
            for item in payload.get("profiles") or []
            if isinstance(item, Mapping)
            and str(item.get("source_type") or "google_drive") != "ubuntu_sftp"
        ]
        payload["profiles"] = profiles
    if version < 2 and (os.name != "nt" if ubuntu is None else ubuntu):
        profiles = [dict(item) for item in payload.get("profiles") or [] if isinstance(item, Mapping)]
        used_ids = {str(item.get("profile_id") or "") for item in profiles}
        used_collectors = {str(item.get("collector_id") or "") for item in profiles}
        for profile in ubuntu_default_profiles():
            if profile.profile_id in used_ids or profile.collector_id in used_collectors:
                continue
            profiles.append(asdict(profile))
        payload["profiles"] = profiles
    payload["config_version"] = CONFIG_VERSION
    return payload, changed


@dataclass
class ClientConfig:
    config_version: int = CONFIG_VERSION
    local_root: str = field(default_factory=lambda: str(default_archive_root()))
    rclone_binary: str = "rclone"
    poll_minutes: int = 15
    history_days: int = 45
    download_workers: int = 2
    bandwidth_limit: str = "20M"
    minimum_free_bytes: int = 10 * 1024 * 1024 * 1024
    auto_download: bool = True
    web_host: str = field(default_factory=default_web_host)
    web_port: int = field(default_factory=default_web_port)
    password_hash: str = ""
    session_secret: str = ""
    profiles: list[ProfileConfig] = field(
        default_factory=lambda: ubuntu_default_profiles() if os.name != "nt" else []
    )

    @property
    def archive_root(self) -> Path:
        return Path(self.local_root).expanduser().resolve()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.config_version != CONFIG_VERSION:
            errors.append("配置版本不受支持")
        if not self.local_root.strip():
            errors.append("本地归档目录不能为空")
        if not self.rclone_binary.strip():
            errors.append("rclone 路径不能为空")
        if not 1 <= int(self.poll_minutes) <= 1440:
            errors.append("检查间隔必须为 1 至 1440 分钟")
        if not 1 <= int(self.history_days) <= 3660:
            errors.append("历史扫描天数必须为 1 至 3660")
        if not 1 <= int(self.download_workers) <= 8:
            errors.append("下载并发必须为 1 至 8")
        if int(self.minimum_free_bytes) < 1024 * 1024 * 1024:
            errors.append("磁盘保留空间不能小于 1 GiB")
        if not 1 <= int(self.web_port) <= 65535:
            errors.append("Web 端口无效")
        try:
            ipaddress.ip_address(self.web_host)
        except ValueError:
            errors.append("Web 监听地址必须是有效 IP 地址")
        profile_ids: set[str] = set()
        collector_ids: set[str] = set()
        for profile in self.profiles:
            errors.extend(f"{profile.profile_id or '未命名'}: {item}" for item in profile.validate())
            if profile.profile_id in profile_ids:
                errors.append(f"配置 ID 重复: {profile.profile_id}")
            if profile.collector_id in collector_ids:
                errors.append(f"采集服务器 ID 重复: {profile.collector_id}")
            profile_ids.add(profile.profile_id)
            collector_ids.add(profile.collector_id)
            if profile.source_type == "verified_directory" and profile.verified_source_root.strip():
                source = Path(profile.verified_source_root).expanduser().resolve()
                destination = self.archive_root / f"collector={profile.collector_id}"
                if source == destination or source in destination.parents or destination in source.parents:
                    errors.append(f"{profile.profile_id}: 来源目录不能与本地目标目录重叠")
        return errors

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("password_hash", None)
        value.pop("session_secret", None)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClientConfig":
        return cls(
            config_version=int(value.get("config_version") or CONFIG_VERSION),
            local_root=str(value.get("local_root") or default_archive_root()),
            rclone_binary=str(value.get("rclone_binary") or "rclone"),
            poll_minutes=int(value.get("poll_minutes") or 15),
            history_days=int(value.get("history_days") or 45),
            download_workers=int(value.get("download_workers") or 2),
            bandwidth_limit=str(value.get("bandwidth_limit") or "20M"),
            minimum_free_bytes=int(value.get("minimum_free_bytes") or 10 * 1024**3),
            auto_download=bool(value.get("auto_download", True)),
            web_host=str(value.get("web_host") or default_web_host()),
            web_port=int(value.get("web_port") or default_web_port()),
            password_hash=str(value.get("password_hash") or ""),
            session_secret=str(value.get("session_secret") or ""),
            profiles=[
                ProfileConfig.from_mapping(item)
                for item in value.get("profiles") or []
                if isinstance(item, Mapping)
            ],
        )


class ConfigStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or app_data_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "config.json"
        self.initial_password_path = self.root / "initial-password.txt"

    def load(self) -> ClientConfig:
        if not self.path.exists():
            return self._create_default()
        raw_payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, Mapping):
            raise RuntimeError("客户端配置格式无效")
        payload, migrated = _migrate_config_payload(raw_payload)
        config = ClientConfig.from_mapping(payload)
        errors = config.validate()
        if errors:
            raise RuntimeError("客户端配置无效: " + "；".join(errors))
        if migrated or not config.password_hash or not config.session_secret:
            config = self._ensure_security(config)
            self.save(config)
        return config

    def save(self, config: ClientConfig) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("；".join(errors))
        payload = asdict(config)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _set_private_mode(temporary)
        temporary.replace(self.path)
        _set_private_mode(self.path)

    def update_public(self, payload: Mapping[str, Any]) -> ClientConfig:
        current = self.load()
        merged = {
            **current.public_dict(),
            **dict(payload),
            "password_hash": current.password_hash,
            "session_secret": current.session_secret,
            "config_version": CONFIG_VERSION,
        }
        updated = ClientConfig.from_mapping(merged)
        self.save(updated)
        return updated

    def change_password(self, password: str) -> ClientConfig:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
        config = self.load()
        config.password_hash = generate_password_hash(password)
        self.save(config)
        if self.initial_password_path.exists():
            self.initial_password_path.unlink()
        return config

    def _ensure_security(self, config: ClientConfig) -> ClientConfig:
        if not config.session_secret:
            config.session_secret = secrets.token_urlsafe(48)
        if not config.password_hash:
            supplied = os.getenv("SMSI_ARCHIVE_CLIENT_PASSWORD", "")
            password = (
                supplied
                if len(supplied) >= MIN_PASSWORD_LENGTH
                else secrets.token_urlsafe(15)
            )
            config.password_hash = generate_password_hash(password)
            if not supplied:
                self.initial_password_path.write_text(password + "\n", encoding="utf-8")
                _set_private_mode(self.initial_password_path)
        return config

    def _create_default(self) -> ClientConfig:
        config = self._ensure_security(ClientConfig())
        self.save(config)
        return config
