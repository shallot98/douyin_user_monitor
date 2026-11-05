# 刷视频 iOS App（WKWebView 外壳）

把网页版竖滑刷视频页装进一个原生壳，做成可安装的 IPA。
壳只做网页做不到的事，业务逻辑全部留在服务端网页里（改网页不用重装 App）：

- **免手势自动播放（含声音）**：`mediaTypesRequiringUserActionForPlayback = []`，
  网页里的 `video.play()` 不再需要用户点一下，滑动即播
- **静音拨片不拦声音**：原生音频会话用 `.playback` 类别（和抖音一致）
- **全屏沉浸**：隐藏状态栏、自动隐藏 Home 指示条、底部上滑不被系统手势抢走、锁竖屏、禁缩放/禁回弹
- **登录态持久**：WKWebView 默认数据存储，登录一次 Cookie 落盘（记住我 30 天 / 游客 30 天）
- **播放中不自动锁屏**：网页通过 `window.webkit.messageHandlers.dymon.postMessage({type:"playing", value:true})` 通知
- **摇一摇菜单**：刷新 / 回到刷视频 / 打开作品库 / 回登录页 / 复制地址
- 站外链接（点描述里的 http 链接）交给 Safari 打开

## 工程结构

```
ios/
  project.yml                 XcodeGen 工程定义（仓库不提交 .xcodeproj）
  DyMon/
    AppDelegate.swift         音频会话 + 窗口
    FeedViewController.swift  WKWebView 壳本体
    Info.plist                竖屏 / 隐藏状态栏 / 深色 / 启动屏
    Assets.xcassets           AppIcon + 启动背景色
  tools/make-icon.sh          用 ImageMagick 重新生成图标
.github/workflows/ios-ipa.yml macOS CI：xcodegen -> xcodebuild -> 临时签名 -> IPA artifact
```

## 构建

CI（推荐，仓库公开所以 macOS runner 免费）：
push 到 `feat/ios-app` 或手动 `workflow_dispatch` 触发 `iOS IPA`，
产物在 Actions artifact `DyMon-ipa`（`DyMon-unsigned.ipa`）。

本机需要 macOS + Xcode：

```bash
brew install xcodegen
cd ios && xcodegen generate
xcodebuild -project DyMon.xcodeproj -scheme DyMon -configuration Release \
  -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
```

## 安装（越狱 / 侧载自选）

- **TrollStore**：手机上直接打开 IPA → 用 TrollStore 安装（iOS ≤17.0 的 CoreTrust 机型）
- **deb（RootHide/rootless 越狱）**：`dpkg -i DyMon_*.deb && uicache -a`
- **自签侧载**：AltStore / Sideloadly 等导入 IPA 用 Apple ID 重签

首次打开会落在登录页：输入账号或点「游客进入」，之后不再需要。

## 改什么在哪里

| 想改 | 位置 |
| --- | --- |
| 首页地址 / 站内域名 | `FeedViewController.swift` 的 `Conf` |
| 应用名、图标 | `Info.plist` 的 `CFBundleDisplayName`、`tools/make-icon.sh` |
| Bundle ID / 版本号 | `project.yml` 的 `settings` |
| App 模式网页行为 | 服务端 `web/swipe.html` 里 `/\bDyMonApp\b/` 判断处 |

网页侧通过 UA 里的 `DyMonApp` 标记识别 App 模式（默认开声音、加深封面预载）。
