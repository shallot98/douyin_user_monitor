import UIKit
import WebKit

/// 全屏刷视频：把网页版竖滑 feed 装进 WKWebView，并打开壳里才有的原生能力：
/// - 免手势自动播放（含声音），滑动即播，不用点一下
/// - 登录 Cookie 落盘；播放中不自动锁屏；底部上滑不被系统手势抢走
/// - 摇一摇出菜单（刷新 / 切页 / 回登录）
final class FeedViewController: UIViewController {

    private enum Conf {
        static let host = "dy.cenai.de"
        static let home = URL(string: "https://\(host)/api/monitor/swipe/dashboard")!
        static let library = URL(string: "https://\(host)/api/monitor/library/dashboard")!
        static let login = URL(string: "https://\(host)/login")!
        /// 网页端靠这个标记识别「App 模式」（默认有声、加深预载、原生保活桥）
        static let userAgentTag = "DyMonApp/1.0"
    }

    private var webView: WKWebView!
    private let errorView = UIView()
    private let errorLabel = UILabel()
    private lazy var retryButton: UIButton = {
        var config = UIButton.Configuration.filled()
        config.title = "重试"
        config.baseBackgroundColor = .white
        config.baseForegroundColor = .black
        config.cornerStyle = .capsule
        config.contentInsets = NSDirectionalEdgeInsets(top: 10, leading: 26, bottom: 10, trailing: 26)
        let button = UIButton(configuration: config)
        button.addTarget(self, action: #selector(retryTapped), for: .touchUpInside)
        return button
    }()

    // MARK: - 生命周期

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        setupWebView()
        setupErrorOverlay()
        load(Conf.home)
    }

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .portrait }
    override var canBecomeFirstResponder: Bool { true }

    /// 上滑手势优先给页面，别被系统底部手势抢走
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .bottom }

    /// 摇一摇出菜单：这个壳没有浏览器按钮，刷新/切页靠它
    override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        if motion == .motionShake {
            presentMenu()
        } else {
            super.motionEnded(motion, with: event)
        }
    }

    // MARK: - WebView

    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        // 关键一行：网页里的 video.play() 不再要求用户手势，且允许带声音自动播放
        config.mediaTypesRequiringUserActionForPlayback = []
        config.allowsPictureInPictureMediaPlayback = false
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = Conf.userAgentTag
        config.preferences.isTextInteractionEnabled = false
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        config.userContentController.add(self, name: "dymon")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.bounces = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.maximumZoomScale = 1
        webView.scrollView.pinchGestureRecognizer?.isEnabled = false
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsLinkPreview = false
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        self.webView = webView
    }

    private func setupErrorOverlay() {
        errorView.backgroundColor = .black
        errorView.isHidden = true
        errorView.translatesAutoresizingMaskIntoConstraints = false

        errorLabel.text = "加载失败，检查手机网络后重试。"
        errorLabel.textColor = .white
        errorLabel.font = .systemFont(ofSize: 15)
        errorLabel.numberOfLines = 0
        errorLabel.textAlignment = .center

        let stack = UIStackView(arrangedSubviews: [errorLabel, retryButton])
        stack.axis = .vertical
        stack.alignment = .center
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        errorView.addSubview(stack)
        view.addSubview(errorView)

        NSLayoutConstraint.activate([
            errorView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            errorView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            errorView.topAnchor.constraint(equalTo: view.topAnchor),
            errorView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            stack.centerXAnchor.constraint(equalTo: errorView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: errorView.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: errorView.widthAnchor, multiplier: 0.8),
        ])
    }

    /// 每次进主文档都重新校验缓存：网页改了什么，壳里刷新就能看到
    private func load(_ url: URL) {
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadRevalidatingCacheData
        webView.load(request)
    }

    private func showError(_ error: Error) {
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled else { return }
        NSLog("DyMon load error: %@", String(describing: error))
        errorView.isHidden = false
    }

    @objc private func retryTapped() {
        errorView.isHidden = true
        load(Conf.home)
    }

    // MARK: - 菜单

    private func presentMenu() {
        let sheet = UIAlertController(title: "刷视频", message: nil, preferredStyle: .actionSheet)
        sheet.addAction(UIAlertAction(title: "刷新", style: .default) { [weak self] _ in
            self?.webView.reload()
        })
        sheet.addAction(UIAlertAction(title: "回到刷视频", style: .default) { [weak self] _ in
            self?.load(Conf.home)
        })
        sheet.addAction(UIAlertAction(title: "打开作品库", style: .default) { [weak self] _ in
            self?.load(Conf.library)
        })
        sheet.addAction(UIAlertAction(title: "回到登录页", style: .default) { [weak self] _ in
            self?.load(Conf.login)
        })
        sheet.addAction(UIAlertAction(title: "复制当前地址", style: .default) { [weak self] _ in
            UIPasteboard.general.string = self?.webView.url?.absoluteString ?? Conf.home.absoluteString
        })
        sheet.addAction(UIAlertAction(title: "取消", style: .cancel))
        if let popover = sheet.popoverPresentationController {
            popover.sourceView = view
            popover.sourceRect = CGRect(x: view.bounds.midX, y: view.bounds.midY, width: 0, height: 0)
            popover.permittedArrowDirections = []
        }
        present(sheet, animated: true)
    }
}

// MARK: - WKNavigationDelegate

extension FeedViewController: WKNavigationDelegate {

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        errorView.isHidden = true
        showMenuHintIfNeeded()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showError(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showError(error)
    }

    /// 首次加载成功后提示一次：这个壳没有按钮，刷新/切页靠摇一摇
    private func showMenuHintIfNeeded() {
        let key = "dymon.menuHintShown"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        UserDefaults.standard.set(true, forKey: key)
        let alert = UIAlertController(
            title: "摇一摇出菜单",
            message: "刷新、回到刷视频、打开作品库、回登录页都在摇一摇菜单里。",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "知道了", style: .default))
        present(alert, animated: true)
    }

    /// 站内照常走 WebView，站外链接交给 Safari
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url,
           url.host != Conf.host,
           ["http", "https"].contains(url.scheme ?? "") {
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

// MARK: - WKUIDelegate

extension FeedViewController: WKUIDelegate {

    /// target="_blank" 的链接在当前 WebView 里打开，别丢出去
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if navigationAction.targetFrame == nil {
            webView.load(navigationAction.request)
        }
        return nil
    }
}

// MARK: - WKScriptMessageHandler

extension FeedViewController: WKScriptMessageHandler {

    /// 网页 -> 原生：目前只用于「播放中不自动锁屏」
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "dymon",
              let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }
        switch type {
        case "playing":
            let value = (body["value"] as? NSNumber)?.boolValue ?? false
            UIApplication.shared.isIdleTimerDisabled = value
        default:
            break
        }
    }
}
