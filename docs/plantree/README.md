# Plantree · douyin_user_monitor

## Authority order

1. This registry (`docs/plantree/README.md`)
2. `baseline/` for project-wide web/runtime facts
3. Active plan roots under `plans/`
4. Mockups under `ui-mockups/` (design evidence only, not production)

## Baseline

Last full inventory 2026-10-30: [baseline/README.md](baseline/README.md)

- [module-map.md](baseline/module-map.md) · [runtime-flows.md](baseline/runtime-flows.md) · [storage-and-state.md](baseline/storage-and-state.md)
- [web-ui-surfaces.md](baseline/web-ui-surfaces.md) · [media-playback.md](baseline/media-playback.md)
- [test-and-release-gates.md](baseline/test-and-release-gates.md) · [risk-hotspots.md](baseline/risk-hotspots.md)

## Active plans

| Plan | Status | Current Phase | Last Landed | Next Target |
|------|--------|---------------|-------------|-------------|
| [maintenance-backlog](plans/maintenance-backlog/README.md) | Done — 2026-10-31 | T1–T14 全部完成（T8 结案不重跑） | 2026-10-31：PR #3 CI 通过，8900 已重启验证 | 合并 PR #3；之后按需另开计划 |

## Closed plans（2026-10-30 关闭，原文保留）

| Plan | 结局 |
|------|------|
| [browser-playback-repair](plans/browser-playback-repair/README.md) | 队列 09-19 跑完，网站发布 12,321 条；89 条未解决转入 maintenance-backlog T8；同步 timer 已 disable |
| [cover-delivery](plans/cover-delivery/README.md) | 22,285 张已验证；26 张因原作验证码阻挡未补，用户决定关闭 |
| [ui-green-console](plans/ui-green-console/README.md) | 浅色 retint 已上线；之后新页面不在范围内，用户决定关闭 |

已知断链（2026-10-30 审计）：browser-playback-repair 与 cover-delivery 的证据文件链接到 `runtime/web-playback-inventory-20260913/`、`runtime/cover-loading-20260914/`、`runtime/cover-backfill-20260914/`，这些目录已不存在；部分笔记留在 `runtime/_archived-notes-20260915/`。证据原文未改，JSON 证据视为丢失。

## 未建计划的近期上线功能

2026-09-29 至 10-28 上线、只记在会话记忆中、无 plan root：RecordStore 拆分、网页登录/游客/网页 Key、AI 内容标注 v5（全量 30,720 条已打）、作品库、刷视频、低价值用户筛选、解析 API 与解析 Cookie 分离。事实已并入 baseline；如需追踪后续工作再建计划。

## Ideas

- [ideas/inbox.md](ideas/inbox.md)

## How to read

1. Read this registry
2. Read baseline web surfaces
3. Open the active plan README + roadmap
4. Only then open topic/decision shards needed for the current phase
