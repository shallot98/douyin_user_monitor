from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from douyin_user_monitor import auth
from douyin_user_monitor.api.archive_media_cache import ArchiveMediaCache
from douyin_user_monitor.api.covers import cover_response, with_cover_urls
from douyin_user_monitor.api.media_proxy import (
    MediaUpstreamError,
    UnsatisfiableRangeError,
    build_response_headers,
    build_upstream_headers,
    iter_file_range,
    open_media_stream,
    parse_byte_range,
)
from douyin_user_monitor.api.models import ErrorResponseModel, ResponseModel
from douyin_user_monitor.crawler.inprocess_client import DEFAULT_HEADERS, InProcessDouyinClient
from douyin_user_monitor.monitor import api_keys
from douyin_user_monitor.monitor.archive_feed import (
    build_archive_item,
    build_archive_page,
    find_record,
    neighbor_items,
    resolve_archive_file,
)
from douyin_user_monitor.monitor.archive_path_aliases import ArchivePathAliasError, load_archive_path_aliases
from douyin_user_monitor.monitor.aweme_feed import (
    DEFAULT_FEED_COUNT,
    AwemeFeedService,
    clamp_cursor,
    clamp_feed_count,
)
from douyin_user_monitor.monitor.content_filter import ContentFilter
from douyin_user_monitor.monitor.content_filter_media import ArchiveMediaLoader
from douyin_user_monitor.monitor.content_filter_queue import ContentFilterQueue
from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessConfig
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.cookie_pool import CookiePoolService
from douyin_user_monitor.monitor.cover_store import CoverStore
from douyin_user_monitor.monitor.dashboard_payload import (
    DASHBOARD_ACTIVITY_LIMIT,
    build_recent_activity,
)
from douyin_user_monitor.monitor.errors import sanitize_public_error
from douyin_user_monitor.monitor.hermes_weixin_sender import HermesWeixinConfig, HermesWeixinSender
from douyin_user_monitor.monitor.library import (
    LibraryEntry,
    build_library_index,
    parse_library_query,
    query_library,
)
from douyin_user_monitor.monitor.new_work_cover import NewWorkCoverPublisher
from douyin_user_monitor.monitor.nickname_history import build_former_nicknames
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.monitor.parse_cookies import ParseCookiePool
from douyin_user_monitor.monitor.parse_service import ParseService
from douyin_user_monitor.monitor.record_store import RecordStore
from douyin_user_monitor.monitor.schedule import MODE_COVERAGE, MODE_INTERVAL
from douyin_user_monitor.monitor.service import (
    MIN_INTERVAL_HOURS,
    MonitorService,
)
from douyin_user_monitor.monitor.statistics import build_user_statistics
from douyin_user_monitor.monitor.storage import FileStamp, MonitorStorage, file_stamp
from douyin_user_monitor.monitor.telegram_notifier import TelegramNotifier
from douyin_user_monitor.monitor.user_lookup import find_user_by_id
from douyin_user_monitor.monitor.user_table import query_monitor_users, wants_paginated_users
from douyin_user_monitor.settings_loaders import load_settings

router = APIRouter()
LOGGER = logging.getLogger(__name__)
AVATAR_CACHE_SECONDS = 3600
# 单张 ~3KB，2048 张 ≈6MB；超出按插入顺序淘汰最旧
AVATAR_CACHE_MAX_ITEMS = 2048
_AVATAR_CACHE: dict[str, tuple[bytes, str]] = {}
# 签名 CDN 地址短时有效，代理响应不做长缓存
MEDIA_CACHE_SECONDS = 300
# 归档文件内容不会变，可以让浏览器多缓存一会儿
ARCHIVE_MEDIA_CACHE_SECONDS = 86400
ARCHIVE_STREAM_ATTEMPTS = 2
ARCHIVE_PREFETCH_LIMIT = 6
# 播放器里预取当前作品的后 2 个、前 1 个，切换上/下一个时直接命中本机缓存
ARCHIVE_NEIGHBOR_PREFETCH_AFTER = 2
ARCHIVE_NEIGHBOR_PREFETCH_BEFORE = 1
# 浏览器直连归档 CDN 的直链。wopan/D2 签名实测约 4 小时有效，本服务还可能缓存 5 分钟，
# 告诉前端 30 分钟内可用，留足余量；过期或播放失败时前端重新取或改走代理。
ARCHIVE_DIRECT_URL_CLIENT_TTL_SECONDS = 1800
# 一页最多 100 条同时解析，限并发，别把 OpenList 连接池占满（见 openlist_client 的池泄漏事故）
ARCHIVE_DIRECT_URL_CONCURRENCY = 8
# 播放文件中位数 2~4MB；超过 10MB 的等整份落盘反而比流式代理慢，继续直接代理
ARCHIVE_MEDIA_CACHE_MAX_FILE_BYTES = 10 * 1024 * 1024
# 缓存只为一次观看内的多次 Range 请求和列表预取服务，不求长期命中，留最近几十个即可
ARCHIVE_MEDIA_CACHE_MAX_TOTAL_BYTES = 200 * 1024 * 1024
# 本机磁盘余量低于此值时停止缓存并清空，把空间让给其他程序
ARCHIVE_MEDIA_CACHE_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


