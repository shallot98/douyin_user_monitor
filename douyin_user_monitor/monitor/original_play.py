from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

PLAY_ENDPOINT = "https://www.douyin.com/aweme/v1/play/"
DEFAULT_RATIO = "default"
RESOLVE_TIMEOUT_SECONDS = 30.0
SOURCE_CDN_MARKERS = ("tos-cn-v-", "-dy-o.", "/obj/tos-cn-v-")


def extract_video_id(aweme_detail: Dict[str, Any]) -> Optional[str]:
    """Extract play resource uri (video_id) from aweme detail."""
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
            if not final_url or final_url.rstrip("/") == play_url.rstrip("/"):
                # No redirect; still accept if body looks like media endpoint
                if response.status_code == 200 and final_url.startswith("http"):
                    return final_url
                return None
            if not final_url.startswith("http"):
                return None
            return final_url
    except httpx.HTTPError:
        return None


def _uri_from_addr(addr: Any) -> Optional[str]:
    if not isinstance(addr, dict):
        return None
    uri = str(addr.get("uri") or "").strip()
    return uri or None


def _play_headers(headers: Dict[str, str]) -> Dict[str, str]:
    result = {str(k): str(v) for k, v in (headers or {}).items()}
    result.setdefault("Referer", "https://www.douyin.com/")
    result.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    return result
