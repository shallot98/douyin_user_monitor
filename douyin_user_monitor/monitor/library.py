"""作品库：跨用户浏览已打过擦边分的作品（纯函数，无 IO）。

索引只收“可入库”的作品：content_filter 第 3 版、status=ok、成年人物有 borderline
且 score>=1。没打分的一律不出现在作品库里。

索引按发布时间倒序预排好，筛选保持该顺序，newest/oldest/score/duration 只需
稳定排序或反转；random 按 crc32(seed, aweme_id) 排，同一 seed 翻页结果稳定。

分面计数是标准的 faceted search：每个分组的计数忽略该分组自己的筛选条件，
所以只需对每条作品算出“挂在哪些分组上”——全部通过计入所有分面，只挂一个
分面分组则只计入那个分面，挂两个及以上不计入任何分面。
"""

from __future__ import annotations

import random
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import chain
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from douyin_user_monitor.monitor.timeutils import CHINA_TZ, safe_int

# 重打期间新旧两版都收（v3 没有身材分项，按 0 处理）；v4 权重有误已作废；全部重打完后只留 5
LIBRARY_FILTER_VERSIONS = (3, 5)

DEFAULT_PAGE_SIZE = 60
MIN_PAGE_SIZE = 12
MAX_PAGE_SIZE = 120

# 单项排序：sort 值 → 子分项名（按该分项从高到低）
SUB_SORTS = {"chest": "chest", "body": "body", "overall": "overall", "camera": "camera", "motion": "motion"}
SORTS = ("newest", "oldest", "score", "duration", "random", *SUB_SORTS)
MEDIA_TYPES = ("video", "image")
CATEGORIES = ("normal", "ad", "text_only", "screenshot")

# 标签分组：outfit 来自 appearance，enhancers 来自 borderline，其余来自 tags
TAG_GROUPS = ("outfit", "scene", "content", "style", "subject", "features", "enhancers")
EXCLUDE_GROUPS = TAG_GROUPS + ("category",)

# 子分项及其上限
# body = 身材（臀部与曲线合成，臀部占大头），由打分程序写入 borderline.body
SUB_SCORES = {"chest": 3, "body": 3, "overall": 3, "camera": 3, "motion": 3}
SCORE_MIN, SCORE_MAX = 1, 5

TAG_FACET_LIMIT = 40
USER_FACET_LIMIT = 60

# 参与分面“排除自身”计算的分组名
_FACET_GROUPS = TAG_GROUPS + ("category", "media_type", "users", "score")
_TOKEN_SEP = "\x00"


# ---- 索引 -----------------------------------------------------------------


@dataclass(frozen=True)
class LibraryEntry:
    user_id: str
    nickname: str
    record: Dict[str, Any]
    aweme_id: str
    aweme_key: bytes
    sort_ms: int
    publish_date: Optional[str]
    duration_ms: int
    score: int
    subs: Dict[str, int]
    violation: bool
    category: str
    media_type: str
    groups: Dict[str, frozenset]
    text: str
    # 分面计数用的扁平 token（"分组\x00值"），一次 Counter 就能数完全部分面
    facet_tokens: Tuple[str, ...]


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ)


_EMPTY_SET: frozenset = frozenset()


def _str_set(values: Any) -> frozenset:
    if not values or not isinstance(values, list):
        return _EMPTY_SET
    return frozenset(text for text in (str(item or "").strip() for item in values) if text)


def eligible_borderline(record: Any) -> Optional[Dict[str, Any]]:
    """可入库作品返回其 borderline 分档，否则 None。"""
    if not isinstance(record, dict):
        return None
    result = record.get("content_filter")
    if not isinstance(result, dict):
        return None
    if result.get("version") not in LIBRARY_FILTER_VERSIONS or result.get("status") != "ok":
        return None
    appearance = result.get("appearance")
    borderline = appearance.get("borderline") if isinstance(appearance, dict) else None
    if not isinstance(borderline, dict):
        return None
    score = borderline.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or score < SCORE_MIN:
        return None
    return borderline


