from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Dict, Optional, Protocol
from urllib.parse import urlsplit

import httpx

from crawlers.douyin.web.utils import AwemeIdFetcher
from crawlers.utils.utils import extract_valid_urls
from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.original_play import (
    RESOLVE_TIMEOUT_SECONDS,
    extract_video_id,
    resolve_original_play_url,
)
from douyin_user_monitor.monitor.parse_cookies import ParseCookiePool, ParseCookiesUnavailable
from douyin_user_monitor.monitor.video_source import extract_video_source

logger = logging.getLogger(__name__)


class CrawlerClientProtocol(Protocol):
    async def fetch_one_video(self, aweme_id: str, cookie: Optional[str] = None) -> Dict[str, Any]:
        ...

    async def get_douyin_headers(self, cookie: Optional[str] = None) -> Dict[str, Any]:
        ...


def _extract_author(detail: Dict[str, Any]) -> Dict[str, str]:
    author = detail.get("author")
    if not isinstance(author, dict):
        author = detail.get("authorInfo")
    if not isinstance(author, dict):
        author = {}
    return {
        "nickname": str(author.get("nickname") or author.get("nickName") or "").strip(),
        "uid": str(author.get("uid") or author.get("user_id") or "").strip(),
        "sec_uid": str(author.get("sec_uid") or author.get("secUid") or "").strip(),
    }


