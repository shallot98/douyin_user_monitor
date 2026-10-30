# 维护与修复待办

Role: entrypoint
Mode: shape-plan
Status: Planning — 2026-10-30 建立
Baseline: [risk-hotspots](../../baseline/risk-hotspots.md) · [runtime-flows](../../baseline/runtime-flows.md) · [test-and-release-gates](../../baseline/test-and-release-gates.md)

## 目的

收拢 2026-10-30 全量盘点后剩下的修复项，以及已关闭计划的遗留项。每项单独验收、单独提交；涉及删除数据或改线上行为的项先确认再做。

## 文件

- [roadmap.md](roadmap.md)：任务清单与状态
- [evidence/playback-repair-unresolved.csv](evidence/playback-repair-unresolved.csv)：播放修复队列 89 条未解决作品（`status`、用户、作品 ID、错误）

## 约束

- 8900 直接跑工作区代码；改 Python 后需重启服务才生效，mover shell 改动下一轮 timer 生效。
- 本地 `download/` 中的文件可能是唯一副本，任何删除逻辑改动都要先 dry-run。
- Bohrium 容器只重启进程，不重启容器。
- 验收按 [test-and-release-gates](../../baseline/test-and-release-gates.md)：ruff、pytest、node 测试；CI 由 PR 触发。
