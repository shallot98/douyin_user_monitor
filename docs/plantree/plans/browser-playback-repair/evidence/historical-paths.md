# 三个网页 404 样本的云盘路径核查

此文件保留修复前的故障证据；后续路径映射及网页验收见[执行证据](execution.md)。

检查时间：2026-09-13T14:32:29.349485+08:00。

三个样本的原文件都仍存在于 D2 和夸克的 `sec_<sec_user_id>` 历史目录。网页当前只查昵称目录，返回 404 是路径候选遗漏；这三个样本不能归类为“已证实云盘原片缺失”。结论置信度高（同一时段的 12 次 OpenList 实际元数据请求与现有路径生成代码相互印证）。

## 范围与方法

- 输入：`runtime/media-source-errors-recheck-20260913.json` 中“八月”“艾米粒儿”“给我一口甜”的连续 404 样本；从当前 `data/monitor_users.json` 读取对应作品记录。
- 历史别名依据：`runtime/archive-migration.json` 的 `done` 同时包含三个样本的昵称路径和 `sec_` 路径；`runtime/rename-25-topdirs.json` 还明确记录“艾米粒儿”“给我一口甜”改到对应 `sec_` 目录且状态为 `ok`。没有凭昵称近似匹配或猜测目录。
- 调用项目现有 `OpenListClient._fs_get`，即 `POST /api/fs/get`，仅查 3 个样本 × 2 个配置读取盘 × 2 个明确路径，共 12 个精确对象。未发起全盘、用户目录递归扫描，也没有读取任何视频 Range。
- 鉴权在内存中由现有客户端读取；没有访问返回的 `raw_url`，报告只保留文件名、大小、时间、驱动名。视频内容读取量为 0 B；没有视频下载、转码、云文件改名/复制、挂载配置更改、生产代码或用户记录更改。
- 原生 `python` 首次 import 因缺少 `aiofiles` 退出，尚未请求云盘；改用项目 `.venv/bin/python` 后完成全部检查。这是诊断环境差异，没有改依赖。

## 实际结果

D2 基目录为 `/d2/vps/抖音视频`，夸克基目录为 `/kuake/抖音视频`。

| 用户 | 作品 ID | 下载记录大小（B） | 昵称路径：D2 / 夸克 | sec_ 历史路径：D2 / 夸克 |
|---|---|---:|---|---|
| 八月 | `7457125265412590867` | 1,687,542 | 未找到 / 未找到 | 已找到 / 已找到 |
| 艾米粒儿 | `7655749752475440762` | 1,867,686 | 未找到 / 未找到 | 已找到 / 已找到 |
| 给我一口甜 | `7679708915500581510` | 6,365,501 | 未找到 / 未找到 | 已找到 / 已找到 |

昵称路径的 6 个请求均返回 `object not found`；历史路径的 6 个请求均返回文件对象，`is_dir=false`，大小与各自下载记录相同。文件名本身无需改变，“给我一口甜”样本原有作品 ID 也完整保留。

### 八月

- 用户 ID：`70580590-c1e0-4671-a3bc-7bf5e116cbce`。
- 作品 ID：`7457125265412590867`。
- 数据库相对路径：`八月/20250107_185449_如果我今天是诚心诚意的 那么明天后悔又有什么关系呢.mp4`。
- 已确认的历史相对路径：`sec_MS4wLjABAAAANVo1hew19RtPMx_LUg71FhJa9smqx84ZnXuXLi_3Ptjn8q4sCdkFLTHpipYwuwbi/20250107_185449_如果我今天是诚心诚意的 那么明天后悔又有什么关系呢.mp4`。
- `/d2/vps/抖音视频/sec_MS4wLjABAAAANVo1hew19RtPMx_LUg71FhJa9smqx84ZnXuXLi_3Ptjn8q4sCdkFLTHpipYwuwbi/20250107_185449_如果我今天是诚心诚意的 那么明天后悔又有什么关系呢.mp4`：1,687,542 B，provider=`HalalCloudOpen`，modified=`2026-09-07T07:42:28.986Z`。
- `/kuake/抖音视频/sec_MS4wLjABAAAANVo1hew19RtPMx_LUg71FhJa9smqx84ZnXuXLi_3Ptjn8q4sCdkFLTHpipYwuwbi/20250107_185449_如果我今天是诚心诚意的 那么明天后悔又有什么关系呢.mp4`：1,687,542 B，provider=`Quark`，modified=`2026-03-23T08:26:47.78Z`。

### 艾米粒儿

- 用户 ID：`4bf23d9e-4e5b-41e7-8ad6-7a29b1bff6ec`。
- 作品 ID：`7655749752475440762`。
- 数据库相对路径：`艾米粒儿/20260627_005903_#可爱特效妆容.mp4`。
- 已确认的历史相对路径：`sec_MS4wLjABAAAAbWEG-T1TDAZkpTshIISJzI1Q19nleWoINq3xz3uf6gzgqsokEpabE3repaymhyqN/20260627_005903_#可爱特效妆容.mp4`。
- `/d2/vps/抖音视频/sec_MS4wLjABAAAAbWEG-T1TDAZkpTshIISJzI1Q19nleWoINq3xz3uf6gzgqsokEpabE3repaymhyqN/20260627_005903_#可爱特效妆容.mp4`：1,867,686 B，provider=`HalalCloudOpen`，modified=`2026-09-05T11:06:59.104Z`。
- `/kuake/抖音视频/sec_MS4wLjABAAAAbWEG-T1TDAZkpTshIISJzI1Q19nleWoINq3xz3uf6gzgqsokEpabE3repaymhyqN/20260627_005903_#可爱特效妆容.mp4`：1,867,686 B，provider=`Quark`，modified=`2026-07-06T02:29:57.828Z`。

