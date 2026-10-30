from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

PLAY_ENDPOINT = "https://www.douyin.com/aweme/v1/play/"
DEFAULT_RATIO = "default"
RESOLVE_TIMEOUT_SECONDS = 30.0
# 源片路径常见标记；注意不要误匹配 tos-cn-ve-（转码）
SOURCE_CDN_MARKERS = ("tos-cn-v-", "/obj/tos-cn-v-", "-dy-o.")
# 真视频资源 id：v + 字母数字，不是 http(s) 音乐/封面 URL
_VIDEO_ID_RE = re.compile(r"^v[0-9a-zA-Z]{8,}$")


def is_valid_video_id(video_id: str) -> bool:
    text = (video_id or "").strip()
    if not text:
        return False
    if "://" in text or "/" in text or "." in text:
        return False
    if text.lower().endswith((".mp3", ".mp4", ".m4a", ".wav")):
        return False
    return bool(_VIDEO_ID_RE.match(text))


def extract_video_id(aweme_detail: Dict[str, Any]) -> Optional[str]:
    """Extract play resource uri (video_id) from aweme detail.

    Rejects music/image URLs mistakenly placed in play_addr.uri (common on type=68).
    """
    video = aweme_detail.get("video")
    if not isinstance(video, dict):
        return None

    for key in ("play_addr", "play_addr_h264", "download_addr"):
        uri = _uri_from_addr(video.get(key))
        if uri:
            return uri

    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            uri = _uri_from_addr(item.get("play_addr"))
            if uri:
                return uri
    return None


def build_original_play_url(video_id: str, *, ratio: str = DEFAULT_RATIO) -> str:
    text = (video_id or "").strip()
    if not text:
        raise ValueError("video_id 不能为空")
    query = urlencode(
        {
            "video_id": text,
            "ratio": ratio,
            "line": "0",
            "is_play_url": "1",
            "source": "PackSourceEnum_PUBLISH",
        }
    )
    return f"{PLAY_ENDPOINT}?{query}"


def looks_like_source_cdn(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in SOURCE_CDN_MARKERS)


async def resolve_original_play_url(
    video_id: str,
    headers: Dict[str, str],
    *,
    timeout_seconds: float = RESOLVE_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Follow official play redirect for ratio=default and return final CDN URL."""
    if not is_valid_video_id(video_id):
        return None
    play_url = build_original_play_url(video_id)
    request_headers = _play_headers(headers)

    if client is not None:
        return await _resolve_with_client(client, play_url, request_headers)

    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as owned:
        return await _resolve_with_client(owned, play_url, request_headers)


async def _resolve_with_client(
    client: httpx.AsyncClient,
    play_url: str,
    headers: Dict[str, str],
) -> Optional[str]:
    try:
        async with client.stream("GET", play_url, headers=headers) as response:
            if response.status_code >= 400:
                return None
            final_url = str(response.url).strip()
            if not final_url.startswith("http"):
                return None
            # 未跳转或仍停在 play 接口：多为无效 video_id / 空响应
            if _same_play_endpoint(final_url, play_url):
                return None
            content_type = (response.headers.get("content-type") or "").lower()
            content_length = _safe_int(response.headers.get("content-length"))
            if content_length == 0:
                return None
            # 仅接受源片 CDN；tos-cn-ve- 转码档交给 bit_rate fallback
            if not looks_like_source_cdn(final_url):
                return None
            if content_type and not any(
                token in content_type for token in ("video/", "application/octet-stream", "binary")
            ):
                # 部分 CDN 不回 content-type，有 source 路径则仍接受
                if "text/" in content_type or "json" in content_type or "html" in content_type:
                    return None
            return final_url
    except httpx.HTTPError:
        return None


def _uri_from_addr(addr: Any) -> Optional[str]:
    if not isinstance(addr, dict):
        return None
    uri = str(addr.get("uri") or "").strip()
    if not is_valid_video_id(uri):
        return None
    return uri


def _same_play_endpoint(final_url: str, play_url: str) -> bool:
    final = final_url.split("?", 1)[0].rstrip("/")
    play = play_url.split("?", 1)[0].rstrip("/")
    return final == play or final.endswith("/aweme/v1/play")


def _safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _play_headers(headers: Dict[str, str]) -> Dict[str, str]:
    result = {str(k): str(v) for k, v in (headers or {}).items()}
    result.setdefault("Referer", "https://www.douyin.com/")
    result.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    return result