def _archive_media_type(relative_path: str, upstream_type: str | None) -> str:
    """Prefer a concrete browser media type when OpenList reports octet-stream."""
    content_type = str(upstream_type or "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "application/octet-stream":
        return content_type
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or content_type or "application/octet-stream"
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

# 统计接口短缓存：状态文件未变时避免反复扫 500+ 用户的 download_records
_STATISTICS_CACHE: dict[str, Any] | None = None
_STATISTICS_CACHE_STAMP: tuple[FileStamp, int] | None = None

# 作品库索引：记录修订号或用户列表（id/昵称）变化才重建；状态文件戳只做快速命中
_LIBRARY_INDEX: list[LibraryEntry] | None = None
_LIBRARY_INDEX_STAMP: tuple[FileStamp, int] | None = None
_LIBRARY_INDEX_KEY: tuple[int, tuple[tuple[str, str], ...]] | None = None
_LIBRARY_INDEX_LOCK = asyncio.Lock()
# 按用户复用已解析条目（见 build_library_index 的 user_cache）
_LIBRARY_USER_CACHE: dict[str, Any] = {}

SETTINGS = load_settings()
DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "dashboard.html"
STATISTICS_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "statistics.html"
COOKIES_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "cookies.html"
SETTINGS_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "settings.html"
API_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "api.html"
WORKS_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "works.html"
LIBRARY_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "library.html"
SWIPE_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "swipe.html"

CRAWLER_CLIENT = InProcessDouyinClient(SETTINGS.crawler.config_path)
MONITOR_STORAGE = MonitorStorage(SETTINGS.monitor.state_path)
COVER_STORE = CoverStore(SETTINGS.monitor.state_path.parent / "covers")
# 网页自助管理的 API Key（auth 里校验并计数）
# 应用自身没配日志输出；借 uvicorn 已配好的 INFO 通道，耗时才会进 journal
PARSE_LOGGER = logging.getLogger("uvicorn.error")
API_KEY_STORE = api_keys.configure(SETTINGS.monitor.state_path.parent / "api_keys.db")
TG_SETTINGS = SETTINGS.notifications.telegram
MONITOR_NOTIFIER = (
    TelegramNotifier(
        bot_token=TG_SETTINGS.bot_token,
        chat_id=TG_SETTINGS.chat_id,
        api_base=TG_SETTINGS.api_base,
        timeout_seconds=TG_SETTINGS.timeout_seconds,
    )
    if TG_SETTINGS.enabled
    else NoopMonitorNotifier()
)

HERMES_SETTINGS = SETTINGS.notifications.hermes_weixin
HERMES_SENDER = (
    HermesWeixinSender(
        HermesWeixinConfig(
            enabled=HERMES_SETTINGS.enabled,
            ssh_host=HERMES_SETTINGS.ssh_host,
            ssh_user=HERMES_SETTINGS.ssh_user,
            hermes_home=HERMES_SETTINGS.hermes_home,
            hermes_bin=HERMES_SETTINGS.hermes_bin,
            target=HERMES_SETTINGS.target,
            timeout_seconds=HERMES_SETTINGS.timeout_seconds,
        )
    )
    if HERMES_SETTINGS.enabled
    else None
)

COOKIE_SETTINGS = SETTINGS.cookie_liveness
COOKIE_LIVENESS_SERVICE = CookieLivenessService(
    crawler=CRAWLER_CLIENT,
    config=CookieLivenessConfig(
        enabled=COOKIE_SETTINGS.enabled,
        interval_hours=COOKIE_SETTINGS.interval_hours,
        stale_days=COOKIE_SETTINGS.stale_days,
        sample_user_count=COOKIE_SETTINGS.sample_user_count,
        min_samples=COOKIE_SETTINGS.min_samples,
        alert_cooldown_hours=COOKIE_SETTINGS.alert_cooldown_hours,
    ),
    alerter=HERMES_SENDER,
)

COOKIE_ROTATION = SETTINGS.cookie_rotation
COOKIE_POOL_SERVICE = CookiePoolService(
    storage=MONITOR_STORAGE,
    crawler=CRAWLER_CLIENT,
    liveness_service=COOKIE_LIVENESS_SERVICE,
    rotation_enabled=COOKIE_ROTATION.enabled,
    rotation_skip_expired=COOKIE_ROTATION.skip_expired,
)

# 过期 Cookie（data/parse_cookies/*.txt）：单作品详情接口对 Cookie 要求低，解析 API 与监控下载共用；
# 登录态 Cookie 池只用来拉作品列表，降低账号风险
PARSE_COOKIES_DIR = SETTINGS.monitor.state_path.parent / "parse_cookies"
DETAIL_COOKIE_POOL = ParseCookiePool(PARSE_COOKIES_DIR)

OPENLIST_CLIENT = OpenListClient(SETTINGS.archive)
ARCHIVE_PATH_ALIASES = load_archive_path_aliases(SETTINGS.monitor.state_path)
# 新作品标注走后台队列，不占 run_lock；mover 搬走本地文件后从网盘读
CONTENT_FILTER_QUEUE = (
    ContentFilterQueue(
        ContentFilter(SETTINGS.content_filter, SETTINGS.monitor.download_root),
        MONITOR_STORAGE,
        ArchiveMediaLoader(SETTINGS.monitor.download_root, OPENLIST_CLIENT, ARCHIVE_PATH_ALIASES, COVER_STORE),
    )
    if SETTINGS.content_filter.enabled
    else None
)

monitor_service = MonitorService(
    crawler=CRAWLER_CLIENT,
    storage=MONITOR_STORAGE,
    download_root=SETTINGS.monitor.download_root,
    notifier=MONITOR_NOTIFIER,
    cookie_liveness_service=COOKIE_LIVENESS_SERVICE,
    cookie_pool_service=COOKIE_POOL_SERVICE,
    cover_publisher=NewWorkCoverPublisher(SETTINGS.monitor.download_root, COVER_STORE).publish,
    content_filter_queue=CONTENT_FILTER_QUEUE,
    detail_cookie_pool=DETAIL_COOKIE_POOL,
)

AWEME_FEED_SERVICE = AwemeFeedService(CRAWLER_CLIENT)
# 跟状态文件放一起（生产即 data/media_cache），测试的临时状态目录也就自然隔离
ARCHIVE_MEDIA_CACHE = ArchiveMediaCache(
    SETTINGS.monitor.state_path.parent / "media_cache",
    max_file_bytes=ARCHIVE_MEDIA_CACHE_MAX_FILE_BYTES,
    max_total_bytes=ARCHIVE_MEDIA_CACHE_MAX_TOTAL_BYTES,
    min_free_bytes=ARCHIVE_MEDIA_CACHE_MIN_FREE_BYTES,
)


class AddUserPayload(BaseModel):
    profile_url: str = Field(min_length=1, description="抖音用户主页链接")


class UpdateUserPayload(BaseModel):
    enabled: bool


class AddCookiePayload(BaseModel):
    cookie: str = Field(min_length=1, description="抖音 Web Cookie 字符串")
    name: str | None = Field(default=None, description="可选名称/备注")


class UpdateCookiePayload(BaseModel):
    name: str | None = Field(default=None, description="可选名称/备注")
    cookie: str | None = Field(default=None, description="可选新的 Cookie 字符串")

class StartMonitorPayload(BaseModel):
    mode: str = Field(default=MODE_INTERVAL, description=f"监控模式: {MODE_INTERVAL} | {MODE_COVERAGE}")
    interval_hours: float = Field(default=0.05, ge=MIN_INTERVAL_HOURS, description="定时模式轮询间隔(小时)")
    coverage_hours: float = Field(default=24.0, gt=0, description="覆盖模式下，全量用户完成一轮的目标小时数")


class PlaybackUrlsPayload(BaseModel):
    aweme_ids: list[str] = Field(min_length=1, max_length=100, description="要取直链的作品 ID，最多 100 个")


class ApiKeyCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=api_keys.NAME_MAX_LENGTH, description="Key 名称，便于辨认用途")
    scope: str = Field(default="parse", description="parse=只能调解析接口；full=管理员权限")


class ParseBody(BaseModel):
    text: str = Field(min_length=1, description="抖音分享文本 / 口令 / 作品链接")
    include_raw: bool = Field(default=False, description="是否附带 aweme_detail 原始字段")


def _build_error(request: Request, message: str, status_code: int = 400) -> HTTPException:
    detail = ErrorResponseModel(
        code=status_code,
        message=message,
        router=request.url.path,
        params=dict(request.query_params),
    )
    return HTTPException(status_code=status_code, detail=detail.model_dump())


def _build_success(request: Request, data: Any) -> ResponseModel:
    return ResponseModel(code=200, router=request.url.path, data=data)


