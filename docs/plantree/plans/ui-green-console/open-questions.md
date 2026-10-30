# Open questions · ui-green-console

## Q1. Same-sprint scope
Do we land **all three pages** in one change set, or **dashboard-first MVP** then cookies/statistics?

- Default proposal: dashboard + cookies in first landing; statistics immediately after if capacity allows.
- Status: **unresolved — needs user confirm if timeboxed**

## Q2. Bottom tab IA
Mobile bottom tabs map to which four items?

- Proposal: `监控(dashboard)` / `统计` / `Cookie` / `更多(打开侧栏或设置)`
- Alternative: drop Cookie from tab, keep refresh/settings in header only
- Status: **proposal ready; confirm if disagree**

## Q3. Visual extras on production dashboard
Include E2 “同步活跃柱” and “趋势图” as **decorative/CSS-only**, or **data-backed**, or **omit in v1**?

- Proposal: **omit charts in v1 production** unless existing data already powers them; keep soft metric cards + lists first.
- Status: **proposal ready**

## Q4. Breakpoint
Unify mobile drawer breakpoint to **1024** for all pages (cookies currently 900)?

- Proposal: unify 1024
- Status: **proposal ready**
