# Baseline

Project-wide facts that plans should link to, not copy.

## Files

- `module-map.md` — packages and ownership
- `web-ui-surfaces.md` — production web pages, shared assets, routes, must-keep DOM/API contracts
- [media-playback.md](media-playback.md) — original archives, playback files, cloud reads, and media validation

## Unknown / needs inventory

- Whether any CDN/cache sits in front of `/static`

## Verified runtime

The Python service runs from `/root/douyin_user_monitor` under
`douyin-monitor-8900.service`. Independent media processing and metadata
publication are documented in [media-playback.md](media-playback.md).