def _map_cookie_error(request: Request, exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return _build_error(request, str(exc) or "Cookie 不存在", status_code=404)
    if isinstance(exc, ValueError):
        return _build_error(request, str(exc), status_code=400)
    return _build_error(request, str(exc), status_code=400)

async def _find_user_or_raise(user_id: str) -> dict[str, Any]:
    user = find_user_by_id(await monitor_service.list_users_async(), user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _records() -> RecordStore:
    return monitor_service._storage.records  # noqa: SLF001


async def _find_user_with_records_or_raise(user_id: str) -> dict[str, Any]:
    """作品/媒体/封面接口用：用户视图带上 RecordStore 里的 download_records。"""
    return _records().attach(await _find_user_or_raise(user_id))


def _validate_avatar_url(raw_url: Any) -> str:
    avatar_url = str(raw_url or "").strip()
    parsed = urlparse(avatar_url)
    if not avatar_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=404, detail="用户头像不存在")
    return avatar_url


async def _download_avatar(avatar_url: str) -> httpx.Response:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=SETTINGS.crawler.timeout_seconds,
    ) as client:
        try:
            response = await client.get(avatar_url, headers=DEFAULT_HEADERS)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"头像拉取失败: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"头像拉取失败: status={response.status_code}",
        )
    return response


@router.get("/dashboard", response_class=HTMLResponse, summary="监控管理页面/Monitoring dashboard page")
async def monitor_dashboard() -> str:
    if not DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"仪表盘文件不存在: {DASHBOARD_PATH}")
    return HTMLResponse(
        content=DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )


@router.get("/statistics/dashboard", response_class=HTMLResponse, summary="用户统计页面/User statistics page")
async def statistics_dashboard() -> str:
    if not STATISTICS_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"统计页面文件不存在: {STATISTICS_DASHBOARD_PATH}")
    return HTMLResponse(
        content=STATISTICS_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )


@router.get("/cookies/dashboard", response_class=HTMLResponse, summary="Cookie 池管理页面/Cookie pool page")
async def cookies_dashboard() -> str:
    if not COOKIES_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"Cookie 池页面不存在: {COOKIES_DASHBOARD_PATH}")
    return HTMLResponse(
        content=COOKIES_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )




@router.get("/settings/dashboard", response_class=HTMLResponse, summary="调度配置页面/Settings dashboard page")
async def settings_dashboard() -> str:
    if not SETTINGS_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"配置页面不存在: {SETTINGS_DASHBOARD_PATH}")
    return HTMLResponse(
        content=SETTINGS_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )


@router.get("/api/dashboard", response_class=HTMLResponse, summary="外部解析 API 文档页/Parse API docs page")
async def api_dashboard() -> str:
    if not API_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"API 页面不存在: {API_DASHBOARD_PATH}")
    return HTMLResponse(
        content=API_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )


@router.get("/works/dashboard", response_class=HTMLResponse, summary="用户作品浏览页/User works browser page")
async def works_dashboard() -> str:
    """作品列表页，目标用户由查询串 ?user_id= 指定（页面侧读取）。"""
    if not WORKS_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"作品页面不存在: {WORKS_DASHBOARD_PATH}")
    return HTMLResponse(
        content=WORKS_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )



@router.get("/library/dashboard", response_class=HTMLResponse, summary="作品库页面/Works library page")
async def library_dashboard() -> str:
    if not LIBRARY_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"作品库页面不存在: {LIBRARY_DASHBOARD_PATH}")
    return HTMLResponse(
        content=LIBRARY_DASHBOARD_PATH.read_text(encoding="utf-8"),
        headers=HTML_NO_CACHE_HEADERS,
    )


@router.get("/swipe/dashboard", response_class=HTMLResponse, summary="刷视频页面/Vertical swipe video feed page")
async def swipe_dashboard(request: Request) -> str:
    """上下滑动刷作品库里的视频；筛选参数与作品库一致（页面侧读取查询串）。"""
    if not SWIPE_DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail=f"刷视频页面不存在: {SWIPE_DASHBOARD_PATH}")
    content = SWIPE_DASHBOARD_PATH.read_text(encoding="utf-8")
    if _is_guest_request(request):
        # 游客在服务端就打上标记，页面不会先闪一下完整界面
        if content.count("<body>") != 1:
            raise HTTPException(status_code=500, detail="刷视频页面缺少唯一的 <body> 标签，无法进入游客模式")
        content = content.replace("<body>", '<body class="sw-guest">', 1)
    return HTMLResponse(content=content, headers=HTML_NO_CACHE_HEADERS)


# ---- 游客：只能随机刷作品库里的视频，其余一律拒绝（路由白名单见 auth.GUEST_ROUTES） ----
GUEST_PAGE_SIZE = 20
# 游客只拿播放需要的字段：不下发昵称、描述、标签、评分等
GUEST_ITEM_FIELDS = (
    "aweme_id", "user_id", "media_type", "play_index", "cover_index", "cover_url",
    "duration_ms", "has_fast_play", "video_source", "fast_play_source",
)
_GUEST_WORKS: tuple[int, dict[tuple[str, str], LibraryEntry]] | None = None


def _is_guest_request(request: Request) -> bool:
    return bool(getattr(request.state, "guest", False))


async def _library_index() -> list[LibraryEntry]:
    async with _LIBRARY_INDEX_LOCK:
        return await asyncio.to_thread(_refresh_library_index)


def _guest_works(index: list[LibraryEntry]) -> dict[tuple[str, str], LibraryEntry]:
    """游客可看的作品：作品库里的视频。按索引对象缓存，索引重建后自动失效。"""
    global _GUEST_WORKS
    if _GUEST_WORKS is None or _GUEST_WORKS[0] != id(index):
        works = {(entry.user_id, entry.aweme_id): entry for entry in index if entry.media_type == "video"}
        _GUEST_WORKS = (id(index), works)
    return _GUEST_WORKS[1]


async def _require_guest_work(request: Request, user_id: str, aweme_id: str) -> None:
    """游客请求具体作品时校验：必须是作品库里的视频，否则按不存在处理。"""
    if not _is_guest_request(request):
        return
    if (user_id, aweme_id) not in _guest_works(await _library_index()):
        raise HTTPException(status_code=404, detail="作品不存在")


async def _require_guest_media(request: Request, user_id: str, aweme_id: str, file_index: int) -> None:
    """游客只能取播放文件与封面，不能取原片等其他归档文件。"""
    if not _is_guest_request(request):
        return
    entry = _guest_works(await _library_index()).get((user_id, aweme_id))
    item = build_archive_item(entry.record) if entry is not None else None
    if item is None or file_index not in {item.get("play_index"), item.get("cover_index")}:
        raise HTTPException(status_code=404, detail="作品不存在")


def _guest_library_query(params: Any):
    """游客的查询只认页码和随机种子，其余参数（筛选、排序、类型）一律忽略。"""
    allowed = {"sort": "random", "media_type": "video", "page_size": str(GUEST_PAGE_SIZE)}
    for key in ("page", "seed"):
        value = params.get(key)
        if value is not None:
            allowed[key] = value
    return parse_library_query(allowed)


