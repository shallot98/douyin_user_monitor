# Topic · Acceptance & verification

## Visual acceptance (vs E2)
- Dark green background + mint accent
- Metrics readable without nested mini-boxes
- Mobile: primary nav reachable via bottom tabs; no unusable crushed desktop sidebar-only UX
- Desktop: left rail navigation works

## Functional acceptance
### Dashboard
- Load users/status
- Start/stop monitor
- Search users / expand list
- Run-once / pause-enable / delete still callable
- Open settings drawer; fields still bind
- Download feed renders

### Cookies
- List/add/activate/check/delete still works
- Active cookie indication visible in new theme

### Statistics
- Page loads metrics/segments without console errors
- Existing toggles/filters still work (whatever currently present)

## Verification path
1. Run app as today; open three routes on desktop width ≥1200 and phone width ~390
2. Chrome devtools device mode smoke
3. Optional screenshot into `plans/ui-green-console/evidence/`
4. ID keep-list diff script (quick python set compare) before commit

## Done means
- Theme consistent across landed pages
- No known broken primary action on landed pages
- Plan evidence updated with screenshots or checklist date
