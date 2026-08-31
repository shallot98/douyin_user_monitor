from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol

from crawlers.douyin.web.utils import AwemeIdFetcher
from crawlers.utils.utils import extract_valid_urls
from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.original_play import extract_video_id, resolve_original_play_url
from douyin_user_monitor.monitor.video_source import extract_video_source

logger = logging.getLogger(__name__)


class CrawlerClientProtocol(Protocol):
    async def fetch_one_video(self, aweme_id: str) -> Dict[str, Any]:
        ...

    async def get_douyin_headers(self) -> Dict[str, Any]:
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


class ParseService:
    """Share-link / 口令 → media direct URLs (no disk download)."""

    def __init__(self, crawler_client: CrawlerClientProtocol) -> None:
        self._crawler = crawler_client

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

        try:
            aweme_id = await AwemeIdFetcher.get_aweme_id(str(resolved_from))
        except Exception as exc:  # noqa: BLE001 — surface readable parse failure
            raise ValueError(f"无法解析作品 ID: {exc}") from exc
        aweme_id = str(aweme_id or "").strip()
        if not aweme_id:
            raise ValueError("无法解析作品 ID: 空结果")

        try:
            payload = await self._crawler.fetch_one_video(aweme_id)
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
                headers_bucket = await self._crawler.get_douyin_headers()
                headers = {}
                if isinstance(headers_bucket, dict):
                    raw_headers = headers_bucket.get("headers")
                    if isinstance(raw_headers, dict):
                        headers = {str(k): str(v) for k, v in raw_headers.items()}
                original_play_url = await resolve_original_play_url(video_id, headers)
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
