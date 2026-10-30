# 旧封面漏检复核

Role: evidence
Status: accepted; historical recovery checkpoints
Date: 2026-09-14
Related: [执行状态](../implementation-status.md)、[首批记录](20260914-final-verification.md)

下文保留 16:30–17:50 CST 的复现与批次证据。归档批次现均已结束，最新全站结果及剩余来源限制见[实际访问验收](20260914-http-availability-audit.md)。

## 已复现原因

用户反馈网页依然显示“封面加载失败”。网站日志显示跳跳糖🍬（`0b9693af-0195-4693-a01f-080430169cd2`）的旧 `/archive/{aweme_id}/media/{cover_index}` 请求返回 404。
作品 `7684525661821952858` 的 `cover_url` 为 null，`cover_index` 为 6，指向旧目录中的 `cover.jpg`；该文件当时仍在本机，云盘路径不可用。
`stream_archived_media` 只读云盘，所以记录中的旧路径不能直接证明网页能加载图片。首次浏览器复现第一页 20 张中的 17 张失败。

首批选择了缺少封面引用的 11,450 条记录；其余 10,861 条仅被记为 `legacy_reference`，没有实际逐张请求和解码。
此前“全站只剩 2 条未完成”的表述超出了已验证范围；首批 11,448 张实际补入的事实不变。

## 本机旧封面恢复

16:30 CST 重新盘点发现 416 个旧封面引用对应本机文件，涉及 89 个用户。通过现有 `CoverStore` 逐张解码、发布并读回校验。
先恢复跳跳糖第一页失败的 17 张；随后批次另发布 383 张、跳过已验证的 17 张，16 条在导入时已找不到原路径文件，明确记录失败并纳入后续云盘核验。
本次共新增 400 张独立封面，索引累计 11,848 张；没有把 16 条失败计作成功。

浏览器刷新后，跳跳糖第一页 20 / 20 张全部加载，失败提示为 0。其中 18 张使用独立封面接口，2 张为已实际加载成功的归档首图。

- [来源盘点](../../../../../runtime/cover-loading-20260914/source-inventory.json)
- [17 张试点](../../../../../runtime/cover-loading-20260914/local-pilot.json)
- [本机批次回执](../../../../../runtime/cover-loading-20260914/local-migration.json)
- [页面恢复验收](../../../../../runtime/cover-loading-20260914/browser-repaired-first-page.json)

## D2 与沃盘恢复

17:50 CST，10,461 个旧引用的 D2 批次完成，成功导入 8,996 张、明确失败 1,465 条。失败不等于不可恢复：已找到沃盘 `/wopan/抖音视频` 中的实际图片，当前网页归档接口只查询 D2／夸克，原先没有覆盖这个位置。
沃盘四张试点通过真实下载、源字节数和 SHA-256、JPEG 生成与解码、网站导入、HTTP 200、Content-Type、网站内容哈希和浏览器解码。前两批另导入 377 张；后续批次继续运行。

WoPan 输出采用独立 `kind=wopan-cover-batch` 回执，保留真实 `remote_path`、`name/size/modified` 快照、源 SHA-256。网站重新核对当前用户、作品和下载版本以及真实沃盘元数据，再发布封面；没有伪造 D2 回执。沃盘未提供 provider hash，元数据复核不能识别 name/size/modified 均未变化的内容替换。
所有图片处理在 BOH，私密临时 URL 清单权限为 0600，传回的网站 tar 只包含公开回执与 JPEG。原 D2 视频队列、原片和下载历史保持原样。

- [D2 同步进度](../../../../../runtime/cover-loading-20260914/sync-progress.json)
- [沃盘试点导入](../../../../../runtime/cover-loading-20260914/wopan-pilot-import.json)
- [沃盘试点 HTTP 与浏览器解码](../../../../../runtime/cover-loading-20260914/wopan-pilot-api.json)
- [沃盘当前进度](../../../../../runtime/cover-loading-20260914/wopan-progress.json)

## 当时待核验项

17:50 CST 时沃盘批次仍在运行，原文件缺失的作品继续查其他云盘副本及同一图文的其他真实图片。随后所有归档恢复批次结束，19:08 CST 完成全量 HTTP、哈希与完整解码验收。
威化饼两条异常随后完成沃盘复核：其中一条只有 AAC 音频、没有视频画面，另一条原路径不存在；未计为已补齐。后续原作恢复及剩余 26 条的原因见最新验收记录。