def _library_users_key(users: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((str(user.get("id") or ""), str(user.get("nickname") or "")) for user in users)


def _refresh_library_index() -> list[LibraryEntry]:
    """在线程里跑：状态文件戳没变直接命中；变了再比对记录修订号 + 用户 id/昵称。"""
    global _LIBRARY_INDEX, _LIBRARY_INDEX_STAMP, _LIBRARY_INDEX_KEY
    records = _records()
    # 先取修订号再取快照：中途有写入时修订号偏旧，下次请求会再重建，不会漏
    revision = records.current_revision()
    try:
        stamp = (file_stamp(monitor_service._storage.state_file.stat()), revision)  # noqa: SLF001
    except OSError:
        stamp = None
    if _LIBRARY_INDEX is not None and stamp is not None and stamp == _LIBRARY_INDEX_STAMP:
        return _LIBRARY_INDEX
    users = monitor_service.list_users()
    key = (revision, _library_users_key(users))
    if _LIBRARY_INDEX is None or key != _LIBRARY_INDEX_KEY:
        _LIBRARY_INDEX = build_library_index(users, records.snapshot(), _LIBRARY_USER_CACHE)
        _LIBRARY_INDEX_KEY = key
    _LIBRARY_INDEX_STAMP = stamp
    return _LIBRARY_INDEX


async def _library_page_items(entries: list[LibraryEntry]) -> list[dict[str, Any]]:
    """当前页作品：归档条目 + 用户字段；封面按用户分组查，顺序保持排序结果。"""
    items: list[dict[str, Any]] = []
    for entry in entries:
        item = build_archive_item(entry.record)
        if item is None:
            raise RuntimeError(f"作品库索引条目无法生成作品：{entry.user_id}/{entry.aweme_id}")
        item["user_id"] = entry.user_id
        item["nickname"] = entry.nickname
        item["avatar_url"] = f"/api/monitor/users/{entry.user_id}/avatar"
        items.append(item)
    by_user: dict[str, list[int]] = {}
    for position, item in enumerate(items):
        by_user.setdefault(item["user_id"], []).append(position)
    for user_id, positions in by_user.items():
        covered = await with_cover_urls(COVER_STORE, user_id, [items[position] for position in positions])
        for position, item in zip(positions, covered):
            items[position] = item
    return items


@router.get("/library", response_model=ResponseModel, summary="作品库：跨用户浏览已评分作品/Works library")
async def list_library(request: Request) -> ResponseModel:
    """只收已打擦边分（score>=1）的作品；参数见 monitor/library.py，非法参数 400。"""
    guest = _is_guest_request(request)
    try:
        query = _guest_library_query(request.query_params) if guest else parse_library_query(request.query_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    index = await _library_index()
    result = await asyncio.to_thread(query_library, index, query)
    items = await _library_page_items(result["entries"])
    if guest:
        # 游客不下发分面、用户统计和作品详情，只给翻页和播放需要的
        payload = {key: result[key] for key in ("page", "page_size", "total", "total_pages", "seed")}
        payload["items"] = [{key: item.get(key) for key in GUEST_ITEM_FIELDS} for item in items]
        return _build_success(request, payload)
    payload = {key: value for key, value in result.items() if key != "entries"}
    payload["items"] = items
    return _build_success(request, payload)

PARSE_SERVICE = ParseService(CRAWLER_CLIENT, cookie_pool=DETAIL_COOKIE_POOL)


@router.post("/parse", response_model=ResponseModel, summary="解析抖音分享链接/Parse Douyin share link")
async def parse_share_post(request: Request, payload: ParseBody) -> ResponseModel:
    try:
        data = await PARSE_SERVICE.parse(payload.text, include_raw=payload.include_raw)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, data)


@router.get("/parse", response_model=ResponseModel, summary="解析抖音分享链接(GET)/Parse Douyin share link via query")
async def parse_share_get(
    request: Request,
    text: str = Query(min_length=1, description="抖音分享文本 / 口令 / 作品链接"),
    include_raw: bool = Query(default=False, description="是否附带 aweme_detail 原始字段"),
) -> ResponseModel:
    try:
        data = await PARSE_SERVICE.parse(text, include_raw=include_raw)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, data)

@router.get(
    "/parse/plugin",
    response_model=ResponseModel,
    summary="解析视频直链（下载插件格式）/Parse video URL for download plugins",
)
async def parse_share_for_plugin(
    request: Request,
    url: str = Query(min_length=1, description="抖音分享文本 / 口令 / 作品链接"),
) -> ResponseModel:
    """给下载插件用：成功时 HTTP 200、code=200，data.url / data.video_url 为最高画质直链。

    只返回一个地址，插件直接下载不弹选择框。图文作品没有视频，按失败返回。
    """
    started = time.perf_counter()
    try:
        data = await PARSE_SERVICE.parse(url)
    except Exception as exc:  # noqa: BLE001
        PARSE_LOGGER.info("plugin parse failed elapsed=%.2fs err=%s", time.perf_counter() - started, exc)
        raise _build_error(request, str(exc))
    # 服务端耗时，和插件侧感受到的总时长对比，能分出是解析慢还是网络慢
    PARSE_LOGGER.info("plugin parse ok aweme_id=%s elapsed=%.2fs", data.get("aweme_id"), time.perf_counter() - started)
    if data.get("is_image_post"):
        raise _build_error(request, f"作品 {data.get('aweme_id')} 是图文，没有视频可下载")
    video_url = data.get("download_url")
    if not video_url:
        raise _build_error(request, f"作品 {data.get('aweme_id')} 未找到视频地址")
    return _build_success(
        request,
        {
            "url": video_url,
            "video_url": video_url,
            "title": data.get("desc") or "",
            "aweme_id": data.get("aweme_id"),
            "quality": data.get("quality_source"),
        },
    )


def _require_key_admin(request: Request) -> None:
    if not auth.can_manage_keys(request):
        raise HTTPException(status_code=403, detail="只有管理员登录或系统 Key 能管理 API Key")


@router.get("/api-keys", response_model=ResponseModel, summary="API Key 列表与使用次数/List API keys")
async def list_api_keys(request: Request) -> ResponseModel:
    _require_key_admin(request)
    return _build_success(request, {"items": API_KEY_STORE.list(), "scopes": list(api_keys.SCOPES)})


@router.post("/api-keys", response_model=ResponseModel, summary="新建 API Key/Create API key")
async def create_api_key(request: Request, payload: ApiKeyCreateBody) -> ResponseModel:
    _require_key_admin(request)
    try:
        record, plaintext = API_KEY_STORE.create(payload.name, payload.scope)
    except ValueError as exc:
        raise _build_error(request, str(exc)) from exc
    # 明文只在这次响应里出现，库里只存哈希
    return _build_success(request, {**record, "key": plaintext})


@router.delete("/api-keys/{key_id}", response_model=ResponseModel, summary="删除 API Key/Delete API key")
async def delete_api_key(request: Request, key_id: str) -> ResponseModel:
    _require_key_admin(request)
    try:
        record = API_KEY_STORE.delete(key_id)
    except KeyError as exc:
        raise _build_error(request, "Key 不存在", status_code=404) from exc
    except ValueError as exc:
        raise _build_error(request, str(exc)) from exc
    return _build_success(request, record)


