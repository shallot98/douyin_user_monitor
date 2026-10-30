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
| T8 | 播放修复 89 条未解决（[CSV](evidence/playback-repair-unresolved.csv)）：44 条源文件与审计时不一致，13 条 ffmpeg 解码失败，10 条 D2 源路径不存在，2 条格式校验失败，20 条作品已从记录删除 | 逐类给出处理结论；20 条已删除作品直接结案 | 是（是否重跑） |
| T9 | 17 条 AI 标注 `status=error` 用补打脚本 `--retry-errors` 重跑 | error 数归零或剩余项有明确原因 | 否 |

## Next — 代码结构（单独排期）

| ID | 任务 | 验收 |
|----|------|------|
| T10 | coverage 模式缩短 `_run_lock` 持锁范围（AI 标注移出锁外或改队列） | 手动 run_once / 停止在同步中途能及时响应 |
| T11 | 拆分 `api/monitor.py`：单例组装与路由分开；`on_event` 改 lifespan；导入不再写 state | 导入 `main` 不产生写盘；现有测试全过 |
| T12 | 逐处审 `except Exception`（约 40 处），去掉静默吞错 | 每处要么记录日志要么向上抛；`user_sync` 通知失败有日志 |
| T13 | 清理弃用警告：Pydantic `.dict()`、httpx `proxies`、重复 operation id `add_user_by_url` | pytest 无这些警告 |
| T14 | 为无直接测试的模块补测试：`telegram_notifier`、`schedule`、`media_urls`、`cover_image`、`sync_utils` | 每个模块至少一个直接测试 |

## Deferred

- 作品库卡片虚拟化、刷视频断点续看（见记忆 works-library / swipe-feed）
- 封面剩余 26 张（cover-delivery 已关闭；原作访问恢复后可再开）
