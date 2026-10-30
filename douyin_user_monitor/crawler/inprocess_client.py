from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}


class InProcessDouyinClient:
    """Process-local Douyin Web crawler adapter implementing MonitorCrawlerProtocol."""

    def __init__(self, config_path: Path | str):
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"crawler config not found: {path}")

        # Must configure path before importing vendored crawler modules (models call TokenManager at import).
        from crawlers.douyin.web.config_loader import set_config_path

        set_config_path(path)

        from crawlers.douyin.web.utils import _reload_token_manager_from_config

        _reload_token_manager_from_config()

        from crawlers.douyin.web.web_crawler import DouyinWebCrawler

        self._config_path = path
        self._crawler = DouyinWebCrawler()
        self._cookie_lock = asyncio.Lock()

    async def aclose(self) -> None:
        return None

    def _headers_bucket(self) -> Dict[str, Any]:
        from crawlers.douyin.web.config_loader import get_config

        config = get_config()
        return config["TokenManager"]["douyin"]["headers"]

    async def get_cookie(self) -> str:
        async with self._cookie_lock:
            return str(self._headers_bucket().get("Cookie") or "")

    async def update_cookie(self, cookie: str) -> None:
        text = (cookie or "").strip()
        if not text:
            raise ValueError("Cookie 不能为空")
        await self._persist_cookie(text)

    async def clear_cookie(self) -> None:
        """Remove a revoked Cookie from runtime and persistent crawler configuration."""
        await self._persist_cookie("")

    async def _persist_cookie(self, text: str) -> None:
        async with self._cookie_lock:
            previous = self._headers_bucket().get("Cookie")
            try:
                await self._crawler.update_cookie(text)
            except Exception as exc:
                self._headers_bucket()["Cookie"] = previous
                raise RuntimeError(f"更新 Cookie 失败: {exc}") from exc

    @asynccontextmanager
    async def temporary_cookie(self, cookie: str) -> AsyncIterator[None]:
        """全程持锁串行化换入→yield→恢复，避免测活与轮换交错互踩全局 Cookie。

        约束：yield 包裹的调用方只调用 fetch_*/handler_*/get_douyin_headers 等
        不获取 _cookie_lock 的方法，故全程持锁不会因 asyncio.Lock 不可重入而死锁。
        """
        text = (cookie or "").strip()
        if not text:
            raise ValueError("Cookie 不能为空")
        async with self._cookie_lock:
            headers = self._headers_bucket()
            previous = headers.get("Cookie")
            headers["Cookie"] = text
            try:
                yield
            finally:
                headers["Cookie"] = previous

    async def get_douyin_headers(self, cookie: str | None = None) -> Dict[str, Dict[str, str]]:
        try:
            kwargs = await (self._crawler.get_douyin_headers(cookie) if cookie else self._crawler.get_douyin_headers())
        except Exception as exc:
            raise RuntimeError(f"读取抖音请求头失败: {exc}") from exc
        headers = kwargs.get("headers") if isinstance(kwargs, dict) else None
        if not isinstance(headers, dict):
            return {"headers": dict(DEFAULT_HEADERS)}
        return {"headers": {str(k): str(v) for k, v in headers.items()}}

    async def get_sec_user_id(self, url: str) -> str:
        try:
            data = await self._crawler.get_sec_user_id(url)
        except Exception as exc:
            raise RuntimeError(f"解析 sec_user_id 失败: {exc}") from exc
        if not isinstance(data, str) or not data.strip():
            raise ValueError("爬虫返回的 sec_user_id 无效")
        return data

    async def handler_user_profile(self, sec_user_id: str) -> Dict[str, Any]:
        try:
            data = await self._crawler.handler_user_profile(sec_user_id)
        except Exception as exc:
            raise RuntimeError(f"获取用户资料失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("爬虫返回的用户信息格式无效")
        return data

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int) -> Dict[str, Any]:
        try:
            data = await self._crawler.fetch_user_post_videos(sec_user_id, max_cursor, count)
        except Exception as exc:
            raise RuntimeError(f"获取用户作品列表失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("爬虫返回的作品列表格式无效")
        if isinstance(data.get("not_login_module"), dict):
            raise RuntimeError("获取用户作品列表失败: 抖音返回未登录状态，Cookie 登录无效（login required）")
        if "aweme_list" not in data:
            raise ValueError("爬虫返回的作品列表缺少有效 aweme_list，无法确认作品和分页结果")
        if data["aweme_list"] is not None and not isinstance(data["aweme_list"], list):
            raise ValueError("爬虫返回的 aweme_list 不是列表")
        return data

    async def fetch_one_video(self, aweme_id: str, cookie: str | None = None) -> Dict[str, Any]:
        """cookie 给定时只用于这一次请求（解析 API 的独立 Cookie），不动全局 Cookie。"""
        try:
            data = await (self._crawler.fetch_one_video(aweme_id, cookie=cookie) if cookie else self._crawler.fetch_one_video(aweme_id))
        except Exception as exc:
            raise RuntimeError(f"获取作品详情失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("爬虫返回的作品详情格式无效")
        return data
