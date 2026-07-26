from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from douyin_user_monitor.api.monitor import monitor_service
from douyin_user_monitor.api.monitor import router as monitor_router

API_KEY_ENV = "DYMON_API_KEY"

# 三页共享的前端静态资源（common.js / common.css）。基于本文件定位，避免依赖 cwd。
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


async def require_api_key(request: Request) -> None:
    # 每次请求读取环境变量：未设置时保持完全开放，便于测试与本机使用
    expected = os.getenv(API_KEY_ENV, "").strip()
    if not expected:
        return
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="API Key 缺失或不正确")


app = FastAPI(title="Douyin User Monitor", version="0.3.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(
    monitor_router,
    prefix="/api/monitor",
    tags=["Monitor"],
    dependencies=[Depends(require_api_key)],
)


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/api/monitor/dashboard")


@app.on_event("startup")
async def startup_monitor() -> None:
    await monitor_service.auto_resume()


@app.on_event("shutdown")
async def shutdown_monitor() -> None:
    await monitor_service.shutdown()
