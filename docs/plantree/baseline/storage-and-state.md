# 存储与状态

Role: baseline
Updated: 2026-10-30

## 持久文件

| 文件 | 内容 | 写入方 | 并发控制 |
|------|------|--------|----------|
| `data/monitor_users.json`（约 1.8MB） | users（2026-10-30：708 个，启用 681）、monitoring、cookie_pool | `MonitorStorage.update_state`；`save_state` 直写剩 3 处（`service.py` 约 184/1120/1141，均无 await） | `.lock` flock + tmp 原子替换 + 文件戳校验进程缓存 |
| `data/monitor_records.db` | `user_records(user_id, rev, records)`，按用户整列表 JSON；30,720 条 | `RecordStore`，只在 `update_state` 锁内写，先于 JSON 提交 | SQLite WAL + RLock；`rev`/`data_version` 增量刷新 |
| `data/covers/` | 独立封面索引 `index.sqlite3` + 内容哈希对象 | `CoverStore` | 原子替换 |
| `data/api_keys.db` | 网页 Key，只存 SHA-256 | `monitor/api_keys.py` | 模块全局 `_STORE` |
| `data/media_cache/` | 归档小文件缓存 | `ArchiveMediaCache` | LRU；磁盘剩余 <2GB 清空 |
| `data/archive_path_aliases.json` | 已核实的历史归档精确路径 | 外部脚本 | 只在启动时加载 |
| `data/parse_cookies/*.txt` | 详情/解析用过期 Cookie | 手动维护 | 目录变更自动重读 |
| `config/douyin_web.yaml` | 爬虫配置与当前 Cookie | `InProcessDouyinClient._persist_cookie` | asyncio.Lock |
| `data/douyin.db` | — | 代码中找不到引用 | Unknown / needs inventory |

`data/` 总计约 1.2G，`runtime/` 168M，`logs/` 56M。根分区 2026-10-30 已用 88%，剩约 12G。

## 写入不变量

- `MonitorStorage.update_state(mutator)` 是唯一并发安全写入口。禁止 `load_state()` + `save_state()` 跨 await 的读改写。
- 含网络的写路径拆成"锁外网络阶段 + 锁内 `update_state` 提交"。
- 读记录必须经 `storage.records.attach/attach_all`；JSON 内联 `download_records` 会抛 `LegacyRecordsInStateError`（有意不兼容）。
- 迁移备份：`data/monitor_users.json.pre-split.20260929_131005.bak`。

## 内存缓存

`api/monitor.py` 模块级全局：头像（2048）、统计、作品库索引、游客作品集。`OpenListClient` URL 缓存：直链 300 秒，代理地址 1800 秒。均为单进程假设。

## 媒体副本

本地 `download/` 只是中转（2026-10-30 约 141M）。主盘 D2 `/d2/vps/抖音视频`，备份夸克 `/kuake/抖音视频`；历史 wopan 迁移情况见记忆，不在本仓库。
