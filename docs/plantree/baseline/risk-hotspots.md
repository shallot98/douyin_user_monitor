# 风险热点

Role: baseline
Updated: 2026-10-30（只读盘点；仅记录事实，修复与否未定）

## 运行中的问题

- **mover 失败**：2026-10-30 18:50 一轮 exit 1，日志累计 107 次 `D2 block upload: HTTP 500`（D2 直传）。本地 `download/` 仅 141M，未见积压。原因（D2 侧故障还是请求问题）未诊断。
- ~~**工作区未提交**~~（2026-10-30 已分 5 个提交入库，未推送）：分支 `feat/split-download-records` 上 48 个文件改动（+3026/−230），另有约 16 个未跟踪文件（`swipe.html`、`swipe-core.js`、`api_keys.py`、`parse_cookies.py` 及其测试等）。这些功能已在线上运行，但 git 里没有记录，回滚无基线。
- ~~**CI 会红**~~（2026-10-30 已修）：见 [test-and-release-gates.md](test-and-release-gates.md)。
- **播放修复同步空转**：boh worker 2026-09-19 退出（exit 3），`douyin-playback-repair-sync.timer` 每小时空跑并刷新 OpenList 目录。对应计划状态已过期。

## 结构

- 大文件：`api/monitor.py` 1367（路由 + 单例组装）、`content_filter.py` 1337、`service.py` 1185、`web/library.html` 1934、`web/statistics.html` 1448。
- `api/monitor.py` 顶层单例 + `global` 缓存，测试只能靠环境变量隔离；跨模块访问私有成员（`monitor_service._storage.records`、`OPENLIST_CLIENT._self_proxy_prefix()`）。
- 导入即写 state（见 [runtime-flows.md](runtime-flows.md)）。

## 事件循环与锁

- `update_state` / `save_state` 与 RecordStore SQLite 调用在事件循环上同步执行。拆分 records 后 JSON 约 1.8MB，2026-09-29 实测探测最大延迟 126ms，属已缓解。
- `_refresh_library_index` 用 `to_thread` 在线程里读写全局缓存与 storage 缓存，线程安全未验证。
- coverage 模式在 `_run_lock` 内完成网络同步、下载、AI 标注；AI 串行（并发 1），会拉长单用户时间并阻塞手动 run_once / 启停。

## 错误处理

`except Exception`：`api/monitor.py` 23 处、`service.py` 6、`inprocess_client.py` 6、`user_sync.py` 4。`user_sync.py` 约 142 行通知失败静默吞掉；`content_filter` 吞全部异常（结论写 error 状态，算显式记录）。

## 运维

- 配置漂移：timer 频率只在 `/etc/systemd/system/*.timer.d/`；playback-repair 单元源在 `runtime/`。
- `douyin-api-8900.service` 同端口冲突（inactive）。
- `migrate_archive_to_primary.py` 文档称备份盘只读，实际会改名夸克目录，且默认不是 dry-run。
- `relay_restore.py` 硬编码 boh2 主机和 `/root/boh2.pass`，远端 `rm -rf /tmp/relay_restore_stage`。
- 去 emoji 规则两套实现：`scripts/openlist-auto-move/paths.sh` `sanitize_component`（perl）与 `monitor/path_sanitize.py`，靠人工同步。多个脚本各自实现 OpenList token 读取与 API 调用。
- mover 删本地只比大小、不比哈希（D2 直传路径与备份暂存另有校验，未逐一审）。
- 密钥经 `EnvironmentFile` 注入，root 可在 `/proc/<pid>/environ` 读到；仅 root 可读。
