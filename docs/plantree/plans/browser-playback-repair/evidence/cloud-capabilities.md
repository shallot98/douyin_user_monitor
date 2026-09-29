# 网页播放修复：云端能力研究

此文件保留执行前的能力研究与方案依据。实际试验、采用的 boh 处理路径及 D2 额度限制见[执行证据](execution.md)。

检查日期：2026-09-13。范围：公开 SDK、OpenList 上游源码、官方文档；未下载任何视频，未调用转码或离线任务接口，未改挂载、生产代码或媒体。本笔记不等于端到端修复验收。

## 推荐顺序

1. 验证 D2 自身是否能按浏览器能力提供 H.264/AAC 流。公共协议里存在相关参数及预览类型，但字段语义、账号权限和返回编码尚未验证。
2. 优先复用已经兼容的云端副本。对确实需要转换的作品，验证夸克已有 H.264 完整播放文件能否通过 D2 官方离线 URL 接口直接保存为独立播放文件。
3. 无可用兼容副本、D2 原生能力或 URL 导入不能满足要求时，在独立计算节点读取云盘原片，生成 H.264/AAC/yuv420p/faststart 播放文件，直接上传 D2。项目本机仅收元数据和状态。

三条路径都必须保留原片内容及原片路径。D2 是完成修复后的主要播放来源。当前结论是“有源码支持的候选路径”，不能写成“全部已可用”。

## D2 原生播放能力

事实（高置信度）：OpenList `HalalCloudOpen.getLink()` 在 redirect 分支调用 SDK `GetDirectDownloadAddress()`，请求仅填 `Identity`、`Path`。非 redirect 分支解析原文件分片并支持 Range，没有编码转换处理。

- [OpenList driver_get_link.go](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/driver_get_link.go)
- [D2 SDK userfile/svc.go](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/userfile/svc.go)

SDK 的确切接口：`POST /v6/userfile/get_direct_download_address`。

`DirectDownloadRequest` 定义的字段：`identity`、`version`、`path`、`flag`、`client_ip`、`client_ua`、`video_codecs`（字符串数组）、`audio_codecs`（字符串数组）、`expire`、`encrypt`、`addon`。响应有 `download_address`、`expire_at`、`file_size`、`content_identity` 等，但没有已验证的视频编码字段。

