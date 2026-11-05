import AVFoundation
import UIKit

final class PaddedLabel: UILabel {
    var insets = UIEdgeInsets(top: 3, left: 8, bottom: 3, right: 8)

    override func drawText(in rect: CGRect) {
        super.drawText(in: rect.inset(by: insets))
    }

    override var intrinsicContentSize: CGSize {
        let size = super.intrinsicContentSize
        return CGSize(width: size.width + insets.left + insets.right, height: size.height + insets.top + insets.bottom)
    }
}

/// 一页 = 一条作品：画面 + 封面 + 左下文案。固定浮层（右侧按钮、进度条）在 FeedViewController 上。
final class FeedPageView: UIView {

    enum Mode {
        case idle
        case preload
        case active
    }

    private let playerLayerView = PlayerLayerView()
    private let coverView = UIImageView()
    private let scrim = UIView()
    private let scrimLayer = CAGradientLayer()

    private let authorLabel = UILabel()
    private let descLabel = UILabel()
    private let scorePill = PaddedLabel()
    private let tagsLabel = UILabel()
    private let messageLabel = UILabel()
    private let pauseIcon = UIImageView()

    private(set) var player = FeedPlayer()
    private(set) var item: FeedItem?
    private var mode: Mode = .idle
    private var proxyURL: URL?
    private var retriedWithProxy = false
    private var imageTask: Task<Void, Never>?
    private var loadToken = UUID()

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        clipsToBounds = true
        setupViews()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) 未实现") }

    private func setupViews() {
        playerLayerView.translatesAutoresizingMaskIntoConstraints = false
        playerLayerView.playerLayer.player = player.player
        player.onGravity = { [weak self] gravity in
            self?.playerLayerView.playerLayer.videoGravity = gravity
        }
        addSubview(playerLayerView)

        coverView.translatesAutoresizingMaskIntoConstraints = false
        coverView.contentMode = .scaleAspectFill
        coverView.clipsToBounds = true
        coverView.backgroundColor = .black
        addSubview(coverView)

        scrim.translatesAutoresizingMaskIntoConstraints = false
        scrimLayer.colors = [UIColor.clear.cgColor, UIColor.black.withAlphaComponent(0.65).cgColor]
        scrimLayer.locations = [0, 1]
        scrim.layer.addSublayer(scrimLayer)
        addSubview(scrim)

        authorLabel.font = .systemFont(ofSize: 17, weight: .semibold)
        authorLabel.textColor = .white
        authorLabel.numberOfLines = 1

        descLabel.font = .systemFont(ofSize: 14)
        descLabel.textColor = UIColor.white.withAlphaComponent(0.92)
        descLabel.numberOfLines = 3

        scorePill.font = .systemFont(ofSize: 12, weight: .semibold)
        scorePill.textColor = .white
        scorePill.backgroundColor = UIColor.systemPink.withAlphaComponent(0.85)
        scorePill.layer.cornerRadius = 9
        scorePill.clipsToBounds = true
        scorePill.isHidden = true

        tagsLabel.font = .systemFont(ofSize: 12)
        tagsLabel.textColor = UIColor.white.withAlphaComponent(0.75)
        tagsLabel.numberOfLines = 1

        let textStack = UIStackView(arrangedSubviews: [authorLabel, descLabel, scorePill, tagsLabel])
        textStack.axis = .vertical
        textStack.alignment = .leading
        textStack.spacing = 6
        textStack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(textStack)

        messageLabel.font = .systemFont(ofSize: 15)
        messageLabel.textColor = UIColor.white.withAlphaComponent(0.8)
        messageLabel.numberOfLines = 0
        messageLabel.textAlignment = .center
        messageLabel.isHidden = true
        messageLabel.translatesAutoresizingMaskIntoConstraints = false
        addSubview(messageLabel)

        pauseIcon.image = UIImage(systemName: "play.fill")
        pauseIcon.tintColor = UIColor.white.withAlphaComponent(0.85)
        pauseIcon.contentMode = .scaleAspectFit
        pauseIcon.isHidden = true
        pauseIcon.translatesAutoresizingMaskIntoConstraints = false
        addSubview(pauseIcon)

        NSLayoutConstraint.activate([
            playerLayerView.leadingAnchor.constraint(equalTo: leadingAnchor),
            playerLayerView.trailingAnchor.constraint(equalTo: trailingAnchor),
            playerLayerView.topAnchor.constraint(equalTo: topAnchor),
            playerLayerView.bottomAnchor.constraint(equalTo: bottomAnchor),

            coverView.leadingAnchor.constraint(equalTo: leadingAnchor),
            coverView.trailingAnchor.constraint(equalTo: trailingAnchor),
            coverView.topAnchor.constraint(equalTo: topAnchor),
            coverView.bottomAnchor.constraint(equalTo: bottomAnchor),

            scrim.leadingAnchor.constraint(equalTo: leadingAnchor),
            scrim.trailingAnchor.constraint(equalTo: trailingAnchor),
            scrim.bottomAnchor.constraint(equalTo: bottomAnchor),
            scrim.heightAnchor.constraint(equalTo: heightAnchor, multiplier: 0.42),

            textStack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 16),
            textStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -88),
            textStack.bottomAnchor.constraint(equalTo: safeAreaLayoutGuide.bottomAnchor, constant: -36),

            messageLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            messageLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
            messageLabel.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, multiplier: 0.7),

            pauseIcon.centerXAnchor.constraint(equalTo: centerXAnchor),
            pauseIcon.centerYAnchor.constraint(equalTo: centerYAnchor),
            pauseIcon.widthAnchor.constraint(equalToConstant: 64),
            pauseIcon.heightAnchor.constraint(equalToConstant: 64),
        ])
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        scrimLayer.frame = scrim.bounds
        if let size = player.player.currentItem?.presentationSize, size != .zero {
            player.updateGravity(for: size, in: bounds.size)
        }
    }

    // MARK: - 内容

    func configure(item: FeedItem?, role: String?, api: APIClient) {
        guard let item else {
            clear()
            return
        }
        if self.item?.key == item.key {
            applyMode()
            return
        }
        self.item = item
        loadToken = UUID()
        retriedWithProxy = false
        messageLabel.isHidden = true
        proxyURL = api.proxyMediaURL(item)
        player.stop()
        coverView.isHidden = false

        let highlights = ItemHighlights(filter: item.filter)
        let guest = (role ?? "guest") != "admin" && (role ?? "guest") != "key"
        authorLabel.text = item.nickname.map { "@\($0)" } ?? (guest ? "游客模式" : "未命名作者")
        authorLabel.isHidden = false
        let text = (item.desc ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        descLabel.text = text.isEmpty ? nil : text
        descLabel.isHidden = text.isEmpty
        if let score = highlights.score {
            scorePill.text = "擦边 \(score)"
            scorePill.isHidden = false
        } else {
            scorePill.isHidden = true
        }
        tagsLabel.text = highlights.tags.isEmpty ? nil : highlights.tags.joined(separator: " · ")
        tagsLabel.isHidden = highlights.tags.isEmpty

        loadCover(api: api, item: item)
        applyMode()
    }

    private func loadCover(api: APIClient, item: FeedItem) {
        imageTask?.cancel()
        coverView.image = nil
        guard let url = api.coverURL(item) else { return }
        if let cached = ImageLoader.shared.cachedImage(for: url) {
            coverView.image = cached
            return
        }
        let token = loadToken
        let headers = api.authHeaders
        imageTask = Task { [weak self] in
            let image = await ImageLoader.shared.load(url: url, headers: headers)
            guard let self, self.loadToken == token, let image else { return }
            self.coverView.image = image
        }
    }

    func clear() {
        item = nil
        imageTask?.cancel()
        player.stop()
        coverView.image = nil
        coverView.isHidden = false
        messageLabel.isHidden = true
        pauseIcon.isHidden = true
        authorLabel.isHidden = true
        descLabel.isHidden = true
        scorePill.isHidden = true
        tagsLabel.isHidden = true
    }

    func showMessage(_ text: String) {
        item = nil
        imageTask?.cancel()
        player.stop()
        coverView.isHidden = true
        messageLabel.text = text
        messageLabel.isHidden = false
        pauseIcon.isHidden = true
        authorLabel.isHidden = true
        descLabel.isHidden = true
        scorePill.isHidden = true
        tagsLabel.isHidden = true
    }

    // MARK: - 播放

    func setMode(_ mode: Mode) {
        self.mode = mode
        applyMode()
    }

    /// 直链解析完成后由控制器调用；url 为 nil 表示走代理
    func setVideo(url: URL, isDirect: Bool, proxyURL: URL?) {
        if player.currentURL == url {
            self.proxyURL = proxyURL
            applyMode()
            return
        }
        player.setItem(url: url, direct: isDirect)
        self.proxyURL = proxyURL
        applyMode()
    }

    private func applyMode() {
        guard item != nil, player.currentURL != nil else { return }
        switch mode {
        case .active:
            player.activate(muted: PlaybackSettings.muted)
            playerLayerView.playerLayer.videoGravity = .resizeAspectFill
            pauseIcon.isHidden = true
        case .preload:
            player.preload(muted: true)
        case .idle:
            player.pause()
        }
    }

    /// 由控制器的定时器驱动：出画面就撤封面、失败就回退代理、暂停时显示播放图标
    func tick() {
        guard mode == .active else { return }
        if player.isPlaying {
            if !coverView.isHidden { coverView.isHidden = true }
            pauseIcon.isHidden = true
        } else {
            pauseIcon.isHidden = false
        }
        if player.didFail {
            if player.isDirect, !retriedWithProxy, let proxyURL {
                retriedWithProxy = true
                AppLog.info("直链播放失败，回落服务端代理：\(item?.awemeId ?? "?")")
                player.setItem(url: proxyURL, direct: false)
                applyMode()
            } else {
                pauseIcon.isHidden = true
                showMessage("这条作品播放失败了，上滑看下一个")
            }
        }
    }
}
