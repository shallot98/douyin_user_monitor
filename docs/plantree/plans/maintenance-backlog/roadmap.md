# Roadmap · maintenance-backlog

Role: current-state
Updated: 2026-10-30

## Done（2026-10-30）

- 工作区分批提交、CI 恢复绿、CI 装 ffmpeg 并跑 Node 测试；PR #2 合并到 main（`ff279dc`）
- mover：尾空格目录静默卡住（`09003b9`）；D2 临时 5xx 不计失败、一次失败中止整轮（`b70e683`）
- 网页读取端加去尾空格候选；8900 已重启，65 条相关作品媒体请求全部可读
- `douyin-playback-repair-sync.timer` 停止并 disable；三个过期计划关闭
- systemd 单元与 drop-in 收进 `scripts/systemd/`（见 T3）

## Next — 运维

| ID | 任务 | 验收 | 需确认 |
|----|------|------|--------|
| T1 | ~~同端口冲突~~ **Done 2026-10-30**：unit 文件在 `/etc` 不能 mask，改加 drop-in `douyin-api-8900.service.d/disabled-port-conflict.conf`（ConditionPathExists 永不满足），`systemctl start` 实测被跳过；删 drop-in + daemon-reload 可恢复 | — | — |
| T2 | ~~清理磁盘~~ **Done 2026-10-30**：已删 3 份 `config.yaml.bak-20261028-*`、0 字节 `data/douyin.db`、无引用的 `scripts/sync-douyin-monitor-download.sh`。保留：`runtime/` 播放修复证据、pre-split 回滚备份、旧 mover 日志（用户未选）。根分区 83G 主要是 `/root` 下其他项目与 docker 镜像（可回收 9.3G 多为 save-restricted-bot 当日回滚镜像，用户决定不动） | — | — |
| T3 | ~~`scripts/systemd/` 与线上一致~~ **Done 2026-10-30**：单元与 drop-in 已收入并写 README，diff 一致 | — | — |

## Next — 数据安全

| ID | 任务 | 验收 | 需确认 |
|----|------|------|--------|
| T4 | ~~两套路径规则一致性~~ **Done 2026-10-30**：去 emoji 已有 shell/Python 对拍测试（样本 + 真实语料 120 条）；本次补尾空格样本与 `trim_relative_dir` 对拍。对拍发现 Python 侧对整串 `strip()` 会吞掉首段前导空白，已修（真实记录无此类路径，线上未受影响） | — | — |
| T5 | ~~删本地前哈希校验~~ **Done 2026-10-30**：OpenList 对 D2/夸克都不返回哈希；D2 自身 `/v6/userfile/get` 有 `content_identity`（4MiB 分块：单块 raw CID，多块一层 UnixFS dag-pb，已与 D2 上 1/2/13 块文件实测一致）。新增 `scripts/cloud_playback/d2_verify.py`，mover 在"同名同大小即删"前对 D2 目标比对，不同→skipped 保留本地，校验失败→下轮再试。夸克仍只比大小。开关 `D2_VERIFY_BEFORE_DELETE` | — | — |
| T6 | ~~迁移脚本默认 dry-run~~ **Done 2026-10-30**：不带参数只打印计划，`--apply` 才执行（与 `--dry-run` 互斥，旧命令兼容）；文档如实写明 `--rename-source` 会改名夸克目录 | — | — |
| T7 | ~~`relay_restore.py`~~ **Done 2026-10-30**：一次性任务已跑完，按用户选择移到 `runtime/relay-20261016/relay_restore.py`（与 chunk 清单、状态同目录，gitignored），不入库；`/root/boh2.pass` 未动 | — | — |

## Next — 遗留数据

| ID | 任务 | 验收 | 需确认 |
|----|------|------|--------|
| T8 | ~~播放修复 89 条未解决~~ **Closed 2026-10-30，用户决定不重跑**：20 条作品已从记录删除；其余 69 条逐条请求网站媒体全部返回 206（原片可读，只缺 H.264 兼容版，不支持 HEVC 的浏览器可能播不了）。清单保留在 [CSV](evidence/playback-repair-unresolved.csv) | — | — |
| T9 | ~~17 条 AI 标注错误~~ **Done 2026-10-31**：`--retry-errors` 重跑 17 条，15 条 ok。剩 2 条是威化饼的坏归档（`7661915140649210728` 只有音频、`7538397219817770255` 0 字节，cover-delivery 已记录），无可修复来源。进度 `runtime/content_filter_retry_20261031.json` | — | — |

## Next — 代码结构（单独排期）

| ID | 任务 | 验收 |
|----|------|------|
| T10 | ~~标注改后台队列~~ **Done 2026-10-31**（线上实测：新作品 `7702408640128635570` 记录先落盘、6.6s 后队列补上结论）：新作品标注改为进程内后台队列 `monitor/content_filter_queue.py`。下载记录先提交、通知照发；队列在 run_lock 外逐条打标，`update_state` 锁内按 aweme_id 补丁，不覆盖已有当前版本结论；本地文件被 mover 搬走后从网盘读；启动时补回近 3 天缺结论的作品。实测前此项每天占锁约 42 分钟 | 手动 run_once / 停止在同步中途能及时响应 |
| T11 | ~~导入不写盘 + lifespan~~ **Done 2026-10-31**：`MonitorService` 构造只读状态，`is_running` 归零与旧字段清理挪到 `auto_resume`（锁内 `update_state`）；`main.py` 改 lifespan。测试锁住"子进程 import main 后状态文件 mtime 不变"。按用户选择**不拆** `api/monitor.py` 单例组装 | — |
| T12 | ~~审 `except Exception`~~ **Done 2026-10-31**：实为 52 处，分类 静默 1 / 已记录 17 / 重抛或转换 34。修了唯一的静默项 `user_sync._notify_if_status_changed`（封号/注销通知失败现记 warning，附测试）。另补两处服务端不留痕的：监控主循环异常（`loop_error` 会被下一轮覆盖）与 Cookie 接口非预期异常，均加 `exc_info` 日志 | — |
| T13 | ~~弃用警告~~ **Done 2026-10-31**：pytest 警告 17 → 1。`on_event`→lifespan（T11）；crawlers `.dict()`→`model_dump()`；httpx `proxies=`→`mounts`（新增 `crawlers.utils.utils.proxy_mounts`，线上代理均为 null，行为不变）；`add_by_url` GET/POST 拆成两条路由各带 operation id。剩 1 条是 starlette 自身 `import multipart`，第三方，不处理。重启后真实解析请求验证通过 | — |
| T14 | ~~补测试~~ **Done 2026-10-31**：`tests/test_small_modules.py` 18 个测试覆盖 `schedule`、`media_urls`、`cover_image`、`sync_utils`、`timeutils`、`telegram_notifier`（含 API 失败与非 JSON 回复） | — |

## Deferred

- 作品库卡片虚拟化、刷视频断点续看（见记忆 works-library / swipe-feed）
- 封面剩余 26 张（cover-delivery 已关闭；原作访问恢复后可再开）
