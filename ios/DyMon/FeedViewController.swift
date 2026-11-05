import AVFoundation
import UIKit

/// 原生刷视频页：三页轮转的竖滑 feed。自己实现翻页（不用 UICollectionView），
/// 每个槽固定一个播放器：划过去之前下一条已经在缓冲，落页即出画面。
final class FeedViewController: UIViewController {

    // MARK: - 依赖与状态

    private let api = APIClient()
    private lazy var cache = DirectLinkCache(api: api)

    private var items: [FeedItem] = []
    private var currentIndex = 0
    private var config = FeedConfig.load()
    private var seed: Int?
    private var page = 1
    private var totalPages = 1
    private var isLoadingPage = false
    private var sessionToken = 0
    private var didPromptLogin = false
    private var userPaused = false

    // MARK: - 视图

    private let pagesContainer = UIView()
    private var pages: [FeedPageView] = []
    private let guestChip = PaddedLabel()
    private let menuButton = UIButton(type: .system)
    private var avatarView = UIImageView()
    private let muteButton = UIButton(type: .system)
    private let copyButton = UIButton(type: .system)
    private let progressTrack = UIView()
    private let progressBar = UIView()
    private var progressWidth: NSLayoutConstraint?
    private var tickTimer: Timer?
    private var avatarTask: Task<Void, Never>?

    private let swipeDistanceRatio: CGFloat = 0.18
    private let edgeResistance: CGFloat = 0.35
    private let transitionDuration: TimeInterval = 0.26

    // MARK: - 生命周期

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        overrideUserInterfaceStyle = .dark
        setupPages()
        setupChrome()
        setupGestures()
        startTick()
        observeAppLifecycle()
        reloadFeed()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if !ServerStore.shared.hasCredentials && !didPromptLogin {
            didPromptLogin = true
            presentLogin(animated: true)
        } else {
            resumeCurrent()
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        pages.forEach { $0.player.pause() }
        UIApplication.shared.isIdleTimerDisabled = false
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        layoutPages()
    }

