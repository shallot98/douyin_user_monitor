# 运行流程

Role: baseline
Updated: 2026-10-30

媒体归档与播放细节见 [media-playback.md](media-playback.md)，本文件只记主干。

## 启动

1. `api/monitor.py` 被导入时创建单例：`load_settings`、crawler、`MonitorStorage`、`MonitorService`、`OPENLIST_CLIENT` 等。
2. `MonitorService.__init__` 同步写一次状态文件（`service.py` 约 168-184）。**任何 `import douyin_user_monitor.main` 都会写 state**；脚本和临时实例必须用 `DYMON_STATE_PATH` 指向副本。
3. startup 事件 `auto_resume()`：`desired_running` 为真时 `create_task(_monitor_loop)`。shutdown 事件关闭 service 与 OpenList client。使用已弃用的 `on_event`，没有 lifespan。

## 监控循环（`service.py` `_monitor_loop`）

- 每轮先处理优先用户。
- interval 模式：`_run_lock` 内对全部启用用户 `sync_users`，然后睡眠。
- coverage 模式：逐用户执行三段：锁内按 id 取磁盘快照 → `sync_one_user` 改副本 → `update_state` 锁内按 `USER_SYNC_MERGE_FIELDS` 字段级合并。注释中的"锁外"指状态文件锁；整个网络同步、下载和 AI 标注都在 `_run_lock` 内 await。

单用户（`user_sync._sync_user_latest`）：拉第一页 → 用 `downloaded_aweme_ids` 判新 → 逐个作品：详情 → "发现"通知 → 下载到 `download/` → 发布封面 → AI 标注 → "下载完成"通知。单作品失败记 errors，下轮重试（`continue_on_error=True`）。

登录态 Cookie 只用于作品列表、测活与轮换；详情、原画跳转和 CDN 下载使用 `data/parse_cookies/` 的过期 Cookie（与解析 API 共用）。

## 归档搬运（Python 之外）

`douyin-user-monitor-mover.timer` 每 10 分钟（`/etc` drop-in；仓库 `scripts/systemd/` 仍写每分钟）运行 `scripts/openlist-auto-move-mp4.sh run --config scripts/openlist-auto-move-douyin-monitor.conf`：

1. 文件超过 `MIN_AGE_SECONDS=120` 才处理。
2. 硬链接进 `download/.backup-staging/` → OpenList copy 到备份盘 `/kuake/抖音视频`。
3. 并行直传主盘 `/d2/vps/抖音视频`（`D2_DIRECT_UPLOAD=true`），成功后删本地源。
4. 远端已有同名文件时，只有大小完全一致才删本地（`SIZE_MISMATCH_TOLERANCE_BYTES=0`，`common.sh:42`）。只比大小，不比哈希。

日志 `logs/openlist-auto-move.log`。

## 网页请求

- 鉴权顺序（`auth.py` 约 209-241）：未配置则开放 → 系统 Key `DYMON_API_KEY` → HMAC 会话 Cookie → 网页 Key（`parse` 档仅 `PARSE_ROUTES`）→ 游客 Cookie（仅 `GUEST_ROUTES`，接口内再按作品库范围校验）。登录失败按 IP 限流，计数在内存。
- 归档播放：`/users/{id}/archive` 读本地记录 → `playback-urls` 并发 8 路换 CDN 直链 → `media/{i}` 依次走 307 直链 / 本机小文件缓存 / OpenList 代理流。

## AI 内容标注

- 新作品下载后同步 await `ContentFilter.__call__`（`user_sync.py` 约 271），默认并发 1，异常全部吞掉，结论写 `record["content_filter"]`。
- OpenAI 兼容 `{api_base}/chat/completions`，图片 base64。2026-10-28 起 `config.yaml` 为本机 `127.0.0.1:18317/v1` + `gemini-3.8-flash-high`（之前为 grok2api `:18082`，代码默认值仍是 grok）。`mode` 只支持 `shadow`。
- 2026-10-30 实测：`monitor_records.db` 30,720 条记录全部为 `version=5`，`ok` 30,703、`error` 17。
- 历史补打：`scripts/content_filter_backfill.py`，改 prompt/结构时升 `RESULT_VERSION` 会自动重做旧版本。

## 部署

- `douyin-monitor-8900.service`：`.venv/bin/uvicorn douyin_user_monitor.main:app`，`127.0.0.1:8900`，`Restart=always`，`EnvironmentFile=/etc/douyin-monitor/auth.env`；drop-in 限制 MemoryHigh 768M / MemoryMax 1G。nginx 反代 `dy.cenai.de`。
- 重启：`restart_8900.sh`。**服务直接跑工作区代码**，切分支或改文件后下次重启即生效。
- `douyin-playback-repair-sync.timer`：每小时（drop-in），单元文件源在 `runtime/web-playback-repair-20260913/`，不在 `scripts/systemd/`。
- 同端口冲突：`douyin-api-8900.service`（另一项目 `/root/Douyin_TikTok_Download_API`，`0.0.0.0:8900`）当前 inactive，启动会抢端口。
