import Foundation

enum APIError: LocalizedError {
    case unauthorized
    case server(String)
    case transport(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .unauthorized: return "登录已过期，请重新登录"
        case .server(let message): return message
        case .transport(let message): return "网络错误：\(message)"
        case .decoding(let message): return "数据解析失败：\(message)"
        }
    }
}

struct PlaybackBatch {
    let urls: [String: String?]
    let ttl: TimeInterval
}

struct LibraryQuery {
    var sort = "random"
    var scoreMin: Int?
    var keyword: String?
    var seed: Int?
    var page = 1
    var pageSize = 20

    static let sorts: [(value: String, title: String)] = [
        ("random", "随机"), ("newest", "最新"), ("score", "擦边分最高"), ("body", "身材分最高"),
        ("chest", "胸部分最高"), ("overall", "整体暴露最高"), ("camera", "镜头强调最高"),
        ("motion", "动作展示最高"), ("duration", "时长最长"), ("oldest", "最早"),
    ]
}

final class APIClient {
    private let store = ServerStore.shared
    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        config.httpShouldSetCookies = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpAdditionalHeaders = ["User-Agent": "DyMonApp/2.0 (iOS)"]
        session = URLSession(configuration: config, delegate: NoRedirectSessionDelegate(), delegateQueue: nil)
    }

    var baseURLString: String { store.baseURL }

    /// 给封面/头像这类图片请求用的鉴权头
    var authHeaders: [String: String] {
        var headers: [String: String] = [:]
        if let cookie = store.sessionCookie, !cookie.isEmpty { headers["Cookie"] = cookie }
        if let key = store.apiKey, !key.isEmpty { headers["X-API-Key"] = key }
        return headers
    }

    // MARK: - URL 拼装

    func archiveBase(_ item: FeedItem) -> String {
        "\(store.baseURL)/api/monitor/users/\(item.userId.urlPathEncoded)/archive/\(item.awemeId.urlPathEncoded)"
    }

    /// 服务端代理播放地址（直链不可用时兜底；走本服务转国内源站，冷启动慢）
    func proxyMediaURL(_ item: FeedItem) -> URL? {
        guard let playIndex = item.playIndex else { return nil }
        return URL(string: "\(archiveBase(item))/media/\(playIndex)")
    }

    /// 封面：优先用接口下发带版本号的封面，其次退到归档里的封面文件
    func coverURL(_ item: FeedItem) -> URL? {
        let local = item.coverUrl ?? ""
        if local.hasPrefix("\(archiveBase(item))/cover") { return URL(string: local) }
        guard let coverIndex = item.coverIndex else { return nil }
        return URL(string: "\(archiveBase(item))/media/\(coverIndex)")
    }

    func avatarURL(_ item: FeedItem) -> URL? {
        guard let path = item.avatarUrl, !path.isEmpty else { return nil }
        if path.hasPrefix("http") { return URL(string: path) }
        return URL(string: store.baseURL + path)
    }

    // MARK: - 请求

    private func makeRequest(path: String, method: String = "GET", body: [String: Any]? = nil) throws -> URLRequest {
        let trimmed = store.baseURL.hasSuffix("/") ? String(store.baseURL.dropLast()) : store.baseURL
        guard let url = URL(string: trimmed + path) else {
            throw APIError.transport("服务器地址不合法：\(store.baseURL)")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let cookie = store.sessionCookie, !cookie.isEmpty {
            request.setValue(cookie, forHTTPHeaderField: "Cookie")
        }
        if let key = store.apiKey, !key.isEmpty {
            request.setValue(key, forHTTPHeaderField: "X-API-Key")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return request
    }

    private func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.transport("没有响应") }
        if http.statusCode == 302 || http.statusCode == 303 || http.statusCode == 307 {
            throw APIError.unauthorized
        }
        return (data, http)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }

    private func detail(from data: Data) -> String? {
        struct Detail: Decodable { let detail: String? }
        return (try? JSONDecoder().decode(Detail.self, from: data))?.detail
    }

    // MARK: - 登录

    func login(username: String, password: String) async throws {
        let request = try makeRequest(
            path: "/auth/login",
            method: "POST",
            body: ["username": username, "password": password, "remember": true]
        )
        let (data, http) = try await send(request)
        switch http.statusCode {
        case 200:
            guard let cookie = Self.sessionCookie(from: http) else {
                throw APIError.server("登录成功但没拿到会话 Cookie")
            }
            store.sessionCookie = cookie
            store.apiKey = nil
            store.role = "admin"
        case 401:
            throw APIError.server(detail(from: data) ?? "用户名或密码错误")
        case 429:
            throw APIError.server(detail(from: data) ?? "失败次数过多，稍后再试")
        default:
            throw APIError.server(detail(from: data) ?? "HTTP \(http.statusCode)")
        }
    }

    func loginAsGuest() async throws {
        let request = try makeRequest(path: "/auth/guest", method: "POST")
        let (data, http) = try await send(request)
        guard http.statusCode == 200, let cookie = Self.sessionCookie(from: http) else {
            throw APIError.server(detail(from: data) ?? "游客模式不可用")
        }
        store.sessionCookie = cookie
        store.apiKey = nil
        store.role = "guest"
    }

    /// 用 API Key 直接进（不校验，进 feed 时第一个请求就会验证）
    func useAPIKey(_ key: String) {
        store.apiKey = key
        store.sessionCookie = nil
        store.role = "key"
    }

    func authStatus() async -> (authenticated: Bool, role: String?, guestEnabled: Bool) {
        struct Status: Decodable {
            let authenticated: Bool?
            let role: String?
            let guestEnabled: Bool?
            enum CodingKeys: String, CodingKey {
                case authenticated, role
                case guestEnabled = "guest_enabled"
            }
        }
        guard let request = try? makeRequest(path: "/auth/status"),
              let (data, http) = try? await send(request),
              http.statusCode == 200,
              let envelope = try? decode(APIEnvelope<Status>.self, from: data) else {
            return (false, nil, false)
        }
        let status = envelope.data
        return (status?.authenticated ?? false, status?.role, status?.guestEnabled ?? false)
    }

    private static func sessionCookie(from response: HTTPURLResponse) -> String? {
        var raw = response.value(forHTTPHeaderField: "Set-Cookie")
        if raw == nil {
            for (key, value) in response.allHeaderFields where String(describing: key).lowercased() == "set-cookie" {
                raw = String(describing: value)
                break
            }
        }
        guard let header = raw,
              let pair = header.components(separatedBy: ";").first,
              pair.contains("=") else { return nil }
        return pair.trimmingCharacters(in: .whitespaces)
    }

    // MARK: - 作品库

    func library(_ query: LibraryQuery) async throws -> LibraryPage {
        var components = URLComponents()
        var queryItems = [
            URLQueryItem(name: "media_type", value: "video"),
            URLQueryItem(name: "sort", value: query.sort),
            URLQueryItem(name: "page", value: String(query.page)),
            URLQueryItem(name: "page_size", value: String(query.pageSize)),
        ]
        if let scoreMin = query.scoreMin {
            queryItems.append(URLQueryItem(name: "score_min", value: String(scoreMin)))
        }
        if let keyword = query.keyword, !keyword.isEmpty {
            queryItems.append(URLQueryItem(name: "q", value: keyword))
        }
        if query.sort == "random", let seed = query.seed {
            queryItems.append(URLQueryItem(name: "seed", value: String(seed)))
        }
        components.queryItems = queryItems
        let request = try makeRequest(path: "/api/monitor/library?" + (components.percentEncodedQuery ?? ""))
        let (data, http) = try await send(request)
        if http.statusCode == 401 { throw APIError.unauthorized }
        guard http.statusCode == 200 else {
            throw APIError.server(detail(from: data) ?? "作品库请求失败（HTTP \(http.statusCode)）")
        }
        let envelope = try decode(APIEnvelope<LibraryPage>.self, from: data)
        guard let page = envelope.data else {
            throw APIError.server(envelope.message ?? "作品库返回为空")
        }
        return page
    }

    /// 批量取播放直链：url 为 nil 表示只能走服务端代理
    func playbackURLs(userId: String, awemeIds: [String]) async throws -> PlaybackBatch {
        var result: [String: String?] = [:]
        var ttl: TimeInterval = 1800
        for chunk in awemeIds.chunked(into: 100) {
            let path = "/api/monitor/users/\(userId.urlPathEncoded)/archive/playback-urls"
            let request = try makeRequest(path: path, method: "POST", body: ["aweme_ids": chunk])
            let (data, http) = try await send(request)
            if http.statusCode == 401 { throw APIError.unauthorized }
            guard http.statusCode == 200 else {
                throw APIError.server(detail(from: data) ?? "取直链失败（HTTP \(http.statusCode)）")
            }
            let envelope = try decode(APIEnvelope<PlaybackURLsData>.self, from: data)
            result.merge(envelope.data?.urls ?? [:]) { _, new in new }
            if let serverTTL = envelope.data?.ttlSeconds, serverTTL > 0 { ttl = TimeInterval(serverTTL) }
        }
        return PlaybackBatch(urls: result, ttl: ttl)
    }
}
