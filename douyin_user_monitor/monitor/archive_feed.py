"""Works list built from local `download_records` — no Douyin request at all.

Reading the list from Douyin live risks anti-bot throttling and, for accounts
that hid their post list, fails outright (403) even though the works are already
archived. The local records carry everything a gallery card needs, so the list
is served from state and only the media bytes come from the archive.

Unlike Douyin's cursor API this is a plain local list, so pagination is真 offset
based: total and total_pages are known and any page can be jumped to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from douyin_user_monitor.monitor.library import rated_borderline_score
from douyin_user_monitor.monitor.timeutils import CHINA_TZ, safe_int

MEDIA_TYPE_IMAGE = "image"
MEDIA_TYPE_VIDEO = "video"

ALLOWED_PAGE_SIZES = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

IMAGE_FILE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp")


def clamp_page_size(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if value < 1:
        return DEFAULT_PAGE_SIZE
    return min(value, MAX_PAGE_SIZE)


def clamp_page(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _clean_files(record: Dict[str, Any]) -> List[str]:
    files = record.get("files")
    if not isinstance(files, list):
        return []
    return [str(item).strip() for item in files if str(item or "").strip()]


def _is_image_file(relative_path: str) -> bool:
    return relative_path.lower().endswith(IMAGE_FILE_SUFFIXES)


def _index_of(files: List[str], wanted: Any) -> Optional[int]:
    target = str(wanted or "").strip()
    if not target:
        return None
    try:
        return files.index(target)
    except ValueError:
        return None


def _cover_index(record: Dict[str, Any], media_type: str, files: List[str]) -> Optional[int]:
    """优先用下载时单独存的小封面；老记录回落到图文第一张图。"""
    explicit = _index_of(files, record.get("cover_file"))
    if explicit is not None:
        return explicit
    if media_type != MEDIA_TYPE_IMAGE:
        return None
    for index, relative_path in enumerate(files):
        if _is_image_file(relative_path):
            return index
    return 0 if files else None


def _play_index(record: Dict[str, Any], media_type: str, files: List[str]) -> Optional[int]:
    """网页播放优先用快播版本；没有就用归档原画。"""
    if media_type != MEDIA_TYPE_VIDEO:
        return None
    for key in ("fast_play_file", "video_file"):
        index = _index_of(files, record.get(key))
        if index is not None:
            return index
    # 老记录：files[0] 就是原画
    return 0 if files else None


def _image_indices(record: Dict[str, Any], media_type: str, files: List[str]) -> List[int]:
    """图文的内容图下标，排除单独存的封面（否则看图器会多出一张）。"""
    if media_type != MEDIA_TYPE_IMAGE:
        return []
    cover_position = _index_of(files, record.get("cover_file"))
    return [
        index
        for index, relative_path in enumerate(files)
        if index != cover_position and _is_image_file(relative_path)
    ]


def _archive_index(record: Dict[str, Any], media_type: str, files: List[str]) -> Optional[int]:
    """归档原画（下载/收藏用），与播放用的快播版本区分开。"""
    if media_type != MEDIA_TYPE_VIDEO:
        return None
    index = _index_of(files, record.get("video_file"))
    if index is not None:
        return index
    return 0 if files else None


def _to_millis(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return int(parsed.timestamp() * 1000)


def _sort_key(record: Dict[str, Any]) -> Tuple[int, int]:
    """按发布时间倒序；缺发布时间的退到最后，用下载时间兜底。"""
    published = _to_millis(record.get("publish_time"))
    downloaded = _to_millis(record.get("downloaded_at"))
    if published is not None:
        return (1, published)
    if downloaded is not None:
        return (0, downloaded)
    return (0, 0)


def build_archive_item(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    aweme_id = str(record.get("aweme_id") or "").strip()
    if not aweme_id:
        return None

    files = _clean_files(record)
    media_type = str(record.get("media_type") or "").strip() or "unknown"
    video_source = record.get("video_source")

    return {
        "aweme_id": aweme_id,
        "desc": str(record.get("desc") or "").strip(),
        "publish_time": record.get("publish_time"),
        "downloaded_at": record.get("downloaded_at"),
        "media_type": media_type,
        "source": str(record.get("source") or "").strip(),
        "image_count": safe_int(record.get("image_count")),
        "duration_ms": safe_int(record.get("video_duration_ms")),
        "total_size_bytes": safe_int(record.get("total_size_bytes")),
        "file_count": len(files),
        "cover_index": _cover_index(record, media_type, files),
        "play_index": _play_index(record, media_type, files),
        "archive_index": _archive_index(record, media_type, files),
        "has_fast_play": _index_of(files, record.get("fast_play_file")) is not None,
        "image_indices": _image_indices(record, media_type, files),
        # 归档目录名（改名后能看出这条属于哪个历史目录）
        "archive_dir": files[0].split("/")[0] if files else "",
        "video_source": dict(video_source) if isinstance(video_source, dict) else None,
        "fast_play_source": (
            dict(record["fast_play_source"]) if isinstance(record.get("fast_play_source"), dict) else None
        ),
        "share_url": f"https://www.douyin.com/video/{aweme_id}",
        "content_filter": _content_filter_summary(record.get("content_filter")),
    }


def _content_filter_summary(value: Any) -> Optional[Dict[str, Any]]:
    """作品页只需要结论，不下发 signals/motion 等调试细节。

    version / caliber 要下发：判定口径变过（violation 规则、滞回），
    前端得能区分新旧口径，否则同一列表里两种算法看起来完全一样。
    """
    if not isinstance(value, dict):
        return None
    keys = ("status", "verdict", "category", "ad_type", "confidence", "reason", "error", "mode", "tags",
            "appearance", "summary", "version", "caliber", "inconsistent")
    return {key: value.get(key) for key in keys if value.get(key) is not None}


def _sorted_items(records: Iterable[Any]) -> List[Dict[str, Any]]:
    usable = [record for record in records if isinstance(record, dict)]
    usable.sort(key=_sort_key, reverse=True)
    items = []
    for record in usable:
        item = build_archive_item(record)
        if item is not None:
            items.append(item)
    return items


AI_FILTER_VIEWS = ("", "flagged", "error", "inconsistent", "borderline3", "borderline4",
                   "borderline3_strict", "borderline4_strict")


def _borderline(item: Dict[str, Any]) -> Dict[str, Any]:
    appearance = (item.get("content_filter") or {}).get("appearance") or {}
    borderline = appearance.get("borderline") if isinstance(appearance, dict) else None
    return borderline if isinstance(borderline, dict) else {}


def borderline_score(item: Dict[str, Any]) -> Optional[int]:
    score = _borderline(item).get("score")
    return score if isinstance(score, int) else None


def _matches_ai_filter(item: Dict[str, Any], view: str) -> bool:
    summary = item.get("content_filter") or {}
    if view == "flagged":
        return summary.get("verdict") == "reject"
    if view == "error":
        return summary.get("status") == "error"
    if view == "inconsistent":
        # 模型自相矛盾的条目：分数不能全信，值得重打或人工看一眼
        return bool(summary.get("inconsistent"))
    if view.startswith("borderline"):
        strict = view.endswith("_strict")
        minimum = view.removeprefix("borderline").removesuffix("_strict")
        # *_strict 用保守档 score_low：重跑一遍分数会抖，实测 15% 会换档，
        # 想「宁可少判」时用这个视图
        score = _borderline(item).get("score_low" if strict else "score")
        return isinstance(score, int) and score >= int(minimum)
    return True


SCORE_FILTER_MIN, SCORE_FILTER_MAX = 0, 5


def _score_bound(raw: Optional[int], name: str) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not SCORE_FILTER_MIN <= raw <= SCORE_FILTER_MAX:
        raise ValueError(f"{name}须在 {SCORE_FILTER_MIN}~{SCORE_FILTER_MAX} 之间：{raw!r}")
    return raw


def build_archive_page(
    user: Dict[str, Any],
    *,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    ai_filter: str = "",
    media_type: str = "",
    score_min: Optional[int] = None,
    score_max: Optional[int] = None,
    unscored: bool = False,
) -> Dict[str, Any]:
    """score_min/score_max 按作品库同口径的擦边分（已评分无人物按 0 分）筛，只留已评分作品；
    unscored 只看没评分/评分失败的作品，不能和区间同时用。"""
    if ai_filter not in AI_FILTER_VIEWS:
        raise ValueError(f"未知的 AI 筛选视图：{ai_filter!r}")
    if media_type and media_type not in (MEDIA_TYPE_VIDEO, MEDIA_TYPE_IMAGE):
        raise ValueError(f"作品类型须为 {MEDIA_TYPE_VIDEO}/{MEDIA_TYPE_IMAGE}：{media_type!r}")
    low = _score_bound(score_min, "擦边分下限")
    high = _score_bound(score_max, "擦边分上限")
    if low is not None and high is not None and low > high:
        raise ValueError(f"擦边分下限不能大于上限：{low} > {high}")
    if unscored and (low is not None or high is not None):
        raise ValueError("「未评分」不能和擦边分区间同时使用")
    records = user.get("download_records")
    items = _sorted_items(records if isinstance(records, list) else [])
    if ai_filter:
        items = [item for item in items if _matches_ai_filter(item, ai_filter)]
    if media_type:
        items = [item for item in items if item["media_type"] == media_type]
    if unscored:
        items = [item for item in items if rated_borderline_score(item) is None]
    elif low is not None or high is not None:
        low = SCORE_FILTER_MIN if low is None else low
        high = SCORE_FILTER_MAX if high is None else high
        items = [item for item in items if (score := rated_borderline_score(item)) is not None and low <= score <= high]

    size = clamp_page_size(page_size)
    total = len(items)
    total_pages = max(1, -(-total // size))
    current_page = min(clamp_page(page), total_pages)
    start = (current_page - 1) * size

    page_items = items[start : start + size]
    return {
        "items": page_items,
        "page": current_page,
        "page_size": size,
        "total": total,
        "total_pages": total_pages,
        "media_counts": {
            MEDIA_TYPE_VIDEO: sum(1 for item in items if item["media_type"] == MEDIA_TYPE_VIDEO),
            MEDIA_TYPE_IMAGE: sum(1 for item in items if item["media_type"] == MEDIA_TYPE_IMAGE),
        },
    }


def neighbor_items(
    user: Dict[str, Any], aweme_id: str, *, before: int = 1, after: int = 2
) -> List[Dict[str, Any]]:
    """列表顺序里 aweme_id 前后的作品（不含自己，跨分页）；找不到返回空列表。

    after 在前、离当前近的在前：预取时优先「下一个」。
    """
    records = user.get("download_records")
    items = _sorted_items(records if isinstance(records, list) else [])
    wanted = str(aweme_id or "").strip()
    position = next((i for i, item in enumerate(items) if item["aweme_id"] == wanted), None)
    if position is None:
        return []
    following = items[position + 1 : position + 1 + max(0, after)]
    preceding = items[max(0, position - max(0, before)) : position][::-1]
    return following + preceding


def find_record(user: Dict[str, Any], aweme_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(aweme_id or "").strip()
    if not wanted:
        return None
    records = user.get("download_records")
    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and str(record.get("aweme_id") or "").strip() == wanted:
            return record
    return None


def resolve_archive_file(user: Dict[str, Any], aweme_id: str, index: Any) -> str:
    """Relative archive path for one file of one work. Raises KeyError/IndexError."""
    record = find_record(user, aweme_id)
    if record is None:
        raise KeyError(f"作品 {aweme_id} 不在下载记录中")
    files = _clean_files(record)
    if not files:
        raise KeyError(f"作品 {aweme_id} 没有已归档文件")
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise IndexError("文件下标无效") from exc
    if position < 0 or position >= len(files):
        raise IndexError(f"文件下标越界: {position}")
    return files[position]


def list_archive_files(user: Dict[str, Any], aweme_id: str) -> List[str]:
    record = find_record(user, aweme_id)
    if record is None:
        return []
    return _clean_files(record)