@router.get("/cookies", response_model=ResponseModel, summary="获取 Cookie 池列表/List cookie pool")
async def list_cookies(request: Request) -> ResponseModel:
    try:
        items = await COOKIE_POOL_SERVICE.list_items()
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, {"items": items})


@router.post("/cookies", response_model=ResponseModel, summary="新增 Cookie/Add cookie to pool")
async def add_cookie(request: Request, payload: AddCookiePayload) -> ResponseModel:
    try:
        item = await COOKIE_POOL_SERVICE.add_item(cookie=payload.cookie, name=payload.name)
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, item)


@router.patch("/cookies/{cookie_id}", response_model=ResponseModel, summary="更新 Cookie/Update cookie")
async def update_cookie(request: Request, cookie_id: str, payload: UpdateCookiePayload) -> ResponseModel:
    if payload.name is None and payload.cookie is None:
        raise _build_error(request, "至少提供 name 或 cookie 之一")
    try:
        item = await COOKIE_POOL_SERVICE.update_item(
            cookie_id,
            name=payload.name,
            cookie=payload.cookie,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, item)


@router.delete("/cookies/{cookie_id}", response_model=ResponseModel, summary="删除 Cookie/Delete cookie")
async def delete_cookie(request: Request, cookie_id: str) -> ResponseModel:
    try:
        item = await COOKIE_POOL_SERVICE.delete_item(cookie_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, item)


@router.post(
    "/cookies/{cookie_id}/activate",
    response_model=ResponseModel,
    summary="启用 Cookie/Activate cookie for crawler",
)
async def activate_cookie(request: Request, cookie_id: str) -> ResponseModel:
    try:
        item = await COOKIE_POOL_SERVICE.activate(cookie_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, item)


@router.post(
    "/cookies/{cookie_id}/check",
    response_model=ResponseModel,
    summary="测活 Cookie/Check cookie liveness",
)
async def check_cookie(request: Request, cookie_id: str) -> ResponseModel:
    try:
        item = await COOKIE_POOL_SERVICE.check_item(cookie_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_cookie_error(request, exc)
    return _build_success(request, item)


@router.get("/users", response_model=ResponseModel, summary="获取监控用户列表/Get monitor users")
async def list_users(
    request: Request,
    search: str | None = Query(None, description="Search nickname/sec_uid/id/profile_url"),
    monitor: str | None = Query(None, description="all|enabled|paused"),
    account: str | None = Query(None, description="all|normal|deleted|banned|abnormal"),
    activity: str | None = Query(
        None, description="all|active_7d|active_30d|silent_7d|silent_30d|silent_60d|silent_90d|silent_180d|publish_unknown"
    ),
    backfill: str | None = Query(None, description="all|idle|active|paused|completed|failed"),
    value: str | None = Query(None, description="all|low_value|long_silent|low_score|score_pending"),
    sort_by: str | None = Query(
        None, description="downloads|last_publish|last_check|last_download|nickname|created|avg_score|high_score_ratio"
    ),
    sort_order: str | None = Query(None, description="asc|desc"),
    page: int | None = Query(None, description="1-based page; presence switches to table mode"),
    page_size: int | None = Query(None, description="20|50|100"),
    view: str | None = Query(None, description="dashboard|table"),
) -> ResponseModel:
    # Compat A: no page/page_size/view=table → legacy dashboard full projection.
    # With pagination params → filter/sort/page table for statistics unified list.
    if wants_paginated_users(page=page, page_size=page_size, view=view):
        # 视频/图集数、最近发布、活跃筛选都由作品记录派生（statistics._build_user_row）
        users = _records().attach_all(monitor_service.list_users())
        payload = query_monitor_users(
            users,
            search=search or "",
            monitor=monitor or "all",
            account=account or "all",
            activity=activity or "all",
            backfill=backfill or "all",
            value=value or "all",
            sort_by=sort_by or "downloads",
            sort_order=sort_order or "desc",
            page=1 if page is None else page,
            page_size=20 if page_size is None else page_size,
        )
        return _build_success(request, payload)

    # 大盘默认返回精简投影：去掉 downloaded_aweme_ids 与全量 download_records，
    # 避免 500+ 用户时 10MB JSON 拖慢首屏。
    payload = await monitor_service.list_dashboard_users()
    return _build_success(request, payload)


@router.get(
    "/recent_activity",
    response_model=ResponseModel,
    summary="获取最近下载动态/Get recent download activity",
)
async def recent_activity(
    request: Request,
    limit: int | None = Query(None, description="Max feed items (default dashboard limit)"),
) -> ResponseModel:
    """Lightweight feed for dashboard — no profile hydration, no full user projection."""
    users = _records().attach_all(monitor_service.list_users())
    activity_limit = DASHBOARD_ACTIVITY_LIMIT if limit is None else max(0, int(limit))
    payload = {
        "recent_activity": build_recent_activity(users, limit=activity_limit),
    }
    return _build_success(request, payload)


@router.get("/statistics", response_model=ResponseModel, summary="获取用户统计/Get user statistics")
async def user_statistics(request: Request) -> ResponseModel:
    global _STATISTICS_CACHE, _STATISTICS_CACHE_STAMP
    # 统计只读本地态；完整文件标识能识别 mtime 相同的原子替换，记录变化看 RecordStore 修订号。
    try:
        stamp = (file_stamp(monitor_service._storage.state_file.stat()), _records().current_revision())  # noqa: SLF001
    except OSError:
        stamp = None
    if (
        _STATISTICS_CACHE is not None
        and stamp is not None
        and _STATISTICS_CACHE_STAMP == stamp
    ):
        return _build_success(request, _STATISTICS_CACHE)

    users = _records().attach_all(monitor_service.list_users())
    payload = build_user_statistics(users)
    _STATISTICS_CACHE = payload
    _STATISTICS_CACHE_STAMP = stamp
    return _build_success(request, payload)


@router.get("/users/{user_id}/avatar", summary="代理监控用户头像/Proxy monitor user avatar")
async def proxy_user_avatar(user_id: str) -> Response:
    user = await _find_user_or_raise(user_id)
    avatar_url = _validate_avatar_url(user.get("avatar_url"))
    # 头像 URL 换图即换地址：按 URL 进程内缓存，避免统计页每次 20+ 个请求都回源抖音 CDN。
    cached = _AVATAR_CACHE.get(avatar_url)
    if cached is None:
        upstream_response = await _download_avatar(avatar_url)
        cached = (
            upstream_response.content,
            upstream_response.headers.get("Content-Type", "image/jpeg"),
        )
        if len(_AVATAR_CACHE) >= AVATAR_CACHE_MAX_ITEMS:
            _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
        _AVATAR_CACHE[avatar_url] = cached
    content, content_type = cached
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={AVATAR_CACHE_SECONDS}"},
    )


