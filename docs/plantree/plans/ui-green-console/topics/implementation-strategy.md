# Topic · Implementation strategy

## Problem
Production pages each own a full light-theme `:root` + layout CSS. Draft green `common.css` alone **cannot win** until page-local theme/layout is stripped or aligned.

## Approach (safe)
### 1. Shared shell layer
In `common.css` own:
- color tokens
- body background
- sidebar rail / mobile drawer
- top header, buttons, inputs, cards, soft stats
- bottom tabbar
- overlay/drawer/toast base

### 2. Page local CSS shrink
Each HTML page keeps **only page-unique rules** (user-row specifics, statistics chart widgets, cookie list rows). Remove duplicate `:root` light theme and duplicate sidebar/header rules where possible.

### 3. Markup pattern (all pages)
```
body
  aside#sidebar.rail
  div.app-wrapper
    header.top-header
    main.main-content
  nav.bottom-tabbar (mobile)
  overlays/drawers/toasts
```
Desktop: icon rail (labels optional/hidden). Mobile: bottom tabs + hamburger drawer for secondary actions.

### 4. Dashboard content mapping (E2 → production)
| E2 block | Production source |
|----------|-------------------|
| status chip | `runStateDot` / `runStateText` + start/stop |
| soft metrics | existing metric-* ids, restyled cards |
| add/monitor users | existing users list + search + actions (may live in card; add-user flow remains API-driven as today) |
| activity | `globalRecords` feed restyle |
| settings | existing settings drawer fields |
| charts/heatmap | **omit v1** unless cheap |

### 5. JS policy
- Do not rewrite business logic in first pass
- Only adjust HTML string templates inside `renderUsers` / cookie list render if class names need soft metric layout
- Optional: tiny `common.js` helper for bottom-tab active state

### 6. Working sequence per page
1. Snapshot current ID list
2. Replace shell markup + link shared classes
3. Delete conflicting local theme CSS
4. Fix page-unique CSS colors to tokens
5. Manual smoke checklist
6. Record evidence

## Explicit non-approach
- No “big bang rewrite all inline JS”
- No introducing React/Vue for this plan
- No serving mockup HTML from FastAPI
