# SMSI 归档备份客户端

跨平台、只读的 SMSI 长期归档下载与验证客户端。Ubuntu 负责全天从 Google Drive 拉取并生成已验证本地副本；Windows 可在工作时间通过局域网只读 SFTP 下载这些已验证归档。

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

## 任务进度与取消

总览表显示远端 manifest 对象总量、本地已传输量和本地对象状态。点击“文件详情”才会读取一次远端 manifest 并对照本地正式目录、`.partial` 暂存目录，查看每个对象的待下载、传输中、已暂存或本地存在状态；页面刷新不会周期性重新列举网盘文件。

下载进度包含当前对象、活动并发、实时速度和预计剩余时间。设置中的带宽限制是整个任务的总上限，客户端会按实际并发分配给各个 rclone 工作进程。取消任务后日期状态变为“已取消”，已完成的临时对象保留在 `.partial`，正式目录不会发布，下次任务可以继续。

## Web 文件浏览与客户端更新

左侧“网盘文件”按采集服务器和日期读取 Google Drive 已发布 manifest；首次读取会把 manifest 的文件索引加载到当前页面，之后进入子目录或返回上级只在浏览器内存中切换，不重复访问网盘。“本地文件”直接浏览已验证目录和 `.partial` 暂存目录，即使网盘暂时不可用也能读取。两个列表只在进入页面、切换采集服务器或日期、手动重新读取时访问文件系统或网盘，不加入总览的周期刷新。

“客户端更新”是独立页面。“检查更新”只读取 GitHub `main` 的提交信息；发现新版本后点击“开始更新”，页面显示下载阶段、字节进度、速度和预计剩余时间。更新包会先保存到状态目录并校验，不会替换正在运行的代码，也不会自动重启服务。客户端空闲时“重启客户端”始终可用：有已校验更新时先切换版本，没有更新时只重启当前版本。Ubuntu 更新助手会再次检查没有下载或校验任务，运行中的归档任务不会被中断。更新助手只接受固定版本号和固定运行目录，不能执行任意命令。首次安装或更新助手变更时，仍需重新运行 `deploy/install_ubuntu.sh`。

## Windows

1. 安装 Python 3.10 或更高版本。
2. 运行 `Windows一键启动.cmd`（`启动客户端.cmd` 也是同一入口），脚本会准备虚拟环境并打开 Windows 原生桌面客户端；不启动 Web 服务、不打开浏览器，也不连接 Google Drive。
3. 在“设置”中添加 Ubuntu SFTP 只读连接，填写 Ubuntu 地址、私钥和独立的 `known_hosts` 文件。
4. 在“归档同步”点击“同步缺失归档”，客户端会复制已发布归档并执行 SHA-256、Parquet schema、行数和业务内容摘要校验。

正常启动不会保留黑色控制台；环境检查日志位于 `%LOCALAPPDATA%\SMSIArchiveBackupClient\windows-launcher.log`，桌面程序启动异常时详见同目录下的 `desktop-error.log`。

Windows 在“设置”中添加 Ubuntu 连接，填写 Ubuntu 地址、私钥和独立的 `known_hosts` 文件。客户端仍会重新执行对象 SHA-256、Parquet schema、行数和业务内容摘要校验；Ubuntu 删除源文件不会删除 Windows 已验证副本。

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

为 Windows 启用 Ubuntu 只读 SFTP：

```bash
sudo bash deploy/setup_sftp_reader.sh /path/to/windows-client.pub
```

脚本创建无密码、无命令执行权限的 `smsi-archive-reader` 用户，并将 `/data/smsi-archive` 只读映射为该用户唯一可见的 `/archive`。Windows 私钥不得上传到 Ubuntu 或提交到 Git。

## 开发测试

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python app.py --debug
```
