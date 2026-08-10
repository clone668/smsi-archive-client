from __future__ import annotations

from archive_backup.desktop import (
    DesktopConfigStore,
    bytes_text,
    cached_directory_result,
    centered_geometry,
    duration_text,
    preflight,
)
from archive_backup.config import ClientConfig, ConfigStore, ProfileConfig


def test_desktop_formatters() -> None:
    assert bytes_text(0) == "0 B"
    assert bytes_text(1536) == "1.5 KiB"
    assert bytes_text(15 * 1024**2) == "15 MiB"
    assert duration_text(8) == "8 秒"
    assert duration_text(125) == "2 分 5 秒"
    assert duration_text(7500) == "2 小时 5 分"


def test_desktop_opens_at_minimum_size_in_screen_center() -> None:
    assert centered_geometry(1920, 1080) == "1040x680+440+200"
    assert centered_geometry(1024, 640) == "1040x680+0+0"


def test_cached_directory_navigation_keeps_one_level_only() -> None:
    index = [
        {"type": "file", "path": "business/market/trades.parquet", "size_bytes": 100, "row_count": 5},
        {"type": "file", "path": "business/orders.parquet", "size_bytes": 200, "row_count": 8},
        {"type": "file", "path": "control/manifest.json", "size_bytes": 50, "row_count": 0},
    ]
    meta = {"archive_date": "2026-08-09", "download_eligible": True}

    root = cached_directory_result(index, meta, "")
    assert [(item["type"], item["name"]) for item in root["entries"]] == [
        ("directory", "business"),
        ("directory", "control"),
    ]

    business = cached_directory_result(index, meta, "business")
    assert [(item["type"], item["name"]) for item in business["entries"]] == [
        ("directory", "market"),
        ("file", "orders.parquet"),
    ]
    assert business["bytes_total"] == 200
    assert business["row_count"] == 8
    assert business["parent_path"] == ""


def test_desktop_preflight_does_not_create_a_window(tmp_path) -> None:
    result = preflight(ConfigStore(tmp_path / "state"))

    assert result["version"] == "3.2.0"
    assert result["profiles"] == 0
    assert result["tk"]
    assert (tmp_path / "state" / "config.json").exists()
    assert (tmp_path / "state" / "state.sqlite3").exists()


def test_windows_store_exposes_only_ubuntu_profiles(tmp_path) -> None:
    root = tmp_path / "state"
    ConfigStore(root).save(
        ClientConfig(
            local_root=str(tmp_path / "archives"),
            profiles=[
                ProfileConfig(
                    profile_id="drive-copy",
                    display_name="Google copy",
                    collector_id="tencent-paper",
                    source_type="google_drive",
                ),
                ProfileConfig(
                    profile_id="ubuntu-copy",
                    display_name="Ubuntu copy",
                    collector_id="tencent-report",
                    source_type="ubuntu_sftp",
                    sftp_host="192.168.2.240",
                    sftp_key_file="client.key",
                    sftp_known_hosts_file="known_hosts",
                ),
            ],
        )
    )

    config = DesktopConfigStore(root).load()

    assert [profile.profile_id for profile in config.profiles] == ["ubuntu-copy"]


def test_windows_store_drops_non_ubuntu_profiles_on_save(tmp_path) -> None:
    store = DesktopConfigStore(tmp_path / "state")
    config = ClientConfig(
        local_root=str(tmp_path / "archives"),
        profiles=[
            ProfileConfig(
                profile_id="drive-copy",
                display_name="Google copy",
                collector_id="tencent-paper",
                source_type="google_drive",
            )
        ],
    )

    store.save(config)

    assert store.load().profiles == []
