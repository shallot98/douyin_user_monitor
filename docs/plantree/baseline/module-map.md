# Module map (web-relevant)

| Path | Role |
|------|------|
| `douyin_user_monitor/web/dashboard.html` | Monitor home: status, users, downloads feed, schedule drawer |
| `douyin_user_monitor/web/statistics.html` | User statistics / coverage analytics |
| `douyin_user_monitor/web/cookies.html` | Cookie pool CRUD + activate/check |
| `douyin_user_monitor/web/static/common.css` | Shared CSS variables + shell primitives |
| `douyin_user_monitor/web/static/common.js` | Shared API helper + mobile sidebar nav helpers |
| `douyin_user_monitor/api/monitor.py` | Serves the three HTML dashboards as `HTMLResponse` |
| `douyin_user_monitor/main.py` | Mounts `/static`, redirects `/` → dashboard |
| `ui-mockups/` | Static design candidates + screenshots (not served by app) |

Backend monitor/crawler packages are out of scope for pure UI restyle unless a new widget needs new API fields.
