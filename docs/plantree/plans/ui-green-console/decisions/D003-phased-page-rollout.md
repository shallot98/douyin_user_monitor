# D003 · Phased page rollout

## Decision
Implement in order: **shared shell → dashboard → cookies → statistics → verify**.

## Why
- dashboard = daily path + medium size
- cookies = smallest consistency win
- statistics = largest regression surface

## Consequence
Partial landing is allowed (dashboard-only) if needed; shell must still be shared so later pages do not fork another theme.
