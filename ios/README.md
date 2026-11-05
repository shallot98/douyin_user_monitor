# 刷视频 iOS App（原生）

原生 iOS 刷短视频客户端：原生 UI + AVPlayer 播放，数据全部来自本项目的接口
（`/api/monitor/library` 选片、`/archive/playback-urls` 取国内 CDN 直链、失败回落服务端代理）。

## 是什么

- **竖滑 feed**：三页轮转的翻页器（不是 UICollectionView），每个槽固定一个 AVPlayer。
  翻到下一页之前，下一条已经静音预热过，落页直接出画面。
- **原生播放**：AVPlayer + `AVAsset` 提前缓冲、`automaticallyWaitsToMinimizeStalling = false`、
  循环播放、失败自动从「国内直链」回落到「服务端代理」。
- **像抖音的部分**：全屏沉浸（隐藏状态栏 / Home 指示条、底部上滑不被系统抢走、锁竖屏）、
  打开即播且有声音（静音拨片不拦）、点画面暂停、底部细进度条、右侧头像/复制链接/声音、
  上滑到底自动换一批随机作品。
- **登录**：账号密码（记住 30 天）或游客进入（只读、随机视频）；也可以直接填 API Key 免登录。
- **筛选**：排序（随机/最新/擦边分/身材分/…）、擦边分下限、关键词，落本地，改完立即换一批。
- **兜底**：右上角 ⋯ 里有「网页版刷视频」和「作品库 / 统计」，用 WKWebView 打开完整站点。

## 工程结构

```
ios/
  project.yml                 XcodeGen 工程定义（仓库不提交 .xcodeproj）
  DyMon/
    AppDelegate.swift         音频会话（.playback，静音拨片也出声）+ 窗口
    FeedViewController.swift  竖滑 feed：三槽轮转、翻页手势、分页加载、菜单
    FeedPageView.swift        单页：画面 + 封面 + 左下文案（作者/描述/擦边分/标签）
    Player.swift              AVPlayer 封装（换源/循环/静音预热/进度）+ 播放器视图
    DirectLinkCache.swift     直链缓存（按用户批量解析、TTL、失败回落代理）
    APIClient.swift           登录/游客/作品库/直链/媒体地址，会话 Cookie 持久化
    Models.swift              作品条目、作品库分页、宽松 JSON（读 AI 标注结论）
    Support.swift             服务器地址与凭据存储、图片缓存、小工具
    LoginViewController.swift 登录页（账号密码 / 游客 / API Key）
    FilterViewController.swift 筛选（排序 / 擦边分下限 / 关键词）
    WebViewController.swift   完整站点兜底（注入会话 Cookie 后打开）
    Info.plist                竖屏 / 隐藏状态栏 / 深色 / 允许明文（自建服务器）
    Assets.xcassets           AppIcon + 启动背景色
  tools/make-icon.sh          用 ImageMagick 重新生成图标
.github/workflows/ios-ipa.yml macOS CI：xcodegen -> xcodebuild -> 临时签名 -> IPA artifact
```

## 构建

CI（推荐，仓库公开所以 macOS runner 免费）：push 到 `feat/ios-app`，或手动
`workflow_dispatch` 触发 `iOS IPA`，产物是 Actions artifact `DyMon-ipa`。

本机需要 macOS + Xcode：

```bash
brew install xcodegen
cd ios && xcodegen generate
xcodebuild -project DyMon.xcodeproj -scheme DyMon -configuration Release \
  -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
```

## 安装

- **TrollStore**：手机上直接打开 IPA → 用 TrollStore 安装（iOS ≤17.0 的 CoreTrust 机型）
- **deb（RootHide/rootless 越狱）**：`dpkg -i DyMon_*.deb && uicache -a`，deb 可以从 IPA 现场打：

```bash
mkdir -p debroot/var/jb/Applications debroot/DEBIAN
unzip -o DyMon-unsigned.ipa -d ipa
cp -R ipa/Payload/DyMon.app debroot/var/jb/Applications/
cat > debroot/DEBIAN/control <<'CTL'
Package: de.cenai.dymon
Name: 刷视频
Version: 0.2.0
Architecture: iphoneos-arm64
Description: 原生刷短视频客户端，数据来自自建监控站点。
Maintainer: shallot98
Section: Applications
Priority: optional
CTL
dpkg-deb -b --root-owner-group debroot DyMon_0.2.0_iphoneos-arm64.deb
```

- **自签侧载**：AltStore / Sideloadly 等导入 IPA 用 Apple ID 重签

首次打开是登录页：填服务器地址（默认 `https://dy.cenai.de`）+ 账号密码，或点「游客进入」。

## 改什么在哪里

| 想改 | 位置 |
| --- | --- |
| 默认服务器地址 | `Support.swift` 的 `ServerStore.defaultBaseURL` |
| App 名、图标 | `Info.plist` 的 `CFBundleDisplayName`、`tools/make-icon.sh` |
| Bundle ID / 版本号 | `project.yml` 的 `settings` |
| 预载深度、翻页阈值 | `FeedViewController.swift` 顶部的常量、`FeedPageView.swift` |
| 画面铺满策略 | `Player.swift` 的 `updateGravity`（竖屏铺满、横屏完整显示） |
| 兜底网页入口 | `FeedViewController.showMenu` 里的路径 |
