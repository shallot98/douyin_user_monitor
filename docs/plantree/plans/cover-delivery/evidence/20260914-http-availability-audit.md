# 全站封面实际访问验收

Role: evidence
Status: partial; 22,285 verified, 26 unresolved, all recovery batches finished
Date: 2026-09-14
Related: [计划](../README.md)、[执行状态](../implementation-status.md)、[旧引用漏检](20260914-legacy-cover-recheck.md)

## 最终核对

20:19:21 CST，全部 22,311 条记录中的 22,285 张封面有效，26 张缺失，涉及 22 个用户，无效图片为 0。已验证封面共 589,505,318 B。核对逐一匹配当前记录、索引、HTTP 验收版本、尺寸及字节数，并重新读取当前图片检查 SHA-256；已验证的相同字节沿用此前完整解码证据，没有重复全量 HTTP 扫描。

20:44:47 CST，再次核对当前记录、索引、文件存在性和大小，仍与最终核对一致；记录指纹保持不变，网站磁盘可用 4,002,107,392 B。20:45:52 CST，婷婷、跳跳糖和艾米粒儿三张代表图片重新通过 HTTP 200、JPEG 类型、SHA-256 及完整解码。

- [最终合并核对](../../../../../runtime/cover-loading-20260914/final-reconciliation.json)
- [当前索引统计](../../../../../runtime/cover-loading-20260914/current-index-status.json)
- [最新线上抽查](../../../../../runtime/cover-loading-20260914/handoff-http-smoke.json)
- [剩余原因报告](../../../../../runtime/cover-loading-20260914/remaining-cover-report.json)
- [逐条缺失明细](../../../../../runtime/cover-loading-20260914/remaining-covers.md)

## 全量 HTTP 基础证据

19:08:35 CST 完成全部 22,311 条下载记录的实际 HTTP 验收。每张已索引封面均检查 HTTP 200、JPEG Content-Type、精确当前版本、字节数、SHA-256、完整 JPEG 解码与尺寸。
已验证 22,244 张，缺少独立封面 67 条、涉及 53 个用户，图片或接口错误为 0。未验证的旧引用全部列为缺失，未计作成功。
验收开始和结束的记录指纹均为 `4a20769bf67a764d4bd5edc677ffb3d1a836ecc4f79737e766a6338cf7c7c8ac`。报告状态为 `incomplete`，不能表述为全部补齐。

- [完整 HTTP 报告](../../../../../runtime/cover-loading-20260914/full-http-audit.json)
- [审计脚本](../../../../../runtime/cover-loading-20260914/audit_all_covers.py)
- [合并核对脚本](../../../../../runtime/cover-loading-20260914/reconcile_cover_evidence.py)

## 新增原作封面

全量验收后，从真实原作另恢复 41 张，分别导入并完成 HTTP、哈希及完整图片解码。四个批次均已结束，成功图片全部发布；失败记录没有计作成功。

| 来源 | 新增有效封面 | 验证证据 |
| --- | ---: | --- |
| 原作试点 | 1 | [试点 HTTP](../../../../../runtime/cover-loading-20260914/upstream-pilot-http.json) |
| 原作图文批次 | 34 | [图文批次 HTTP](../../../../../runtime/cover-loading-20260914/upstream-image-http.json) |
| 已保存页面重新解析 | 5 | [重新解析 HTTP](../../../../../runtime/cover-loading-20260914/upstream-reparse-http.json) |
| 新账号登录批次 | 1 | [登录批次 HTTP](../../../../../runtime/cover-loading-20260914/upstream-auth-http.json) |

试点 `7651293366441925093` 与作者“是野媛吗！”的账号标识匹配。BOH 浏览器读取作品自身的 `aweme.detail.images[0].urlList[0]`，取得真实 WebP，生成并验证 JPEG；网站仅接收小 JPEG 和不含签名地址的来源回执。该图片为 31,454 B、360 × 540。

原作图文批次为 34 成功、30 失败。随后修复 RSC Flight 的 `T<十六进制 UTF-8 字节长度>,<文本>` 分段解析，从已保存的原页面恢复 5 张；不能按换行跳过大文本，否则会漏掉后面的作品数据。新账号登录批次另恢复 1 张、24 条失败，回执状态为 `finished_with_failures`。

## 网页验证

19:09 CST 婷婷首屏 20 / 20 张正常加载；19:11 CST 跳跳糖首屏 20 / 20 张正常加载，两页均使用独立封面接口，没有“封面加载失败”提示。
此前婷婷全部 203 张及第 1、2、11 页的验证证据保留在首批报告。

20:31 CST，艾米粒儿第 2 页 20 / 20 张加载成功，包含重新解析后恢复的目标卡片。`showCoverError()` 会把失败的图片替换成提示块；后台发布新封面不会自动重试这个旧元素，重新打开作品弹层会取得新封面地址。这不是对原先 404 的缓存归因。

- [婷婷当前页面](../../../../../runtime/cover-loading-20260914/tingting-browser-current.json)
- [跳跳糖当前页面](../../../../../runtime/cover-loading-20260914/jumping-sugar-browser-current.json)
- [艾米粒儿恢复卡片](../../../../../runtime/cover-loading-20260914/restored-card-browser.json)

## 剩余来源限制

26 条缺失中有 24 条图文：归档未取得可用图片，新账号登录复核后仍未取得对应原作数据，表现为无作品详情、访问超时或离开原作页。备用旧账号访问已经成功恢复过的校准作品 `7651293366441925093` 也进入验证码，因此旧账号的 24 条批量重试没有启动。不能把这些结果解释为作品已删除。

20:52 CST 的最后复核仍未通过验证码，鼠标拖动前、中、后的截图相同，没有观察到成功交互。三组原作批次均已处理完全部输入，旧进程均已结束；BOH 恢复浏览器已成功关闭，本机网页验证浏览器此前已关闭。见[来源访问及进程状态](../../../../../runtime/cover-loading-20260914/final-source-access-status.json)。

威化饼两条视频中，`7661915140649210728` 的沃盘归档实际只有 AAC 音频、没有视频流；`7538397219817770255` 的 D2／夸克对象为零字节、沃盘原路径不存在。登录后的官方视频页均进入验证码。目前没有取得这两条作品的真实封面。

## 恢复与校验

本次新增脚本仅在维护目录，生产代码没有修改，服务没有重启。接续核对时服务仍为 active/running，PID `3618400`，启动时间 14:55:26 CST。后台监控仍可按原设置下载新作品，不能把本次维护未改下载历史表述为整个系统没有新下载。
原作批次要求作品 ID、作者账号、清单及当前下载版本一致；只接受原作首图，并验证导入 tar、图片内容和来源回执。不会把推荐图片、音乐图片、头像或官方拒绝访问计作成功。
原作恢复测试 11 项通过，覆盖分段合并、大文本 UTF-8 字节长度、推荐作品混淆、作者和版本变化、图片损坏及归档越界；Ruff 通过。合并核对脚本的 3 个真实运行校准用例分别验证已有证据复用、新 HTTP 验证和缺失识别，见[校准结果](../../../../../runtime/cover-loading-20260914/reconciliation-calibration.json)。测试使用项目 `.venv/bin/python` 和 60 秒超时；最初用默认 Python 的命令因缺少 `aiofiles` 未执行测试，已改用项目环境。
已有整体回归中仍有一项播放策略测试失败，见首批验收记录；本次没有声称全仓库测试全部通过。
