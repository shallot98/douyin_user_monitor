# 已执行修复与限制

Role: evidence
Status: partial
Date: 2026-09-13

本文件记录 2026-09-13 阶段：866 条视频路径与 17 条封面路径已上线，12,410 条媒体任务已部署，当时新兼容视频上传、发布均为 0。2026-09-14 上传和发布已恢复，最新结果以[上传恢复验收](upload-recovery.md)和[当前交接](../implementation-status.md)为准。

## 路径部署

合并旧 742 条证据与新增 124 条精确匹配，按每条 `selected_source_role` 选择目标。导入时再次核对当前用户身份、下载时间与原路径：866 条通过，0 条过期。映射单独安装在状态文件旁，不覆盖 `monitor_users.json`。

- [合并证据](../../../../../runtime/web-playback-repair-20260913/path-recovery-evidence-combined.json)
- [安装与服务验收](../../../../../runtime/web-playback-repair-20260913/alias-installation.json)
- [866 条网站复核](../../../../../runtime/web-playback-repair-20260913/recovered-paths-report.md)
- [去重与覆盖校验](../../../../../runtime/web-playback-repair-20260913/recovered-paths-validation.json)

866 条来自 62 个账号，561 条 H.264、305 条 HEVC；全部网站文件长度与核实的 D2 对象一致。只读取了 32,981,026 字节头部与索引，没有保存媒体载荷。H.264 样本 “八月 / 7457125265412590867” 已通过网页点击、帧回调、音频解码、完整结束和拖动，见[浏览器证据](../../../../../runtime/web-playback-repair-20260913/browser-restored-path.json)。首帧约 3.88 秒，该样本此前做过索引检查，不作为受控冷缓存性能对比。

## 封面部署

检查 1,211 个已有封面引用，1,194 个可读取、17 个 HTTP 404。17 个失效引用在相应视频的 D2 历史目录找到唯一对象，完成图片头、来源及记录校验。新增后合计 883 条映射，安装时过期项为 0，服务已重启。

17 张封面全部通过浏览器 `Image.decode()`，宽高非零，涉及 9 个账号。该结果证明封面可显示，不代表关联视频全部通过解码。

- [已有封面检查](../../../../../runtime/web-playback-repair-20260913/existing-cover-summary.json)
- [映射证据](../../../../../runtime/web-playback-repair-20260913/cover-alias-evidence.json)与[安装结果](../../../../../runtime/web-playback-repair-20260913/cover-alias-installation.json)
- [17 张封面浏览器结果](../../../../../runtime/web-playback-repair-20260913/browser-recovered-covers.json)

## 兼容版本试验

试点为“贴心的小饱饱 / 7683916068545630137”。夸克现有 Cookie 转码接口返回 `plf_invalid`；D2 原生预览没有给出兼容播放文件。`boh` 可直接读取原片并执行 FFmpeg，未使用项目本机媒体代理或上传缓存。

首次生成后，D2 `create_upload_task` 返回 HTTP 429。实际配额接口显示已超出当日普通上传额度，存储空间和文件数仍有余额。新播放文件和封面均未创建；原片身份、内容身份、大小及修改时间未变。

- [原生能力试验](../../../../../runtime/web-playback-repair-20260913/cloud-native-pilot-outcome.md)
- [实际配额](../../../../../runtime/web-playback-repair-20260913/d2-upload-quota.json)
- [首次试验后对象检查](../../../../../runtime/web-playback-repair-20260913/pilot-after-first-attempt.json)
- [离线导入协议依据](../../../../../runtime/web-playback-repair-20260913/offline-import-protocol.md)

HTTPS 离线试验已经结束：节点源 HEAD 200，文件大小 2,454,914 B；D2 `parse` 返回 HTTP 200/空对象，`add` 返回 HTTP 400/code 3，未得到任务 ID，没有证据表明任务被受理。临时 origin 和 tunnel 已停止。不能据一次失败推断 D2 全部 HTTPS 导入不支持，也不能把 parse 的 200 当成功交付。

- [离线失败回执](../../../../../runtime/web-playback-repair-20260913/pilot-offline-failure.json)
- [HTTPS 源及接口诊断](../../../../../runtime/web-playback-repair-20260913/offline-origin-diagnostic.json)
- [原片与目标复核](../../../../../runtime/web-playback-repair-20260913/pilot-source-and-output-status.json)：原片身份、内容身份、大小、修改时间未变，两个新目标均为 404。

兼容试点通过 boh 临时 URL 的真实浏览器验收：H.264、540×960、14.966667 秒，实际视频帧、音频解码、完整结束和拖动通过。首帧约 2.38 秒，记录中有 45 个掉帧，非受控冷缓存性能测试；这不是 D2 网站验收，不能宣称“贴心的小饱饱”已修复。见[试点浏览器证据](../../../../../runtime/web-playback-repair-20260913/browser-derived-origin.json)。