def rated_borderline_score(record: Any) -> Optional[int]:
    """已评分作品的擦边分；没评分/评分失败返回 None。

    与作品库同一套版本（LIBRARY_FILTER_VERSIONS）。已评分但画面里没有可评的成年人物
    （无 borderline 分档）按 0 分算：对“这个号值不值得看”来说它就是不擦边。
    """
    if not isinstance(record, dict):
        return None
    result = record.get("content_filter")
    if not isinstance(result, dict):
        return None
    if result.get("version") not in LIBRARY_FILTER_VERSIONS or result.get("status") != "ok":
        return None
    appearance = result.get("appearance")
    borderline = appearance.get("borderline") if isinstance(appearance, dict) else None
    score = borderline.get("score") if isinstance(borderline, dict) else None
    if isinstance(score, bool) or not isinstance(score, int):
        return 0
    return max(0, score)


def _build_entry(user_id: str, nickname: str, record: Dict[str, Any]) -> Optional[LibraryEntry]:
    borderline = eligible_borderline(record)
    aweme_id = str(record.get("aweme_id") or "").strip() if borderline is not None else ""
    if not aweme_id:
        return None
    result = record["content_filter"]
    appearance = result["appearance"]
    tags = result.get("tags") if isinstance(result.get("tags"), dict) else {}
    groups = {group: _str_set(tags.get(group)) for group in ("scene", "content", "style", "subject", "features")}
    groups["outfit"] = _str_set(appearance.get("outfit"))
    groups["enhancers"] = _str_set(borderline.get("enhancers"))

    published = _parse_time(record.get("publish_time"))
    downloaded = _parse_time(record.get("downloaded_at"))
    # 缺发布时间的退到最后（与作品页一致）
    if published is not None:
        sort_ms = int(published.timestamp() * 1000)
    else:
        sort_ms = -(10**15) + (int(downloaded.timestamp() * 1000) if downloaded is not None else 0)

    desc = str(record.get("desc") or "")
    summary = str(result.get("summary") or "")
    category = str(result.get("category") or "").strip()
    media_type = str(record.get("media_type") or "").strip() or "unknown"
    score = borderline["score"]
    tokens = [f"{group}{_TOKEN_SEP}{value}" for group in TAG_GROUPS for value in groups[group]]
    tokens += [f"category{_TOKEN_SEP}{category}", f"media_type{_TOKEN_SEP}{media_type}",
               f"users{_TOKEN_SEP}{user_id}", f"score{_TOKEN_SEP}{score}"]
    return LibraryEntry(
        user_id=user_id,
        nickname=nickname,
        record=record,
        aweme_id=aweme_id,
        aweme_key=aweme_id.encode("utf-8"),
        sort_ms=sort_ms,
        publish_date=published.date().isoformat() if published is not None else None,
        duration_ms=safe_int(record.get("video_duration_ms")),
        score=score,
        subs={name: safe_int(borderline.get(name)) for name in SUB_SCORES},
        violation=borderline.get("violation") is True,
        category=category,
        media_type=media_type,
        groups=groups,
        text="\n".join((nickname, desc, summary)).lower(),
        facet_tokens=tuple(tokens),
    )


def _user_entries(user_id: str, nickname: str, records: Iterable[Any]) -> List[LibraryEntry]:
    entries: List[LibraryEntry] = []
    seen = set()
    for record in records:
        entry = _build_entry(user_id, nickname, record) if isinstance(record, dict) else None
        if entry is None or entry.aweme_id in seen:
            continue
        seen.add(entry.aweme_id)
        entries.append(entry)
    return entries


def build_library_index(
    users: Iterable[Mapping[str, Any]],
    records_snapshot: Mapping[str, List[Dict[str, Any]]],
    user_cache: Optional[Dict[str, Tuple[Any, str, List[LibraryEntry]]]] = None,
) -> List[LibraryEntry]:
    """展平所有用户的可入库作品，按发布时间倒序（同刻按 aweme_id 倒序）。

    user_cache（可选，会被就地更新）：{user_id: (records 列表, 昵称, 条目)}。RecordStore
    的列表只整体替换不原地改，所以列表对象与昵称都没变的用户直接复用上次的条目——
    后台打标每批只改少数用户，重建就只解析这几个用户。
    """
    entries: List[LibraryEntry] = []
    fresh: Dict[str, Tuple[Any, str, List[LibraryEntry]]] = {}
    for user in users:
        user_id = str(user.get("id") or "").strip()
        if not user_id or user_id in fresh:
            continue
        nickname = str(user.get("nickname") or "").strip()
        records = records_snapshot.get(user_id) or []
        cached = user_cache.get(user_id) if user_cache is not None else None
        if cached is not None and cached[0] is records and cached[1] == nickname:
            user_entries = cached[2]
        else:
            user_entries = _user_entries(user_id, nickname, records)
        fresh[user_id] = (records, nickname, user_entries)
        entries.extend(user_entries)
    if user_cache is not None:
        user_cache.clear()
        user_cache.update(fresh)
    entries.sort(key=lambda entry: (entry.sort_ms, entry.aweme_id), reverse=True)
    return entries


