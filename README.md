# SMSI 归档备份客户端

SMSI Ubuntu 长期归档下载与验证客户端。Ubuntu 负责从 Google Drive 拉取归档、完成校验并发布本地副本，同时提供 Web 运维界面。

## 数据路径

```text
采集服务器 -> Google Drive -> Ubuntu 已验证副本
```

客户端不会调用 Google Drive 删除、移动或同步删除命令。来源文件缺失时，本地已验证副本保持不变。

## 验证门禁

- 新归档必须处于 `_smsi-archive-progress.json` 的 `verified` 终态；旧归档可使用已验证的 V3 manifest。
- 校验 manifest 协议、对象数、总行数和 Google Drive 完整读回证据。
- 每个对象校验大小与 SHA-256。
- 每个 Parquet 校验 schema 和行数；业务表额外重算内容摘要。
- 下载先写入 `.partial`，整日通过后原子发布并写入 `.smsi-verified.json`。
- 已验证日期出现新的 manifest 时默认停止处理，不自动覆盖；唯一例外是带有严格维护证明的“移除旧运行报告”变更，客户端会重新验证现有业务文件、更新清单和凭据，不重新下载业务文件。

## 任务进度与取消

总览表显示远端 manifest 对象总量、本地已传输量和本地对象状态。点击“文件详情”才会读取一次远端 manifest 并对照本地正式目录、`.partial` 暂存目录，查看每个对象的待下载、传输中、已暂存或本地存在状态；页面刷新不会周期性重新列举网盘文件。

下载进度包含当前对象、活动并发、实时速度和预计剩余时间。设置中的带宽限制是整个任务的总上限，客户端会按实际并发分配给各个 rclone 工作进程。取消任务后日期状态变为“已取消”，已完成的临时对象保留在 `.partial`，正式目录不会发布，下次任务可以继续。

启动和定时任务采用增量检查：先读取远端日期清单，只处理新日期、未完成状态或本地验证凭据缺失的日期。完整验证过的历史日期不会因客户端重启而再次遍历所有对象。客户端只在最近 14 个归档日中低频抽查远端 manifest，每台服务器 7 天最多抽查 1 天；该抽查只读取清单和本地验证凭据，不读取业务对象。历史对象完整校验仅在用户点击单日“重新校验”时执行。

## Web 文件浏览与客户端更新

左侧统一使用“归档文件”入口，页面顶部在“云端 / 本地”之间切换。云端按采集服务器和日期读取 Google Drive 已发布 manifest；首次读取会把 manifest 的文件索引加载到当前页面，之后进入子目录或返回上级只在浏览器内存中切换，不重复访问网盘。本地直接浏览已验证目录和 `.partial` 暂存目录，即使网盘暂时不可用也能读取。两个列表只在进入页面、切换采集服务器或日期、手动重新读取时访问文件系统或网盘，不加入总览的周期刷新。

运行总览会在 manifest 包含 `runtime-report.json` 时读取小型健康摘要。当前分层报告分别显示归档日数据质量、运行过程记录和已恢复观察；只有数据质量会标记为归档日告警。未分层历史报告只显示为历史记录，不计入当前异常或跨服务器数据比较。旧报告经受控维护移除后显示“未生成”，业务归档、跨服务器业务数据比较和恢复验证仍保持有效。

“客户端更新”是独立页面。“检查更新”只读取 GitHub `main` 的提交信息；发现新版本后点击“开始更新”，页面显示下载阶段、字节进度、速度和预计剩余时间。更新包会先保存到状态目录并校验，不会替换正在运行的代码，也不会自动重启服务。“重启客户端”在归档任务运行时也可使用：客户端会先请求当前任务安全停止，将任务登记为待恢复并保留 `.partial` 中已完成的对象，再切换版本或重启；启动后任务会从已完成对象继续。若更新助手拒绝或无法执行重启，客户端会恢复后台归档服务。Ubuntu 更新助手仍会执行最终忙碌检查，只接受固定版本号和固定运行目录，不能执行任意命令。首次安装或更新助手变更时，仍需重新运行 `deploy/install_ubuntu.sh`。

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