    deinit {
        tickTimer?.invalidate()
        NotificationCenter.default.removeObserver(self)
    }

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .portrait }
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .bottom }

    // MARK: - 视图搭建

    private func setupPages() {
        pagesContainer.backgroundColor = .black
        pagesContainer.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(pagesContainer)
        NSLayoutConstraint.activate([
            pagesContainer.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            pagesContainer.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            pagesContainer.topAnchor.constraint(equalTo: view.topAnchor),
            pagesContainer.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        for _ in 0..<3 {
            let page = FeedPageView()
            pagesContainer.addSubview(page)
            pages.append(page)
        }
    }

    private func setupChrome() {
        guestChip.text = "游客模式"
        guestChip.font = .systemFont(ofSize: 12, weight: .medium)
        guestChip.textColor = .white
        guestChip.backgroundColor = UIColor.black.withAlphaComponent(0.35)
        guestChip.layer.cornerRadius = 10
        guestChip.clipsToBounds = true
        guestChip.isHidden = true

        var menuConfig = UIButton.Configuration.plain()
        menuConfig.image = UIImage(systemName: "ellipsis")
        menuConfig.baseForegroundColor = .white
        menuButton.configuration = menuConfig
        menuButton.addTarget(self, action: #selector(showMenu), for: .touchUpInside)

        avatarView = UIImageView()
        avatarView.contentMode = .scaleAspectFill
        avatarView.backgroundColor = UIColor.white.withAlphaComponent(0.15)
        avatarView.layer.cornerRadius = 21
        avatarView.clipsToBounds = true
        avatarView.layer.borderWidth = 1.5
        avatarView.layer.borderColor = UIColor.white.withAlphaComponent(0.8).cgColor
        avatarView.translatesAutoresizingMaskIntoConstraints = false
        avatarView.widthAnchor.constraint(equalToConstant: 42).isActive = true
        avatarView.heightAnchor.constraint(equalToConstant: 42).isActive = true
        avatarView.isHidden = true

        configureRailButton(muteButton, symbol: "speaker.wave.2.fill", action: #selector(toggleMute))
        configureRailButton(copyButton, symbol: "doc.on.doc", action: #selector(copyLink))

        let railStack = UIStackView(arrangedSubviews: [avatarView, copyButton, muteButton])
        railStack.axis = .vertical
        railStack.alignment = .center
        railStack.spacing = 22
        railStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(railStack)

        guestChip.translatesAutoresizingMaskIntoConstraints = false
        menuButton.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(guestChip)
        view.addSubview(menuButton)

        progressTrack.translatesAutoresizingMaskIntoConstraints = false
        progressTrack.backgroundColor = UIColor.white.withAlphaComponent(0.2)
        view.addSubview(progressTrack)
        progressBar.translatesAutoresizingMaskIntoConstraints = false
        progressBar.backgroundColor = UIColor.white.withAlphaComponent(0.85)
        progressTrack.addSubview(progressBar)

        let widthConstraint = progressBar.widthAnchor.constraint(equalToConstant: 0)
        progressWidth = widthConstraint

        NSLayoutConstraint.activate([
            menuButton.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -8),
            menuButton.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 4),
            menuButton.widthAnchor.constraint(equalToConstant: 44),
            menuButton.heightAnchor.constraint(equalToConstant: 44),

            guestChip.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 14),
            guestChip.centerYAnchor.constraint(equalTo: menuButton.centerYAnchor),

            railStack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -14),
            railStack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -60),

            progressTrack.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progressTrack.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progressTrack.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            progressTrack.heightAnchor.constraint(equalToConstant: 2),

            progressBar.leadingAnchor.constraint(equalTo: progressTrack.leadingAnchor),
            progressBar.topAnchor.constraint(equalTo: progressTrack.topAnchor),
            progressBar.bottomAnchor.constraint(equalTo: progressTrack.bottomAnchor),
            widthConstraint,
        ])
        updateRail()
    }

    private func configureRailButton(_ button: UIButton, symbol: String, action: Selector) {
        var config = UIButton.Configuration.plain()
        config.image = UIImage(systemName: symbol)
        config.baseForegroundColor = .white
        config.contentInsets = NSDirectionalEdgeInsets(top: 10, leading: 10, bottom: 10, trailing: 10)
        button.configuration = config
        button.addTarget(self, action: action, for: .touchUpInside)
    }

    private func setupGestures() {
        let pan = UIPanGestureRecognizer(target: self, action: #selector(handlePan(_:)))
        pagesContainer.addGestureRecognizer(pan)
        let tap = UITapGestureRecognizer(target: self, action: #selector(handleTap))
        pagesContainer.addGestureRecognizer(tap)
    }

    private func observeAppLifecycle() {
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(appDidEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        center.addObserver(self, selector: #selector(appWillEnterForeground), name: UIApplication.willEnterForegroundNotification, object: nil)
    }

    // MARK: - 布局与绑定

    private func layoutPages() {
        let size = pagesContainer.bounds.size
        guard size.height > 0 else { return }
        for (slot, page) in pages.enumerated() {
            page.frame = CGRect(x: 0, y: CGFloat(slot - 1) * size.height, width: size.width, height: size.height)
        }
    }

    /// 把当前索引前后各一条绑到三个槽上：当前激活、下一条预热、上一条待命
    private func bind() {
        guard pages.count == 3 else { return }
        for slot in 0..<3 {
            let page = pages[slot]
            let index = currentIndex + (slot - 1)
            switch slot {
            case 1: page.setMode(.active)
            case 2: page.setMode(.preload)
            default: page.setMode(.idle)
            }
            guard items.indices.contains(index) else {
                if index < 0 {
                    page.clear()
                } else if isLoadingPage || self.page < totalPages {
                    page.showMessage("正在加载…")
                } else {
                    page.showMessage("已经看完全部作品\n上滑换一批新的")
                }
                continue
            }
            let item = items[index]
            page.configure(item: item, role: ServerStore.shared.role, api: api)
            resolveAndAttach(page: page, item: item)
        }
        updateRail()
        updateProgress()
    }

    private func resolveAndAttach(page: FeedPageView, item: FeedItem) {
        Task { [weak self] in
            guard let self else { return }
            let direct = await self.cache.resolve(item)
            let proxyURL = self.api.proxyMediaURL(item)
            let url: URL
            let isDirect: Bool
            if let direct, let parsed = URL(string: direct) {
                url = parsed
                isDirect = true
            } else if let proxyURL {
                url = proxyURL
                isDirect = false
            } else {
                page.showMessage("这条作品没有可播放文件")
                return
            }
            guard page.item?.key == item.key else { return }
            page.setVideo(url: url, isDirect: isDirect, proxyURL: proxyURL)
        }
    }

    private func updateRail() {
        guard items.indices.contains(currentIndex) else {
            avatarView.isHidden = true
            return
        }
        let item = items[currentIndex]
        let role = ServerStore.shared.role
        guestChip.isHidden = !(role == "guest" || role == nil)
        if role == "admin", let url = api.avatarURL(item) {
            avatarView.isHidden = false
            avatarView.image = nil
            avatarTask?.cancel()
            let headers = api.authHeaders
            let token = item.key
            avatarTask = Task { [weak self] in
                let image = await ImageLoader.shared.load(url: url, headers: headers)
                guard let self, let image else { return }
                guard self.items.indices.contains(self.currentIndex), self.items[self.currentIndex].key == token else { return }
                self.avatarView.image = image
            }
        } else {
            avatarView.isHidden = true
        }
        let muted = PlaybackSettings.muted
        muteButton.setImage(UIImage(systemName: muted ? "speaker.slash.fill" : "speaker.wave.2.fill"), for: .normal)
    }

    // MARK: - 手势

    @objc private func handlePan(_ gesture: UIPanGestureRecognizer) {
        let height = max(pagesContainer.bounds.height, 1)
        switch gesture.state {
        case .changed:
            var offset = gesture.translation(in: view).y
            if (offset > 0 && currentIndex == 0) || (offset < 0 && isAtTail) {
                offset *= edgeResistance
            }
            pagesContainer.transform = CGAffineTransform(translationX: 0, y: offset)
        case .ended, .cancelled:
            let offset = gesture.translation(in: view).y
            let velocity = gesture.velocity(in: view).y
            let threshold = max(64, height * swipeDistanceRatio)
            var direction = 0
            if abs(offset) >= threshold || (abs(offset) > 24 && abs(velocity) > 450) {
                direction = offset < 0 ? 1 : -1
            }
            settle(direction: direction)
        default:
            break
        }
    }

    private var isAtTail: Bool {
        currentIndex >= items.count - 1 && page >= totalPages && !isLoadingPage
    }

    private func settle(direction: Int) {
        let height = pagesContainer.bounds.height
        guard direction != 0 else {
            UIView.animate(withDuration: 0.18) { self.pagesContainer.transform = .identity }
            return
        }
        // 上滑到尾部：换一批新随机
        if direction == 1 && isAtTail {
            UIView.animate(withDuration: transitionDuration, animations: {
                self.pagesContainer.transform = CGAffineTransform(translationX: 0, y: -height)
            }, completion: { _ in
                self.pagesContainer.transform = .identity
                self.reloadFeed()
            })
            return
        }
        // 下一条还没到货：先把数据拉回来，动画归位
        if direction == 1 && currentIndex + 1 >= items.count {
            ensureAhead(force: true)
            pages[2].showMessage("正在加载…")
            UIView.animate(withDuration: 0.18) { self.pagesContainer.transform = .identity }
            return
        }
        UIView.animate(withDuration: transitionDuration, delay: 0, options: [.curveEaseOut], animations: {
            self.pagesContainer.transform = CGAffineTransform(translationX: 0, y: -CGFloat(direction) * height)
        }, completion: { _ in
            self.commitStep(direction)
        })
    }

    private func commitStep(_ direction: Int) {
        currentIndex += direction
        UIView.performWithoutAnimation {
            if direction > 0 {
                let first = pages.removeFirst()
                pages.append(first)
            } else {
                let last = pages.removeLast()
                pages.insert(last, at: 0)
            }
            self.pagesContainer.transform = .identity
            self.layoutPages()
        }
        bind()
        ensureAhead()
    }

    @objc private func handleTap() {
        let page = pages[1]
        if page.player.isPlaying {
            userPaused = true
            page.player.pause()
        } else {
            userPaused = false
            page.player.activate(muted: PlaybackSettings.muted)
        }
    }

    @objc private func toggleMute() {
        PlaybackSettings.muted.toggle()
        pages[1].player.player.isMuted = PlaybackSettings.muted
        updateRail()
    }

    @objc private func copyLink() {
        guard items.indices.contains(currentIndex) else { return }
        let item = items[currentIndex]
        UIPasteboard.general.string = item.shareUrl ?? "\(api.baseURLString)/api/monitor/users/\(item.userId)/archive/\(item.awemeId)"
        showToast("已复制作品链接")
    }

    private func showToast(_ text: String) {
        let label = PaddedLabel()
        label.text = text
        label.font = .systemFont(ofSize: 13)
        label.textColor = .white
        label.backgroundColor = UIColor.black.withAlphaComponent(0.7)
        label.layer.cornerRadius = 14
        label.clipsToBounds = true
        label.alpha = 0
        label.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
        UIView.animate(withDuration: 0.15) { label.alpha = 1 }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            UIView.animate(withDuration: 0.2, animations: { label.alpha = 0 }) { _ in label.removeFromSuperview() }
        }
    }

    // MARK: - 数据

    private func reloadFeed() {
        sessionToken += 1
        items = []
        currentIndex = 0
        page = 1
        totalPages = 1
        seed = nil
        pages.forEach { $0.clear() }
        layoutPages()
        pages[1].showMessage("正在加载…")
        let token = sessionToken
        Task { await loadPage(1, token: token) }
    }

    private func ensureAhead(force: Bool = false) {
        guard !isLoadingPage, page < totalPages else { return }
        guard force || items.count - currentIndex <= 6 else { return }
        let token = sessionToken
        Task { await loadPage(page + 1, token: token) }
    }

    private func loadPage(_ number: Int, token: Int) async {
        guard !isLoadingPage else { return }
        isLoadingPage = true
        defer { isLoadingPage = false }

        var query = LibraryQuery()
        query.sort = config.sort
        query.scoreMin = config.scoreMin
        query.keyword = config.keyword
        query.seed = seed
        query.page = number
        query.pageSize = 20

        do {
            let result = try await api.library(query)
            guard token == sessionToken else { return }
            if number == 1 { seed = result.seed }
            page = number
            totalPages = max(result.totalPages ?? number, number)
            let known = Set(items.map { $0.key })
            let incoming = result.items.filter { $0.isPlayable && !known.contains($0.key) }
            items.append(contentsOf: incoming)
            if items.isEmpty {
                pages[1].showMessage("作品库里没有可播放的视频\n右上角 ⋯ 可以换筛选条件")
                return
            }
            bind()
            let window = Array(items[currentIndex...].prefix(6))
            Task { await cache.prefetch(window) }
        } catch let error as APIError {
            handle(error)
        } catch {
            pages[1].showMessage("加载失败：\(error.localizedDescription)\n右上角 ⋯ 刷新")
        }
    }

    private func handle(_ error: APIError) {
        if case .unauthorized = error {
            ServerStore.shared.clearCredentials()
            presentLogin(animated: true)
            return
        }
        pages[1].showMessage(error.localizedDescription + "\n右上角 ⋯ 刷新")
    }

    // MARK: - 播放

    private func resumeCurrent() {
        guard items.indices.contains(currentIndex), pages[1].item != nil else { return }
        pages[1].player.activate(muted: PlaybackSettings.muted)
    }

    private func startTick() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            guard let self, self.pages.count == 3 else { return }
            self.pages[1].tick()
            self.updateProgress()
            UIApplication.shared.isIdleTimerDisabled = self.pages[1].player.isPlaying
        }
    }

    private func updateProgress() {
        guard let progressWidth, pagesContainer.bounds.width > 0 else { return }
        let value = pages.count == 3 ? pages[1].player.progress : 0
        progressWidth.constant = pagesContainer.bounds.width * CGFloat(value)
    }

    @objc private func appDidEnterBackground() {
        pages.forEach { $0.player.pause() }
        UIApplication.shared.isIdleTimerDisabled = false
    }

    @objc private func appWillEnterForeground() {
        resumeCurrent()
    }

    // MARK: - 菜单 / 登录 / 筛选

    @objc private func showMenu() {
        let sheet = UIAlertController(title: nil, message: nil, preferredStyle: .actionSheet)
        sheet.addAction(UIAlertAction(title: "刷新（换一批）", style: .default) { [weak self] _ in self?.reloadFeed() })
        sheet.addAction(UIAlertAction(title: "筛选条件", style: .default) { [weak self] _ in self?.presentFilters() })
        sheet.addAction(UIAlertAction(title: "网页版刷视频", style: .default) { [weak self] _ in
            guard let self else { return }
            self.presentWeb(path: "/api/monitor/swipe/dashboard", title: "网页版刷视频")
        })
        sheet.addAction(UIAlertAction(title: "作品库 / 统计", style: .default) { [weak self] _ in
            guard let self else { return }
            self.presentWeb(path: "/api/monitor/library/dashboard", title: "作品库")
        })
        sheet.addAction(UIAlertAction(title: "切换账号 / 退出登录", style: .destructive) { [weak self] _ in
            ServerStore.shared.clearCredentials()
            self?.presentLogin(animated: true)
        })
        sheet.addAction(UIAlertAction(title: "取消", style: .cancel))
        if let popover = sheet.popoverPresentationController {
            popover.sourceView = menuButton
            popover.sourceRect = menuButton.bounds
        }
        present(sheet, animated: true)
    }

    private func presentLogin(animated: Bool) {
        pages.forEach { $0.player.pause() }
        let login = LoginViewController(api: api)
        login.onSuccess = { [weak self] in
            guard let self else { return }
            self.didPromptLogin = true
            self.reloadFeed()
        }
        login.modalPresentationStyle = .fullScreen
        present(login, animated: animated)
    }

    private func presentFilters() {
        let filters = FilterViewController(config: config)
        filters.onApply = { [weak self] updated in
            guard let self else { return }
            self.config = updated
            updated.save()
            self.reloadFeed()
        }
        let nav = UINavigationController(rootViewController: filters)
        present(nav, animated: true)
    }

    private func presentWeb(path: String, title: String) {
        let web = WebViewController(path: path, title: title, api: api)
        web.modalPresentationStyle = .fullScreen
        present(web, animated: true)
    }
}

