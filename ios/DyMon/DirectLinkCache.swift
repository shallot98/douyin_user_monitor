import Foundation

/// 播放直链缓存：服务端按用户批量解析，客户端按作品存 URL + TTL，失败回落服务端代理
actor DirectLinkCache {
    private struct Entry {
        let url: String?
        let expiresAt: Date
    }

    private let api: APIClient
    private let refreshMargin: TimeInterval = 60
    private let failureBackoff: TimeInterval = 60

    private var entries: [String: Entry] = [:]
    private var pending: Set<String> = []

    init(api: APIClient) {
        self.api = api
    }

    private func fresh(_ awemeId: String) -> Bool {
        guard let entry = entries[awemeId] else { return false }
        return entry.expiresAt > Date()
    }

    /// 当前这条：返回直链；nil 表示只能走服务端代理
    func resolve(_ item: FeedItem) async -> String? {
        if fresh(item.awemeId) { return entries[item.awemeId]?.url }
        if pending.contains(item.awemeId) {
            // 同一批已经在解析：等它落库，别再打一次
            for _ in 0..<32 {
                try? await Task.sleep(nanoseconds: 250_000_000)
                if fresh(item.awemeId) { return entries[item.awemeId]?.url }
            }
            return nil
        }
        await fetch(userId: item.userId, awemeIds: [item.awemeId])
        return entries[item.awemeId]?.url
    }

    /// 后台把接下来几条的直链都解析好（按用户分组批量请求）
    func prefetch(_ items: [FeedItem]) {
        var grouped: [String: [String]] = [:]
        for item in items where !fresh(item.awemeId) && !pending.contains(item.awemeId) {
            grouped[item.userId, default: []].append(item.awemeId)
        }
        for (userId, awemeIds) in grouped {
            for chunk in awemeIds.chunked(into: 100) {
                Task { await self.fetch(userId: userId, awemeIds: chunk) }
            }
        }
    }

    func invalidate(_ awemeId: String) {
        entries.removeValue(forKey: awemeId)
    }

    private func fetch(userId: String, awemeIds: [String]) async {
        for awemeId in awemeIds { pending.insert(awemeId) }
        defer { for awemeId in awemeIds { pending.remove(awemeId) } }
        do {
            let batch = try await api.playbackURLs(userId: userId, awemeIds: awemeIds)
            let expiresAt = Date().addingTimeInterval(max(batch.ttl - refreshMargin, 30))
            for awemeId in awemeIds {
                entries[awemeId] = Entry(url: batch.urls[awemeId] ?? nil, expiresAt: expiresAt)
            }
        } catch {
            AppLog.info("直链解析失败，本条改走代理：\(error.localizedDescription)")
            let expiresAt = Date().addingTimeInterval(failureBackoff)
            for awemeId in awemeIds { entries[awemeId] = Entry(url: nil, expiresAt: expiresAt) }
        }
    }
}
