"""登录鉴权：签名 Cookie 会话 + X-API-Key，两者任一通过即可。

配置全部来自环境变量（每次请求读取，便于测试切换）：
  DYMON_LOGIN_USERNAME / DYMON_LOGIN_PASSWORD  浏览器登录账号；都设置才启用登录
  DYMON_SESSION_SECRET                         会话签名密钥；启用登录时必填
  DYMON_API_KEY                                脚本 / 外部调用用的 Key（原有行为）
都未设置时完全开放，与改造前一致。
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


def _session_key() -> bytes:
    secret = _env(SESSION_SECRET_ENV)
    if not secret:
        raise HTTPException(status_code=500, detail=f"已启用登录但未配置 {SESSION_SECRET_ENV}")
    # 账号密码参与派生：改用户名或密码后旧会话全部失效
    material = f"dymon-session|{_env(LOGIN_USERNAME_ENV)}|{_env(LOGIN_PASSWORD_ENV)}"
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).digest()


def make_session_token(expires_at: int) -> str:
    signature = hmac.new(_session_key(), str(expires_at).encode(), hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def session_valid(token: str) -> bool:
    if not login_enabled() or not token:
        return False
    expires_raw, _, signature = token.partition(".")
    if not expires_raw.isdigit() or not signature:
        return False
    expected = make_session_token(int(expires_raw)).partition(".")[2]
    return secrets.compare_digest(signature, expected) and int(expires_raw) > time.time()


def credentials_valid(username: str, password: str) -> bool:
    # 两项都比较，避免按用户名是否正确泄露时序差异
    user_ok = secrets.compare_digest(username.encode(), _env(LOGIN_USERNAME_ENV).encode())
    pass_ok = secrets.compare_digest(password.encode(), _env(LOGIN_PASSWORD_ENV).encode())
    return user_ok and pass_ok


def api_key_valid(request: Request) -> bool:
    expected = _env(API_KEY_ENV)
    if not expected:
        return False
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    return secrets.compare_digest(provided, expected)


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    return api_key_valid(request) or session_valid(request.cookies.get(SESSION_COOKIE, ""))


def safe_next_path(value: str | None) -> str:
    """只允许站内相对路径，防止登录页被用作开放跳转。"""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def login_url(next_path: str) -> str:
    return "/login?next=" + quote(safe_next_path(next_path), safe="")


async def require_auth(request: Request) -> None:
    if is_authenticated(request):
        return
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
