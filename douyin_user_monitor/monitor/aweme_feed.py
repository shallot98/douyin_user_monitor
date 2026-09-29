"""User works feed: normalize Douyin post pages into browsable UI items.

Read-only path — never touches monitor state, so it stays outside the
`update_state` concurrency contract that all write paths must follow.

Playback URL handling: the list call already carries `play_addr`, so resolved
video URLs are cached here and the media proxy looks them up by aweme_id
instead of accepting a caller-supplied URL (no open proxy / SSRF surface).
Cache misses fall back to a single `fetch_one_video`.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set

from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.sync_utils import format_publish_time
from douyin_user_monitor.monitor.timeutils import safe_int
from douyin_user_monitor.monitor.video_source import extract_video_source

MEDIA_TYPE_IMAGE = "image"
MEDIA_TYPE_VIDEO = "video"

MIN_FEED_COUNT = 1
MAX_FEED_COUNT = 20
DEFAULT_FEED_COUNT = 20

# 签名 CDN 地址有时效，缓存别留太久；条数上限防长时间浏览把内存吃掉
DEFAULT_PLAY_URL_TTL_SECONDS = 1800.0
DEFAULT_PLAY_URL_MAX_ENTRIES = 2000

STATISTICS_FIELDS = ("digg_count", "comment_count", "collect_count", "share_count")


class FeedCrawlerProtocol(Protocol):
    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int) -> Dict[str, Any]:
        ...

    async def fetch_one_video(self, aweme_id: str) -> Dict[str, Any]:
        ...


def clamp_feed_count(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FEED_COUNT
    if value < MIN_FEED_COUNT:
        return DEFAULT_FEED_COUNT
    return min(value, MAX_FEED_COUNT)


def clamp_cursor(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _first_url(url_container: Any) -> Optional[str]:
    if not isinstance(url_container, dict):
        return None
    url_list = url_container.get("url_list")
    if not isinstance(url_list, list):
        return None
    for candidate in url_list:
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def extract_cover_url(aweme_detail: Dict[str, Any]) -> Optional[str]:
    """Static thumbnail. Image posts still carry a video.cover, so try it first."""
    video = aweme_detail.get("video")
    if isinstance(video, dict):
        for key in ("cover", "origin_cover", "dynamic_cover"):
            url = _first_url(video.get(key))
            if url:
                return url
    image_urls = extract_image_urls(aweme_detail)
    return image_urls[0] if image_urls else None


def _extract_statistics(aweme_detail: Dict[str, Any]) -> Dict[str, int]:
    raw = aweme_detail.get("statistics")
    if not isinstance(raw, dict):
        raw = {}
    return {field: safe_int(raw.get(field)) for field in STATISTICS_FIELDS}


def _video_duration_ms(aweme_detail: Dict[str, Any]) -> int:
    video = aweme_detail.get("video")
    if not isinstance(video, dict):
        return 0
    return safe_int(video.get("duration"))


def build_feed_item(aweme_detail: Dict[str, Any], *, downloaded_ids: Set[str]) -> Optional[Dict[str, Any]]:
    """Normalize one aweme into a UI card. Returns None when unusable (no id)."""
    if not isinstance(aweme_detail, dict):
        return None
    aweme_id = str(aweme_detail.get("aweme_id") or "").strip()
    if not aweme_id:
        return None

    image_post = is_image_post(aweme_detail)
    image_urls = extract_image_urls(aweme_detail) if image_post else []
    create_time = safe_int(aweme_detail.get("create_time"))

    return {
        "aweme_id": aweme_id,
        "desc": str(aweme_detail.get("desc") or "").strip(),
        "media_type": MEDIA_TYPE_IMAGE if image_post else MEDIA_TYPE_VIDEO,
        "create_time": create_time or None,
        "publish_time": format_publish_time(create_time),
        "cover_url": extract_cover_url(aweme_detail),
        "image_urls": image_urls,
        "image_count": len(image_urls),
        "duration_ms": 0 if image_post else _video_duration_ms(aweme_detail),
        "statistics": _extract_statistics(aweme_detail),
        "downloaded": aweme_id in downloaded_ids,
        "share_url": f"https://www.douyin.com/video/{aweme_id}",
    }


def _normalize_aweme_list(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def build_feed_page(page: Dict[str, Any], *, downloaded_ids: Iterable[str]) -> Dict[str, Any]:
    """Turn a raw user-post page into `{items, has_more, next_cursor}`."""
    downloaded = {str(item).strip() for item in downloaded_ids if str(item).strip()}
    aweme_list = _normalize_aweme_list((page or {}).get("aweme_list"))
    aweme_list.sort(key=lambda item: safe_int(item.get("create_time")), reverse=True)

    items: List[Dict[str, Any]] = []
    for aweme_detail in aweme_list:
        item = build_feed_item(aweme_detail, downloaded_ids=downloaded)
        if item is not None:
            items.append(item)

    next_cursor = safe_int((page or {}).get("max_cursor"))
    has_more = bool((page or {}).get("has_more")) and next_cursor > 0
    return {
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor if has_more else None,
    }


class PlayUrlCache:
    """TTL + size bounded aweme_id → play URL map (insertion-order eviction)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PLAY_URL_TTL_SECONDS,
        max_entries: int = DEFAULT_PLAY_URL_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: Dict[str, tuple[str, float]] = {}

    def put(self, aweme_id: str, url: str) -> None:
        key = str(aweme_id or "").strip()
        value = str(url or "").strip()
        if not key or not value:
            return
        # 重新插入以刷新淘汰顺序
        self._entries.pop(key, None)
        self._entries[key] = (value, self._clock() + self._ttl_seconds)
        while len(self._entries) > self._max_entries:
            oldest_key = next(iter(self._entries))
            self._entries.pop(oldest_key, None)

    def get(self, aweme_id: str) -> Optional[str]:
        key = str(aweme_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            return None
        url, expires_at = entry
        if self._clock() >= expires_at:
            self._entries.pop(key, None)
            return None
        return url


class AwemeFeedService:
    """List a monitored user's works and resolve playable video URLs."""

    def __init__(self, crawler: FeedCrawlerProtocol, *, cache: Optional[PlayUrlCache] = None) -> None:
        self._crawler = crawler
        self._cache = cache if cache is not None else PlayUrlCache()

    async def list_user_awemes(
        self,
        sec_user_id: str,
        *,
        cursor: int = 0,
        count: int = DEFAULT_FEED_COUNT,
        downloaded_ids: Iterable[str] = (),
    ) -> Dict[str, Any]:
        page = await self._crawler.fetch_user_post_videos(
            sec_user_id,
            clamp_cursor(cursor),
            clamp_feed_count(count),
        )
        if not isinstance(page, dict):
            raise ValueError("作品列表返回格式无效")
        result = build_feed_page(page, downloaded_ids=downloaded_ids)
        self._cache_play_urls(page)
        return result

    def _cache_play_urls(self, page: Dict[str, Any]) -> None:
        """Reuse play_addr already present in the list page — no extra request on play."""
        for aweme_detail in _normalize_aweme_list(page.get("aweme_list")):
            aweme_id = str(aweme_detail.get("aweme_id") or "").strip()
            if not aweme_id or is_image_post(aweme_detail):
                continue
            source = extract_video_source(aweme_detail)
            if source is not None:
                self._cache.put(aweme_id, source.url)

    async def resolve_play_url(self, aweme_id: str) -> str:
        key = str(aweme_id or "").strip()
        if not key:
            raise ValueError("缺少作品 ID")
        cached = self._cache.get(key)
        if cached:
            return cached

        payload = await self._crawler.fetch_one_video(key)
        detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
        if not isinstance(detail, dict):
            raise ValueError(f"作品 {key} 缺少 aweme_detail")
        if is_image_post(detail):
            raise ValueError(f"作品 {key} 是图文，没有视频地址")
        source = extract_video_source(detail)
        if source is None:
            raise ValueError(f"作品 {key} 未找到可播放地址")
        self._cache.put(key, source.url)
        return source.url