def _local_media_response(local_path: Path, range_header: str | None) -> Response:
    """本地归档文件，自己处理 Range。

    Starlette 0.37 的 FileResponse 不解析 Range（直接回 200 + 整份文件），
    视频会退化成整包下载且拖不动进度条，所以这里显式实现 206。
    """
    media_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    file_size = local_path.stat().st_size
    try:
        byte_range = parse_byte_range(range_header, file_size)
    except UnsatisfiableRangeError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
        )

    if byte_range is None:
        return FileResponse(
            local_path,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": f"private, max-age={ARCHIVE_MEDIA_CACHE_SECONDS}",
            },
        )

    start, end = byte_range
    return StreamingResponse(
        iter_file_range(local_path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Cache-Control": f"private, max-age={ARCHIVE_MEDIA_CACHE_SECONDS}",
        },
    )


def _feed_user_header(user: dict[str, Any]) -> dict[str, Any]:
    """作品页头部展示所需的最小用户投影（不含 download_records 等大字段）。"""
    return {
        "id": str(user.get("id") or ""),
        "nickname": str(user.get("nickname") or ""),
        "avatar_url": user.get("avatar_url"),
        "profile_url": str(user.get("profile_url") or ""),
        "enabled": bool(user.get("enabled", True)),
        "downloaded_count": int(user.get("downloaded_count", 0) or 0),
        "account_status": str(user.get("account_status") or "normal"),
        "account_status_label": str(user.get("account_status_label") or "正常"),
        "last_checked_at": user.get("last_checked_at"),
        # 改名后归档会散在多个昵称目录下，带上曾用名便于认出网盘上的历史目录
        "former_nicknames": build_former_nicknames(user),
    }


