import Foundation

struct APIEnvelope<T: Decodable>: Decodable {
    let code: Int?
    let message: String?
    let data: T?
}

struct LibraryPage: Decodable {
    let page: Int?
    let totalPages: Int?
    let total: Int?
    let seed: Int?
    let items: [FeedItem]

    enum CodingKeys: String, CodingKey {
        case page, total, seed, items
        case totalPages = "total_pages"
    }
}

struct PlaybackURLsData: Decodable {
    let urls: [String: String?]?
    let ttlSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case urls
        case ttlSeconds = "ttl_seconds"
    }
}

/// 作品库条目：字段全部可选（游客接口只下发播放需要的字段）
struct FeedItem: Decodable {
    let awemeId: String
    let userId: String
    let mediaType: String?
    let nickname: String?
    let avatarUrl: String?
    let desc: String?
    let playIndex: Int?
    let coverIndex: Int?
    let coverUrl: String?
    let durationMs: Int?
    let publishTime: String?
    let shareUrl: String?
    let filter: LooseJSON?

    var key: String { "\(userId)/\(awemeId)" }

    /// 没有可播放文件的作品不进 feed（和网页 playable() 一致）
    var isPlayable: Bool { !awemeId.isEmpty && !userId.isEmpty && playIndex != nil }

    enum CodingKeys: String, CodingKey {
        case awemeId = "aweme_id"
        case userId = "user_id"
        case mediaType = "media_type"
        case nickname, desc
        case avatarUrl = "avatar_url"
        case playIndex = "play_index"
        case coverIndex = "cover_index"
        case coverUrl = "cover_url"
        case durationMs = "duration_ms"
        case publishTime = "publish_time"
        case shareUrl = "share_url"
        case filter = "content_filter"
    }
}

/// 只用来读 AI 标注结论（shape 随服务端演进，用宽松解码不炸）
enum LooseJSON: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([LooseJSON])
    case object([String: LooseJSON])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null; return }
        if let value = try? container.decode(Bool.self) { self = .bool(value); return }
        if let value = try? container.decode(Double.self) { self = .number(value); return }
        if let value = try? container.decode(String.self) { self = .string(value); return }
        if let value = try? container.decode([LooseJSON].self) { self = .array(value); return }
        if let value = try? container.decode([String: LooseJSON].self) { self = .object(value); return }
        self = .null
    }

    var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    var intValue: Int? {
        switch self {
        case .number(let value): return Int(value)
        case .string(let value): return Int(value)
        default: return nil
        }
    }

    subscript(key: String) -> LooseJSON? {
        if case .object(let dict) = self { return dict[key] }
        return nil
    }

    var strings: [String] {
        switch self {
        case .array(let values): return values.compactMap { $0.stringValue }
        case .string(let value): return [value]
        default: return []
        }
    }
}

/// 从 content_filter 里取展示需要的结论：擦边分 + 标签
struct ItemHighlights {
    let score: Int?
    let tags: [String]

    /// 标签展示顺序与网页 TAG_GROUPS 一致；来源：着装看 appearance，增强看 appearance.borderline，其余看 tags
    private static let groupOrder = ["outfit", "content", "scene", "style", "subject", "features", "enhancers"]
    private static let tagSources: [String: String] = ["outfit": "appearance", "enhancers": "borderline"]

    init(filter: LooseJSON?) {
        let borderline = filter?["appearance"]?["borderline"]
        score = borderline?["score"]?.intValue
        var collected: [String] = []
        var seen = Set<String>()
        for group in Self.groupOrder {
            let source = Self.tagSources[group] ?? "tags"
            let bag = source == "borderline" ? borderline : filter?[source]
            for value in bag?[group]?.strings ?? [] {
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !trimmed.isEmpty, !seen.contains(trimmed) else { continue }
                seen.insert(trimmed)
                collected.append(trimmed)
            }
        }
        tags = Array(collected.prefix(8))
    }
}
