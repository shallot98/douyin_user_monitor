# Roadmap · ui-green-console

## Done
- [x] Collect mobile pain from live screenshot (desktop sidebar crushed on phone)
- [x] Produce candidate mockups A–D
- [x] User style reference (dark green console)
- [x] Produce E / E2 refined mockups; fix broken uploads
- [x] Push mockups to GitHub branch `refactor/concurrency-and-hardening`
- [x] Draft shared green `common.css` (not yet consumed cleanly by pages)

## In Progress
- [ ] Re-plan implementation (this document set) and ready-check
- [ ] Reconcile page-local `:root`/layout CSS with shared theme (remove override wars)

## Next
1. **Phase A — Shell contract**  
   Finalize shared shell markup pattern (rail + header + bottom tabs + drawer/toast) and document required IDs.
2. **Phase B — Dashboard production restyle**  
   Restyle `dashboard.html` only: keep all IDs/handlers; soft metrics; mobile bottom tabs; green theme.
3. **Phase C — Cookies restyle**  
   Same shell; keep cookie form/list behavior.
4. **Phase D — Statistics restyle**  
   Same shell; recolor/spacing for dense cards/charts; no metric renames.
5. **Phase E — Verify**  
   Desktop + mobile viewport smoke: nav, start/stop, add/search user, cookie add/list, statistics load.
6. **Phase F — Land**  
   Commit on feature branch; optional PR; screenshot evidence into `evidence/`.

## Deferred
- Extract duplicated `showToast`/`Icons` into `common.js`
- Real activity heatmap wired to backend events (mock/visual-only first)
- Light theme / theme toggle
- Component framework migration
- i18n

## Suggested MVP cut (if timeboxed)
**MVP = Phase A + B + E(dashboard-only)**  
Cookies/statistics can follow in a second landing if needed. Default recommendation: still do C before D because C is cheap consistency.
