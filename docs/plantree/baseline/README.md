# Baseline

Project-wide facts that plans should link to, not copy. Last full inventory: 2026-10-30.

## Files

- [module-map.md](module-map.md) — 包、模块与脚本分组
- [runtime-flows.md](runtime-flows.md) — 启动、监控循环、mover、鉴权、AI 标注、部署
- [storage-and-state.md](storage-and-state.md) — 持久文件、写入不变量、缓存
- [web-ui-surfaces.md](web-ui-surfaces.md) — 页面、路由、必须保留的 DOM/API 合同
- [media-playback.md](media-playback.md) — 原片、播放文件、云端读取与媒体校验
- [test-and-release-gates.md](test-and-release-gates.md) — 测试命令、当前结果、CI
- [risk-hotspots.md](risk-hotspots.md) — 当前运行问题与结构风险

## Unknown / needs inventory

- 是否有 CDN/缓存位于 `/static` 之前
- `data/douyin.db` 用途
- mover D2 直传 HTTP 500 的原因

## Verified runtime

Python 服务从 `/root/douyin_user_monitor` 工作区直接运行，由 `douyin-monitor-8900.service` 管理；详见 [runtime-flows.md](runtime-flows.md)。
