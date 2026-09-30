from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from douyin_user_monitor import auth
from douyin_user_monitor.api.monitor import OPENLIST_CLIENT, monitor_service
from douyin_user_monitor.api.monitor import router as monitor_router
from douyin_user_monitor.auth import API_KEY_ENV  # noqa: F401  测试与脚本沿用 main.API_KEY_ENV

# 三页共享的前端静态资源（common.js / common.css）。基于本文件定位，避免依赖 cwd。
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
LOGIN_PAGE_PATH = Path(__file__).resolve().parent / "web" / "login.html"


class ShortCacheStaticMiddleware(BaseHTTPMiddleware):
    """UI 迭代期给 /static 强制短缓存，避免 Cloudflare/浏览器长期命中旧 CSS。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


class LoginBody(BaseModel):
    username: str
    password: str
    remember: bool = True


# 默认的 /docs、/openapi.json 会公开完整路由表，下面改成带鉴权的版本
app = FastAPI(title="Douyin User Monitor", version="0.3.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(ShortCacheStaticMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(
    monitor_router,
    prefix="/api/monitor",
    tags=["Monitor"],
    dependencies=[Depends(auth.require_auth)],
)

LOGIN_LIMITER = auth.LoginRateLimiter()


@app.exception_handler(auth.LoginRequired)
async def redirect_to_login(request: Request, exc: auth.LoginRequired) -> RedirectResponse:
    return RedirectResponse(url=auth.login_url(exc.next_path), status_code=302)


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/api/monitor/dashboard")


@app.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    if not auth.login_enabled() or auth.is_authenticated(request):
        return RedirectResponse(url=auth.safe_next_path(next), status_code=302)
    return HTMLResponse(content=LOGIN_PAGE_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.post("/auth/login", include_in_schema=False)
async def login(request: Request, payload: LoginBody) -> JSONResponse:
    if not auth.login_enabled():
        raise HTTPException(status_code=404, detail="未启用登录")
    ip = auth.client_ip(request)
    retry_after = LOGIN_LIMITER.retry_after(ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"失败次数过多，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )
    if not auth.credentials_valid(payload.username, payload.password):
        LOGIN_LIMITER.record_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    LOGIN_LIMITER.reset(ip)

    lifetime = auth.REMEMBER_SECONDS if payload.remember else auth.SESSION_ONLY_SECONDS
    response = JSONResponse({"code": 200, "message": "ok", "data": {"remember": payload.remember}})
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session_token(int(time.time()) + lifetime),
        # 不勾选“记住我”时不设 max_age，关闭浏览器即失效
        max_age=lifetime if payload.remember else None,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return response


@app.post("/auth/logout", include_in_schema=False)
async def logout(request: Request) -> JSONResponse:
    response = JSONResponse({"code": 200, "message": "ok", "data": None})
    response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=_is_https(request))
    return response


@app.get("/auth/status", include_in_schema=False)
async def auth_status(request: Request) -> JSONResponse:
    data = {"login_enabled": auth.login_enabled(), "authenticated": auth.is_authenticated(request)}
    return JSONResponse({"code": 200, "message": "ok", "data": data})


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(auth.require_auth)])
async def openapi_schema() -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(auth.require_auth)])
async def swagger_docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Docs")


def _is_https(request: Request) -> bool:
    # nginx 反代时 uvicorn 看到的是 http，以 nginx 传来的协议为准
    return request.headers.get("X-Forwarded-Proto", request.url.scheme) == "https"


@app.on_event("startup")
async def startup_monitor() -> None:
    await monitor_service.auto_resume()


@app.on_event("shutdown")
async def shutdown_monitor() -> None:
    await monitor_service.shutdown()
    await OPENLIST_CLIENT.close()
