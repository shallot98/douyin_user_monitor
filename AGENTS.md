# Project agent notes

## Trellis status: soft-disabled

Trellis is **soft-disabled** in this repo (injection off; data kept).

- Pi extension disabled: `.pi/settings.json` → `extensions: []`
- Claude Trellis hooks cleared: `.claude/settings.json` → `hooks: {}`
- Historical data still on disk: `.trellis/` (tasks, spec, workspace, scripts)
- Skills/agents files under `.pi/`, `.claude/`, etc. are **not** deleted

Do **not** enforce Trellis task creation, channel workers, or workflow phases unless the user explicitly re-enables Trellis.

### Restore Trellis

```bash
# From project-local backup (preferred)
cp -a .trellis-disabled-backup/pi-settings.json .pi/settings.json
cp -a .trellis-disabled-backup/AGENTS.md AGENTS.md
cp -a .trellis-disabled-backup/claude-settings.json .claude/settings.json

# Or from /tmp copy (same machine, may be cleaned on reboot)
# cp -a /tmp/trellis-soft-off-backup/pi-settings.json .pi/settings.json
# cp -a /tmp/trellis-soft-off-backup/AGENTS.md AGENTS.md
# cp -a /tmp/trellis-soft-off-backup/claude-settings.json .claude/settings.json
```

Then open a **new** Pi/Claude session (or `/reload`).

Details: `.trellis-disabled-backup/RESTORE.md`