def _unwrap_aweme_detail(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("作品详情格式无效")
    detail = payload.get("aweme_detail")
    if isinstance(detail, dict):
        return detail
    # Some crawlers may already return the detail object itself.
    if "aweme_id" in payload or "video" in payload or "images" in payload:
        return payload
    raise ValueError("作品详情格式无效 / 缺少 aweme_detail")


RESULT_CACHE_SECONDS = 600
PARSE_COOKIE_ATTEMPTS = 3
RESULT_CACHE_MAX = 256
# 长链接里直接带作品 ID：/video/<id>、/note/<id>、/share/video/<id>、?modal_id=<id>、?vid=<id>
_DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
_ID_IN_PATH = re.compile(r"/(?:share/)?(?:video|note|slides)/(\d{8,})")
_ID_IN_QUERY = re.compile(r"(?:^|&)(?:modal_id|vid)=(\d{8,})")


def local_aweme_id(url: str) -> Optional[str]:
    """不发请求就能从长链接取出作品 ID；短链（v.douyin.com）或认不出时返回 None。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "v.douyin.com" or not any(host == base or host.endswith(f".{base}") for base in _DOUYIN_HOSTS):
        return None
    match = _ID_IN_PATH.search(parts.path) or _ID_IN_QUERY.search(parts.query)
    return match.group(1) if match else None


class ParseService:
    """Share-link / 口令 → media direct URLs (no disk download)."""

    def __init__(self, crawler_client: CrawlerClientProtocol, cookie_pool: Optional[ParseCookiePool] = None) -> None:
        self._crawler = crawler_client
        # 给了 cookie_pool：解析只用这里的 Cookie（和监控 Cookie 池分开），没有就报错不回落
        self._cookie_pool = cookie_pool
        # aweme_id → (过期时刻, 结果)；下载插件常对同一作品连点几次，直链本身有效数小时
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        # 原画跳转每次都要跨洋握手；常驻连接省掉 TLS 往返
        self._play_client: Optional[httpx.AsyncClient] = None

    def _cached(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        hit = self._cache.get(aweme_id)
        if hit is None or hit[0] <= time.monotonic():
            self._cache.pop(aweme_id, None)
            return None
        return copy.deepcopy(hit[1])

    def _remember(self, result: Dict[str, Any]) -> None:
        now = time.monotonic()
        if len(self._cache) >= RESULT_CACHE_MAX:
            for key in [key for key, (expires, _) in self._cache.items() if expires <= now] or list(self._cache)[:1]:
                self._cache.pop(key, None)
        self._cache[result["aweme_id"]] = (now + RESULT_CACHE_SECONDS, copy.deepcopy(result))

    def _shared_play_client(self) -> httpx.AsyncClient:
        if self._play_client is None or self._play_client.is_closed:
            self._play_client = httpx.AsyncClient(timeout=httpx.Timeout(RESOLVE_TIMEOUT_SECONDS), follow_redirects=True)
        return self._play_client

    async def parse(self, text: str, *, include_raw: bool = False) -> Dict[str, Any]:
        raw_text = str(text or "").strip()
        if not raw_text:
            raise ValueError("未找到有效抖音链接")

        extracted = extract_valid_urls(raw_text)
        if isinstance(extracted, list):
            resolved_from = extracted[0] if extracted else None
        else:
            resolved_from = extracted
        if not resolved_from:
            raise ValueError("未找到有效抖音链接")

        # 长链接自带作品 ID，不必再请求一次抖音（跨洋约 1 秒）；短链仍需跟随跳转
        aweme_id = local_aweme_id(str(resolved_from))
        if aweme_id is None:
            try:
                aweme_id = await AwemeIdFetcher.get_aweme_id(str(resolved_from))
            except Exception as exc:  # noqa: BLE001 — surface readable parse failure
                raise ValueError(f"无法解析作品 ID: {exc}") from exc
        aweme_id = str(aweme_id or "").strip()
        if not aweme_id:
            raise ValueError("无法解析作品 ID: 空结果")

        if include_raw:
            return await self._fetch(aweme_id, str(resolved_from), include_raw=True)
        cached = self._cached(aweme_id)
        if cached is not None:
            cached["resolved_from"] = str(resolved_from)
            logger.info("parse cache hit aweme_id=%s", aweme_id)
            return cached
        result = await self._fetch(aweme_id, str(resolved_from), include_raw=False)
        self._remember(result)
        return result

    async def _fetch_detail(self, aweme_id: str) -> tuple[Any, Optional[str]]:
        """返回 (作品详情, 用到的 Cookie)。专用 Cookie 请求出错时换下一条再试，最多 PARSE_COOKIE_ATTEMPTS 条。"""
        if self._cookie_pool is None:
            return await self._crawler.fetch_one_video(aweme_id), None
        attempts = min(PARSE_COOKIE_ATTEMPTS, max(1, self._cookie_pool.count()))
        errors = []
        for _ in range(attempts):
            name, cookie = self._cookie_pool.take()
            try:
                return await self._crawler.fetch_one_video(aweme_id, cookie=cookie), cookie
            except Exception as exc:  # noqa: BLE001
                logger.warning("parse cookie %s failed aweme_id=%s err=%s", name, aweme_id, exc)
                errors.append(f"{name}: {exc}")
        raise RuntimeError("；".join(errors))

    async def _fetch(self, aweme_id: str, resolved_from: str, *, include_raw: bool) -> Dict[str, Any]:
        try:
            payload, cookie = await self._fetch_detail(aweme_id)
        except ParseCookiesUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"获取作品详情失败: {exc}") from exc

        detail = _unwrap_aweme_detail(payload)
        author = _extract_author(detail)
        create_time = detail.get("create_time")
        try:
            create_time_val: Optional[int] = int(create_time) if create_time is not None else None
        except (TypeError, ValueError):
            create_time_val = None

        aweme_type = detail.get("aweme_type")
        try:
            aweme_type_val: Optional[int] = int(aweme_type) if aweme_type is not None else None
        except (TypeError, ValueError):
            aweme_type_val = None

        result: Dict[str, Any] = {
            "aweme_id": str(detail.get("aweme_id") or aweme_id),
            "aweme_type": aweme_type_val,
            "is_image_post": False,
            "desc": str(detail.get("desc") or "").strip(),
            "author": author,
            "create_time": create_time_val,
            "resolved_from": str(resolved_from),
            "image_urls": [],
            "download_url": None,
            "original_play_url": None,
            "video_source": None,
            "quality_source": None,
            "raw": detail if include_raw else None,
        }

        if is_image_post(detail):
            image_urls = extract_image_urls(detail)
            result["is_image_post"] = True
            result["image_urls"] = image_urls
            result["download_url"] = image_urls[0] if image_urls else None
            result["quality_source"] = "image"
            result["original_play_url"] = None
            result["video_source"] = None
            logger.info("parse image post aweme_id=%s images=%s", result["aweme_id"], len(image_urls))
            return result

        # Video path: original first, then bit_rate / fallback.
        video_source_payload: Optional[Dict[str, Any]] = None
        source = extract_video_source(detail)
        if source is not None:
            video_source_payload = {**source.to_record(), "url": source.url}

        original_play_url: Optional[str] = None
        video_id = extract_video_id(detail)
        if video_id:
            try:
                headers_bucket = await self._crawler.get_douyin_headers(cookie)
                headers = {}
                if isinstance(headers_bucket, dict):
                    raw_headers = headers_bucket.get("headers")
                    if isinstance(raw_headers, dict):
                        headers = {str(k): str(v) for k, v in raw_headers.items()}
                original_play_url = await resolve_original_play_url(
                    video_id, headers, client=self._shared_play_client()
                )
            except Exception as exc:  # noqa: BLE001 — intentional product fallback
                logger.warning(
                    "original play resolve failed aweme_id=%s video_id=%s err=%s",
                    result["aweme_id"],
                    video_id,
                    exc,
                )
                original_play_url = None

        if original_play_url:
            result["original_play_url"] = original_play_url
            result["download_url"] = original_play_url
            result["quality_source"] = "original"
            result["video_source"] = video_source_payload
            logger.info("parse video original aweme_id=%s", result["aweme_id"])
            return result

        if source is not None:
            result["original_play_url"] = None
            result["download_url"] = source.url
            result["quality_source"] = source.quality_source
            result["video_source"] = video_source_payload
            logger.info(
                "parse video fallback aweme_id=%s quality=%s",
                result["aweme_id"],
                source.quality_source,
            )
            return result

        raise ValueError(f"作品 {result['aweme_id']} 未找到可下载媒体地址")
