import Foundation
import UIKit

enum AppLog {
    static func info(_ message: String) { NSLog("[DyMon] %@", message) }
}

/// 服务器地址与凭据（会话 Cookie / API Key）持久化
final class ServerStore {
    static let shared = ServerStore()
    static let defaultBaseURL = "https://dy.cenai.de"

    private let defaults = UserDefaults.standard

    private enum Key {
        static let baseURL = "dymon.baseURL"
        static let cookie = "dymon.sessionCookie"
        static let apiKey = "dymon.apiKey"
        static let role = "dymon.role"
    }

    var baseURL: String {
        get { defaults.string(forKey: Key.baseURL) ?? Self.defaultBaseURL }
        set { defaults.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Key.baseURL) }
    }

    /// 形如 "dymon_session=xxx"（也兼容游客的 dymon_guest）
    var sessionCookie: String? {
        get { defaults.string(forKey: Key.cookie) }
        set { defaults.set(newValue, forKey: Key.cookie) }
    }

    var apiKey: String? {
        get { defaults.string(forKey: Key.apiKey) }
        set { defaults.set(newValue, forKey: Key.apiKey) }
    }

    /// admin / guest / key
    var role: String? {
        get { defaults.string(forKey: Key.role) }
        set { defaults.set(newValue, forKey: Key.role) }
    }

    var hasCredentials: Bool {
        !(sessionCookie ?? "").isEmpty || !(apiKey ?? "").isEmpty
    }

    func clearCredentials() {
        defaults.removeObject(forKey: Key.cookie)
        defaults.removeObject(forKey: Key.apiKey)
        defaults.removeObject(forKey: Key.role)
    }
}

/// 不跟随重定向：未登录时服务端对页面回 302，跟过去只会拿到登录页 HTML
final class NoRedirectSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

/// 封面/头像小图：内存缓存 + URLSession 磁盘缓存
final class ImageLoader {
    static let shared = ImageLoader()

    private let cache = NSCache<NSString, UIImage>()
    private let session: URLSession

    private init() {
        let config = URLSessionConfiguration.default
        config.requestCachePolicy = .returnCacheDataElseLoad
        config.urlCache = URLCache(memoryCapacity: 32 << 20, diskCapacity: 256 << 20, diskPath: "dymon-images")
        config.timeoutIntervalForRequest = 20
        session = URLSession(configuration: config, delegate: NoRedirectSessionDelegate(), delegateQueue: nil)
    }

    func cachedImage(for url: URL) -> UIImage? {
        cache.object(forKey: url.absoluteString as NSString)
    }

    func load(url: URL, headers: [String: String]) async -> UIImage? {
        if let hit = cachedImage(for: url) { return hit }
        var request = URLRequest(url: url)
        request.cachePolicy = .returnCacheDataElseLoad
        headers.forEach { request.setValue($0.value, forHTTPHeaderField: $0.key) }
        guard let (data, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse,
              http.statusCode == 200,
              let image = UIImage(data: data) else { return nil }
        cache.setObject(image, forKey: url.absoluteString as NSString)
        return image
    }
}

extension String {
    var urlPathEncoded: String {
        addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? self
    }
}

extension Array {
    func chunked(into size: Int) -> [[Element]] {
        guard size > 0 else { return [self] }
        return stride(from: 0, to: count, by: size).map { Array(self[$0..<Swift.min($0 + size, count)]) }
    }
}
