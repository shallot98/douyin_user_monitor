from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from douyin_user_monitor.api.models import ErrorResponseModel, ResponseModel
from douyin_user_monitor.crawler.inprocess_client import DEFAULT_HEADERS, InProcessDouyinClient
from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessConfig
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.cookie_pool import CookiePoolService
from douyin_user_monitor.monitor.dashboard_payload import (
    DASHBOARD_ACTIVITY_LIMIT,
    build_recent_activity,
)
from douyin_user_monitor.monitor.hermes_weixin_sender import HermesWeixinConfig, HermesWeixinSender
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.parse_service import ParseService
from douyin_user_monitor.monitor.schedule import MODE_COVERAGE, MODE_INTERVAL
from douyin_user_monitor.monitor.service import (
    MIN_INTERVAL_HOURS,
    MonitorService,
)
from douyin_user_monitor.monitor.statistics import build_user_statistics
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.telegram_notifier import TelegramNotifier
from douyin_user_monitor.monitor.user_lookup import find_user_by_id
from douyin_user_monitor.monitor.user_table import query_monitor_users, wants_paginated_users
from douyin_user_monitor.settings_loaders import load_settings

router = APIRouter()
AVATAR_CACHE_SECONDS = 3600
# 单张 ~3KB，2048 张 ≈6MB；超出按插入顺序淘汰最旧
AVATAR_CACHE_MAX_ITEMS = 2048
_AVATAR_CACHE: dict[str, tuple[bytes, str]] = {}
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

# 统计接口短缓存：状态文件未变时避免反复扫 500+ 用户的 download_records
_STATISTICS_CACHE: dict[str, Any] | None = None
_STATISTICS_CACHE_MTIME_NS: int | None = None

SETTINGS = load_settings()
DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "dashboard.html"
STATISTICS_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "statistics.html"
COOKIES_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "cookies.html"
SETTINGS_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "settings.html"
API_DASHBOARD_PATH = SETTINGS.project_root / "douyin_user_monitor" / "web" / "api.html"

CRAWLER_CLIENT = InProcessDouyinClient(SETTINGS.crawler.config_path)
MONITOR_STORAGE = MonitorStorage(SETTINGS.monitor.state_path)
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

monitor_service = MonitorService(
    crawler=CRAWLER_CLIENT,
    storage=MONITOR_STORAGE,
    download_root=SETTINGS.monitor.download_root,
    notifier=MONITOR_NOTIFIER,
    cookie_liveness_service=COOKIE_LIVENESS_SERVICE,
    cookie_pool_service=COOKIE_POOL_SERVICE,
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

def _find_user_or_raise(user_id: str) -> dict[str, Any]:
    user = find_user_by_id(monitor_service.list_users(), user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


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


PARSE_SERVICE = ParseService(CRAWLER_CLIENT)


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
    activity: str | None = Query(None, description="all|active_7d|active_30d|silent_7d|silent_30d|publish_unknown"),
    backfill: str | None = Query(None, description="all|idle|active|paused|completed|failed"),
    sort_by: str | None = Query(None, description="downloads|last_publish|last_check|last_download|nickname|created"),
    sort_order: str | None = Query(None, description="asc|desc"),
    page: int | None = Query(None, description="1-based page; presence switches to table mode"),
    page_size: int | None = Query(None, description="20|50|100"),
    view: str | None = Query(None, description="dashboard|table"),
) -> ResponseModel:
    # Compat A: no page/page_size/view=table → legacy dashboard full projection.
    # With pagination params → filter/sort/page table for statistics unified list.
    if wants_paginated_users(page=page, page_size=page_size, view=view):
        users = monitor_service.list_users()
        payload = query_monitor_users(
            users,
            search=search or "",
            monitor=monitor or "all",
            account=account or "all",
            activity=activity or "all",
            backfill=backfill or "all",
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
    users = monitor_service.list_users()
    activity_limit = DASHBOARD_ACTIVITY_LIMIT if limit is None else max(0, int(limit))
    payload = {
        "recent_activity": build_recent_activity(users, limit=activity_limit),
    }
    return _build_success(request, payload)


@router.get("/statistics", response_model=ResponseModel, summary="获取用户统计/Get user statistics")
async def user_statistics(request: Request) -> ResponseModel:
    global _STATISTICS_CACHE, _STATISTICS_CACHE_MTIME_NS
    # 统计只读本地态，无需资料补全网络请求；并按状态文件 mtime 做短缓存。
    mtime_ns = None
    try:
        mtime_ns = monitor_service._storage.state_file.stat().st_mtime_ns  # noqa: SLF001
    except OSError:
        mtime_ns = None
    if (
        _STATISTICS_CACHE is not None
        and mtime_ns is not None
        and _STATISTICS_CACHE_MTIME_NS == mtime_ns
    ):
        return _build_success(request, _STATISTICS_CACHE)

    users = monitor_service.list_users()
    payload = build_user_statistics(users)
    _STATISTICS_CACHE = payload
    _STATISTICS_CACHE_MTIME_NS = mtime_ns
    return _build_success(request, payload)


@router.get("/users/{user_id}/avatar", summary="代理监控用户头像/Proxy monitor user avatar")
async def proxy_user_avatar(user_id: str) -> Response:
    user = _find_user_or_raise(user_id)
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


@router.post("/users", response_model=ResponseModel, summary="添加监控用户/Add monitor user")
async def add_user(request: Request, payload: AddUserPayload) -> ResponseModel:
    try:
        result = await monitor_service.add_user_with_status(payload.profile_url)
    except Exception as exc:  # noqa: BLE001
        raise _build_error(request, str(exc))
    return _build_success(request, result)


@router.api_route(
    "/users/add_by_url",
    methods=["GET", "POST"],
    response_model=ResponseModel,
    summary="通过主页链接快速添加监控用户/Quick add user by profile URL",
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
