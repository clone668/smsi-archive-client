from __future__ import annotations

from unittest.mock import patch

from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.sources import RcloneDriveSource, VerifiedDirectorySource, build_source
from archive_backup.sources import split_bandwidth_limit


def make_drive_profile() -> ProfileConfig:
    return ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="google_drive",
        drive_remote="gdrive:",
        drive_prefix="smsi/v3",
    )


@patch("archive_backup.sources.resolve_rclone_binary", return_value="rclone")
def test_drive_source_uses_configured_remote_and_prefix(_resolve) -> None:
    profile = make_drive_profile()
    source = RcloneDriveSource(ClientConfig(), profile)

    assert source._remote_path() == "gdrive:smsi/v3/collector=collector-a"
    assert source._remote_path("date=2026-08-07/manifest.json") == (
        "gdrive:smsi/v3/collector=collector-a/date=2026-08-07/manifest.json"
    )
    assert source._command(["lsf", source._remote_path()])[:3] == [
        "rclone", "lsf", "gdrive:smsi/v3/collector=collector-a"
    ]


@patch("archive_backup.sources.resolve_rclone_binary", return_value="rclone")
def test_build_source_selects_drive(_resolve) -> None:
    source = build_source(ClientConfig(), make_drive_profile())
    assert isinstance(source, RcloneDriveSource)


def test_global_bandwidth_limit_is_split_across_workers() -> None:
    assert split_bandwidth_limit("20M", 2) == "10000000B"
    assert split_bandwidth_limit("off", 4) == "off"
    assert split_bandwidth_limit("", 4) == ""


def test_verified_directory_discovery_does_not_revalidate_historical_day(
    tmp_path, monkeypatch
) -> None:
    profile = ProfileConfig(
        profile_id="collector-a",
        display_name="A",
        collector_id="collector-a",
        source_type="verified_directory",
        verified_source_root=str(tmp_path),
    )
    day = tmp_path / "date=2026-08-07"
    day.mkdir()
    (day / "manifest.json").write_text("{}", encoding="utf-8")
    (day / ".smsi-verified.json").write_text("{}", encoding="utf-8")
    source = VerifiedDirectorySource(profile)
    monkeypatch.setattr(
        source,
        "_day_is_verified",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("discovery must not revalidate a completed day")
        ),
    )

    assert source.discover_dates() == {"2026-08-07"}
