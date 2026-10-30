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
| T1 | 处理 `douyin-api-8900.service` 同端口冲突 | 该 unit disabled 或改端口；8900 只剩监控服务 | 是（属于另一项目） |
| T2 | 清理磁盘：`runtime/` 旧补打 JSON、根目录 `config.yaml.bak-*`、`data/douyin.db`、`sync-douyin-monitor-download.sh` | 先列清单与大小，归档后删除；根分区用量下降 | 是（删除） |
| T3 | ~~`scripts/systemd/` 与线上一致~~ **Done 2026-10-30**：单元与 drop-in 已收入并写 README，diff 一致 | — | — |

## Next — 数据安全

| ID | 任务 | 验收 | 需确认 |
|----|------|------|--------|
| T4 | 去 emoji / 去空白规则单一来源：shell `paths.sh` 与 Python `path_sanitize.py` 共用测试样例 | 同一组真实目录名样例，两边输出一致的测试 | 否 |
| T5 | mover 删本地前加哈希校验（远端能返回哈希时） | 只比大小的删除路径改为大小 + 哈希；测试覆盖不一致时保留本地 | 否 |
| T6 | `migrate_archive_to_primary.py` 默认 dry-run，文档改为如实说明会改名夸克目录 | 不带参数运行不写云端 | 否 |
| T7 | `relay_restore.py`：删除，或主机与密码文件改为参数后入库 | 不再有未跟踪的硬编码凭据路径脚本 | 是 |

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
