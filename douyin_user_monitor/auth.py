"""登录鉴权：签名 Cookie 会话 + X-API-Key，两者任一通过即可。

配置全部来自环境变量（每次请求读取，便于测试切换）：
  DYMON_LOGIN_USERNAME / DYMON_LOGIN_PASSWORD  浏览器登录账号；都设置才启用登录
  DYMON_SESSION_SECRET                         会话签名密钥；启用登录时必填
  DYMON_API_KEY                                脚本 / 外部调用用的 Key（原有行为）
  DYMON_GUEST_ENABLED=1                        开放游客入口（需同时启用登录）
另有网页里自助管理的 Key（monitor/api_keys.py）：parse 档只能调解析接口，full 档等同管理员。
都未设置时完全开放，与改造前一致。

游客会话用单独的 Cookie 和单独派生的签名密钥，不能冒充管理员会话；游客只能访问
GUEST_ROUTES 里的路由（刷视频页、随机视频列表、播放所需的媒体），具体作品范围由
各接口再按作品库索引校验。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from urllib.parse import quote

from fastapi import HTTPException, Request

API_KEY_ENV = "DYMON_API_KEY"
LOGIN_USERNAME_ENV = "DYMON_LOGIN_USERNAME"
LOGIN_PASSWORD_ENV = "DYMON_LOGIN_PASSWORD"
SESSION_SECRET_ENV = "DYMON_SESSION_SECRET"

SESSION_COOKIE = "dymon_session"
GUEST_ENABLED_ENV = "DYMON_GUEST_ENABLED"
GUEST_COOKIE = "dymon_guest"
GUEST_SECONDS = 30 * 24 * 3600
GUEST_HOME = "/api/monitor/swipe/dashboard"
# 只读解析 Key（scope=parse）可调用的路由
PARSE_ROUTES = frozenset(
    {
        ("GET", "/api/monitor/parse"),
        ("POST", "/api/monitor/parse"),
        ("GET", "/api/monitor/parse/plugin"),
    }
)
# 游客可访问的路由（方法, 路由模板）；其余一律拒绝
GUEST_ROUTES = frozenset(
    {
        ("GET", GUEST_HOME),
        ("GET", "/api/monitor/library"),
        ("POST", "/api/monitor/users/{user_id}/archive/playback-urls"),
        ("GET", "/api/monitor/users/{user_id}/archive/{aweme_id}/media/{file_index}"),
        ("HEAD", "/api/monitor/users/{user_id}/archive/{aweme_id}/media/{file_index}"),
        ("GET", "/api/monitor/users/{user_id}/archive/{aweme_id}/cover"),
    }
)
REMEMBER_SECONDS = 30 * 24 * 3600
# 不勾选“记住我”时仍给会话一个上限，浏览器会话 Cookie 可能被“恢复标签页”长期保留
SESSION_ONLY_SECONDS = 12 * 3600

LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_SECONDS = 600


class LoginRequired(Exception):
    """HTML 页面未登录：由 main 的异常处理器转成跳转登录页。"""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def login_enabled() -> bool:
    return bool(_env(LOGIN_USERNAME_ENV) and _env(LOGIN_PASSWORD_ENV))


def auth_enabled() -> bool:
    return login_enabled() or bool(_env(API_KEY_ENV))


def guest_enabled() -> bool:
    return login_enabled() and _env(GUEST_ENABLED_ENV).lower() in {"1", "true", "yes", "on"}


def _session_key(purpose: str = "session") -> bytes:
    secret = _env(SESSION_SECRET_ENV)
    if not secret:
        raise HTTPException(status_code=500, detail=f"已启用登录但未配置 {SESSION_SECRET_ENV}")
    # 账号密码参与派生：改用户名或密码后旧会话（含游客会话）全部失效；
    # purpose 区分管理员/游客，两种 Cookie 的签名互不通用
    material = f"dymon-{purpose}|{_env(LOGIN_USERNAME_ENV)}|{_env(LOGIN_PASSWORD_ENV)}"
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).digest()


def make_session_token(expires_at: int, purpose: str = "session") -> str:
    signature = hmac.new(_session_key(purpose), str(expires_at).encode(), hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def make_guest_token(expires_at: int) -> str:
    return make_session_token(expires_at, "guest")


def _token_valid(token: str, purpose: str) -> bool:
    if not login_enabled() or not token:
        return False
    expires_raw, _, signature = token.partition(".")
    if not expires_raw.isdigit() or not signature:
        return False
    expected = make_session_token(int(expires_raw), purpose).partition(".")[2]
    return secrets.compare_digest(signature, expected) and int(expires_raw) > time.time()


def session_valid(token: str) -> bool:
    return _token_valid(token, "session")


def guest_session_valid(token: str) -> bool:
    return guest_enabled() and _token_valid(token, "guest")


def credentials_valid(username: str, password: str) -> bool:
    # 两项都比较，避免按用户名是否正确泄露时序差异
    user_ok = secrets.compare_digest(username.encode(), _env(LOGIN_USERNAME_ENV).encode())
    pass_ok = secrets.compare_digest(password.encode(), _env(LOGIN_PASSWORD_ENV).encode())
    return user_ok and pass_ok


def _provided_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""


def api_key_valid(request: Request) -> bool:
    """系统 Key（环境变量 DYMON_API_KEY）。"""
    expected = _env(API_KEY_ENV)
    if not expected:
        return False
    return secrets.compare_digest(_provided_key(request), expected)


def managed_key(request: Request) -> tuple[str, str] | None:
    """网页里创建的 Key → (id, scope)。"""
    from douyin_user_monitor.monitor import api_keys

    store = api_keys.get_store()
    provided = _provided_key(request)
    return store.match(provided) if store is not None and provided else None


def is_authenticated(request: Request) -> bool:
    """管理员（会话 / 系统 Key / full 档 Key / 未启用鉴权）；游客与 parse 档 Key 不算。"""
    if not auth_enabled():
        return True
    if api_key_valid(request) or session_valid(request.cookies.get(SESSION_COOKIE, "")):
        return True
    matched = managed_key(request)
    return matched is not None and matched[1] == "full"


def is_guest(request: Request) -> bool:
    """持有效游客会话且不是管理员。管理员同时带着游客 Cookie 时按管理员算。"""
    if is_authenticated(request):
        return False
    return guest_session_valid(request.cookies.get(GUEST_COOKIE, ""))


def session_role(request: Request) -> str | None:
    if is_authenticated(request):
        return "admin"
    return "guest" if is_guest(request) else None


def _route_key(request: Request) -> tuple[str, str | None]:
    route = request.scope.get("route")
    return request.method, getattr(route, "path", None)


def _guest_route_allowed(request: Request) -> bool:
    return _route_key(request) in GUEST_ROUTES


def _record_key_use(key_id: str, request: Request) -> None:
    from douyin_user_monitor.monitor import api_keys

    store = api_keys.get_store()
    if store is not None:
        store.record_use(key_id, client_ip(request))


def can_manage_keys(request: Request) -> bool:
    """增删 Key 只认管理员会话 / 系统 Key：网页创建的 Key 不能再发 Key。"""
    return getattr(request.state, "auth_kind", None) in {"open", "session", "system"}


def safe_next_path(value: str | None) -> str:
    """只允许站内相对路径，防止登录页被用作开放跳转。"""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def login_url(next_path: str) -> str:
    return "/login?next=" + quote(safe_next_path(next_path), safe="")


async def require_auth(request: Request) -> None:
    if not auth_enabled():
        request.state.auth_kind = "open"
        return
    if api_key_valid(request):
        request.state.auth_kind = "system"
        _record_key_use("system", request)
        return
    if session_valid(request.cookies.get(SESSION_COOKIE, "")):
        request.state.auth_kind = "session"
        return
    matched = managed_key(request)
    if matched is not None:
        key_id, scope = matched
        _record_key_use(key_id, request)
        request.state.auth_kind = "key"
        if scope == "full" or _route_key(request) in PARSE_ROUTES:
            return
        raise HTTPException(status_code=403, detail="该 Key 只能调用解析接口")
    if is_guest(request):
        if _guest_route_allowed(request):
            request.state.guest = True
            return
        # 游客打开别的页面：去登录页（可改用管理员账号登录）；接口一律 403
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise LoginRequired(path)
        raise HTTPException(status_code=403, detail="游客只能刷视频")
    # 浏览器直接打开页面（Accept 含 text/html）时跳登录；接口 / 媒体请求照旧 401
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(path)
    raise HTTPException(status_code=401, detail="未登录或 API Key 不正确")


def client_ip(request: Request) -> str:
    # 8900 只监听本机、前面是 Cloudflare → nginx，这两个头才可信
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )


class LoginRateLimiter:
    """按 IP 统计失败次数；单用户场景内存计数足够，重启清零。"""

    def __init__(self, limit: int = LOGIN_FAIL_LIMIT, window: float = LOGIN_FAIL_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, ip: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(ip, deque())
        while failures and now - failures[0] > self.window:
            failures.popleft()
        return failures

    def retry_after(self, ip: str) -> int:
        now = time.monotonic()
        failures = self._recent(ip, now)
        if len(failures) < self.limit:
            return 0
        return max(1, int(self.window - (now - failures[0])))

    def record_failure(self, ip: str) -> None:
        self._recent(ip, time.monotonic()).append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
