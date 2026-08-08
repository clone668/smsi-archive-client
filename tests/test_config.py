from __future__ import annotations

from archive_backup.config import ClientConfig, ProfileConfig


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
