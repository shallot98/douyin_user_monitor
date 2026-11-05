import UIKit

/// 登录页：账号密码登录（记住 30 天）、游客进入、或直接填 API Key
final class LoginViewController: UIViewController {

    var onSuccess: (() -> Void)?

    private let api: APIClient
    private let store = ServerStore.shared

    private let serverField = UITextField()
    private let userField = UITextField()
    private let passwordField = UITextField()
    private let keyField = UITextField()
    private let statusLabel = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private var loginButton: UIButton!
    private var guestButton: UIButton!

    init(api: APIClient) {
        self.api = api
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) 未实现") }

    override func viewDidLoad() {
        super.viewDidLoad()
        overrideUserInterfaceStyle = .dark
        view.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1)
        buildUI()
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    private func buildUI() {
        let title = UILabel()
        title.text = "刷视频"
        title.font = .systemFont(ofSize: 30, weight: .bold)
        title.textColor = .white

        let subtitle = UILabel()
        subtitle.text = "登录监控站点账号，或游客进入刷随机视频"
        subtitle.font = .systemFont(ofSize: 14)
        subtitle.textColor = UIColor.white.withAlphaComponent(0.6)
        subtitle.numberOfLines = 0

        configure(serverField, placeholder: "服务器地址", text: store.baseURL, keyboard: .URL, secure: false)
        configure(userField, placeholder: "账号", text: nil, keyboard: .default, secure: false)
        configure(passwordField, placeholder: "密码", text: nil, keyboard: .default, secure: true)
        configure(keyField, placeholder: "API Key（可选，填了就免登录）", text: nil, keyboard: .default, secure: false)

        statusLabel.font = .systemFont(ofSize: 13)
        statusLabel.textColor = .systemRed
        statusLabel.numberOfLines = 0
        statusLabel.isHidden = true

        loginButton = UIButton(configuration: filledConfig(title: "登录", background: .white, foreground: .black))
        loginButton.addTarget(self, action: #selector(loginTapped), for: .touchUpInside)

        guestButton = UIButton(configuration: filledConfig(title: "游客进入（随机视频，只读）", background: UIColor.white.withAlphaComponent(0.14), foreground: .white))
        guestButton.addTarget(self, action: #selector(guestTapped), for: .touchUpInside)

        spinner.color = .white
        spinner.hidesWhenStopped = true

        let stack = UIStackView(arrangedSubviews: [
            title, subtitle, serverField, userField, passwordField, keyField, loginButton, guestButton, spinner, statusLabel,
        ])
        stack.axis = .vertical
        stack.spacing = 14
        stack.setCustomSpacing(6, after: title)
        stack.setCustomSpacing(24, after: subtitle)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let scrollView = UIScrollView()
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.keyboardDismissMode = .interactive
        scrollView.addSubview(stack)
        view.addSubview(scrollView)

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: view.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            stack.leadingAnchor.constraint(equalTo: scrollView.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: scrollView.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: scrollView.topAnchor, constant: 90),
            stack.bottomAnchor.constraint(equalTo: scrollView.bottomAnchor, constant: -40),
            stack.widthAnchor.constraint(equalTo: scrollView.widthAnchor, constant: -48),
        ])
    }

    private func configure(_ field: UITextField, placeholder: String, text: String?, keyboard: UIKeyboardType, secure: Bool) {
        field.placeholder = placeholder
        field.text = text
        field.keyboardType = keyboard
        field.isSecureTextEntry = secure
        field.autocapitalizationType = .none
        field.autocorrectionType = .no
        field.textColor = .white
        field.backgroundColor = UIColor.white.withAlphaComponent(0.08)
        field.layer.cornerRadius = 12
        field.font = .systemFont(ofSize: 15)
        field.heightAnchor.constraint(equalToConstant: 48).isActive = true
        field.leftView = UIView(frame: CGRect(x: 0, y: 0, width: 14, height: 1))
        field.leftViewMode = .always
        field.returnKeyType = .done
    }

    private func filledConfig(title: String, background: UIColor, foreground: UIColor) -> UIButton.Configuration {
        var config = UIButton.Configuration.filled()
        config.title = title
        config.baseBackgroundColor = background
        config.baseForegroundColor = foreground
        config.cornerStyle = .large
        config.contentInsets = NSDirectionalEdgeInsets(top: 14, leading: 18, bottom: 14, trailing: 18)
        return config
    }

    // MARK: - 动作

    @objc private func loginTapped() {
        view.endEditing(true)
        let server = serverField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let url = URL(string: server), url.scheme != nil else {
            showError("服务器地址不合法：\(server)")
            return
        }
        store.baseURL = server

        let key = keyField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !key.isEmpty {
            api.useAPIKey(key)
            onSuccess?()
            dismiss(animated: true)
            return
        }

        let username = userField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let password = passwordField.text ?? ""
        guard !username.isEmpty, !password.isEmpty else {
            showError("请填账号和密码，或直接填 API Key")
            return
        }
        setBusy(true)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.api.login(username: username, password: password)
                self.finish()
            } catch {
                self.setBusy(false)
                self.showError(error.localizedDescription)
            }
        }
    }

    @objc private func guestTapped() {
        view.endEditing(true)
        let server = serverField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let url = URL(string: server), url.scheme != nil else {
            showError("服务器地址不合法：\(server)")
            return
        }
        store.baseURL = server
        setBusy(true)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.api.loginAsGuest()
                self.finish()
            } catch {
                self.setBusy(false)
                self.showError(error.localizedDescription)
            }
        }
    }

    private func finish() {
        setBusy(false)
        onSuccess?()
        dismiss(animated: true)
    }

    private func setBusy(_ busy: Bool) {
        loginButton.isEnabled = !busy
        guestButton.isEnabled = !busy
        if busy { spinner.startAnimating() } else { spinner.stopAnimating() }
    }

    private func showError(_ message: String) {
        statusLabel.text = message
        statusLabel.isHidden = false
    }
}
