import UIKit
import WebKit

/// 完整站点（作品库 / 统计 / 网页版刷视频）：原生 feed 之外的兜底入口
final class WebViewController: UIViewController, WKNavigationDelegate {

    private let path: String
    private let api: APIClient
    private var webView: WKWebView!
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let titleLabel = UILabel()

    init(path: String, title: String, api: APIClient) {
        self.path = path
        self.api = api
        super.init(nibName: nil, bundle: nil)
        self.title = title
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) 未实现") }

    override func viewDidLoad() {
        super.viewDidLoad()
        overrideUserInterfaceStyle = .dark
        view.backgroundColor = .black
        buildUI()
        load()
    }

    private func buildUI() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = "DyMonApp/2.0"
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.translatesAutoresizingMaskIntoConstraints = false

        let bar = UIView()
        bar.backgroundColor = UIColor(white: 0.08, alpha: 1)
        bar.translatesAutoresizingMaskIntoConstraints = false

        let closeButton = UIButton(type: .system)
        closeButton.setImage(UIImage(systemName: "xmark"), for: .normal)
        closeButton.tintColor = .white
        closeButton.addTarget(self, action: #selector(close), for: .touchUpInside)

        let reloadButton = UIButton(type: .system)
        reloadButton.setImage(UIImage(systemName: "arrow.clockwise"), for: .normal)
        reloadButton.tintColor = .white
        reloadButton.addTarget(self, action: #selector(reload), for: .touchUpInside)

        titleLabel.text = title
        titleLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        titleLabel.textColor = .white
        titleLabel.textAlignment = .center

        spinner.color = .white
        spinner.hidesWhenStopped = true

        [closeButton, titleLabel, reloadButton, spinner].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            bar.addSubview($0)
        }
        view.addSubview(bar)
        view.addSubview(webView)

        NSLayoutConstraint.activate([
            bar.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            bar.topAnchor.constraint(equalTo: view.topAnchor),
            bar.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 44),

            closeButton.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 14),
            closeButton.bottomAnchor.constraint(equalTo: bar.bottomAnchor, constant: -10),
            reloadButton.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -14),
            reloadButton.centerYAnchor.constraint(equalTo: closeButton.centerYAnchor),
            titleLabel.centerXAnchor.constraint(equalTo: bar.centerXAnchor),
            titleLabel.centerYAnchor.constraint(equalTo: closeButton.centerYAnchor),
            spinner.centerXAnchor.constraint(equalTo: bar.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: closeButton.centerYAnchor),

            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: bar.bottomAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    /// 原生侧存的是会话 Cookie，WebView 有自己的 Cookie 库，先灌进去再加载
    private func load() {
        guard let url = URL(string: api.baseURLString + path) else { return }
        let cookieStore = webView.configuration.websiteDataStore.httpCookieStore
        guard let raw = ServerStore.shared.sessionCookie,
              let host = URL(string: api.baseURLString)?.host,
              let pair = raw.components(separatedBy: ";").first,
              let name = pair.components(separatedBy: "=").first else {
            webView.load(URLRequest(url: url))
            return
        }
        let value = pair.components(separatedBy: "=").dropFirst().joined(separator: "=")
        let properties: [HTTPCookiePropertyKey: Any] = [
            .name: name, .value: value, .domain: host, .path: "/", .secure: "TRUE",
        ]
        if let cookie = HTTPCookie(properties: properties) {
            cookieStore.setCookie(cookie) { [weak self] in
                DispatchQueue.main.async { self?.webView.load(URLRequest(url: url)) }
            }
        } else {
            webView.load(URLRequest(url: url))
        }
    }

    @objc private func close() {
        dismiss(animated: true)
    }

    @objc private func reload() {
        webView.reload()
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        spinner.startAnimating()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        spinner.stopAnimating()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        spinner.stopAnimating()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        spinner.stopAnimating()
    }
}