- [D2 SDK DirectDownloadRequest](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/userfile/model.go)
- [公开协议 DirectDownloadRequest](https://github.com/city404/v6-public-rpc-proto/blob/main/userfile/public_user_file.proto)

**未知**：`video_codecs/audio_codecs` 究竟只是客户端能力声明、用于挑选已有版本，还是会请求服务端转码；SDK 和协议均没有说明允许的字符串值、触发条件、权限和计费。不能假定填入 `h264` 或 `avc1` 就会产生转码，也不能把这两个字符串作为已验证的请求参数。

公开协议还定义 `POST /v6/userfile/get_download_and_preview_info`，请求是 `File`，响应 `DownloadAndPreviewInfo` 含 `can_preview`、`preview_type`、`download_type`、`can_download`、`cannot_download_message`，但没有播放 URL。`PreviewStatus.play_mode` 注释含 `1: converted hls, 2: slice file`。这些定义说明协议有预览/转换播放概念，**不证明当前 D2 账号可用，也不证明当前 OpenList 挂载已接入**。公开协议与当前部署版本的一致性没验证。

此路径验收条件：取得服务商说明或官方客户端的确切字段语义；保留原片下载接口；取回兼容流的真实索引并确认视频/音轨编码、完整时长；在目标浏览器验证真正首帧、声音、拖动和链接刷新；验证不会改写原片；若返回 HLS，需验证原生 HLS 或明确接入播放器，不能把清单当普通 MP4。

## 夸克兼容流及用途隔离

事实（高置信度）：当前 OpenList 的 Quark 驱动存在如下接口调用：

`POST https://drive.quark.cn/1/clouddrive/file/v2/play/project`

公共查询参数由驱动加上 `pr=ucpro`、`fr=pc`；请求使用该账号 Cookie、`Referer: https://pan.quark.cn` 和驱动定义的 User-Agent。请求 JSON 字段为 `fid`、`resolutions: "low,normal,high,super,2k,4k"`、`supports: "fmp4_av,m3u8,dolby_vision"`。这里只记录上游现有代码，没有调用。

- [OpenList util.go:139](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc/util.go#L139)
- [OpenList meta.go](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc/meta.go)

响应 `video_list[].video_info` 的类型包含 `codec`、`audio.codec`、`size`、`format`、`url`、`finish`、`success`、`duration`、`width`、`height`、`hls_type`。但现有 `getTranscodingLink()` **只选第一个 URL 非空的结果**，没有检查编码、完成状态或时长。因此仅开启 `use_transcoding_address` 不能保证得到 H.264 或完整文件。

- [OpenList TranscodingResp](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc/types.go)

`Link()` 在 `UseTransCodingAddress && Quark && Category == 1 && Size > 0` 时一律返回转码地址。这会同时改变该挂载下视频的“下载”内容。相关 PR 明确说明转码后大小与原片不同。

- [OpenList driver.go](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc/driver.go)
- [OpenList PR 470](https://github.com/OpenListTeam/OpenList/pull/470)

**不能在现有 /kuake 挂载全局开启该选项**：它也是夸克→D2 原片复制的来源。设计应使用独立的播放接口或独立播放挂载，现有原片复制仍只读取原始下载地址。无论选哪一种，仍需按响应编码和实际媒体索引选出 H.264/AAC 完整文件。

维护限制：[OpenList 夸克文档](https://doc.oplist.org/guide/drivers/quark) 声明这是历史逆向接口，项目不主动维护。该文档的“仅本地代理”描述与当前源码及 PR 对转码地址支持 302 的说明有差异；不能由旧文档推定所有转码 URL 都不能直读，也不能由源码推定账号和所有文件均可直读，必须实测。

此路径验收条件：确认当前账号权限、Cookie 生命周期、URL 是否需额外请求头或绑定 IP、URL 有效期和完整文件时长；H.264/AAC 并不只靠名称判断。返回 HLS 清单时不能直接存成 `.mp4`，应采用清单播放或由独立节点重封装后上传。

## D2 URL 离线导入

事实（高置信度）：D2 官方 `golang-sdk-lite` 提供 `OfflineTaskService`；示例使用 `openapi.2dland.cn`，通过 SDK 客户端签名和账号授权发送请求。

| 接口 | 请求定义 | 作用 |
| --- | --- | --- |
| `POST /v6/offline_task/parse` | `TaskParseRequest`：`url`、`file`、`identity`、`addon`、`flag`、`from` | 解析任务，返回 `meta`、`task_files` |
| `POST /v6/offline_task/add` | `UserTask`：包含 `url`、`name`、`save_path`、`task_identity`、`addon`、`flag`、`callbacks`、`ignore_files` 等 | 添加离线任务 |
| `POST /v6/offline_task/list` | `OfflineTaskListRequest.list_info` | 查询任务及状态、进度、字节数、错误信息 |

- [SDK offline/svc.go:20](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/offline/svc.go#L20)
- [SDK offline/model.go:45](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/offline/model.go#L45)
- [SDK 示例](https://github.com/halalcloud/golang-sdk-lite/blob/main/examples/offline/main.go)
- [SDK 签名客户端](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/apiclient/client.go)

`UserTask` 的状态字段包括 `status`、`code`、`message`、`progress`、`bytes_total`、`bytes_processed`。当前 SDK 没给出状态数字枚举，不能编造某个值代表完成。公开 proto 额外有 `save_identity`，SDK 当前模型未含该字段，不应依赖未经验证的新字段。

因此“夸克兼容 MP4 URL → D2 离线导入 → D2 独立播放文件”**有实际 API 定义支持**，不是由 OpenList 复制功能推测出来。但还没有执行这个链路。

**待验证事项**：账号是否有离线导入权限/额度；普通 HTTP(S) 直链能否作为源；是否支持所需重定向、签名和源请求头；D2 拉取时是否受到夸克 IP 绑定或有效期限制；文件名称及目标目录如何确定；导入是否得到完整媒体；失败时是否保留部分文件。`svc.go` 注释“目前只支持 HTTP/HTTPS”说的是 **callbacks 回调 URL**，不能把它当成源 URL 协议支持的证据。

OpenList 当前 `HalalCloudOpen` 驱动暴露了 `Put`，没有在已检查的驱动方法中接入 `PutURL` 或上述离线任务服务。不能把 OpenList `/api/fs/copy`、离线下载工具或普通上传接口直接当成 D2 原生 URL 导入。服务端支持与驱动已接入是两件事。

此路径验收条件：使用独立播放目标名，先完成单个作品的受控验证；离线任务完成后确认 D2 文件大小、视频/音轨编码、完整时长和真正浏览器首帧；确认原片文件未变化；再把该作品的播放索引切换到新对象。任务“已接收”不等于成功，失败需保留明确错误，不能静默切回不兼容文件。

## 独立计算节点方案

方案：项目本机只读取/保存对象标识、路径、编码和任务进度。独立节点通过云盘原始地址或直接 SDK 读取原片，生成一个浏览器兼容的播放文件及需要的封面，再直接用 D2 上传 API 写入独立派生路径。不能让节点从项目的视频代理 URL 取原片，也不能通过本机 OpenList 接收视频上传。

必要条件：可用且获授权的独立计算节点；云盘源 URL 对该节点有效并支持需要的 Range/鉴权；节点具备 FFmpeg H.264/AAC 编码能力、任务空间及上传权限；D2 有派生文件空间；逐作品记录原片标识、编码检查和完成状态。已有兼容文件直接复用，不重复编码。

D2 上游上传实现为：`CreateUploadTask`（`/v6/userfile/create_upload_task`，含路径、文件大小）→取得块大小、任务标识和上传地址→按块计算 CID 和上传→提交块列表。驱动自身逐块读流，存在直接 SDK 上传实现基础；**不意味着经任意 OpenList API 上传都不缓存**。

- [D2 上传源码](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/halalcloud_upload.go)
- [D2 上传任务 SDK](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/userfile/svc.go)

普通 MP4 `faststart` 要在结束时把索引移到开头，FFmpeg 文档说明它是第二遍处理；它与 fragmented output 不兼容。推荐在独立节点完成派生文件后确定长度再上传。不要承诺“任意 ffmpeg 管道直接上传既是普通 faststart MP4，又无临时存储”，这两个条件没有相应实现证据。

- [FFmpeg MOV/MP4 fragmentation 和 faststart](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv)

验收条件：在独立节点检测 H.264/AAC/yuv420p、完整时长及 MP4 前置索引；直接上传 D2 后复检；页面验证首帧和拖动；只有验收后才修改播放引用。原片保留，本次修复任务不得向项目本机的视频目录、上传缓存或下载任务写入历史视频；后台正常增量监控另行记录。

## 必须避免的本机缓存路径

OpenList 当前上游代码中：

- `server/handles/fsup.go` 读取 `As-Task`，从 `Content-Length` 或 `X-File-Size` 得到长度；没提供时大小为 -1。
- `internal/fs/put.go` 的 `putAsTask()` 对 `NeedStore()` 为真的流调用 `CacheFullAndWriter()`。
- `internal/stream/stream.go` 的 `NeedStore()` 返回 `WebPutAsTask`；全量缓存可能在内存或临时文件。
- `internal/op/fs.go:662` 遇到未知文件大小调用全量缓存；`stream.go` 对未知长度的非空流明确调用 `CreateTempFile`，将完整输入写到磁盘再算长度。

所以 `ffmpeg → 本机 OpenList（未知长度）→ D2` 会触发本机磁盘缓存；`As-Task=true` 也先全量缓存。即使已知长度的同步流路径看上去逐块发送，也仍由本机中转完整视频，且本次没验证反向代理缓存配置。修复方案应绕开本机的整个媒体上传/下载链路，不能只更改临时目录就宣称符合要求。

- [上传 handler](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/fsup.go)
- [上传任务缓存](https://github.com/OpenListTeam/OpenList/blob/main/internal/fs/put.go)
- [未知长度缓存触发位置](https://github.com/OpenListTeam/OpenList/blob/main/internal/op/fs.go#L662)
- [实际缓存实现](https://github.com/OpenListTeam/OpenList/blob/main/internal/stream/stream.go)

## 证据边界

研究读取的是 2026-09-13 可访问的公开上游 main 源码，不代表部署二进制与之完全一致。D2 官方 SDK 链接的飞书文档本次未能抓取；字段含义不明的部分已明确保留为未知。未读取或输出账号秘密，未执行任何媒体读写、云端导入或转换。本次唯一写入文件是本笔记。