### 给我一口甜

- 用户 ID：`e363d51e-9a0b-4b8c-8dee-c2b4da5cf257`。
- 作品 ID：`7679708915500581510`。
- 数据库相对路径：`给我一口甜/20260830_143250_7679708915500581510_你不想我了吗 我的可爱 我的依赖 我的撒娇 你都忘了吗#ootd女生穿搭御姐 #黑色系御姐穿搭 .mp4`。
- 已确认的历史相对路径：`sec_MS4wLjABAAAAuL-qDFw9AiP9I5auw0ZzMlA04FcUZ8aEtfCnSzACpHQ/20260830_143250_7679708915500581510_你不想我了吗 我的可爱 我的依赖 我的撒娇 你都忘了吗#ootd女生穿搭御姐 #黑色系御姐穿搭 .mp4`。
- `/d2/vps/抖音视频/sec_MS4wLjABAAAAuL-qDFw9AiP9I5auw0ZzMlA04FcUZ8aEtfCnSzACpHQ/20260830_143250_7679708915500581510_你不想我了吗 我的可爱 我的依赖 我的撒娇 你都忘了吗#ootd女生穿搭御姐 #黑色系御姐穿搭 .mp4`：6,365,501 B，provider=`HalalCloudOpen`，modified=`2026-09-05T11:55:24.62Z`。
- `/kuake/抖音视频/sec_MS4wLjABAAAAuL-qDFw9AiP9I5auw0ZzMlA04FcUZ8aEtfCnSzACpHQ/20260830_143250_7679708915500581510_你不想我了吗 我的可爱 我的依赖 我的撒娇 你都忘了吗#ootd女生穿搭御姐 #黑色系御姐穿搭 .mp4`：6,365,501 B，provider=`Quark`，modified=`2026-08-30T17:39:08.786Z`。

## 原因与现有覆盖范围

- [openlist_client.py:67](/root/douyin_user_monitor/douyin_user_monitor/monitor/openlist_client.py:67) 的 `remote_paths_for` 根据记录路径生成主盘原名、两盘清理名、备份盘原名，并去重。运行同一函数可见这三个样本都只生成昵称下的 D2/夸克两个路径，没有 `sec_` 路径。
- [path_sanitize.py:145](/root/douyin_user_monitor/douyin_user_monitor/monitor/path_sanitize.py:145) 在目录清理结果非空时直接保留；仅当首段清理为空时使用 `fallback_dir_for(sec_user_id)`（第 155 行）。因此普通中文昵称即便已有 `sec_` 迁移历史，也不会通过这条规则找到历史文件。
- 写入/迁移与读取的契约不一致：[migrate_archive_to_primary.py:418](/root/douyin_user_monitor/scripts/migrate_archive_to_primary.py:418) 会在“清理为空”**或**“目录进入网盘拒收名单”时把顶层目录改为 `sec_<sid>`。后者包括非空中文目录，读取侧遗漏了这种情况。
- [monitor.py:586](/root/douyin_user_monitor/douyin_user_monitor/api/monitor.py:586) 已把准确的 `sec_user_id` 传给客户端，不需要新增账号识别请求；第 595 行把所有候选失败映射为网页 404。
- 已有清理规则覆盖 emoji/特殊字符移除和空白合并，及“清理后为空”的 sec_ 目录；当前没有利用迁移别名表，也不在相关目录内按作品 ID 搜索。三个本次样本的精确历史路径已直接找到，因此没有扩大搜索，也没有无必要地尝试重新拼装文件名。

## 可评估的修复方案

1. 优先修复云端路径解析：为账号存在的历史 `sec_<sec_user_id>` 目录补充明确的读取候选，或将已验证的迁移别名写入独立归档路径映射。复用已有 `fallback_dir_for` 与文件名清理逻辑；保留记录原名并覆盖历史原名/清理名，确保 **D2 的所有候选在夸克之前**。不修改原片、不重新下载、不改全局夸克转码开关。
2. 若选择范围最小的落地方式，先对已确认历史别名的账号生成可审阅映射，记录对应的用户 ID、作品 ID、盘、真实路径、size 和核查时间，再由网页读取；不要把无法证明的近似文件名自动当作匹配成功。
3. 对其余 404 作品采取同样的分级核对：记录精确路径 → 已知迁移/历史目录别名 → 只在关联目录内按作品 ID 查找。含作品 ID 的新文件可精确关联；老文件不含 ID 时必须结合记录时间、文件名和元信息，不单靠相似标题。未找到的范围要明示，不能据一次 404 断言两盘全部丢失。
4. 路径修复后仍要重新做编码/Range/浏览器首帧验收；“对象存在”只说明可以重新定位文件，并不能证明浏览器一定能解码。若该原片是 HEVC，路径问题解决后仍需要独立的网页兼容播放方案。当前三个文件的编码与网页首帧**没验证**。
5. 建议回归检查覆盖：普通中文昵称迁移到 sec_、纯 emoji 昵称、原名与清理名并存、优先选择 D2、候选全部不存在时继续明确报错。只做方案，当前没有实施修复。

## 结论边界

- 这 3 个样本全部定位到了两盘对象；不能把此结果扩展为“此前 31 个账号的所有 404 都相同”。
- 文件大小一致不是内容哈希一致证明，本次没有读取视频内容进行完整性或编码核验。
- 未进行目录递归或全盘扫描；检查范围外还有其他历史路径的可能性不在本次结论内。
