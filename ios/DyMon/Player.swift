import AVFoundation
import UIKit

final class PlayerLayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }

    override init(frame: CGRect) {
        super.init(frame: frame)
        playerLayer.videoGravity = .resizeAspectFill
        playerLayer.backgroundColor = UIColor.black.cgColor
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) 未实现") }
}

/// 一条作品一个 AVPlayer：换源、循环、静音预热、激活即播
final class FeedPlayer {
    let player = AVPlayer()

    private(set) var currentURL: URL?
    private(set) var isDirect = false
    private var loopObserver: NSObjectProtocol?
    private var hasStarted = false

    init() {
        player.actionAtItemEnd = .none
        player.automaticallyWaitsToMinimizeStalling = false
        player.volume = 1
    }

    deinit {
        if let loopObserver { NotificationCenter.default.removeObserver(loopObserver) }
    }

    @discardableResult
    func setItem(url: URL, direct: Bool) -> AVPlayerItem {
        // 归档视频都是 mp4；网关/代理可能回 application/octet-stream，这里显式告诉 AVFoundation 类型
        let asset = AVURLAsset(url: url, options: ["AVURLAssetOutOfBandMIMETypeKey": "video/mp4"])
        let item = AVPlayerItem(asset: asset)
        item.preferredForwardBufferDuration = 1.5
        player.replaceCurrentItem(with: item)
        currentURL = url
        isDirect = direct
        hasStarted = false
        if let loopObserver { NotificationCenter.default.removeObserver(loopObserver) }
        loopObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.player.seek(to: .zero)
            self.player.play()
        }
        return item
    }

    /// 后台静音跑一小会儿，把数据灌进缓冲；滑到它时能直接出画面
    func preload(muted: Bool) {
        player.isMuted = muted
        player.play()
        let token = currentURL
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self, self.currentURL == token, self.player.isMuted else { return }
            self.player.pause()
        }
    }

    func activate(muted: Bool) {
        player.isMuted = muted
        if !hasStarted {
            // 预热时可能已经静音跑了几百毫秒，正式播从头开始
            hasStarted = true
            player.seek(to: .zero)
        }
        player.play()
    }

    func pause() {
        player.pause()
    }

    func stop() {
        player.pause()
        if let loopObserver {
            NotificationCenter.default.removeObserver(loopObserver)
            self.loopObserver = nil
        }
        player.replaceCurrentItem(with: nil)
        currentURL = nil
    }

    var isPlaying: Bool { player.timeControlStatus == .playing && player.rate > 0 }

    var isBuffering: Bool { player.timeControlStatus == .waitingToPlayAtSpecifiedRate }

    var didFail: Bool { player.currentItem?.status == .failed }

    /// 当前进度 0...1；未知返回 0
    var progress: Double {
        guard let item = player.currentItem else { return 0 }
        let duration = item.duration.seconds
        let current = player.currentTime().seconds
        guard duration.isFinite, duration > 0, current.isFinite else { return 0 }
        return max(0, min(1, current / duration))
    }

    /// 视频比例接近竖屏就铺满，否则完整显示（横屏作品不裁掉内容）
    func updateGravity(for size: CGSize, in bounds: CGSize) {
        guard size.width > 0, size.height > 0, bounds.width > 0, bounds.height > 0 else { return }
        let videoRatio = size.height / size.width
        let frameRatio = bounds.height / bounds.width
        let shouldFill = abs(videoRatio - frameRatio) <= 0.35
        onGravity?(shouldFill ? .resizeAspectFill : .resizeAspect)
    }

    var onGravity: ((AVLayerVideoGravity) -> Void)?
}

/// 播放设置：静音偏好持久化（默认有声，和抖音一致）
enum PlaybackSettings {
    private static let key = "dymon.muted"

    static var muted: Bool {
        get { UserDefaults.standard.bool(forKey: key) }
        set { UserDefaults.standard.set(newValue, forKey: key) }
    }
}
