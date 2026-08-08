# SMSI 归档备份客户端

跨平台、只读的 SMSI 长期归档下载与验证客户端。Ubuntu 负责全天从 Google Drive 拉取并生成已验证本地副本；Windows 可在工作时间从 Ubuntu 的只读共享目录复制这些已验证归档。

## 数据路径

```text
采集服务器 -> Google Drive -> Ubuntu 已验证副本 -> Windows 已验证副本
```

客户端不会调用 Google Drive 删除、移动或同步删除命令。来源文件缺失时，本地已验证副本保持不变。

## 验证门禁

- 新归档必须处于 `_smsi-archive-progress.json` 的 `verified` 终态；旧归档可使用已验证的 V3 manifest。
- 校验 manifest 协议、对象数、总行数和 Google Drive 完整读回证据。
- 每个对象校验大小与 SHA-256。
- 每个 Parquet 校验 schema 和行数；业务表额外重算内容摘要。
- 下载先写入 `.partial`，整日通过后原子发布并写入 `.smsi-verified.json`。
- 已验证日期出现新的 manifest 时停止处理，不自动覆盖。

## Windows

1. 安装 Python 3.11 或更高版本。
2. 运行 `Windows一键启动.cmd`，脚本会准备虚拟环境并在缺少时安装 rclone。
3. 用 `rclone config` 创建只读 Google Drive remote。
4. 再次运行 `Windows一键启动.cmd`，打开 `http://127.0.0.1:8788/`。
5. 初始密码位于 `%LOCALAPPDATA%\SMSIArchiveBackupClient\initial-password.txt`。

Windows 若从 Ubuntu 复制，请先把 Ubuntu 已验证归档目录以只读 SMB/NFS 方式挂载到 Windows，再将来源设为“已验证目录”。来源根目录应直接指向对应的 `collector=<id>` 目录。

## Ubuntu

```bash
sudo apt install python3 python3-venv rclone
sudo bash deploy/install_ubuntu.sh
```

默认安装目录为 `/data/smsi-archive-client`，状态目录为 `/data/smsi-archive-client-state`，归档目录为 `/data/smsi-archive`。Web 服务监听 `0.0.0.0:8788`，应在防火墙中只允许可信局域网访问。

初次配置 Google Drive：

```bash
sudo systemctl stop smsi-archive-client
sudo -u smsi-archive rclone --config /data/smsi-archive-client-state/rclone.conf config
sudo chmod 600 /data/smsi-archive-client-state/rclone.conf
sudo systemctl start smsi-archive-client
```

服务状态与日志：

```bash
systemctl status smsi-archive-client
journalctl -u smsi-archive-client -n 100 --no-pager
```

## 开发测试

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python app.py --debug
```
