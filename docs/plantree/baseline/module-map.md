# 模块地图

Role: baseline
Updated: 2026-10-30（全量只读盘点，工作区 = 线上代码，含未提交改动）

规模：`douyin_user_monitor/` 70 个文件，`scripts/` 57 个，`crawlers/` 17 个（vendored），`tests/` 107 个。Python 源码 + 单文件 HTML 前端，无构建步骤。

## 入口 / 配置 / 鉴权

| 路径 | 职责 |
|------|------|
| `douyin_user_monitor/main.py` | FastAPI app；登录/游客/登出、受保护 docs；挂 `/static` 与 `/api/monitor` |
| `settings.py` / `settings_loaders.py` | 默认值、环境变量名；解析 `config.yaml` 为 `Settings` |
| `auth.py` | 会话 Cookie、`X-API-Key`、网页 Key（full/parse 档）、游客白名单 |

## API 层 `api/`

| 路径 | 职责 |
|------|------|
| `monitor.py`（1367 行） | 全部业务路由，**且在模块顶层组装全部单例**（约 132-226 行） |
| `media_proxy.py` | Range 流式透传 |
| `archive_media_cache.py` | 归档小文件（<10MB）本机缓存，总量 200MB |
| `covers.py` / `models.py` | 本地封面响应 / 响应模型 |

## 监控与同步 `monitor/`

| 分组 | 模块 |
|------|------|
| 主循环 | `service.py`（1185 行，interval/coverage 模式、优先队列）、`schedule.py`、`sync_utils.py` |
| 单用户同步 | `user_sync.py`（增量 + 历史回填 + 下载编排）、`history_sync.py`、`aweme_feed.py` |
| 下载与播放文件 | `downloader.py`、`video_source.py`、`original_play.py`、`media_urls.py`、`playback_prepare.py`、`playback_media.py` |
| 用户资料 | `profile_parser.py`、`user_lookup.py`、`nickname_history.py` |
| Cookie | `cookie_pool*.py`、`cookie_rotation.py`、`cookie_liveness*.py`、`cookie_download_liveness.py`、`parse_cookies.py` |
| 存储 | `storage.py`、`record_store.py`、`cover_store.py`、`api_keys.py`、`archive_path_aliases.py` |
| 归档 / 网盘 | `openlist_client.py`、`archive_feed.py`、`path_sanitize.py` |
| AI 内容标注 | `content_filter.py`（1337 行）、`content_filter_media.py` |
| 作品库 / 统计 | `library.py`、`statistics.py`、`user_table.py`、`dashboard_payload.py` |
| 对外解析 | `parse_service.py` |
| 通知 | `notifier.py`、`telegram_notifier.py`、`hermes_weixin_sender.py` |
| 其他 | `errors.py`（`sanitize_public_error`）、`timeutils.py` |

`crawler/inprocess_client.py` 进程内包装 `crawlers/`（签名 abogus/xbogus、端点、请求工具），写 Cookie 持锁。

## 前端 `web/`

页面与共享资源见 [web-ui-surfaces.md](web-ui-surfaces.md)。

## 脚本 `scripts/`

| 分组 | 运行位置 | 状态 |
|------|----------|------|
| `openlist-auto-move-mp4.sh` + `openlist-auto-move/*.sh`（15 个库） | hdz，`douyin-user-monitor-mover.timer` 每 10 分钟 | 常驻 |
| `cloud_playback/` | 媒体处理在 boh；回执同步在 hdz timer | 队列 2026-09-19 跑完；同步 timer 2026-10-30 已 disable |
| `cover_backfill/` | boh 提帧 → hdz 导入 | 历史任务，已结束 |
| `content_filter_backfill.py`（+ `resume_content_filter_backfill.sh`） | hdz，`systemd-run` 临时单元 | 按需；当前全量已打完 |
| `migrate_archive_to_primary.py` | hdz，OpenList 服务端 copy | 历史任务。默认 dry-run，`--apply` 才执行；`--rename-source` 会改名夸克目录 |
| `split_download_records.py` | hdz，需停服务 | 已完成（2026-09-29） |
| `archive_sec_dir_aliases.py`、`recalculate_borderline.py` | hdz | 按需；默认 dry-run，`--write` / `--apply` 才写 |
| `audit_staging_vs_d2.py`、`count_unscored.py`、`dump_scoring_sample.py` | hdz | 只读工具 |
| `relay_restore.py` | hdz ← boh2 | 一次性；硬编码主机与密码文件路径 |
