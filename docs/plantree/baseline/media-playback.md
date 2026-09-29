# 媒体归档与网页播放

Role: baseline
Updated: 2026-09-14

## 当前数据流

用户及作品记录保存在 `data/monitor_users.json`。归档页读取 `download_records`，不需要实时请求抖音作品列表。媒体角色包括原片 `video_file`、播放文件 `fast_play_file` 和封面 `cover_file`；网页媒体请求使用作品 ID 与文件下标。

| 文件 | 当前职责 |
| --- | --- |
| [archive_feed.py](../../../douyin_user_monitor/monitor/archive_feed.py) | 构造归档列表，优先选择记录中的播放文件；缺少时选择原片 |
| [monitor.py](../../../douyin_user_monitor/api/monitor.py) | 读取本地已有文件或解析云盘地址，提供 Range 流式响应 |
| [openlist_client.py](../../../douyin_user_monitor/monitor/openlist_client.py) | D2 优先、夸克备份；构造历史清理路径，缓存签名地址 |
| [archive_path_aliases.py](../../../douyin_user_monitor/monitor/archive_path_aliases.py) | 单独保存并校验历史对象精确路径，保留原下载记录 |
| [media_proxy.py](../../../douyin_user_monitor/api/media_proxy.py) | 透传媒体 Range，管理上游响应及连接生命周期 |
| [playback_prepare.py](../../../douyin_user_monitor/monitor/playback_prepare.py) | 新下载流程中准备独立 H.264/AAC/faststart 播放文件 |
| [playback_media.py](../../../douyin_user_monitor/monitor/playback_media.py) | 检查实际编码、像素格式、尺寸、码率及 MP4 索引位置 |
| [user_sync.py](../../../douyin_user_monitor/monitor/user_sync.py) | 保存下载记录中的媒体角色和来源信息 |
| [storage.py](../../../douyin_user_monitor/monitor/storage.py) | 文件锁内重新读取磁盘状态后合并更新，原子替换 JSON，并维护进程内缓存 |
| [works.html](../../../douyin_user_monitor/web/works.html) | 卡片封面、视频弹层和播放错误展示 |
| [cloud_playback](../../../scripts/cloud_playback/README.md) | 独立节点转换、D2 直接上传、原片恢复、元数据回执及网站发布 |

## 已验证的边界

- 原片与浏览器播放文件承担不同职责。原片保留上游字节，不能为了兼容浏览器而覆盖原片。
- `.mp4` 文件名、较低分辨率以及 `has_fast_play` 均不能独立证明视频可以解码；旧播放文件实测混有 H.264 和 H.265。
- 卡片优先读取独立 `cover_url`，其次使用归档 `cover_index`；两者均无时显示占位块。独立索引保存在 `data/covers/`，绑定用户、作品及下载版本。封面缺失本身不证明视频播放失败。
- 单独观察 `playing` 事件不够。已有 HEVC 样本触发该事件但宽高为零；验收应检查真正的视频帧。
- 当前播放选择主要按记录中是否有文件路径判断；云端历史文件不会因下载代码升级自动变为兼容格式。
- 现有夸克挂载同时服务原片复制。不能全局改为转码下载地址，否则可能改变备份内容。

## 运维约束

本项目是 Python 源码运行，由 `douyin-monitor-8900.service` 管理。2026-09-13 已安装 866 条视频与 17 条封面的精确路径映射；视频通过网站 Range、大小和索引复核，17 张封面通过浏览器解码。用户明确要求：历史视频修复不得下载到项目本机，必须保留原片，完成后的主要播放源继续使用 D2。

历史媒体处理在 `boh`，通过 D2 原生地址读取和上传，不经过项目本机 `hdz` 的媒体代理或 OpenList 上传缓存。派生文件保存到 `/vps/抖音视频/_web_playback/`；损坏原片的最高可用源另存到 `_recovered_originals/`，验收后追加引用，保留旧云端文件及下载历史。恢复源须通过完整 SHA-256、媒体信息和完整解码检查。

`boh` 使用本任务独立的 Supervisor 配置；`hdz` 的 `douyin-playback-repair-sync.timer` 定时同步元数据。发布先复核 D2 对象和完整解码回执，再从父目录到用户目录刷新 OpenList 元数据缓存，随后锁内合并状态并检查网站一字节 Range 与总大小，成功才推进同步位置。目录刷新失败会停止本次发布；网站检查失败时已合并引用可能保留，同步位置不前移，下一次幂等重试。

2026-09-14 已恢复 12,410 条队列的批量上传和网站发布，试点通过 D2 真实网页验收，10 份恢复原片通过云端完整验证；整个队列尚未完成。额度不足时每 600 秒检查，不猜重置时间。D2 上传任务 URL 保留冒号；最终提交后统一读取正式元数据并核对内容标识。

普通播放文件与封面用 `TemporaryDirectory`，处理结束或抛出异常后清理；等待额度期间可能保留。恢复候选和早期试点副本是独立保留文件，没有自动清理逻辑。清理 BOH 暂存不删除云盘对象。当前数量及验收范围见[执行交接](../plans/browser-playback-repair/implementation-status.md)。

后台增量监控仍可能按既有设置下载新作品；只读审计不启动下载、转码或补片。不得把“审计没有写视频”描述为“整个项目期间没有新增视频”。

相关方案：[浏览器播放修复](../plans/browser-playback-repair/README.md)。
