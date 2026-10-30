# 风险热点

Role: baseline
Updated: 2026-10-30（只读盘点；仅记录事实，修复与否未定）

## 运行中的问题

- **mover 失败**（2026-10-30 诊断，未修）：
  - 已修（2026-10-30）：D2 5xx/超时改为下轮重试、不计失败，同一文件连续 6 轮才计失败（状态 `runtime/...pending.d2-transient`）；顺带修了一次失败因 `set -e` 让整轮提前退出、同批后续文件被推迟的问题。修前：D2 直传 `HTTP 500/504`、finalize 500、超时为间歇性上游错误。日志累计 123 次失败、3735 次成功；失败文件下一轮重试，抽查的都已成功。任一文件失败时 `runner.sh` 末尾 `exit 1`，unit 显示 failed，但不阻塞其他文件。
  - 已修（2026-10-30）：mover 目标目录（D2 与夸克）每段去首尾空白，`/qbt` 源路径不变；网页读取端加去尾空格候选（`path_sanitize.trim_relative_dirs`）。修前情况：17 个图集目录名以空格结尾（标题截断落在空格上）。mover 用原名拼远端目录，OpenList 返回 `object not found`，dry-run 判为 wait，每轮静默跳过，最早 2026-09-10。其中 9 个已以去尾空格的目录名存在于 D2，8 个（66 张）在 D2 只有空目录或没有文件；抽查其中 5 个，夸克备份只有 2 个找到同 ID 目录（内容未核对）；其余可能只有本地一份。
  - 记录中 65 条作品路径含尾空格目录；网页 Range 检查 61 条正常（读取端会去空格），4 条 404，就是未上传的那几组。
- ~~**工作区未提交**~~（2026-10-30 已分 5 个提交入库，未推送）：分支 `feat/split-download-records` 上 48 个文件改动（+3026/−230），另有约 16 个未跟踪文件（`swipe.html`、`swipe-core.js`、`api_keys.py`、`parse_cookies.py` 及其测试等）。这些功能已在线上运行，但 git 里没有记录，回滚无基线。
- ~~**CI 会红**~~（2026-10-30 已修）：见 [test-and-release-gates.md](test-and-release-gates.md)。
- ~~**播放修复同步空转**~~（2026-10-30 已停）：worker 09-19 是正常跑完（exit 3 = 有失败项）；timer 已 disable，89 条未解决见 maintenance-backlog T8。

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

- 配置漂移（2026-10-30 已收进 `scripts/systemd/`，含 drop-in）：此前 timer 频率只在 `/etc/systemd/system/*.timer.d/`，playback-repair 单元源在 `runtime/`。
- ~~`douyin-api-8900.service` 同端口冲突~~（2026-10-30 已用 drop-in 禁止启动）。
- ~~`migrate_archive_to_primary.py` 文档与默认行为不符~~（2026-10-30 已改为默认 dry-run、`--apply` 才执行）。
- ~~`relay_restore.py` 硬编码凭据路径~~（2026-10-30 已归档到 `runtime/relay-20261016/`，不入库）。
- 去 emoji 规则两套实现：`scripts/openlist-auto-move/paths.sh` `sanitize_component`（perl）与 `monitor/path_sanitize.py`，靠人工同步。多个脚本各自实现 OpenList token 读取与 API 调用。
- mover 删本地：D2 目标 2026-10-30 起比对 `content_identity`；夸克备份盘仍只比大小（OpenList 不给哈希）。
- 密钥经 `EnvironmentFile` 注入，root 可在 `/proc/<pid>/environ` 读到；仅 root 可读。