@router.get(
    "/users/{user_id}/archive",
    response_model=ResponseModel,
    summary="获取用户已归档作品列表/List archived works from local records",
)
async def list_user_archive(
    request: Request,
    user_id: str,
    background_tasks: BackgroundTasks,
    page: int | None = Query(None, description="1-based 页码"),
    page_size: int | None = Query(None, description="每页条数 20|50|100"),
    ai_filter: str = Query("", description="AI 筛选视图：空=全部 | flagged=判为筛掉 | error=检测失败"),
) -> ResponseModel:
    """只读本地 download_records，不发起任何抖音请求。

    抖音端实时拉列表容易触发风控，且作品列表被隐藏的账号会直接 403；
    已下载的作品元数据本地都有，够渲染列表，只有媒体字节需要走归档。
    """
    user = await _find_user_with_records_or_raise(user_id)
    try:
        payload = build_archive_page(user, page=page, page_size=page_size, ai_filter=ai_filter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload["items"] = await with_cover_urls(COVER_STORE, user_id, payload["items"])
    payload["user"] = _feed_user_header(user)
    payload["archive_enabled"] = OPENLIST_CLIENT.enabled
    background_tasks.add_task(_prefetch_archive_urls, user, payload["items"])
    return _build_success(request, payload)


@router.get(
    "/users/{user_id}/archive/{aweme_id}/cover",
    summary="读取独立封面/Read a verified local cover",
)
async def read_archive_cover(
    request: Request, user_id: str, aweme_id: str, *,
    v: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
) -> Response:
    await _require_guest_work(request, user_id, aweme_id)
    user = await _find_user_with_records_or_raise(user_id)
    return await cover_response(request, user, COVER_STORE, aweme_id=aweme_id, version=v)


async def _open_archived_cloud_stream(
    relative_path: str, sec_user_id: str | None, range_header: str | None, *, verified_paths: tuple[str, ...]
) -> tuple[int, dict[str, str], Any]:
    upstream_headers = build_upstream_headers(None, range_header)
    try:
        candidates = OPENLIST_CLIENT.remote_paths_for(
            relative_path, sec_user_id=sec_user_id, verified_paths=verified_paths
        )
    except ArchiveUnavailableError as exc:
        raise HTTPException(status_code=404, detail=sanitize_public_error(exc)) from exc
    last_metadata_error: Exception | None = None
    last_media_error: Exception | None = None
    for remote_path in candidates:
        # Retry an expired URL on this exact replica before trying the next one.
        for attempt in range(ARCHIVE_STREAM_ATTEMPTS):
            try:
                stream_url = await OPENLIST_CLIENT.resolve_candidate_url(remote_path, refresh=bool(attempt))
            except ArchiveUnavailableError as exc:
                last_metadata_error = exc
                break
            try:
                return await open_media_stream(
                    stream_url,
                    headers=upstream_headers,
                    timeout_seconds=SETTINGS.archive.timeout_seconds,
                )
            except MediaUpstreamError as exc:
                last_media_error = exc
    last_error = last_media_error or last_metadata_error
    raise HTTPException(
        status_code=502 if last_media_error else 404,
        detail=sanitize_public_error(last_error),
    ) from last_error


async def _cached_archive_media(
    user: dict[str, Any], aweme_id: str, relative_path: str, verified_paths: tuple[str, ...]
) -> Path | None:
    """小文件整份落本机缓存后返回路径；超限/不可缓存返回 None，由调用方走流式代理。"""
    key = ArchiveMediaCache.key_for(str(user.get("id") or ""), aweme_id, relative_path)
    return await ARCHIVE_MEDIA_CACHE.get_or_fetch(
        key,
        Path(relative_path).suffix.lower(),
        lambda: _open_archived_cloud_stream(
            relative_path, user.get("sec_user_id"), None, verified_paths=verified_paths
        ),
    )


async def _prefetch_archive_urls(user: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """列表页前几条预先落缓存，点开即播。"""
    jobs = []
    for item in items[:ARCHIVE_PREFETCH_LIMIT]:
        play_index = item.get("play_index")
        if play_index is None:
            continue
        try:
            aweme_id = str(item["aweme_id"])
            relative_path = resolve_archive_file(user, aweme_id, play_index)
            verified = ARCHIVE_PATH_ALIASES.paths_for(user, aweme_id, relative_path)
        except (KeyError, IndexError, ArchivePathAliasError) as exc:
            LOGGER.warning("archive playback prefetch skipped aweme=%s: %s", item.get("aweme_id"), exc)
            continue
        jobs.append(_cached_archive_media(user, aweme_id, relative_path, verified))
    if not jobs:
        return
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            LOGGER.info("archive playback prefetch unavailable: %s", result)


@router.post(
    "/users/{user_id}/archive/{aweme_id}/prefetch",
    response_model=ResponseModel,
    summary="预取相邻作品到本机缓存/Prefetch neighbouring archived works",
)
async def prefetch_archive_neighbors(
    request: Request, user_id: str, aweme_id: str, background_tasks: BackgroundTasks
) -> ResponseModel:
    """播放器打开某个作品后调用：按列表顺序（跨分页）预取前后相邻作品的播放文件。"""
    user = await _find_user_with_records_or_raise(user_id)
    items = neighbor_items(
        user, aweme_id, before=ARCHIVE_NEIGHBOR_PREFETCH_BEFORE, after=ARCHIVE_NEIGHBOR_PREFETCH_AFTER
    )
    background_tasks.add_task(_prefetch_archive_urls, user, items)
    return _build_success(request, {"queued": [item["aweme_id"] for item in items]})


async def _direct_file_url(
    user: dict[str, Any], aweme_id: str, file_index: int, semaphore: asyncio.Semaphore
) -> str | None:
    """单个归档文件的 CDN 直链；只能经 OpenList 自代理（夸克）时返回 None。"""
    relative_path = resolve_archive_file(user, aweme_id, file_index)
    verified_paths = ARCHIVE_PATH_ALIASES.paths_for(user, aweme_id, relative_path)
    async with semaphore:
        stream_url = await OPENLIST_CLIENT.resolve_stream_url(
            relative_path, sec_user_id=user.get("sec_user_id"), verified_paths=verified_paths
        )
    # 自代理地址指向本机 OpenList（127.0.0.1），浏览器访问不到
    if stream_url.startswith(OPENLIST_CLIENT._self_proxy_prefix()):  # noqa: SLF001
        return None
    return stream_url


async def _direct_work_urls(
    user: dict[str, Any], aweme_id: str, semaphore: asyncio.Semaphore
) -> tuple[str | None, list[str | None] | None]:
    """(视频播放文件直链, 图文各张图片直链)；视频作品图片为 None，图文作品视频为 None。"""
    record = find_record(user, aweme_id)
    item = build_archive_item(record) if record is not None else None
    if item is None:
        raise KeyError(f"作品 {aweme_id} 不在下载记录中")
    if item["media_type"] == "image":
        images = await asyncio.gather(
            *(_direct_file_url(user, aweme_id, index, semaphore) for index in item["image_indices"])
        )
        return None, list(images)
    if item["play_index"] is None:
        return None, None
    return await _direct_file_url(user, aweme_id, item["play_index"], semaphore), None


@router.post(
    "/users/{user_id}/archive/playback-urls",
    response_model=ResponseModel,
    summary="批量获取作品播放直链/Resolve direct CDN playback URLs",
)
async def resolve_archive_playback_urls(
    request: Request, user_id: str, payload: PlaybackUrlsPayload
) -> ResponseModel:
    """归档源站在国内，本服务在美国：经代理播放要来回跨两次太平洋（冷启动 4~5 秒/请求）。
    把直链交给浏览器直连国内 CDN（实测 0.4~0.9 秒）。浏览器直连时不能带 Referer，否则网关 403。

    urls：视频作品的播放直链；image_urls：图文作品按 image_indices 顺序的各张直链。
    None 表示该文件只能走代理；解析失败的作品放进 errors，不混进结果。
    """
    user = await _find_user_with_records_or_raise(user_id)
    # 信号量只包住 OpenList 查询：图文作品内部还会并发解析多张图
    semaphore = asyncio.Semaphore(ARCHIVE_DIRECT_URL_CONCURRENCY)
    aweme_ids = list(dict.fromkeys(str(aweme_id) for aweme_id in payload.aweme_ids))
    if _is_guest_request(request):
        # 只读：解析播放直链不改任何数据；游客只能解析作品库里的视频
        works = _guest_works(await _library_index())
        if any((user_id, aweme_id) not in works for aweme_id in aweme_ids):
            raise HTTPException(status_code=404, detail="作品不存在")

    async def resolve(aweme_id: str) -> tuple[str, tuple[str | None, list[str | None] | None] | None, str | None]:
        try:
            return aweme_id, await _direct_work_urls(user, aweme_id, semaphore), None
        except (KeyError, IndexError, ArchivePathAliasError, ArchiveUnavailableError) as exc:
            return aweme_id, None, sanitize_public_error(exc)

    urls: dict[str, str | None] = {}
    image_urls: dict[str, list[str | None]] = {}
    errors: dict[str, str] = {}
    for aweme_id, result, error in await asyncio.gather(*(resolve(aweme_id) for aweme_id in aweme_ids)):
        if error is not None or result is None:
            errors[aweme_id] = error or "unknown"
            continue
        video_url, images = result
        if images is not None:
            image_urls[aweme_id] = images
        else:
            urls[aweme_id] = video_url
    return _build_success(
        request,
        {
            "urls": urls,
            "image_urls": image_urls,
            "errors": errors,
            "ttl_seconds": ARCHIVE_DIRECT_URL_CLIENT_TTL_SECONDS,
        },
    )


@router.get(
    "/users/{user_id}/archive/{aweme_id}/media/{file_index}",
    summary="播放/查看已归档作品文件/Stream archived work file",
)
async def stream_archived_media(
    request: Request,
    user_id: str,
    aweme_id: str,
    file_index: int,
    direct: bool = Query(False, description="重定向到短时直链，不经本服务转发"),
) -> Response:
    """按记录从 OpenList 云端流式读取归档媒体。

    媒体不从项目本机读取；本机只保留状态和路径元数据。
    路径来自服务端记录，不接受调用方传路径，避免变成任意文件读取。
    """
    await _require_guest_media(request, user_id, aweme_id, file_index)
    user = await _find_user_with_records_or_raise(user_id)
    try:
        relative_path = resolve_archive_file(user, aweme_id, file_index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        verified_paths = ARCHIVE_PATH_ALIASES.paths_for(user, aweme_id, relative_path)
    except ArchivePathAliasError as exc:
        raise HTTPException(status_code=409, detail=sanitize_public_error(exc)) from exc
    if direct:
        try:
            stream_url = await OPENLIST_CLIENT.resolve_stream_url(
                relative_path, sec_user_id=user.get("sec_user_id"), verified_paths=verified_paths
            )
        except ArchiveUnavailableError as exc:
            raise HTTPException(status_code=404, detail=sanitize_public_error(exc)) from exc
        # OpenList may return its own loopback /p URL for providers without a
        # public raw URL (for example Quark). That address is valid only on the
        # server, so keep proxying instead of redirecting the browser to 127.0.0.1.
        if not stream_url.startswith(OPENLIST_CLIENT._self_proxy_prefix()):  # noqa: SLF001
            return RedirectResponse(stream_url, status_code=307, headers={"Cache-Control": "no-store"})
    cached_path = await _cached_archive_media(user, aweme_id, relative_path, verified_paths)
    if cached_path is not None:
        return _local_media_response(cached_path, request.headers.get("range"))
    status_code, upstream_response_headers, body = await _open_archived_cloud_stream(
        relative_path, user.get("sec_user_id"), request.headers.get("range"), verified_paths=verified_paths
    )

    response_headers = build_response_headers(
        upstream_response_headers,
        cache_seconds=0 if status_code == 416 else ARCHIVE_MEDIA_CACHE_SECONDS,
    )
    media_type = _archive_media_type(relative_path, response_headers.get("Content-Type"))
    response_headers["Content-Type"] = media_type
    return StreamingResponse(
        body,
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
    )


@router.get(
    "/users/{user_id}/awemes",
    response_model=ResponseModel,
    summary="获取监控用户作品列表(实时抖音)/List monitor user works from Douyin",
)
async def list_user_awemes(
    request: Request,
    user_id: str,
    cursor: int | None = Query(None, description="翻页游标，首页留空；取上一页 next_cursor"),
    count: int | None = Query(None, description=f"每页条数，1..20，默认 {DEFAULT_FEED_COUNT}"),
) -> ResponseModel:
    """实时拉取抖音端作品列表；只读，不写监控状态。

    每次调用都是一次真实抖音请求，前端按需翻页，避免无谓消耗 Cookie 配额。
    """
    # 头部曾用名要从作品记录的目录名反推
    user = await _find_user_with_records_or_raise(user_id)
    sec_user_id = str(user.get("sec_user_id") or "").strip()
    if not sec_user_id:
        raise _build_error(request, "该用户缺少 sec_user_id，无法拉取作品列表")
    try:
        payload = await AWEME_FEED_SERVICE.list_user_awemes(
            sec_user_id,
            cursor=clamp_cursor(cursor),
            count=clamp_feed_count(count),
            downloaded_ids=user.get("downloaded_aweme_ids", []),
        )
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, sanitize_public_error(exc), status_code=502)
    payload["user"] = _feed_user_header(user)
    return _build_success(request, payload)


@router.get(
    "/users/{user_id}/awemes/{aweme_id}/video",
    summary="代理作品视频流/Proxy aweme video stream",
)
async def proxy_aweme_video(request: Request, user_id: str, aweme_id: str) -> Response:
    """透传 Range 的视频代理。

    浏览器直连抖音 CDN 会带上本面板的 Referer 而被 403，且直连会把签名地址暴露在
    页面里；这里改用爬虫请求头代理，并双向透传 Range 以支持拖动进度条。
    地址由服务端按 aweme_id 解析（不接受调用方传 URL），避免变成开放代理。
    """
    await _find_user_or_raise(user_id)
    try:
        play_url = await AWEME_FEED_SERVICE.resolve_play_url(aweme_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, sanitize_public_error(exc), status_code=502)

    crawler_headers = (await CRAWLER_CLIENT.get_douyin_headers())["headers"]
    upstream_headers = build_upstream_headers(crawler_headers, request.headers.get("range"))
    try:
        status_code, upstream_response_headers, body = await open_media_stream(
            play_url,
            headers=upstream_headers,
            timeout_seconds=SETTINGS.crawler.timeout_seconds,
        )
    except MediaUpstreamError as exc:
        raise HTTPException(status_code=502, detail=sanitize_public_error(exc)) from exc

    response_headers = build_response_headers(
        upstream_response_headers,
        cache_seconds=0 if status_code == 416 else MEDIA_CACHE_SECONDS,
    )
    return StreamingResponse(
        body,
        status_code=status_code,
        media_type=response_headers.get("Content-Type", "video/mp4"),
        headers=response_headers,
    )


@router.post("/users", response_model=ResponseModel, summary="添加监控用户/Add monitor user")
async def add_user(request: Request, payload: AddUserPayload) -> ResponseModel:
    try:
        result = await monitor_service.add_user_with_status(payload.profile_url)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)


# GET/POST 分两条路由注册同一个函数：多 method 的单条路由会生成重复 operation id
@router.get(
    "/users/add_by_url",
    response_model=ResponseModel,
    summary="通过主页链接快速添加监控用户（GET，已 deprecated）/Quick add user by profile URL",
    operation_id="add_user_by_url_get",
)
@router.post(
    "/users/add_by_url",
    response_model=ResponseModel,
    summary="通过主页链接快速添加监控用户/Quick add user by profile URL",
    operation_id="add_user_by_url_post",
)
async def add_user_by_url(
    request: Request,
    profile_url: str = Query(min_length=1, description="抖音用户主页链接"),
) -> ResponseModel:
    """添加监控用户；GET 方式已 deprecated（状态变更应使用 POST），仅为兼容旧链接保留。"""
    try:
        result = await monitor_service.add_user_with_status(profile_url)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)