# ---- 查询参数 ---------------------------------------------------------------


@dataclass
class LibraryQuery:
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort: str = "newest"
    seed: Optional[int] = None
    score_min: int = SCORE_MIN
    score_max: int = SCORE_MAX
    sub_mins: Dict[str, int] = field(default_factory=dict)
    media_type: str = ""
    categories: frozenset = frozenset()
    user_ids: frozenset = frozenset()
    q: str = ""
    tags: Dict[str, frozenset] = field(default_factory=dict)
    exclude: Dict[str, frozenset] = field(default_factory=dict)
    violation: bool = False
    date_from: Optional[str] = None
    date_to: Optional[str] = None


def _text(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    return str(value).strip() if value is not None else ""


def _int_param(params: Mapping[str, Any], key: str, label: str) -> Optional[int]:
    text = _text(params, key)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{label}须为整数：{text!r}") from None


def _ranged(params: Mapping[str, Any], key: str, label: str, low: int, high: int) -> Optional[int]:
    value = _int_param(params, key, label)
    if value is not None and not low <= value <= high:
        raise ValueError(f"{label}须在 {low}~{high} 之间：{value}")
    return value


def _comma_list(params: Mapping[str, Any], key: str) -> frozenset:
    return frozenset(item.strip() for item in _text(params, key).split(",") if item.strip())


def _date_param(params: Mapping[str, Any], key: str, label: str) -> Optional[str]:
    text = _text(params, key)
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValueError(f"{label}须为 YYYY-MM-DD：{text!r}")
    return parsed.isoformat()


def _parse_exclude(params: Mapping[str, Any]) -> Dict[str, frozenset]:
    grouped: Dict[str, set] = {}
    for pair in _comma_list(params, "exclude"):
        group, sep, value = pair.partition(":")
        group, value = group.strip(), value.strip()
        if not sep or not value or group not in EXCLUDE_GROUPS:
            raise ValueError(f"排除条件须为 分组:值，分组可选 {'/'.join(EXCLUDE_GROUPS)}：{pair!r}")
        if group == "category" and value not in CATEGORIES:
            raise ValueError(f"未知的排除分类：{value!r}")
        grouped.setdefault(group, set()).add(value)
    return {group: frozenset(values) for group, values in grouped.items()}


def parse_library_query(params: Mapping[str, Any]) -> LibraryQuery:
    """校验查询参数；非法输入抛 ValueError（中文信息），不静默忽略。"""
    query = LibraryQuery()
    page = _int_param(params, "page", "页码")
    if page is not None:
        if page < 1:
            raise ValueError(f"页码须 >= 1：{page}")
        query.page = page
    page_size = _int_param(params, "page_size", "每页条数")
    if page_size is not None:
        query.page_size = min(max(page_size, MIN_PAGE_SIZE), MAX_PAGE_SIZE)

    sort = _text(params, "sort") or "newest"
    if sort not in SORTS:
        raise ValueError(f"排序须为 {'/'.join(SORTS)}：{sort!r}")
    query.sort = sort
    query.seed = _int_param(params, "seed", "随机种子")
    if sort == "random" and query.seed is None:
        query.seed = random.randint(1, 2**31 - 1)
    if sort != "random":
        query.seed = None

    score_min = _int_param(params, "score_min", "擦边评分下限")
    score_max = _ranged(params, "score_max", "擦边评分上限", SCORE_MIN, SCORE_MAX)
    if score_min is not None:
        if score_min > SCORE_MAX:
            raise ValueError(f"擦边评分下限须在 {SCORE_MIN}~{SCORE_MAX} 之间：{score_min}")
        query.score_min = max(score_min, SCORE_MIN)
    if score_max is not None:
        query.score_max = score_max
    if query.score_min > query.score_max:
        raise ValueError(f"擦边评分下限不能大于上限：{query.score_min} > {query.score_max}")

    for name, maximum in SUB_SCORES.items():
        value = _ranged(params, f"{name}_min", f"{name}_min", 0, maximum)
        if value:
            query.sub_mins[name] = value

    media_type = _text(params, "media_type")
    if media_type and media_type not in MEDIA_TYPES:
        raise ValueError(f"作品类型须为 {'/'.join(MEDIA_TYPES)}：{media_type!r}")
    query.media_type = media_type
    query.categories = _comma_list(params, "category")
    unknown = sorted(query.categories - set(CATEGORIES))
    if unknown:
        raise ValueError(f"未知的分类：{'、'.join(unknown)}")
    query.user_ids = _comma_list(params, "user_ids")
    query.q = _text(params, "q").lower()
    query.tags = {group: values for group in TAG_GROUPS if (values := _comma_list(params, group))}
    query.exclude = _parse_exclude(params)

    violation = _text(params, "violation")
    if violation not in ("", "0", "1"):
        raise ValueError(f"violation 只能为 1 或 0：{violation!r}")
    query.violation = violation == "1"

    query.date_from = _date_param(params, "date_from", "起始日期")
    query.date_to = _date_param(params, "date_to", "结束日期")
    if query.date_from and query.date_to and query.date_from > query.date_to:
        raise ValueError(f"起始日期不能晚于结束日期：{query.date_from} > {query.date_to}")
    return query


# ---- 查询 -----------------------------------------------------------------

Predicate = Callable[[LibraryEntry], bool]


def _tag_predicate(group: str, wanted: frozenset) -> Predicate:
    return lambda entry: not wanted.isdisjoint(entry.groups[group])


def _exclude_predicate(exclude: Dict[str, frozenset]) -> Predicate:
    tag_excludes = [(group, values) for group, values in exclude.items() if group != "category"]
    categories = exclude.get("category", frozenset())

    def keep(entry: LibraryEntry) -> bool:
        if entry.category in categories:
            return False
        return all(values.isdisjoint(entry.groups[group]) for group, values in tag_excludes)

    return keep


def _sub_predicate(sub_mins: Dict[str, int]) -> Predicate:
    items = tuple(sub_mins.items())
    return lambda entry: all(entry.subs[name] >= minimum for name, minimum in items)


def _date_predicate(date_from: Optional[str], date_to: Optional[str]) -> Predicate:
    low, high = date_from or "0000-00-00", date_to or "9999-99-99"
    return lambda entry: entry.publish_date is not None and low <= entry.publish_date <= high


def _predicates(query: LibraryQuery) -> List[Tuple[str, Predicate]]:
    """只收激活的条件，便宜的放前面；分组名用于分面“排除自身”。"""
    active: List[Tuple[str, Predicate]] = []
    if query.score_min > SCORE_MIN or query.score_max < SCORE_MAX:
        low, high = query.score_min, query.score_max
        active.append(("score", lambda entry: low <= entry.score <= high))
    if query.media_type:
        active.append(("media_type", lambda entry, value=query.media_type: entry.media_type == value))
    if query.categories:
        active.append(("category", lambda entry, values=query.categories: entry.category in values))
    if query.user_ids:
        active.append(("users", lambda entry, values=query.user_ids: entry.user_id in values))
    if query.violation:
        active.append(("violation", lambda entry: entry.violation))
    if query.sub_mins:
        active.append(("sub", _sub_predicate(query.sub_mins)))
    if query.date_from or query.date_to:
        active.append(("date", _date_predicate(query.date_from, query.date_to)))
    for group, wanted in query.tags.items():
        active.append((group, _tag_predicate(group, wanted)))
    if query.exclude:
        active.append(("exclude", _exclude_predicate(query.exclude)))
    if query.q:
        active.append(("q", lambda entry, needle=query.q: needle in entry.text))
    return active


def _facet_values(group: str, entries: List[LibraryEntry]) -> Iterable[str]:
    if group in TAG_GROUPS:
        return chain.from_iterable(entry.groups[group] for entry in entries)
    if group == "category":
        return (entry.category for entry in entries)
    if group == "media_type":
        return (entry.media_type for entry in entries)
    if group == "users":
        return (entry.user_id for entry in entries)
    return (str(entry.score) for entry in entries)


def _facet_counts(matched: List[LibraryEntry], near: Dict[str, List[LibraryEntry]]) -> Dict[str, Counter]:
    """每个分面 = 全部命中 + 只差本分组条件的作品（Counter 走 C 实现，25k 条也很快）。"""
    counters: Dict[str, Counter] = {group: Counter() for group in _FACET_GROUPS}
    for token, count in Counter(chain.from_iterable(entry.facet_tokens for entry in matched)).items():
        group, _, value = token.partition(_TOKEN_SEP)
        counters[group][value] = count
    for group, entries in near.items():
        if entries:
            counters[group].update(_facet_values(group, entries))
    return counters


def _ranked(bucket: Dict[str, int], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    ordered = sorted(bucket.items(), key=lambda item: (-item[1], item[0]))
    return [{"value": value, "count": count} for value, count in ordered[:limit]]


def _sorted(entries: List[LibraryEntry], sort: str, seed: Optional[int]) -> List[LibraryEntry]:
    """entries 已是 newest 顺序；其余排序都以它为次序键（稳定排序）。"""
    if sort == "oldest":
        return entries[::-1]
    if sort == "score":
        return sorted(entries, key=lambda entry: -entry.score)
    if sort == "duration":
        return sorted(entries, key=lambda entry: -entry.duration_ms)
    if sort in SUB_SORTS:
        # 单项高分：先按该分项，同分再按总分，其余维持 newest 顺序
        key = SUB_SORTS[sort]
        return sorted(entries, key=lambda entry: (-entry.subs.get(key, 0), -entry.score))
    if sort == "random":
        prefix = zlib.crc32(f"{seed}:".encode("utf-8"))
        # 撞 hash 时靠稳定排序落回 newest 顺序，仍然确定
        return sorted(entries, key=lambda entry: zlib.crc32(entry.aweme_key, prefix))
    return entries


def _match(index: List[LibraryEntry], predicates: List[Tuple[str, Predicate]]):
    """返回 (全部命中, {分面分组: 只差这一个条件的作品})。"""
    near: Dict[str, List[LibraryEntry]] = {group: [] for group in _FACET_GROUPS}
    if not predicates:
        return index, near
    matched: List[LibraryEntry] = []
    for entry in index:
        failed: Optional[str] = None
        for group, predicate in predicates:
            if predicate(entry):
                continue
            if failed is not None or group not in near:
                break
            failed = group
        else:
            if failed is None:
                matched.append(entry)
            else:
                near[failed].append(entry)
    return matched, near


def query_library(index: List[LibraryEntry], query: LibraryQuery) -> Dict[str, Any]:
    matched, near = _match(index, _predicates(query))
    counters = _facet_counts(matched, near)

    total = len(matched)
    total_pages = max(1, -(-total // query.page_size))
    # 页码超出范围返回空页（page 保持请求值），不回退到末页，避免前端重复渲染
    page = query.page
    start = (page - 1) * query.page_size
    ordered = _sorted(matched, query.sort, query.seed) if start < total else []

    nicknames = {entry.user_id: entry.nickname for entry in index}
    users_facet = [
        {"user_id": item["value"], "nickname": nicknames.get(item["value"], ""), "count": item["count"]}
        for item in _ranked(counters["users"], USER_FACET_LIMIT)
    ]
    facets: Dict[str, Any] = {group: _ranked(counters[group], TAG_FACET_LIMIT) for group in TAG_GROUPS}
    facets["category"] = _ranked(counters["category"])
    facets["media_type"] = _ranked(counters["media_type"])
    facets["users"] = users_facet

    return {
        "entries": ordered[start : start + query.page_size],
        "page": page,
        "page_size": query.page_size,
        "total": total,
        "total_pages": total_pages,
        "seed": query.seed,
        "users_total": len({entry.user_id for entry in matched}),
        "score_histogram": {str(score): counters["score"].get(str(score), 0)
                            for score in range(SCORE_MIN, SCORE_MAX + 1)},
        "facets": facets,
        "eligible_total": len(index),
    }
