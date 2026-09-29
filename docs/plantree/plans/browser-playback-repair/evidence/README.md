# 检查证据

Role: evidence-index
Status: 上传与网站发布已恢复，普通批量正在执行
Date: 2026-09-14

## 当前执行

- [上传恢复与网页验收](upload-recovery.md)：实际上传故障、目录缓存修复、D2 试点、原片恢复、测试与暂存清理。
- [03:13 状态与清理核对](../../../../../runtime/web-playback-repair-20260913/final-upload-recovery-status.json)：完成并发布 89 条，当前失败作品 0，已完成任务无暂存媒体残留。
- [D2 试点网页结果](../../../../../runtime/web-playback-repair-20260913/browser-pilot-d2.json)：真实帧、音频解码、完整结束、拖动与重开通过，保留掉帧记录。
- [执行结果与限制](execution.md)：883 条映射、浏览器验收范围、D2 失败记录、原片恢复与后台部署。
- [866 条视频的网站复核](../../../../../runtime/web-playback-repair-20260913/recovered-paths-report.md)：全部 Range、大小与索引检查通过。
- [17 条封面安装](../../../../../runtime/web-playback-repair-20260913/cover-alias-installation.json)与[浏览器解码](../../../../../runtime/web-playback-repair-20260913/browser-recovered-covers.json)：合计 883 条映射，17 张封面解码成功。
- [早期临时 URL 浏览器结果](../../../../../runtime/web-playback-repair-20260913/browser-derived-origin.json)：当时仅验证 boh 临时 URL，`website_d2_playback_verified=false`。
- [普通队列摘要](../../../../../runtime/web-playback-repair-20260913/queue-summary.json)与[原片恢复摘要](../../../../../runtime/web-playback-repair-20260913/source-recovery-queue-summary.json)：合计 12,410 条，初次部署时新增 D2 播放文件发布为 0。
- [初次部署及恢复源验证](../../../../../runtime/web-playback-repair-20260913/deployment-v2-validation.json)：2026-09-13 的 26 个文件哈希和 10 份恢复源 SHA-256 一致，当时处于试点额度等待阶段。
- [自动同步验收](../../../../../runtime/web-playback-repair-20260913/deployment-v2-local-sync.json)与[实时同步状态](../../../../../runtime/web-playback-repair-20260913/batch-sync/progress.json)。
- [前一阶段定向测试](../../../../../runtime/web-playback-repair-20260913/final-targeted-tests.txt)：296 passed、95 subtests passed、7 warnings；本次检查见上传恢复记录。

## 统计产物

- [固定视频快照](../../../../../runtime/web-playback-inventory-20260913/manifest.json)：2026-09-13 14:14:19 +08:00，538 个有视频的账号、12,768 条作品。
- [统计报告](../../../../../runtime/web-playback-inventory-20260913/report.md)：分类、范围、受影响账号及限制。
- [问题视频 CSV](../../../../../runtime/web-playback-inventory-20260913/problem-videos.csv)：6,597 条作品及其证据、处理建议。
- [受影响账号 CSV](../../../../../runtime/web-playback-inventory-20260913/affected-users.csv)：466 个账号的分类计数。
- [全部视频及账号 JSON](../../../../../runtime/web-playback-inventory-20260913/video-inventory.json)：包含 6,171 条 H.264 兼容候选，保留原始作品 ID。
- [扫描完成记录](../../../../../runtime/web-playback-inventory-20260913/scan-summary.json)：完成 12,768 条，剩余 0 条。
- [逐文件原始结果](../../../../../runtime/web-playback-inventory-20260913/results.jsonl)：编码、轨道、索引、读取异常、记录指纹及证据时间。
- [既有账号级报告](../../../../../runtime/media-problem-users-20260913.json)：先前调查的下限，不能替代逐视频清单。
- [浏览器 H.264/HEVC 对照](../../../../../runtime/xiaobaobao-playback-diagnosis-20260913.json)：H.264 真实首帧与 HEVC 无画面结果。

## 方案依据

- [云端能力研究](cloud-capabilities.md)：官方 SDK/上游源码、接口范围、已知缓存行为和未验证事项。
- [历史路径早期复核](path-recovery-inventory.md)：保留当时 742 条已定位、124 条未定位的证据；后续 124 条已补齐，当前结果见执行记录。
- [三个路径样本](historical-paths.md)：写入与读取规则不同的代码和实时对象依据。
- [特殊媒体复核](special-media.md)：逐条确认空文件、音频、加密封装和长度异常，不能统一按编码问题处理。
- 现有媒体代码与状态更新方式见[项目基线](../../../baseline/media-playback.md)。

索引证据为 11,580 条本次新检查和 1,188 条未变化记录的既有成功检查，原证据时间分别保留。H.264 分类不是完整浏览器验收；HEVC 风险以不支持该编码的浏览器为条件。账号类别可能重叠，不能相加。

原始统计阶段仅执行索引读取、元数据核对与浏览器样本对照。后续已完成路径映射、D2 试点和恢复原片交付，普通批量正在执行；早期失败记录保留，不能与后续成功回执混为一谈。历史媒体没有下载到项目本机进行修复。
