# Plan · UI Green Console

## Status
**Planning / ready-check** — design direction chosen; production pages not fully restyled yet.

## Intent
把正式三页控制台（dashboard / statistics / cookies）落到 **E2 深绿控制台精修** 风格，优先解决手机端难用与“数字+方框突兀”问题，同时 **零破坏现有 API/DOM 合同**。

## Non-goals
- 不改后端业务逻辑 / 新 API（除非验收发现缺字段）
- 不重写爬虫、下载、cookie rotation
- 不把 `ui-mockups/` 直接当生产入口
- 不做多主题切换（先单深绿暗色）
- 不在本计划内大拆 SPA/框架化

## Scope (in)
1. Shared shell theme + layout primitives in `common.css` (+ minimal `common.js` if bottom-tab needs it)
2. Restyle `dashboard.html` first (daily path)
3. Restyle `cookies.html` (small, same shell)
4. Restyle `statistics.html` (largest; same shell + recolor dense widgets)
5. Mobile: bottom tab bar + usable touch targets; desktop: left icon rail
6. Soft metrics: typography + dividers, avoid nested number boxes

## Affected surfaces
- `douyin_user_monitor/web/static/common.css` (already draft-green; needs reconciliation with pages)
- `douyin_user_monitor/web/static/common.js` (optional small helpers)
- `douyin_user_monitor/web/dashboard.html`
- `douyin_user_monitor/web/cookies.html`
- `douyin_user_monitor/web/statistics.html`
- Docs only: this plan tree; mockups remain evidence

## File map
- [roadmap.md](roadmap.md) — phases Done/In Progress/Next/Deferred
- [open-questions.md](open-questions.md)
- [topics/implementation-strategy.md](topics/implementation-strategy.md)
- [topics/acceptance-and-verification.md](topics/acceptance-and-verification.md)
- [topics/risks-and-rollback.md](topics/risks-and-rollback.md)
- [decisions/D001-design-direction-e2.md](decisions/D001-design-direction-e2.md)
- [decisions/D002-preserve-dom-api-contract.md](decisions/D002-preserve-dom-api-contract.md)
- [decisions/D003-phased-page-rollout.md](decisions/D003-phased-page-rollout.md)
- [evidence/README.md](evidence/README.md)

## Reading path
1. This README
2. `roadmap.md`
3. `topics/implementation-strategy.md`
4. Decisions D001–D003
5. Acceptance topic before coding

## Current snapshot
- **Last Landed:** `ui-mockups/` on branch + draft green `common.css` (pages still self-theme)
- **Next Target:** freeze open questions → implement dashboard shell restyle only
- **Blocked By:** none hard; soft block = confirm MVP vs full three-page same sprint
