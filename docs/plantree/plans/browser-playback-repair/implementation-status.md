# 执行交接

> **Archived 2026-10-30**：计划已关闭，本文件只作历史快照；见同目录 README 顶部关闭说明。


Role: current-state
Mode: execute-ready
Updated: 2026-09-14 03:13 +08:00

## Current Phase

866 条视频路径、17 条封面路径已上线。BOH 已恢复普通批量处理，12,410 条去重作品中已完成并发布 89 条：普通队列 79 条、原片恢复 10 条；剩余 12,321 条，当前按作品去重后的未解决失败为 0。整个修复尚未完成。

“贴心的小饱饱”的试点已通过 D2 完整字节、解码和真实网页播放验收，原片核对未变。无头浏览器有掉帧记录，多设备流畅度仍没验证。数量是上述时间的快照，后续以[实时同步](../../../../runtime/web-playback-repair-20260913/batch-sync/progress.json)为准。

## Last Landed

- 883 条映射已安装；866 条视频的网站 Range、大小、索引复核通过，17 张封面浏览器解码通过。
- 10 份最高可用恢复源已上传 D2，完整 SHA-256、媒体和解码验证通过；旧对象和旧下载记录保留。
- 修复额度零值读取、BOH 证书配置、上传任务冒号编码和最终提交元数据核对；真实批量上传已持续成功。
- 发布前刷新 OpenList 目录缓存，避免 D2 已创建文件而网站仍返回 404；元数据同步和网站 Range 已恢复成功。
- 本次 70 tests、64 subtests 通过，Ruff、C901 通过。见[上传恢复与验收](evidence/upload-recovery.md)。

## Next Target

继续执行普通队列，按实际 D2 额度上传并逐条验证、发布；跟踪真实失败和网站检查失败，不将历史重试次数视为未修复作品数。

## Active TODO

1. 持续处理剩余 12,321 条，并监控 BOH 进程与元数据同步结果。
2. 汇总实际上传、发布、失败和期间记录变化，复核原片与封面覆盖。
3. 保留无头浏览器掉帧的性能限制，补充代表设备的流畅度验收。
4. 明确保留 4 条特殊限制，不对权限受限、加密或文章内容伪造视频恢复。

## Blocked By

当前没有阻止批量执行的已知故障。03:13 D2 报告每日额度 10,737,418,240 B、已用 9,691,026,923 B、剩余 1,046,391,317 B；其他任务也可能消耗余额。额度不足时每 600 秒重查，不猜重置时区或完成日期。

HTTPS 离线导入的早期 add 为 HTTP 400/code 3，未受理；该路径仍未用于当前队列，临时 origin 和 tunnel 已停止。

## Last Verified

03:13 BOH program 为 RUNNING、阶段 batch，云端成功 89、网站发布 89、当前失败作品 0，同步状态为 ok。所有已完成任务均未发现暂存媒体残留，普通暂存仅有当前任务的 1 个媒体文件、1,310,768 B；见[最终状态核对](../../../../runtime/web-playback-repair-20260913/final-upload-recovery-status.json)。进程的系统 CA 配置此前已验证生效；浏览器确认真实视频帧、音频解码、完整结束、拖动和重开，性能范围及测试结果见[验收证据](evidence/upload-recovery.md)。

## Handoff

- BOH 根目录：`/var/tmp/douyin-playback-repair-20260913`。远端操作使用 ssh-skill；仅管理本任务 program，不重启容器。
- Supervisor 必须带 `-c /var/tmp/douyin-playback-repair-20260913/supervisor/repair.conf`，program 为 `douyin-playback-repair`。
- 当前普通队列的 total=12400 不含已完成的 10 条恢复；试点包含在普通队列内。历史 worker_failed=5 均为同一试点的失败尝试，最终回执已成功。
- `douyin-playback-repair-sync.timer` 持续运行；每次同步刷新相关 OpenList 目录后锁内合并状态，随后检查网站 Range。同步失败不会推进检查点。
- 普通播放文件和封面在任务结束后清理。21 个恢复候选与 1 个试点副本约 58.3 MB，没有自动删除逻辑，本次仍保留。
- 项目本机只接收元数据和进行浏览器验收；历史媒体处理在 BOH。状态继续使用 `MonitorStorage.update_state` 锁内合并，禁止恢复旧全量快照。
