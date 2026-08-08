from __future__ import annotations

from unittest.mock import patch

from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.sources import RcloneSftpSource, build_source
from archive_backup.sources import split_bandwidth_limit


def make_sftp_profile() -> ProfileConfig:
    return ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="ubuntu_sftp",
        sftp_host="192.168.2.240",
        sftp_port=22,
        sftp_user="smsi-archive-reader",
        sftp_key_file=r"C:\keys\smsi_ed25519",
        sftp_known_hosts_file=r"C:\keys\known_hosts",
        sftp_root="/archive",
    )


@patch("archive_backup.sources.resolve_rclone_binary", return_value="rclone")
def test_sftp_source_uses_fixed_host_key_and_read_only_root(_resolve) -> None:
    profile = make_sftp_profile()
    source = RcloneSftpSource(ClientConfig(), profile)

    assert source._remote_path() == ":sftp:/archive/collector=collector-a"
    assert source._remote_path("date=2026-08-07/manifest.json") == (
        ":sftp:/archive/collector=collector-a/date=2026-08-07/manifest.json"
    )
    command = source._command(["lsf", source._remote_path()])
    assert command[:3] == ["rclone", "lsf", ":sftp:/archive/collector=collector-a"]
    assert command[command.index("--sftp-host") + 1] == "192.168.2.240"
    assert "--sftp-known-hosts-file" in command
    assert "--sftp-disable-hashcheck" in command
    assert "--sftp-ask-password" not in command


@patch("archive_backup.sources.resolve_rclone_binary", return_value="rclone")
def test_build_source_selects_sftp(_resolve) -> None:
    source = build_source(ClientConfig(), make_sftp_profile())
    assert isinstance(source, RcloneSftpSource)


def test_global_bandwidth_limit_is_split_across_workers() -> None:
    assert split_bandwidth_limit("20M", 2) == "10000000B"
    assert split_bandwidth_limit("off", 4) == "off"
    assert split_bandwidth_limit("", 4) == ""