## 特殊记录

14 条异常记录中，10 条作品共有 20 个候选在 boh 完整解码通过；选择其中 10 份最高可用原始源，合计 32,910,290 B。9 条属于威化饼空文件，1 条属于小言柚截断文件。原始字节保存在 boh 的 `special-sources/`，项目本机仅接收 JSON 结果；部署时再次逐条核对大小及完整 SHA-256。

恢复队列先将原始字节保存到 D2 `_recovered_originals/`，再验证完整 SHA-256、媒体信息和完整解码，随后生成 `_web_playback/` 播放文件、封面。发布更新原片引用但保留旧文件、旧 `files` 下标和 `cloud_playback_repair.previous_references`。该队列已部署，D2 恢复源尚未上传成功。

剩余 4 条：小言柚 `7678206504341111274` 的 CENC 新候选实际解码失败；威化饼 `7661915140649210728`、`7538397219817770255` 详情为 `status_self_see`；奇怪KT `7642262303110466862` 为 type 163 文章及 MP3。不能将权限限制说成删除，也不生成黑屏视频伪装恢复。

- [早期特殊来源报告](../../../../../runtime/web-playback-repair-20260913/special-source-report.md)
- [候选完整解码结果](../../../../../runtime/web-playback-repair-20260913/special-source-byte-results.jsonl)与[汇总](../../../../../runtime/web-playback-repair-20260913/special-source-byte-finished.json)
- [恢复队列](../../../../../runtime/web-playback-repair-20260913/source-recovery-queue.json)与[摘要](../../../../../runtime/web-playback-repair-20260913/source-recovery-queue-summary.json)

## 后台部署

2026-09-13 19:16 +08:00 验证：boh 新进程 RUNNING，26 个部署文件哈希一致，阶段为 `pilot`，状态为 `waiting_for_upload_quota`；试点成功 0、失败 0、待处理 1。阶段计数不是全量队列大小。D2 报告额度 10,737,418,240 B、已用 17,271,364,183 B、剩余 0；后台每 600 秒重查，每个文件上传前也检查是否有足够余额。

普通队列 12,400 条、恢复队列 10 条，合计 12,410 个唯一作品；试点包含在普通队列中。合计转码 10,543 条、仅重封装 51 条、仅封面 1,816 条，封面输出 11,540 个。340 条无需当前改动，另 14 条小暖暖记录已从当前状态消失而排除，它们与原始 14 条特殊媒体不是同一组。

运行目录为 `/var/tmp/douyin-playback-repair-20260913`，独立 Supervisor 配置为 `supervisor/repair.conf`，program 为 `douyin-playback-repair`。只停止、更新和重启了本任务程序；未重启容器或已有 Supervisor 实例。旧代码和配置备份在该目录的 `deployments/v2-before.tar.gz`。预检曾因系统 Python 不支持 `hashlib.file_digest` 失败，改用流式 SHA-256 后通过，失败时未修改运行服务。

- [部署包与去重统计](../../../../../runtime/web-playback-repair-20260913/deployment-v2-package.json)
- [部署文件哈希](../../../../../runtime/web-playback-repair-20260913/deployment-file-hashes-v2.json)
- [原始源、部署与进程验收](../../../../../runtime/web-playback-repair-20260913/deployment-v2-validation.json)

## 发布与检查

`hdz` 的 `douyin-playback-repair-sync.timer` 已启用，每 60 秒读取远端元数据回执。部署后的同步服务 `Result=success`、退出码 0，当前发布计数为空、检查点 0，见[本机同步验收](../../../../../runtime/web-playback-repair-20260913/deployment-v2-local-sync.json)及[实时状态](../../../../../runtime/web-playback-repair-20260913/batch-sync/progress.json)。

同步先重新检查 D2 对象及远端完整解码证据，通过 `MonitorStorage.update_state` 锁内合并记录，再检查网站 `Range: bytes=0-0` 的 HTTP 206、总大小。网站检查通过才推进检查点；若失败，已验证的云端引用可能已经更新，但检查点不动，后续同步幂等重试并明确记录失败。

`MonitorStorage.update_state` 已改为持锁后重新读取磁盘状态，防止两个原子替换具有相同 mtime 时遗漏新作品；确定性回归测试还验证 mutator 失败不会污染磁盘与缓存。最终定向测试 **296 passed、95 subtests passed、7 warnings，16.73 秒**，见[测试记录](../../../../../runtime/web-playback-repair-20260913/final-targeted-tests.txt)。Ruff 和 C901 检查通过；脚本文件长度和函数非空行数检查通过。

历史媒体处理未下载到项目本机，现有云端原片不覆盖，`/kuake` 原片读取模式保持。仍未完成的是新增产物 D2 交付、真实 D2 网页样本验收及全量队列执行。
