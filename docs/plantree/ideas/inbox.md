# Ideas inbox

- Optional later: wire real sync activity bars from download/history events
- Optional later: extract Icons/toast to common.js
- Optional later: PWA install for phone home-screen

## 2026-10-30 全量审计候选（已全部转入 [maintenance-backlog](../plans/maintenance-backlog/roadmap.md)）

- ~~提交工作区~~（已做，2026-10-30，未推送）
- ~~恢复 CI 绿~~（已做）；~~CI 加 node 测试~~（已做，并补装 ffmpeg）
- 诊断 mover `D2 block upload: HTTP 500`
- systemd drop-in 与 playback-repair 单元收回仓库 `scripts/systemd/`
- 处理 `douyin-api-8900.service` 同端口冲突（禁用或改端口）
- 去 emoji 规则单一来源（perl 与 Python 共用测试样例）
- `migrate_archive_to_primary.py` 文档与默认 dry-run 行为对齐
- 拆 `api/monitor.py` 单例组装与路由；`on_event` → lifespan