/// feed 的筛选条件（排序 / 擦边分下限 / 关键词），落 UserDefaults
struct FeedConfig {
    var sort = "random"
    var scoreMin: Int?
    var keyword: String?

    private enum Key {
        static let sort = "dymon.feed.sort"
        static let scoreMin = "dymon.feed.scoreMin"
        static let keyword = "dymon.feed.keyword"
    }

    static func load() -> FeedConfig {
        let defaults = UserDefaults.standard
        var config = FeedConfig()
        if let sort = defaults.string(forKey: Key.sort) { config.sort = sort }
        if defaults.object(forKey: Key.scoreMin) != nil { config.scoreMin = defaults.integer(forKey: Key.scoreMin) }
        let keyword = defaults.string(forKey: Key.keyword)
        config.keyword = (keyword?.isEmpty ?? true) ? nil : keyword
        return config
    }

    func save() {
        let defaults = UserDefaults.standard
        defaults.set(sort, forKey: Key.sort)
        if let scoreMin { defaults.set(scoreMin, forKey: Key.scoreMin) } else { defaults.removeObject(forKey: Key.scoreMin) }
        defaults.set(keyword ?? "", forKey: Key.keyword)
    }

    var sortTitle: String {
        LibraryQuery.sorts.first(where: { $0.value == sort })?.title ?? sort
    }
}
