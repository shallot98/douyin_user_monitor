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

Status 列在 2026-10-30 审计时复核；三个计划的 handoff 都早于 10 月，标注为 stale 的需要用户确认后再更新或归档。

| Plan | Status | Current Phase | Last Landed | Next Target |
|------|--------|---------------|-------------|-------------|
| [ui-green-console](plans/ui-green-console/README.md) | Stale（最后状态 2026-08-06） | 浅色主题 retint 已上线 | `common.css` 浅色 token；之后新增 6 个页面未纳入本计划 | 用户决定：关闭或重定范围 |
| [browser-playback-repair](plans/browser-playback-repair/README.md) | Stalled（handoff 2026-09-14） | boh worker 2026-09-19 exit 3 后未恢复 | 883 映射；89/12,410 条已发布（09-14 快照） | 用户决定：重启队列或收口；同步 timer 现为空转 |
| [cover-delivery](plans/cover-delivery/README.md) | In Progress / source access blocked | 26 张封面无可用来源 | 2026-09-14：22,285 张验证 | 原作访问恢复后再补 |

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
