# Web UI surfaces

## Routes
| URL | File |
|-----|------|
| `/api/monitor/dashboard` | `web/dashboard.html` |
| `/api/monitor/statistics/dashboard` | `web/statistics.html` |
| `/api/monitor/cookies/dashboard` | `web/cookies.html` |
| `/static/*` | `web/static/*` |

## Architecture today
- Three large single-file HTML pages
- Each page has a large local `<style>` that redefines `:root` and layout (overrides shared CSS)
- Each page has a large inline `<script>` for page logic
- `common.js` only shares: `API_BASE`, sidebar toggle/nav, `api()`, some helpers
- Mobile today: hamburger + off-canvas 260px text sidebar (desktop-first)

## Must preserve (contract)
### Shared shell IDs
`sidebar`, `sidebarTitle`, `sidebarOverlay`, `toast-container`

### Dashboard IDs (non-exhaustive but critical)
`btnStart`, `btnStop`, `runStateDot`, `runStateText`,
`metric-users`, `metric-downloads`, `metric-downloads-hint`,
`metric-mode-name`, `metric-mode-desc`, `metric-health`,
`metric-cookie-status`, `metric-cookie-detail`,
`userSearchInput`, `userCountText`, `toggleAllUsersBtn`, `users`,
`globalRecords`, `recordsTotal`,
`settingsDrawer`, `settingsOverlay`, `monitorMode`, `intervalHours`, `coverageHours`, `modeHint`

### Cookies IDs
`cookieName`, `cookieValue`, `btnAddCookie`, `cookieCountText`, `cookieList`

### Statistics IDs
Many metric/hero/segment IDs (`metricUsers`, `heroWorks`, coverage badges, etc.) — do not rename while restyling.

### JS behavior contract
- Keep `api(path, options)` call shapes
- Keep `navigateToPage`, `toggleSidebar`, `isMobileViewport` semantics
- Keep per-page `render*` functions working against same DOM roots
- Page-local `showToast` / `Icons` / `renderUserAvatar` may stay page-local unless explicitly extracted later

## Size / risk
| File | Lines | Local style | Risk |
|------|------:|------------:|------|
| dashboard.html | ~918 | ~12k chars | Medium — core daily page |
| statistics.html | ~1410 | ~17k chars | High — densest markup/JS |
| cookies.html | ~329 | ~6k chars | Low — smallest |
| common.css | ~568 (draft green) | n/a | Partial — pages still override |
| common.js | ~133 | n/a | Low if shell IDs kept |

## Design evidence (approved direction)
- Style reference: dark green ops console (user-provided)
- Chosen mockup: **E2 Green Console Refined** (`ui-mockups/06-green-console-refined.html`)
- Key visual rules: soft metric typography, divider-based stats, list health rows, mobile bottom tabs, green monochrome
