# 上传恢复与网页验收

Role: evidence
Status: partial — 上传与发布已恢复，批量仍在执行
Date: 2026-09-14
Related: [当前交接](../implementation-status.md)、[前一阶段](execution.md)

## 当前交付

2026-09-14 02:47 +08:00，12,410 条队列已完成并发布 74 条：普通队列 64 条、原片恢复 10 条；剩余 12,336 条。74 个播放文件、74 张封面、10 份恢复原片通过对应云端验证。按作品去重后的未解决失败为 0；5 条历史失败回执属于同一试点的早期尝试。

BOH program 已恢复为 RUNNING，阶段为 `batch`，实际进程已加载系统 CA。D2 日额度为 10,737,418,240 B，本次已用 9,613,733,225 B、剩余 1,123,685,015 B；余额不是为本任务预留的，上传前仍需查询。队列还包含速度与封面优化，不能将剩余任务全部计为不能播放。

- [远端队列与暂存核对](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/upload-recovery-checkpoint.json)
- [网站发布检查点](../../../../../runtime/web-playback-repair-20260913/upload-recovery-publication-checkpoint.json)
- [实时同步状态](../../../../../runtime/web-playback-repair-20260913/batch-sync/progress.json)
- [03:13 状态与清理核对](../../../../../runtime/web-playback-repair-20260913/final-upload-recovery-status.json)：后续已完成并发布 89 条，剩余 12,321 条，当前失败作品 0；BOH 与同步正常，所有已完成任务无暂存媒体残留。

## 上传错误修复

凌晨额度读取曾因 `bytes_uploaded_today` 校验退出。原始响应正文没有保留，不能断言当时该字段一定缺省；公开协议将它定义为 proto3 普通 int64，零值允许省略。修复仅将缺省上传计数解释为零，非法显式值、缺失或非正总额度继续报错。

BOH 的 Mamba 证书库无法验证 `simple-file-proxy.2dland.cn`，报 issuer 校验错误；系统 CA 的实际 TLS 校验通过。本任务 Supervisor 已设置 `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` 并执行 `reread`、指定 program 的 `update`，证书校验保持开启。

上传任务编号含 5 个冒号。旧脚本将冒号变成 `%3A`，服务端错误中的编号也保留了编码形式，返回“upload session not found”。同一任务、同一文件只保留冒号后，分块 POST 从 404 变为 201。任务 URL 现在保留冒号，不更换接口或 HTTP 方法。

D2 最终提交的实际响应包含 `path`、`size`、`content_identity`、`user_identity`，没有 `identity`。文件实际已创建，正式元数据接口能返回标识。上传器现在统一读取 `/v6/userfile/get`，核对路径、大小和内容标识一致后才返回成功，不用缺失字段伪造标识，也不以文件存在代替媒体验收。

- [额度与证书部署](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/quota-tls-deployment.json)、[实际证书检查](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/d2-upload-tls-check.json)
- [404 诊断](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/upload-block-diagnostic.json)、[同任务冒号对照](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/upload-task-colon-diagnostic.json)
- [提交字段与正式元数据](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/cover-finalize-diagnostic.json)
- [实际恢复运行](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/upload-repair-restart.json)
- [公开额度协议](https://github.com/city404/v6-public-rpc-proto/blob/main/user/user_svc.proto)、[ProtoJSON 零值规则](https://protobuf.dev/programming-guides/json/#presence-and-default-values)
- [OpenList 上传实现](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/halalcloud_upload.go)、[上游响应模型](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/meta.go)

## 网站目录缓存

直接上传 D2 不会自动刷新 OpenList 的目录缓存。实测 `_web_playback` 缓存只有 1 个目录、缺少恢复用户，刷新后变为 3 个目录。此前返回 404 的原片、播放文件和封面随后均返回 206，且总大小与 D2 一致。

发布现在先用 `/api/fs/list` 的 `refresh=true` 从父目录到用户目录刷新元数据，再锁内合并记录并检查网站 Range。同一回执批次内共享目录只刷新一次，每次新同步重新刷新。接口只返回一项列表内容；上游在分页前已获取并缓存完整目录。刷新失败会使同步明确失败并保留检查点，不跳过检查。

- [缓存前后与三种媒体 Range 对照](../../../../../runtime/web-playback-repair-20260913/openlist-cache-diagnostic.json)
- [OpenList 官方 ListReq / FsList](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/fsread.go)

## 试点网页验收

“贴心的小饱饱 / 7683916068545630137”的 D2 播放版为 2,454,914 B、H.264/AAC、540×960、30 fps、faststart，完整解码 449 帧。D2 全部字节的 SHA-256 与 BOH 保留产物相同；封面为 32,917 B、360×640，完整字节和解码通过。原片的路径、标识、内容标识、19,625,016 B 大小及修改时间均未变。

试点在“提交成功、响应解析失败”后，使用限定该作品的复核脚本恢复回执，重新读取 D2 全部字节并完整解码。普通队列仍拒绝直接接纳未经验证的既有播放文件。复核脚本第一次使用未规范化的字符串大小导致失败，改为调用现有 `get_source` 后通过，失败回执仍保留。

网站卡片选中新播放下标 3、封面下标 4。浏览器确认实际视频帧、音频解码、14.966667 秒完整结束、拖动到 7.5 秒和重新打开成功；首帧分别约 3.59 秒、3.62 秒。无头浏览器两次分别记录 287、291 个掉帧，不能宣称完全流畅或受控冷缓存性能通过，多设备流畅度仍没验证。

- [云端完整校验及原片快照](../../../../../runtime/web-playback-repair-20260913/upload-recovery-evidence-20260914/pilot-cloud-verification.json)
- [真实 D2 网页结果](../../../../../runtime/web-playback-repair-20260913/browser-pilot-d2.json)、[画面截图](../../../../../runtime/web-playback-repair-20260913/browser-pilot-d2-frame.png)

## 暂存清理

普通播放文件和封面位于 `TemporaryDirectory`，处理成功或抛出异常后都会清理；等待上传额度时任务尚未结束，暂存可能保留。02:47 普通目录仅有当前任务的 1 个处理中媒体文件，约 4.19 MB，并非积累了 74 份已完成产物。

`special-sources/` 的 21 个恢复候选共 55,838,409 B，早期试点保留副本为 2,454,914 B，共 58,293,323 B。这两类没有自动清理逻辑，本次没有删除。普通暂存清理不会删除 D2 原片、播放版或封面。历史媒体处理始终在 BOH，本机仅收元数据并进行正常浏览器验收。

## 代码检查

- 上传、签名、额度：47 passed、60 subtests passed，0.55 秒；冒号和提交字段回归测试修复前分别失败 1 项、2 项。
- 目录刷新、同步、网站 Range、D2 读取：23 passed、4 subtests passed，1.32 秒。
- 两组均使用 `timeout 60 .venv/bin/python -m pytest -q ...`，本次合计 70 passed、64 subtests passed。
- 相关 Ruff、C901、文件长度及差异空白检查通过。类型检查没验证。

原有 883 条映射仍有效，4 条加密、权限或文章类型的特殊限制仍保留。批量队列尚未完成，后续状态以当前交接和实时检查点为准。
