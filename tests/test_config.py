from __future__ import annotations

import pytest

from archive_backup.config import ClientConfig, ConfigStore, ProfileConfig


def test_verified_source_must_not_overlap_destination(tmp_path) -> None:
    local = tmp_path / "archives"
    profile = ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="verified_directory",
        verified_source_root=str(local / "collector=collector-a"),
    )
    config = ClientConfig(local_root=str(local), profiles=[profile])
    assert any("不能与本地目标目录重叠" in item for item in config.validate())


def test_web_host_must_be_an_ip_address() -> None:
    config = ClientConfig(web_host="public.example.com")
    assert "Web 监听地址必须是有效 IP 地址" in config.validate()


def test_password_minimum_is_six_characters(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    store.change_password("123456")
    with pytest.raises(ValueError, match="至少需要 6 个字符"):
        store.change_password("12345")


def test_sftp_profile_requires_key_and_known_hosts() -> None:
    profile = ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="ubuntu_sftp",
        sftp_host="192.168.2.240",
    )
    errors = profile.validate()
    assert "Ubuntu SFTP 私钥文件不能为空" in errors
    assert "Ubuntu SFTP known_hosts 文件不能为空" in errors


def test_sftp_profile_builds_collector_root() -> None:
    profile = ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="ubuntu_sftp",
        sftp_host="192.168.2.240",
        sftp_key_file="client.key",
        sftp_known_hosts_file="known_hosts",
    )
    assert profile.validate() == []
    assert profile.sftp_archive_root == "/archive/collector=collector-a"
