# 封面补全执行状态

> **Archived 2026-10-30**：计划已关闭，本文件只作历史快照；见同目录 README 顶部关闭说明。


Role: current-state
Date: 2026-09-14
Current Phase: Deployed; all recovery batches finished, 26 covers unresolved
Last Landed: 2026-09-14 20:19 CST，22,285 张封面的现有 HTTP、解码证据与当前文件 SHA-256 一致。
Next Target: 原作访问恢复后，再恢复剩余 26 张真实封面并逐张验收。
Blocked By: 归档原图缺失或原媒体异常；登录后未取得剩余原作数据，备用账号的校准页也要求验证码，不能判定作品已删除。
Last Verified: 2026-09-14 20:45 CST，索引及三张线上图片通过；20:52 CST 确认剩余原作仍受验证码阻挡、各批次已结束，见[实际访问验收](evidence/20260914-http-availability-audit.md)。

## 当前结果

- 首批 11,450 条只覆盖缺少封面引用的记录，已补 11,448；其余旧引用的实际不可用情况被漏检。
- 全站 22,311 条记录。首批之外已导入本机旧图 400 张、D2 旧图 8,996 张、沃盘主批次 1,370 张及定向恢复 30 张；这些批次均已结束。
- 19:08 CST 全量 HTTP 验收确认 22,244 张有效；随后原作试点、图文批次、页面重新解析和登录批次分别新增 1、34、5、1 张，全部通过 HTTP 与完整解码验证。
- 20:19 CST 最终核对为有效 22,285、缺失 26、无效 0，涉及 22 个用户。20:44 CST 当前记录、索引及图片大小仍与最终核对一致。缺失项为 24 条图文和 2 条视频，详见[明细](../../../../runtime/cover-loading-20260914/remaining-covers.md)。
- 跳跳糖🍬的旧 `cover.jpg` / `.cover.jpg` 尚在本机，但网页 `/media/` 接口只读云盘，导致 404；导入独立封面后第一页失败数为 0。
- 婷婷 203 / 203 张封面已通过接口验证；重新打开婷婷和跳跳糖首屏、艾米粒儿第 2 页，各 20 张全部加载，均使用独立封面接口。20:45 CST 三个用户的代表图片再次通过 HTTP、哈希和完整解码。
- 已失败的图片会被 `showCoverError()` 替换为提示块；后台补图不会改变旧弹层，重新打开作品弹层才会请求新封面。
- `CoverStore`、图片接口、卡片及 video poster 已部署。新下载在保存有效封面后才登记成功和发送成功通知；失败保留重试。
- 损坏封面、原图和原片缓存的重试已修复；替换失败保留旧原片，云端校验前不提前清理源媒体。
- 首批 BOH worker 和同步已结束；旧封面核验另用工作目录，保留首批回执和游标。本任务尚未全部完成。

## 待恢复来源

威化饼（`24ecb8c4-e7f8-4860-a64e-0c7ece980f38`）的未完成项：

- `7661915140649210728`：D2 与夸克对象均为 1,243,735 B，仅 M4A 音频，无视频画面。
- `7538397219817770255`：D2 与夸克对象均为 0 B。

本机、D2 与夸克未找到对应额外图片或元数据。沃盘前者已实际下载和探测，也只有 AAC 音频；后者原路径不存在。原官方请求曾返回 `status_self_see`，登录后的官方视频页当前进入验证码。24 条图文经新账号复核后仍未取得原作详情；备用旧账号访问已成功恢复过的校准作品也进入验证码，因此旧账号批量重试没有启动。不能据此断言作品已删除。

## Active TODO

- 原作页面可正常访问后，按剩余 26 条明细恢复真实图片；不重复已结束的归档和原作批次。
- 对新增封面核对作者、作品及下载版本，发布后逐张检查 HTTP、哈希与完整解码，再更新全站缺失数。

## 运行位置与恢复

网站服务 `douyin-monitor-8900.service` 于 14:55:26 CST 重启，最终检查为 active/running、PID `3618400`。
20:19 CST 已验证封面占用 589,505,318 B；20:44 CST 网站磁盘可用 4,002,107,392 B。
首批记录目录为 `runtime/cover-backfill-20260914/`，只作为首批图片和婷婷验证的证据。旧引用漏检修复记录在 `runtime/cover-loading-20260914/`。
`sync-progress.json` 为 finished；`full-sync-checkpoint.json` 的 `next_offset` 与 BOH journal 均为 `15,644,712`。原始 12 条失败回执保留，不能用批次失败数代替当前缺失数。
BOH 根目录为 `/var/tmp/douyin-cover-backfill-20260914`；原 `full-run/`、小图和恢复证据保留。旧引用核验使用独立的 `legacy-image-*` 目录。
旧封面 `legacy-image-run/` 与网站 `legacy-sync-checkpoint.json` 已完成，累计 8,996 张成功、1,465 条失败。
沃盘协调进程已结束，批次输入、lookup 报告、JPEG tar 与导入回执均保留。
原作 `upstream-image-run/`、`upstream-reparse-run/`、`upstream-auth-run/` 均已结束，成功图片全部导入；旧 PID `934005`、`946597`、`947137` 已结束。所有 tar、来源回执和私密页面均保留。只通过 `ssh-skill` 检查与传输。
本机 `cover-web-verify` 已关闭；BOH `cover-upstream-recovery` 于 20:52 CST 成功关闭。当前没有仍在执行的封面恢复批次。
全量 HTTP 证据为 `full-http-audit.json`，最终合并核对为 `final-reconciliation.json`；缺少索引的旧引用一律列入未完成。无需重复完整 HTTP 扫描，新增或变化的图片必须重新请求验证。
历史媒体提帧只在 BOH 执行，网站接收小图；旧 D2 视频修复队列没有修改。Trellis 保持停用。

## 实施接口

`CoverStore.publish(user_id, record, source)` 发布真实图片；`lookup(user_id, record)` 与 `lookup_many(user_id, records)` 按作品及下载版本查询。
`NewWorkCoverPublisher(download_root, cover_store).publish(user, record)` 在下载成功提交前执行。
历史导入校验当前用户及下载记录版本，原子写入 JPEG 后提交独立索引。旧 `cover_file`、`files`、`cover_index` 未批量改写，缺图统计须读取当前封面索引。