@router.patch("/users/{user_id}", response_model=ResponseModel, summary="更新用户监控状态/Update monitor user")
async def update_user(request: Request, user_id: str, payload: UpdateUserPayload) -> ResponseModel:
    try:
        user = monitor_service.set_user_enabled(user_id, payload.enabled)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, user)


@router.delete("/users/{user_id}", response_model=ResponseModel, summary="删除监控用户/Delete monitor user")
async def delete_user(request: Request, user_id: str) -> ResponseModel:
    try:
        user = monitor_service.remove_user(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, user)


@router.post("/users/{user_id}/run_once", response_model=ResponseModel, summary="手动执行单个用户/Run one user once")
async def run_user_once(request: Request, user_id: str) -> ResponseModel:
    try:
        result = await monitor_service.run_user_once(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)


@router.post(
    "/users/{user_id}/backfill/start",
    response_model=ResponseModel,
    summary="启动用户历史回填/Start user history backfill",
)
async def start_user_history_backfill(request: Request, user_id: str) -> ResponseModel:
    try:
        user = monitor_service.start_user_history_backfill(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, user)


@router.post(
    "/users/{user_id}/backfill/pause",
    response_model=ResponseModel,
    summary="暂停用户历史回填/Pause user history backfill",
)
async def pause_user_history_backfill(request: Request, user_id: str) -> ResponseModel:
    try:
        user = monitor_service.pause_user_history_backfill(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, user)


@router.post(
    "/users/{user_id}/backfill/resume",
    response_model=ResponseModel,
    summary="恢复用户历史回填/Resume user history backfill",
)
async def resume_user_history_backfill(request: Request, user_id: str) -> ResponseModel:
    try:
        user = monitor_service.resume_user_history_backfill(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, user)


@router.post(
    "/users/{user_id}/backfill/run_once",
    response_model=ResponseModel,
    summary="手动执行一页历史回填/Run one history backfill page",
)
async def run_user_history_backfill_once(request: Request, user_id: str) -> ResponseModel:
    try:
        result = await monitor_service.run_user_history_backfill_once(user_id)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)


@router.get("/status", response_model=ResponseModel, summary="获取监控状态/Get monitor status")
async def monitor_status(request: Request) -> ResponseModel:
    # 面板轮询：屏蔽 Cookie 原文与测活 samples，减小状态包。
    return _build_success(request, monitor_service.get_public_status())


@router.post("/start", response_model=ResponseModel, summary="启动监控/Start monitoring")
async def start_monitor(request: Request, payload: StartMonitorPayload) -> ResponseModel:
    try:
        status = await monitor_service.start_monitoring(
            mode=payload.mode,
            interval_hours=payload.interval_hours,
            coverage_hours=payload.coverage_hours,
        )
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, status)


@router.post("/stop", response_model=ResponseModel, summary="停止监控/Stop monitoring")
async def stop_monitor(request: Request) -> ResponseModel:
    try:
        status = await monitor_service.stop_monitoring()
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, status)


@router.post("/run_once", response_model=ResponseModel, summary="手动执行一次监控/Run monitor once")
async def run_once(request: Request) -> ResponseModel:
    try:
        result = await monitor_service.run_once()
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)
